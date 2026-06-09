# bgperf 使用说明

本文说明如何构建、运行、打包、读取包、生成报告，以及如何使用
Podman 和 systemd-nspawn 两种 runtime。

## 1. 环境准备

安装 Python 依赖：

```bash
pip install -r pip-requirements.txt
```

Podman runtime 需要本机可用的 Podman 服务：

```bash
podman system service --time=0 &
./bgperf.py doctor
```

systemd-nspawn runtime 需要以下命令可用：

```bash
systemd-nspawn
machinectl
systemd-run
debootstrap
ip
```

nspawn runtime 会在 `--runtime-dir` 下保存 Debian rootfs、构建后的系统镜像、
运行时 machine 目录、网络 metadata 和日志。默认目录是当前工作目录下的
`.bgperf-nspawn`。

## 2. 选择 Runtime

默认使用 Podman：

```bash
sudo ./bgperf.py bench -n 1 -p 10
```

使用 systemd-nspawn：

```bash
sudo ./bgperf.py --runtime nspawn --runtime-dir .bgperf-nspawn bench -n 1 -p 10
```

nspawn runtime 使用 Debian trixie rootfs。GoBGP 构建时不会使用 Debian 自带的
旧 Go 包，而是从 `go.dev` 获取最新稳定 Go 并安装到 rootfs 内。

常用 nspawn 资源限制参数：

```bash
--nspawn-cpu-quota 100%
--nspawn-memory-max 1G
```

这些限制通过 systemd scope 施加到每个 nspawn machine 上。

## 3. 构建 BGP 软件镜像

构建全部默认镜像：

```bash
./bgperf.py prepare
```

nspawn runtime 下构建：

```bash
sudo ./bgperf.py --runtime nspawn --runtime-dir .bgperf-nspawn prepare
```

只重建某一个实现：

```bash
./bgperf.py update gobgp
./bgperf.py update bird
./bgperf.py update frr
```

使用自定义上游仓库，但仍使用 bgperf 固定构建模板：

```bash
./bgperf.py prepare --bird-repo https://example.com/my/bird.git
./bgperf.py update bird --repo https://example.com/my/bird.git -c my-branch
```

支持自定义仓库的组件：

* ExaBGP
* MRTParse
* GoBGP
* BIRD
* FRR

不支持为这些组件传入自定义构建命令。

## 4. 运行 Bench

默认测试 GoBGP：

```bash
sudo ./bgperf.py bench
```

指定目标实现：

```bash
sudo ./bgperf.py bench -t gobgp
sudo ./bgperf.py bench -t bird
sudo ./bgperf.py bench -t frr
```

控制测试规模：

```bash
sudo ./bgperf.py bench -n 100 -p 100
```

其中：

* `-n` 是 BGP 测试 peer 数量
* `-p` 是每个 peer 宣告的 prefix 数量

最小可用性测试建议：

```bash
sudo ./bgperf.py bench -n 1 -p 10 -g 0
```

运行时输出会展示当前 benchmark 容器自身的 CPU 和内存，不是整个宿主机：

```text
elapsed: 1sec, target_cpu: 0.11%, target_mem: 158.86MB, recved: 10, containers: monitor=3.31%/109.65MB, gobgp_target=0.11%/158.86MB, exabgp_tester_tester=81.93%/185.36MB
```

`containers:` 后面列出 monitor、target、tester 等 bgperf 容器或 nspawn
machine scope 的资源占用。

## 5. 打包和读取包

只打包，不运行 perf：

```bash
./bgperf.py bench --package-only bgperf-case.tar.gz -t gobgp -n 1 -p 10
```

nspawn runtime 下打包完整 rootfs：

```bash
sudo ./bgperf.py --runtime nspawn --runtime-dir .bgperf-nspawn bench \
  --package-only bgperf-case.tar.gz -t gobgp -n 1 -p 10
```

从压缩包读取镜像或 rootfs，并直接运行 perf：

```bash
sudo ./bgperf.py bench --from-package bgperf-case.tar.gz
```

nspawn runtime 的 package 会包含完整 Debian rootfs 系统压缩包。读取 package 时，
bgperf 会把 rootfs 恢复到指定的 `--runtime-dir`，不重新构建 BGP 软件：

```bash
sudo ./bgperf.py --runtime nspawn --runtime-dir .bgperf-nspawn-loaded \
  bench --from-package bgperf-case.tar.gz
```

## 6. 报告生成

bench 默认会在运行目录下生成：

* `metrics.jsonl`：原始逐秒采样
* `run.json`：本次运行元数据
* `report.md`：Markdown 报告

默认运行目录是：

```text
DIR/BENCH_NAME
```

也就是默认的 `/tmp/bgperf`。

指定报告输出路径：

```bash
sudo ./bgperf.py bench -n 1 -p 10 --report /tmp/bgperf-report.md
```

禁用自动报告：

```bash
sudo ./bgperf.py bench -n 1 -p 10 --no-report
```

从已有采样重新生成报告：

```bash
./bgperf.py report --run-dir /tmp/bgperf -o /tmp/bgperf-report-regenerated.md
```

报告包含：

* runtime 和目标 BGP 实现
* peer 数量和预期路由数
* 最终收到的路由数
* 每个容器的平均/峰值 CPU
* 每个容器的平均/峰值/最终内存
* 路由接收进度

## 7. TUI

启动 TUI：

```bash
./bgperf.py tui
```

TUI 会读取 CLI parser 自动生成界面，因此新增的 `report` 命令、`--report`、
`--no-report`、nspawn runtime 参数都会出现在 TUI 中。

从 TUI 启动 bench 后，会显示和 CLI 相同的运行时输出，包括每个容器自身的
CPU 和内存。

## 8. 清理

Podman runtime 可用常规 Podman 命令清理容器和网络。

nspawn runtime 的运行文件集中在 `--runtime-dir` 下，管理员也可以手动清理：

```bash
machinectl list
sudo machinectl terminate <machine-name>
sudo ip link del <bridge-name>
sudo rm -rf .bgperf-nspawn
```

如果 bench 运行期间被 kill，`--runtime-dir/run/machines` 和
`--runtime-dir/run/networks` 里的 metadata 可以帮助定位要清理的 machine 和
bridge。日志在 `--runtime-dir/run/logs`。
