# Copyright (C) 2016 Nippon Telegraph and Telephone Corporation.
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

from settings import podman_client, runtime_config
import io
import os
import shlex
import yaml
from itertools import chain
from threading import Thread
import netaddr
import sys

flatten = lambda l: chain.from_iterable(l)


def shell_quote(value):
    return shlex.quote(str(value))

def get_ctn_names():
    if runtime_config.name == 'nspawn':
        from nspawn import nspawn_manager
        return nspawn_manager.container_names()
    names = list(flatten(n['Names'] for n in podman_client.containers(all=True)))
    return [n[1:] if n and n[0] == '/' else n for n in names]


def ctn_exists(name):
    return name in get_ctn_names()


def remove_ctn(name, force=True):
    if runtime_config.name == 'nspawn':
        from nspawn import nspawn_manager
        return nspawn_manager.remove_container(name, force=force)
    return podman_client.remove_container(name, force=force)


def img_exists(name):
    if runtime_config.name == 'nspawn':
        from nspawn import nspawn_manager
        return nspawn_manager.image_exists(name)
    image_names = []
    for image in podman_client.images():
        for repo_tag in image.get('RepoTags') or []:
            repo = repo_tag.rsplit(':', 1)[0]
            if repo.startswith('localhost/'):
                repo = repo[len('localhost/'):]
            image_names.append(repo)
    return name in image_names


def rm_line():
    print('\x1b[1A\x1b[2K\x1b[1D\x1b[1A')


class Container(object):
    def __init__(self, name, image, host_dir, guest_dir, conf):
        self.name = name
        self.image = image
        self.host_dir = host_dir
        self.guest_dir = guest_dir
        self.conf = conf
        self.config_name = None
        if not os.path.exists(host_dir):
            os.makedirs(host_dir)
            os.chmod(host_dir, 0o777)

    @classmethod
    def build_image(cls, force, tag, nocache=False):
        if runtime_config.name == 'nspawn':
            raise NotImplementedError('nspawn image build is implemented by each BGP image class')

        def insert_after_from(containerfile, line):
            lines = containerfile.split('\n')
            i = -1
            for idx, l in enumerate(lines):
                elems = [e.strip() for e in l.split()]
                if len(elems) > 0 and elems[0] == 'FROM':
                    i = idx
            if i < 0:
                raise Exception('no FROM statement')
            lines.insert(i+1, line)
            return '\n'.join(lines)

        containerfile = cls.containerfile
        for env in ['http_proxy', 'https_proxy']:
            if env in os.environ:
                containerfile = insert_after_from(containerfile, 'ENV {0} {1}'.format(env, os.environ[env]))

        f = io.StringIO(containerfile)
        if force or not img_exists(tag):
            print('build {0}...'.format(tag))
            for line in podman_client.build(fileobj=f, rm=True, tag=tag, decode=True, nocache=nocache):
                if 'stream' in line:
                    print(line['stream'].strip())

    def get_ipv4_addresses(self):
        if 'local-address' in self.conf:
            local_addr = self.conf['local-address']
            return [local_addr]
        raise NotImplementedError()

    def run(self, network_name='', rm=True):
        if runtime_config.name == 'nspawn':
            from nspawn import nspawn_manager
            return nspawn_manager.start_container(self, network_name, rm=rm)

        if rm and ctn_exists(self.name):
            print('remove container:', self.name)
            remove_ctn(self.name, force=True)

        host_config = podman_client.create_host_config(
            binds=['{0}:{1}'.format(os.path.abspath(self.host_dir), self.guest_dir)],
            privileged=True,
            network_mode='bridge',
            cap_add=['NET_ADMIN']
        )

        ctn = podman_client.create_container(image=self.image, entrypoint='bash', detach=True, name=self.name,
                                    stdin_open=True, volumes=[self.guest_dir], host_config=host_config)
        self.ctn_id = ctn['Id']

        ipv4_addresses = self.get_ipv4_addresses()

        net_id = None
        for network in podman_client.networks(names=[network_name]):
            if network['Name'] != network_name:
                continue

            net_id = network['Id']
            if not 'IPAM' in network:
                print('can\'t verify if container\'s IP addresses '
                      'are valid for Podman network {}: missing IPAM'.format(network_name))
                break
            ipam = network['IPAM']

            if not 'Config' in ipam:
                print('can\'t verify if container\'s IP addresses '
                      'are valid for Podman network {}: missing IPAM.Config'.format(network_name))
                break

            ip_ok = False
            network_subnets = [item['Subnet'] for item in ipam['Config'] if 'Subnet' in item]
            for ip in ipv4_addresses:
                for subnet in network_subnets:
                    ip_ok = netaddr.IPAddress(ip) in netaddr.IPNetwork(subnet)

                if not ip_ok:
                    print('the container\'s IP address {} is not valid for Podman network {} '
                          'since it\'s not part of any of its subnets ({})'.format(
                              ip, network_name, ', '.join(network_subnets)))
                    print('Please consider removing the Podman network {net} '
                          'to allow bgperf to create it again using the '
                          'expected subnet:\n'
                          '  podman network rm {net}'.format(net=network_name))
                    sys.exit(1)
            break

        if net_id is None:
            print('Podman network "{}" not found!'.format(network_name))
            return

        podman_client.connect_container_to_network(self.ctn_id, net_id, ipv4_address=ipv4_addresses[0])
        podman_client.start(container=self.name)

        if len(ipv4_addresses) > 1:

            # get the interface used by the first IP address already added by Podman
            dev = None
            res = self.local('ip addr')
            for line in res.split('\n'):
                if ipv4_addresses[0] in line:
                    dev = line.split(' ')[-1].strip()
            if not dev:
                dev = "eth0"

            for ip in ipv4_addresses[1:]:
                self.local('ip addr add {} dev {}'.format(ip, dev))

        return ctn

    def stats(self, queue):
        if runtime_config.name == 'nspawn':
            from nspawn import nspawn_manager
            return nspawn_manager.stats(self.name, queue)

        def stats():
            for stat in podman_client.stats(self.ctn_id, decode=True):
                cpu_percentage, mem_usage = self._parse_stats(stat)
                queue.put({'who': self.name, 'cpu': cpu_percentage, 'mem': mem_usage})

        t = Thread(target=stats)
        t.daemon = True
        t.start()

    def _parse_stats(self, stat):
        if 'cpu_stats' in stat and 'precpu_stats' in stat:
            cpu_percentage = 0.0
            prev_cpu = stat['precpu_stats']['cpu_usage']['total_usage']
            prev_system = stat['precpu_stats'].get('system_cpu_usage', 0)
            cpu = stat['cpu_stats']['cpu_usage']['total_usage']
            system = stat['cpu_stats'].get('system_cpu_usage', 0)
            percpu = stat['cpu_stats']['cpu_usage'].get('percpu_usage') or []
            cpu_num = len(percpu) or 1
            cpu_delta = float(cpu) - float(prev_cpu)
            system_delta = float(system) - float(prev_system)
            if system_delta > 0.0 and cpu_delta > 0.0:
                cpu_percentage = (cpu_delta / system_delta) * float(cpu_num) * 100.0
            mem_usage = stat.get('memory_stats', {}).get('usage', 0)
            return cpu_percentage, mem_usage

        return self._parse_podman_stats(stat)

    def _parse_podman_stats(self, stat):
        cpu_percentage = self._parse_percentage(
            stat.get('CPUPerc') or stat.get('CPU') or stat.get('cpu_percent') or 0
        )
        mem_usage = (
            stat.get('MemUsageBytes') or
            stat.get('MemUsage') or
            stat.get('mem_usage') or
            stat.get('memory') or
            0
        )
        if isinstance(mem_usage, str):
            mem_usage = mem_usage.split('/')[0].strip()
            mem_usage = self._parse_size(mem_usage)
        return cpu_percentage, mem_usage

    def _parse_percentage(self, value):
        if isinstance(value, str):
            value = value.strip().rstrip('%')
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _parse_size(self, value):
        if not isinstance(value, str):
            return int(value)
        factors = {
            'b': 1,
            'kb': 1000,
            'kib': 1024,
            'mb': 1000 ** 2,
            'mib': 1024 ** 2,
            'gb': 1000 ** 3,
            'gib': 1024 ** 3,
        }
        value = value.strip()
        number = ''.join(ch for ch in value if ch.isdigit() or ch == '.')
        unit = ''.join(ch for ch in value if ch.isalpha()).lower() or 'b'
        try:
            return int(float(number) * factors.get(unit, 1))
        except (TypeError, ValueError):
            return 0

    def local(self, cmd, stream=False, detach=False):
        if runtime_config.name == 'nspawn':
            from nspawn import nspawn_manager
            return nspawn_manager.exec(self.name, cmd, stream=stream, detach=detach)
        i = podman_client.exec_create(container=self.name, cmd=cmd)
        return podman_client.exec_start(i['Id'], stream=stream, detach=detach)

    def get_startup_cmd(self):
        raise NotImplementedError()

    def exec_startup_cmd(self, stream=False, detach=False):
        startup_content = self.get_startup_cmd()

        if not startup_content:
            return

        filename = '{0}/start.sh'.format(self.host_dir)
        with open(filename, 'w') as f:
            f.write(startup_content)
        os.chmod(filename, 0o777)

        return self.local('{0}/start.sh'.format(self.guest_dir),
                          detach=detach,
                          stream=stream)


class Target(Container):

    CONFIG_FILE_NAME = None

    def write_config(self, scenario_global_conf):
        raise NotImplementedError()

    def use_existing_config(self):
        if 'config_path' in self.conf:
            with open('{0}/{1}'.format(self.host_dir, self.CONFIG_FILE_NAME), 'w') as f:
                with open(self.conf['config_path'], 'r') as orig:
                    f.write(orig.read())
            return True
        return False

    def run(self, scenario_global_conf, network_name=''):
        ctn = super(Target, self).run(network_name)

        if not self.use_existing_config():
            self.write_config(scenario_global_conf)

        self.exec_startup_cmd(detach=True)

        return ctn


class Tester(Container):

    CONTAINER_NAME_PREFIX = None

    def __init__(self, name, host_dir, conf, image):
        Container.__init__(self, self.CONTAINER_NAME_PREFIX + name, image, host_dir, self.GUEST_DIR, conf)

    def get_ipv4_addresses(self):
        res = []
        peers = self.conf.get('neighbors', {}).values()
        for p in peers:
            res.append(p['local-address'])
        return res

    def configure_neighbors(self, target_conf):
        raise NotImplementedError()

    def run(self, target_conf, network_name):
        ctn = super(Tester, self).run(network_name)

        self.configure_neighbors(target_conf)

        if runtime_config.name == 'nspawn':
            self.exec_startup_cmd(detach=True)
            return ctn

        output = self.exec_startup_cmd(stream=True, detach=False)

        cnt = 0
        prev_pid = 0
        for lines in output: # This is the ExaBGP output
            for line in lines.strip().split('\n'):
                fields = line.split('|')
                # Get PID from ExaBGP output
                try:
                    # ExaBGP Version >= 4
                    # e.g. 00:00:00 | 111 | control | command/comment
                    pid = int(fields[1])
                except ValueError:
                    # ExaBGP Version = 3
                    # e.g. 00:00:00 | INFO | 111 | control | command
                    pid = int(fields[2])
                if pid != prev_pid:
                    prev_pid = pid
                    cnt += 1
                    if cnt > 1:
                        rm_line()
                    print('tester booting.. ({0}/{1})'.format(cnt, len(self.conf.get('neighbors', {}).values())))

        return ctn
