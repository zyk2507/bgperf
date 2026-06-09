bgperf
========

bgperf is a performance measurement tool for BGP implementation.

* [How to install](#how_to_install)
* [How to use](#how_to_use)
* [How bgperf works](https://github.com/osrg/bgperf/blob/master/docs/how_bgperf_works.md)
* [Benchmark remote target](https://github.com/osrg/bgperf/blob/master/docs/benchmark_remote_target.md)
* [MRT injection](https://github.com/osrg/bgperf/blob/master/docs/mrt.md)

## Prerequisites

* Python 3.8 or later
* Podman, or systemd-nspawn with debootstrap

##  <a name="how_to_install">How to install

```bash
$ git clone https://github.com/osrg/bgperf
$ cd bgperf
$ pip install -r pip-requirements.txt
$ podman system service --time=0 &
$ ./bgperf.py --help
usage: bgperf.py [-h] [-b BENCH_NAME] [-d DIR]
                 {doctor,prepare,update,bench,config} ...

BGP performance measuring tool

positional arguments:
  {doctor,prepare,update,bench,config}
    doctor              check env
    prepare             prepare env
    update              rebuild bgp container images
    bench               run benchmarks
    config              generate config

optional arguments:
  -h, --help            show this help message and exit
  -b BENCH_NAME, --bench-name BENCH_NAME
  -d DIR, --dir DIR
$ ./bgperf.py prepare
$ ./bgperf.py doctor
podman version ... ok (5.8.2)
bgperf image ... ok
gobgp image ... ok
bird image ... ok
frr image ... ok
```

## Runtimes

Podman is the default runtime. bgperf can also run with systemd-nspawn and a
Debian rootfs stored below the current working tree.

```bash
$ sudo ./bgperf.py --runtime nspawn --runtime-dir .bgperf-nspawn prepare
$ sudo ./bgperf.py --runtime nspawn --runtime-dir .bgperf-nspawn bench -n 1 -p 10
```

The nspawn runtime requires `systemd-nspawn`, `machinectl`, `systemd-run`,
`debootstrap`, and `ip`. It builds Debian trixie rootfs images by default and
downloads the latest stable Go toolchain from `go.dev` inside the rootfs for
GoBGP builds. Resource limits are passed through systemd with
`--nspawn-cpu-quota` and `--nspawn-memory-max`.

The nspawn runtime keeps its base rootfs, built systems, run directories, and
logs under `--runtime-dir`, so cleanup is usually removing that directory after
terminating any active bgperf machines.

## <a name="how_to_use">How to use

Use `bench` command to start benchmark test.
By default, `bgperf` benchmarks [GoBGP](https://github.com/osrg/gobgp).
`bgperf` boots 100 BGP test peers each advertises 100 routes to `GoBGP`.

```bash
$ sudo ./bgperf.py bench
run tester
tester booting.. (100/100)
run gobgp
elapsed: 16sec, cpu: 0.20%, mem: 580.90MB
elapsed time: 11sec
```

To change a target implementation, use `-t` option.
Currently, `bgperf` supports [BIRD](http://bird.network.cz/) and
[FRRouting](https://frrouting.org/) other than GoBGP.

```bash
$ sudo ./bgperf.py bench -t bird
run tester
tester booting.. (100/100)
run bird
elapsed: 16sec, cpu: 0.00%, mem: 147.55MB
elapsed time: 11sec
```

To build from a custom upstream repository, pass the repository URL while still
using bgperf's fixed build template for that implementation.

```bash
$ ./bgperf.py prepare --bird-repo https://example.com/my/bird.git
$ ./bgperf.py update bird --repo https://example.com/my/bird.git -c my-branch
```

Custom repositories are supported for ExaBGP, MRTParse, GoBGP, BIRD, and FRR.
Custom build commands are not supported.

To change a load, use following options.

* `-n` : the number of BGP test peer (default 100)
* `-p` : the number of prefix each peer advertise (default 100)
* `-a` : the number of as-path filter (default 0)
* `-e` : the number of prefix-list filter (default 0)
* `-c` : the number of community-list filter (default 0)
* `-x` : the number of ext-community-list filter (default 0)

```bash
$ sudo ./bgperf.py bench -n 200 -p 50
run tester
tester booting.. (200/200)
run gobgp
elapsed: 23sec, cpu: 0.02%, mem: 1.26GB
elapsed time: 18sec
```

To prepare a benchmark scenario on one host and run it later, package it first.
This writes a compressed archive and exits before starting containers.

```bash
$ ./bgperf.py bench --package-only bgperf-case.tar.gz -t bird -n 200 -p 50
$ sudo ./bgperf.py bench --from-package bgperf-case.tar.gz
```

The package contains the rendered scenario, any referenced target config or MRT files,
and the runtime artifacts needed by the benchmark. Podman packages contain
Podman image archives. nspawn packages contain compressed full Debian rootfs
systems. Run `./bgperf.py prepare` before creating a package.

```bash
$ sudo ./bgperf.py --runtime nspawn --runtime-dir .bgperf-nspawn bench --package-only bgperf-case.tar.gz -t gobgp -n 1 -p 10
$ sudo ./bgperf.py --runtime nspawn --runtime-dir .bgperf-nspawn-loaded bench --from-package bgperf-case.tar.gz
```

`--from-package` loads the packaged images or rootfs systems and runs the
benchmark without rebuilding them.

For a comprehensive list of options, run `sudo ./bgperf.py bench --help`.

## Terminal UI

Run the terminal UI to configure and launch any bgperf command from one screen.

```bash
$ ./bgperf.py tui
```

Use the arrow keys to move between commands, fields, and the Run/Reset/Quit
buttons. Press Enter to edit text fields or activate a button; use left/right
to change choices and toggle boolean options.
