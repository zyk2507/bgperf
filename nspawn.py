# Copyright (C) 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
import time
from threading import Thread

import netaddr

from settings import runtime_config


class NspawnManager(object):
    BASE_PACKAGES = [
        'systemd',
        'systemd-sysv',
        'dbus',
        'iproute2',
        'procps',
        'psmisc',
        'ca-certificates',
        'curl',
        'wget',
        'gnupg',
        'git',
        'build-essential',
        'python3',
        'python3-pip',
        'python3-setuptools',
        'python3-dev',
        'iputils-ping',
    ]

    def runtime_dir(self):
        return runtime_config.runtime_dir

    def base_dir(self):
        return os.path.join(self.runtime_dir(), 'base', runtime_config.nspawn_debian_suite)

    def images_dir(self):
        return os.path.join(self.runtime_dir(), 'images')

    def run_dir(self):
        return os.path.join(self.runtime_dir(), 'run')

    def networks_dir(self):
        return os.path.join(self.run_dir(), 'networks')

    def machines_dir(self):
        return os.path.join(self.run_dir(), 'machines')

    def logs_dir(self):
        return os.path.join(self.run_dir(), 'logs')

    def ensure_dirs(self):
        for path in [self.images_dir(), self.networks_dir(), self.machines_dir(), self.logs_dir()]:
            if not os.path.exists(path):
                os.makedirs(path)

    def sanitize(self, value):
        value = value.replace('localhost/', '')
        return ''.join(ch if ch.isalnum() or ch in '._-' else '_' for ch in value)

    def image_key(self, image_name):
        return self.sanitize(image_name.rsplit(':', 1)[0])

    def image_root(self, image_name):
        return os.path.join(self.images_dir(), self.image_key(image_name), 'rootfs')

    def image_exists(self, image_name):
        return os.path.isfile(os.path.join(self.image_root(image_name), 'bin', 'bash'))

    def images(self):
        if not os.path.isdir(self.images_dir()):
            return []
        result = []
        for name in os.listdir(self.images_dir()):
            root = os.path.join(self.images_dir(), name, 'rootfs')
            if os.path.isfile(os.path.join(root, 'bin', 'bash')):
                result.append(name.replace('_', '/'))
        return result

    def machine_name(self, container_name):
        safe = ''.join(ch if ch.isalnum() or ch == '-' else '-' for ch in container_name)
        digest = hashlib.sha1(container_name.encode('utf-8')).hexdigest()[:8]
        safe = safe.strip('-') or 'bgperf'
        safe = safe[:48]
        return '{0}-{1}'.format(safe, digest)

    def machine_dir(self, container_name):
        return os.path.join(self.machines_dir(), self.machine_name(container_name))

    def machine_meta_path(self, container_name):
        return os.path.join(self.machine_dir(container_name), 'machine.json')

    def container_names(self):
        if not os.path.isdir(self.machines_dir()):
            return []
        result = []
        for name in os.listdir(self.machines_dir()):
            meta_path = os.path.join(self.machines_dir(), name, 'machine.json')
            if not os.path.isfile(meta_path):
                continue
            with open(meta_path) as f:
                meta = json.load(f)
            result.append(meta['container_name'])
        return result

    def container_exists(self, container_name):
        meta = self.read_machine_meta(container_name)
        if not meta:
            return False
        return self.machine_state(meta['machine_name']) not in [None, 'closing']

    def read_machine_meta(self, container_name):
        path = self.machine_meta_path(container_name)
        if not os.path.isfile(path):
            return None
        with open(path) as f:
            return json.load(f)

    def command_exists(self, name):
        return shutil.which(name) is not None

    def version(self):
        missing = [cmd for cmd in ['systemd-nspawn', 'machinectl', 'systemd-run', 'ip'] if not self.command_exists(cmd)]
        if missing:
            return 'missing: {0}'.format(', '.join(missing))
        try:
            out = subprocess.check_output(['systemd-nspawn', '--version'], text=True)
            return out.splitlines()[0]
        except Exception as e:
            return 'error: {0}'.format(e)

    def run_checked(self, argv, **kwargs):
        print('+ {0}'.format(' '.join(argv)))
        return subprocess.check_call(argv, **kwargs)

    def run_output(self, argv, **kwargs):
        return subprocess.check_output(argv, text=True, **kwargs)

    def ensure_base(self):
        self.ensure_dirs()
        if os.path.isfile(os.path.join(self.base_dir(), 'bin', 'bash')):
            return
        if not self.command_exists('debootstrap'):
            raise RuntimeError('debootstrap is required to build nspawn Debian rootfs')

        tmp = self.base_dir() + '.tmp'
        if os.path.exists(tmp):
            shutil.rmtree(tmp)
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        include = ','.join(self.BASE_PACKAGES)
        self.run_checked([
            'debootstrap',
            '--variant=minbase',
            '--include={0}'.format(include),
            runtime_config.nspawn_debian_suite,
            tmp,
            runtime_config.nspawn_debian_mirror,
        ])
        self.prepare_rootfs(tmp)
        os.rename(tmp, self.base_dir())

    def prepare_rootfs(self, rootfs):
        resolv = os.path.join(rootfs, 'etc', 'resolv.conf')
        if os.path.exists('/etc/resolv.conf'):
            shutil.copy2('/etc/resolv.conf', resolv)
        policy = os.path.join(rootfs, 'usr', 'sbin', 'policy-rc.d')
        with open(policy, 'w') as f:
            f.write('#!/bin/sh\nexit 101\n')
        os.chmod(policy, 0o755)

    def copy_base_rootfs(self, dest):
        if os.path.exists(dest):
            shutil.rmtree(dest)
        os.makedirs(dest)
        self.run_checked(['cp', '-a', '--reflink=auto', os.path.join(self.base_dir(), '.'), dest])
        self.prepare_rootfs(dest)

    def run_in_rootfs(self, rootfs, script):
        script_path = os.path.join(rootfs, 'root', 'bgperf-build.sh')
        with open(script_path, 'w') as f:
            f.write(script)
        os.chmod(script_path, 0o755)
        self.run_checked([
            'systemd-nspawn',
            '--quiet',
            '--directory', rootfs,
            '--as-pid2',
            '/bin/bash',
            '/root/bgperf-build.sh',
        ])

    def build_image(self, image_name, force, script):
        self.ensure_base()
        rootfs = self.image_root(image_name)
        if os.path.isfile(os.path.join(rootfs, 'bin', 'bash')) and not force:
            print('nspawn image {0} already exists'.format(image_name))
            return
        image_dir = os.path.dirname(rootfs)
        if os.path.exists(image_dir):
            shutil.rmtree(image_dir)
        os.makedirs(image_dir)
        print('build nspawn image {0}...'.format(image_name))
        self.copy_base_rootfs(rootfs)
        self.run_in_rootfs(rootfs, script)
        with open(os.path.join(image_dir, 'image.json'), 'w') as f:
            json.dump({'name': image_name, 'built_at': time.time()}, f, indent=2, sort_keys=True)

    def install_latest_go_script(self):
        return r'''
install_latest_go() {
    arch="$(dpkg --print-architecture)"
    case "$arch" in
        amd64) goarch="amd64" ;;
        arm64) goarch="arm64" ;;
        armhf) goarch="armv6l" ;;
        *) echo "unsupported Go architecture: $arch" >&2; exit 1 ;;
    esac
    version="$(python3 - <<'PY'
import json
import urllib.request
releases = json.load(urllib.request.urlopen('https://go.dev/dl/?mode=json'))
for release in releases:
    if release.get('stable'):
        print(release['version'])
        break
PY
)"
    curl -fsSLo /tmp/go.tar.gz "https://go.dev/dl/${version}.linux-${goarch}.tar.gz"
    rm -rf /usr/local/go
    tar -C /usr/local -xzf /tmp/go.tar.gz
    ln -sf /usr/local/go/bin/go /usr/local/bin/go
    ln -sf /usr/local/go/bin/gofmt /usr/local/bin/gofmt
}
'''

    def build_gobgp(self, image_name, force, repo, checkout):
        script = r'''#!/bin/bash
set -eux
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl git build-essential python3
{install_go}
install_latest_go
cd /root
rm -rf gobgp
git clone {repo} gobgp
cd gobgp
git checkout {checkout}
go install ./cmd/gobgp ./cmd/gobgpd
ln -sf /root/go/bin/gobgp /usr/local/bin/gobgp
ln -sf /root/go/bin/gobgpd /usr/local/bin/gobgpd
'''.format(
            install_go=self.install_latest_go_script(),
            repo=self.shell_quote(repo),
            checkout=self.shell_quote(checkout),
        )
        self.build_image(image_name, force, script)

    def build_exabgp(self, image_name, force, repo, checkout, mrtparse_repo=None):
        mrtparse = ''
        if mrtparse_repo:
            mrtparse = r'''
rm -rf /root/mrtparse
git clone {mrtparse_repo} /root/mrtparse
cd /root/mrtparse
python3 setup.py install
'''.format(mrtparse_repo=self.shell_quote(mrtparse_repo))
        script = r'''#!/bin/bash
set -eux
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates git python3 python3-setuptools gcc python3-dev python3-pip
cd /root
rm -rf exabgp
git clone {repo} exabgp
cd exabgp
git checkout {checkout}
python3 -m pip install --break-system-packages six
python3 -m pip install --break-system-packages -r requirements.txt
python3 setup.py install
ln -sf /root/exabgp /exabgp
{mrtparse}
'''.format(
            repo=self.shell_quote(repo),
            checkout=self.shell_quote(checkout),
            mrtparse=mrtparse,
        )
        self.build_image(image_name, force, script)

    def build_bird(self, image_name, force, repo, checkout):
        script = r'''#!/bin/bash
set -eux
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates git autoconf libtool gawk make flex bison libncurses-dev libreadline-dev build-essential
cd /root
rm -rf bird
git clone {repo} bird
cd bird
git checkout {checkout}
autoreconf -i
./configure
make -j"$(nproc)"
make install
'''.format(repo=self.shell_quote(repo), checkout=self.shell_quote(checkout))
        self.build_image(image_name, force, script)

    def build_frr(self, image_name, force, repo, checkout):
        script = r'''#!/bin/bash
set -eux
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates git autoconf automake libtool make gawk libreadline-dev texinfo dejagnu pkg-config libpam0g-dev libjson-c-dev bison flex python3-pytest libc-ares-dev python3-dev libsystemd-dev build-essential
getent group frr >/dev/null || groupadd -g 92 frr
getent group frrvty >/dev/null || groupadd -r -g 85 frrvty
id frr >/dev/null 2>&1 || adduser --system --ingroup frr --home /var/run/frr/ --gecos "FRR suite" --shell /sbin/nologin frr
usermod -a -G frrvty frr
cd /root
rm -rf frr
git clone {repo} frr
cd frr
git checkout {checkout}
./bootstrap.sh
./configure --prefix=/usr --enable-exampledir=/usr/share/doc/frr/examples/ --localstatedir=/var/run/frr --sbindir=/usr/lib/frr --sysconfdir=/etc/frr --enable-watchfrr --enable-multipath=64 --enable-user=frr --enable-group=frr --enable-vty-group=frrvty --enable-configfile-mask=0640 --enable-logfile-mask=0640 --enable-rtadv --with-pkg-git-version --with-pkg-extra-version=-bgperf_frr
make -j2
make check
make install
'''.format(repo=self.shell_quote(repo), checkout=self.shell_quote(checkout))
        self.build_image(image_name, force, script)

    def shell_quote(self, value):
        import shlex
        return shlex.quote(str(value))

    def network_name(self, network_name):
        digest = hashlib.sha1(network_name.encode('utf-8')).hexdigest()[:12]
        return 'bp{0}'.format(digest)

    def network_meta_path(self, network_name):
        return os.path.join(self.networks_dir(), self.sanitize(network_name) + '.json')

    def ensure_network(self, network_name, subnet):
        self.ensure_dirs()
        subnet = netaddr.IPNetwork(subnet)
        bridge = self.network_name(network_name)
        gateway = str(subnet.ip + 1)
        prefix = subnet.prefixlen
        meta = {
            'Id': bridge,
            'Name': network_name,
            'BridgeName': bridge,
            'Gateway': gateway,
            'IPAM': {'Config': [{'Subnet': str(subnet), 'Gateway': gateway}]},
        }
        if subprocess.call(['ip', 'link', 'show', bridge], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
            self.run_checked(['ip', 'link', 'add', bridge, 'type', 'bridge'])
        subprocess.call(['ip', 'addr', 'add', '{0}/{1}'.format(gateway, prefix), 'dev', bridge],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.run_checked(['ip', 'link', 'set', bridge, 'up'])
        with open(self.network_meta_path(network_name), 'w') as f:
            json.dump(meta, f, indent=2, sort_keys=True)
        return meta

    def networks(self, names=None):
        if not os.path.isdir(self.networks_dir()):
            return []
        wanted = set(names or [])
        result = []
        for filename in os.listdir(self.networks_dir()):
            if not filename.endswith('.json'):
                continue
            with open(os.path.join(self.networks_dir(), filename)) as f:
                meta = json.load(f)
            if wanted and meta['Name'] not in wanted:
                continue
            result.append(meta)
        return result

    def start_container(self, container, network_name, rm=True):
        self.ensure_dirs()
        if rm and self.read_machine_meta(container.name):
            self.remove_container(container.name, force=True)

        rootfs_image = self.image_root(container.image)
        if not os.path.isfile(os.path.join(rootfs_image, 'bin', 'bash')):
            raise RuntimeError('nspawn image not found: {0}. Run `bgperf prepare --runtime nspawn` first.'.format(container.image))

        networks = self.networks([network_name])
        if not networks:
            raise RuntimeError('nspawn network not found: {0}'.format(network_name))
        network = networks[0]
        machine = self.machine_name(container.name)
        machine_dir = self.machine_dir(container.name)
        rootfs = os.path.join(machine_dir, 'rootfs')
        if os.path.exists(machine_dir):
            shutil.rmtree(machine_dir)
        os.makedirs(machine_dir)
        self.run_checked(['cp', '-a', '--reflink=auto', os.path.join(rootfs_image, '.'), rootfs])

        host_dir = os.path.abspath(container.host_dir)
        if not os.path.exists(host_dir):
            os.makedirs(host_dir)
        guest_dir = container.guest_dir
        os.makedirs(os.path.dirname(os.path.join(rootfs, guest_dir.lstrip('/'))), exist_ok=True)
        log_path = os.path.join(self.logs_dir(), machine + '.log')
        log = open(log_path, 'ab')
        cmd = [
            'systemd-nspawn',
            '--quiet',
            '--register=yes',
            '--machine', machine,
            '--directory', rootfs,
            '--boot',
            '--network-veth',
            '--network-bridge', network['BridgeName'],
            '--bind', '{0}:{1}'.format(host_dir, guest_dir),
            '--property=CPUQuota={0}'.format(runtime_config.nspawn_cpu_quota),
            '--property=MemoryMax={0}'.format(runtime_config.nspawn_memory_max),
        ]
        proc = subprocess.Popen(cmd, stdout=log, stderr=log)
        log.close()
        meta = {
            'container_name': container.name,
            'machine_name': machine,
            'pid': proc.pid,
            'rootfs': rootfs,
            'image': container.image,
            'network': network,
            'log': log_path,
        }
        with open(self.machine_meta_path(container.name), 'w') as f:
            json.dump(meta, f, indent=2, sort_keys=True)
        try:
            self.wait_machine(machine, proc=proc, log_path=log_path)
            self.configure_addresses(container.name, container.get_ipv4_addresses(), network)
        except Exception:
            self.remove_container(container.name, force=True)
            raise
        container.ctn_id = machine
        return {'Id': machine}

    def wait_machine(self, machine, proc=None, log_path=None):
        deadline = time.time() + 60
        while time.time() < deadline:
            if proc is not None and proc.poll() is not None:
                raise RuntimeError('systemd-nspawn machine exited before startup: {0}. See {1}'.format(
                    machine, log_path or 'nspawn log'))
            state = self.machine_state(machine)
            if state in ['running', 'degraded']:
                self.wait_machine_systemd(machine)
                return
            time.sleep(0.5)
        raise RuntimeError('systemd-nspawn machine did not start: {0}'.format(machine))

    def wait_machine_systemd(self, machine):
        deadline = time.time() + 60
        last_error = None
        while time.time() < deadline:
            try:
                subprocess.check_output(
                    ['systemd-run', '--machine', machine, '--quiet', '--collect',
                     '--pipe', '--wait', '/bin/true'],
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                return
            except subprocess.CalledProcessError as e:
                last_error = e.output.strip()
            time.sleep(0.5)
        raise RuntimeError('systemd-nspawn machine systemd is not ready: {0}: {1}'.format(
            machine, last_error or 'unknown error'))

    def machine_state(self, machine):
        try:
            out = subprocess.check_output(
                ['machinectl', 'show', machine, '--property=State', '--value'],
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
            return out or None
        except subprocess.CalledProcessError:
            return None

    def configure_addresses(self, container_name, addresses, network):
        subnet = netaddr.IPNetwork(network['IPAM']['Config'][0]['Subnet'])
        gateway = network['Gateway']
        commands = ['ip link set host0 up']
        for address in addresses:
            commands.append('ip addr add {0}/{1} dev host0 2>/dev/null || true'.format(address, subnet.prefixlen))
        commands.append('ip route add default via {0} 2>/dev/null || true'.format(gateway))
        self.exec(container_name, '; '.join(commands))

    def exec(self, container_name, cmd, stream=False, detach=False):
        meta = self.read_machine_meta(container_name)
        if not meta:
            raise RuntimeError('nspawn container not found: {0}'.format(container_name))
        machine = meta['machine_name']
        argv = ['systemd-run', '--machine', machine, '--quiet', '--collect']
        if detach:
            argv.extend(['/bin/bash', '-lc', cmd])
            subprocess.check_call(argv)
            return ''
        argv.extend(['--pipe', '--wait', '/bin/bash', '-lc', cmd])
        if stream:
            return self.stream_process(argv)
        try:
            return subprocess.check_output(argv, stderr=subprocess.STDOUT, text=True)
        except subprocess.CalledProcessError as e:
            raise RuntimeError('nspawn exec failed in {0}: {1}\n{2}'.format(
                container_name, cmd, e.output.strip()))

    def stream_process(self, argv):
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        def generate():
            try:
                for line in proc.stdout:
                    yield line
            finally:
                proc.wait()

        return generate()

    def remove_container(self, container_name, force=True):
        meta = self.read_machine_meta(container_name)
        machine = self.machine_name(container_name)
        if meta:
            machine = meta['machine_name']
            subprocess.call(['machinectl', 'terminate', machine],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.time() + 20
            while time.time() < deadline and self.machine_state(machine):
                time.sleep(0.5)
        self.cleanup_machine_runtime(machine)
        machine_dir = self.machine_dir(container_name)
        if os.path.exists(machine_dir):
            shutil.rmtree(machine_dir)

    def cleanup_machine_runtime(self, machine):
        for base in ['/run/systemd/nspawn/unix-export', '/run/systemd/nspawn/propagate']:
            path = os.path.join(base, machine)
            if not path.startswith(base + os.sep):
                continue
            subprocess.call(['umount', '-R', path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def stats(self, container_name, queue):
        meta = self.read_machine_meta(container_name)
        if not meta:
            return
        machine = meta['machine_name']

        def read():
            prev_cpu = None
            prev_time = None
            while self.machine_state(machine):
                cpu_ns, mem = self.read_scope_stats(machine)
                now = time.time()
                cpu = 0.0
                if prev_cpu is not None and prev_time is not None:
                    elapsed_ns = (now - prev_time) * 1000 * 1000 * 1000
                    if elapsed_ns > 0:
                        cpu = max(0.0, (cpu_ns - prev_cpu) / elapsed_ns * 100.0)
                prev_cpu = cpu_ns
                prev_time = now
                queue.put({
                    'kind': 'resource',
                    'who': container_name,
                    'cpu': cpu,
                    'mem': mem,
                })
                time.sleep(1)

        t = Thread(target=read)
        t.daemon = True
        t.start()

    def read_scope_stats(self, machine):
        props = self.read_scope_properties(machine)
        values = {}
        for line in props.splitlines():
            if '=' in line:
                k, v = line.split('=', 1)
                values[k] = v
        return self.systemd_uint(values.get('CPUUsageNSec')), self.systemd_uint(values.get('MemoryCurrent'))

    def read_scope_properties(self, machine):
        scopes = [
            '{0}.scope'.format(machine),
            'machine-{0}.scope'.format(machine.replace('-', '\\x2d')),
        ]
        for scope in scopes:
            props = subprocess.check_output(
                ['systemctl', 'show', scope, '--property=CPUUsageNSec', '--property=MemoryCurrent'],
                stderr=subprocess.DEVNULL,
                text=True,
            )
            if '[not set]' not in props:
                return props
        return props

    def systemd_uint(self, value):
        if not value or value in ['[not set]', 'infinity']:
            return 0
        try:
            return int(value)
        except ValueError:
            return 0

    def pack_image(self, image_name, archive_path):
        rootfs = self.image_root(image_name)
        if not os.path.isdir(rootfs):
            raise RuntimeError('nspawn image not found: {0}'.format(image_name))
        with tarfile.open(archive_path, 'w:gz', dereference=False) as tar:
            tar.add(rootfs, arcname='rootfs')

    def load_image(self, image_name, archive_path):
        image_dir = os.path.dirname(self.image_root(image_name))
        rootfs = os.path.join(image_dir, 'rootfs')
        if os.path.exists(image_dir):
            shutil.rmtree(image_dir)
        os.makedirs(image_dir)
        with tarfile.open(archive_path, 'r:*') as tar:
            self.safe_extract_rootfs(tar, image_dir)
        if not os.path.isfile(os.path.join(rootfs, 'bin', 'bash')):
            raise RuntimeError('loaded nspawn image is invalid: {0}'.format(image_name))
        with open(os.path.join(image_dir, 'image.json'), 'w') as f:
            json.dump({'name': image_name, 'loaded_at': time.time()}, f, indent=2, sort_keys=True)

    def safe_extract_rootfs(self, tar, dest_dir):
        dest_dir = os.path.abspath(dest_dir)
        link_paths = set()
        for member in tar.getmembers():
            if os.path.isabs(member.name):
                raise RuntimeError('rootfs archive member must be relative: {0}'.format(member.name))
            target = os.path.abspath(os.path.join(dest_dir, member.name))
            if target != dest_dir and not target.startswith(dest_dir + os.sep):
                raise RuntimeError('rootfs archive member escapes destination: {0}'.format(member.name))
            if member.issym():
                link_paths.add(member.name.rstrip('/'))
            elif member.islnk():
                if os.path.isabs(member.linkname):
                    raise RuntimeError('rootfs hard link must be relative: {0}'.format(member.name))
                link_target = os.path.abspath(os.path.join(dest_dir, member.linkname))
                if link_target != dest_dir and not link_target.startswith(dest_dir + os.sep):
                    raise RuntimeError('rootfs hard link escapes destination: {0}'.format(member.name))

        for member in tar.getmembers():
            name = member.name.rstrip('/')
            for link_path in link_paths:
                if name != link_path and name.startswith(link_path + '/'):
                    raise RuntimeError('rootfs member would extract through symlink: {0}'.format(member.name))

        try:
            tar.extractall(dest_dir, filter='fully_trusted')
        except TypeError:
            tar.extractall(dest_dir)


nspawn_manager = NspawnManager()
