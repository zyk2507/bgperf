#
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

import os
import json
import uuid


class IPAMPool(dict):
    def __init__(self, subnet=None, iprange=None, gateway=None, aux_addresses=None):
        super(IPAMPool, self).__init__()
        values = {
            'AuxiliaryAddresses': aux_addresses,
            'Gateway': gateway,
            'IPRange': iprange,
            'Subnet': subnet,
        }
        self.update({k: v for k, v in values.items() if v is not None})


class IPAMConfig(dict):
    def __init__(self, driver='host-local', pool_configs=None, options=None):
        super(IPAMConfig, self).__init__()
        values = {
            'Config': pool_configs or [],
            'Driver': driver,
            'Options': options or {},
        }
        self.update({k: v for k, v in values.items() if v is not None})


def _default_podman_base_url():
    for key in ('PODMAN_HOST', 'CONTAINER_HOST'):
        if os.environ.get(key):
            return os.environ[key]

    candidates = []
    if os.environ.get('XDG_RUNTIME_DIR'):
        candidates.append(os.path.join(os.environ['XDG_RUNTIME_DIR'], 'podman', 'podman.sock'))
    candidates.extend([
        '/run/user/{0}/podman/podman.sock'.format(os.getuid()),
        '/run/podman/podman.sock',
    ])

    for path in candidates:
        if os.path.exists(path):
            return 'unix://{0}'.format(path)

    return None


def _load_podman_client(base_url):
    try:
        from podman import PodmanClient
    except ImportError:
        raise RuntimeError(
            'The Python package "podman" is required. Install dependencies with '
            '`python3 -m pip install -r pip-requirements.txt`.'
        )

    if not base_url:
        raise RuntimeError(
            'Podman service socket not found. Start it with '
            '`podman system service --time=0` and set PODMAN_HOST or CONTAINER_HOST.'
        )

    return PodmanClient(base_url=base_url, version=None)


class PodmanCompatClient(object):
    def __init__(self, base_url=None):
        self.base_url = base_url
        self._client = None
        self._execs = {}

    @property
    def client(self):
        if self._client is None:
            self._client = _load_podman_client(self.base_url or _default_podman_base_url())
        return self._client

    def version(self):
        info = self.client.version()
        if 'Version' not in info:
            info['Version'] = info.get('version', 'unknown')
        return info

    def containers(self, all=False):
        return [self._container_attrs(ctn) for ctn in self.client.containers.list(all=all)]

    def images(self):
        images = []
        for image in self.client.images.list():
            tags = getattr(image, 'tags', None) or image.attrs.get('RepoTags') or []
            images.append({'RepoTags': tags})
        return images

    def build(self, fileobj, rm=True, tag=None, decode=True, nocache=False):
        image, logs = self.client.images.build(fileobj=fileobj, rm=rm, tag=tag, nocache=nocache)
        for line in logs:
            if isinstance(line, bytes):
                line = line.decode('utf-8', 'replace')
            if isinstance(line, str):
                try:
                    line = json.loads(line)
                except ValueError:
                    pass
            if isinstance(line, dict):
                yield line
            else:
                yield {'stream': line}

    def create_host_config(self, binds=None, privileged=False, network_mode=None, cap_add=None):
        volumes = {}
        for bind in binds or []:
            host_path, guest_path = bind.split(':', 1)
            volumes[host_path] = {'bind': guest_path, 'mode': 'rw'}
        return {
            'volumes': volumes,
            'privileged': privileged,
            'network_mode': network_mode,
            'cap_add': cap_add or [],
        }

    def create_container(self, image, entrypoint=None, detach=True, name=None,
                         stdin_open=True, volumes=None, host_config=None):
        kwargs = host_config.copy() if host_config else {}
        kwargs.update({
            'image': image,
            'entrypoint': entrypoint,
            'detach': detach,
            'name': name,
            'stdin_open': stdin_open,
        })
        ctn = self.client.containers.create(**kwargs)
        return {'Id': ctn.id}

    def remove_container(self, container, force=True):
        self.client.containers.get(container).remove(force=force)

    def networks(self, names=None):
        if isinstance(names, (list, tuple, set)):
            names = list(names)
            networks = self.client.networks.list()
            return [
                self._network_attrs(net)
                for net in networks
                if self._network_name(net) in names
            ]
        return [self._network_attrs(net) for net in self.client.networks.list(names=names)]

    def create_network(self, name, driver='bridge', ipam=None):
        network = self.client.networks.create(name, driver=driver, ipam=ipam)
        return self._network_attrs(network)

    def connect_container_to_network(self, container, network, ipv4_address=None):
        self.client.networks.get(network).connect(container, ipv4_address=ipv4_address)

    def start(self, container):
        self.client.containers.get(container).start()

    def stats(self, container, decode=True):
        return self.client.containers.get(container).stats(decode=decode, stream=True)

    def exec_create(self, container, cmd):
        exec_id = uuid.uuid4().hex
        self._execs[exec_id] = (container, cmd)
        return {'Id': exec_id}

    def exec_start(self, exec_id, stream=False, detach=False, socket=False):
        container, cmd = self._execs.pop(exec_id)
        kwargs = {
            'stream': stream,
            'detach': detach,
        }
        if socket and not detach:
            kwargs['socket'] = True

        _, output = self.client.containers.get(container).exec_run(cmd, **kwargs)
        if stream:
            return self._decode_stream(output)
        if isinstance(output, bytes):
            return output.decode('utf-8', 'replace')
        return output

    def _decode_stream(self, output):
        for chunk in output:
            if isinstance(chunk, bytes):
                yield chunk.decode('utf-8', 'replace')
            else:
                yield chunk

    def _container_attrs(self, ctn):
        attrs = dict(getattr(ctn, 'attrs', {}) or {})
        names = attrs.get('Names') or attrs.get('names') or [ctn.name]
        if isinstance(names, str):
            names = [names]
        return {
            'Id': ctn.id,
            'Names': names,
        }

    def _network_attrs(self, network):
        attrs = dict(getattr(network, 'attrs', {}) or {})
        name = self._network_name(network)
        network_id = attrs.get('Id') or attrs.get('id') or network.id
        ipam = attrs.get('IPAM') or attrs.get('ipam') or {}
        if 'Config' not in ipam:
            subnets = attrs.get('subnets') or attrs.get('Subnets') or []
            ipam = {'Config': [
                {
                    'Subnet': item.get('subnet') or item.get('Subnet'),
                    'Gateway': item.get('gateway') or item.get('Gateway'),
                }
                for item in subnets
            ]}
        return {
            'Id': network_id,
            'Name': name,
            'IPAM': ipam,
        }

    def _network_name(self, network):
        attrs = dict(getattr(network, 'attrs', {}) or {})
        return attrs.get('Name') or attrs.get('name') or network.name


podman_client = PodmanCompatClient()
