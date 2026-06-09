#!/usr/bin/env python3
#
# Copyright (C) 2015, 2016 Nippon Telegraph and Telephone Corporation.
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
import sys
import copy
import yaml
import time
import shutil
import tarfile
import tempfile
import netaddr
import datetime
from argparse import ArgumentParser, REMAINDER
from itertools import chain, islice
from pyroute2 import IPRoute
from socket import AF_INET
from nsenter import Namespace
from base import *
from exabgp import ExaBGP, ExaBGP_MRTParse
from gobgp import GoBGP, GoBGPTarget
from bird import BIRD, BIRDTarget
from frr import FRRouting, FRRoutingTarget
from tester import ExaBGPTester
from mrt_tester import GoBGPMRTTester, ExaBGPMrtTester
from monitor import Monitor
from settings import IPAMConfig, IPAMPool, podman_client, configure_runtime, runtime_config
from reports import BenchReporter, expected_routes, format_resource_status, mem_human, write_report_from_run
from queue import Queue
from mako.template import Template
from packaging import version

PACKAGE_MANIFEST = 'bgperf-package.yaml'
PACKAGE_SCENARIO = 'scenario.yaml'
PACKAGE_FILES_DIR = 'files'
PACKAGE_IMAGES_DIR = 'images'
TARGET_CHOICES = ['gobgp', 'bird', 'frr']
UPDATE_IMAGE_CHOICES = ['exabgp', 'exabgp_mrtparse', 'gobgp', 'bird', 'frr', 'all']

def gen_mako_macro():
    return '''<%
    import netaddr
    from itertools import islice

    it = netaddr.iter_iprange('100.0.0.0','160.0.0.0')

    def gen_paths(num):
        return list('{0}/32'.format(ip) for ip in islice(it, num))
%>
'''

def rm_line():
    print('\x1b[1A\x1b[2K\x1b[1D\x1b[1A')


def gc_thresh3():
    gc_thresh3 = '/proc/sys/net/ipv4/neigh/default/gc_thresh3'
    if not os.path.exists(gc_thresh3):
        return None
    with open(gc_thresh3) as f:
        return int(f.read().strip())


def render_conf(args, config_dir, write_generated_scenario=True):
    if args.file:
        with open(args.file) as f:
            return yaml.safe_load(Template(f.read()).render())

    scenario = gen_conf(args)
    if write_generated_scenario:
        if not os.path.exists(config_dir):
            os.makedirs(config_dir)
        with open('{0}/scenario.yaml'.format(config_dir), 'w') as f:
            f.write(scenario)
    return yaml.safe_load(Template(scenario).render())


def sanitize_archive_name(value):
    value = os.path.basename(value)
    sanitized = ''.join(ch if ch.isalnum() or ch in '._-' else '_' for ch in value)
    return sanitized or 'file'


def copy_package_file(package_root, src, label, counter):
    src = os.path.abspath(os.path.expanduser(src))
    if not os.path.isfile(src):
        raise ValueError('package input file not found: {0}'.format(src))

    counter[0] += 1
    files_dir = os.path.join(package_root, PACKAGE_FILES_DIR)
    if not os.path.exists(files_dir):
        os.makedirs(files_dir)

    filename = '{0}-{1}-{2}'.format(label, counter[0], sanitize_archive_name(src))
    dst = os.path.join(files_dir, filename)
    shutil.copy2(src, dst)
    return os.path.relpath(dst, package_root)


def package_path(package_root, package_relpath):
    if os.path.isabs(package_relpath):
        raise ValueError('package path must be relative: {0}'.format(package_relpath))

    path = os.path.abspath(os.path.join(package_root, package_relpath))
    package_root = os.path.abspath(package_root)
    if path != package_root and not path.startswith(package_root + os.sep):
        raise ValueError('package path escapes package root: {0}'.format(package_relpath))
    return path


def rewrite_file_refs_for_package(conf, package_root):
    counter = [0]

    target_conf = conf.get('target', {})
    if 'config_path' in target_conf:
        target_conf['config_path'] = copy_package_file(
            package_root, target_conf['config_path'], 'target-config', counter
        )

    for tester in conf.get('testers', []):
        if 'mrt-file' in tester:
            tester['mrt-file'] = copy_package_file(
                package_root, tester['mrt-file'], 'mrt', counter
            )

        for neighbor in tester.get('neighbors', {}).values():
            if 'mrt-file' in neighbor:
                neighbor['mrt-file'] = copy_package_file(
                    package_root, neighbor['mrt-file'], 'mrt', counter
                )


def required_bench_images(conf, args):
    images = ['bgperf/gobgp']

    for tester in conf.get('testers', []):
        tester_type = tester.get('type', 'normal')
        if tester_type == 'normal':
            images.append('bgperf/exabgp')
        elif tester_type == 'mrt':
            mrt_injector = tester.get('mrt_injector', 'gobgp')
            if mrt_injector == 'gobgp':
                images.append('bgperf/gobgp')
            elif mrt_injector == 'exabgp':
                images.append('bgperf/exabgp_mrtparse')
            else:
                raise ValueError('invalid mrt_injector: {0}'.format(mrt_injector))
        else:
            raise ValueError('invalid tester type: {0}'.format(tester_type))

    is_remote = True if 'remote' in conf['target'] and conf['target']['remote'] else False
    if not is_remote:
        images.append(args.image or 'bgperf/{0}'.format(args.target))

    result = []
    for image in images:
        if image not in result:
            result.append(image)
    return result


def image_has_tag(image_name):
    return ':' in image_name.rsplit('/', 1)[-1]


def image_has_registry(image_name):
    if '/' not in image_name:
        return False
    first = image_name.split('/', 1)[0]
    return first == 'localhost' or '.' in first or ':' in first


def package_image_name_candidates(image_name):
    candidates = [image_name]
    if not image_has_tag(image_name):
        candidates.append('{0}:latest'.format(image_name))

    if not image_has_registry(image_name):
        localhost_name = 'localhost/{0}'.format(image_name)
        candidates.append(localhost_name)
        if not image_has_tag(image_name):
            candidates.append('{0}:latest'.format(localhost_name))

    result = []
    for candidate in candidates:
        if candidate not in result:
            result.append(candidate)
    return result


def image_repo_tags(image):
    tags = getattr(image, 'tags', None)
    if tags is None:
        tags = getattr(image, 'attrs', {}).get('RepoTags')
    return tags or []


def package_image_save_name(image_name, image):
    tags = image_repo_tags(image)
    for candidate in package_image_name_candidates(image_name):
        if candidate in tags:
            return candidate
    if tags:
        return tags[0]
    return None


def save_package_images(package_root, conf, args):
    images = []
    images_dir = os.path.join(package_root, PACKAGE_IMAGES_DIR)
    if not os.path.exists(images_dir):
        os.makedirs(images_dir)

    for idx, image_name in enumerate(required_bench_images(conf, args), 1):
        archive_name = 'image-{0}-{1}.tar'.format(idx, sanitize_archive_name(image_name.replace(':', '_')))
        archive_path = os.path.join(images_dir, archive_name)
        try:
            image = podman_client.client.images.get(image_name)
        except Exception as e:
            raise ValueError(
                'required image not found: {0}. Run `bgperf prepare` first or provide an existing image. '
                'Original error: {1}'.format(image_name, e)
            )

        save_name = package_image_save_name(image_name, image)
        print('saving image {0}'.format(image_name))
        with open(archive_path, 'wb') as f:
            save_kwargs = {'named': save_name} if save_name else {'named': False}
            for chunk in image.save(**save_kwargs):
                if chunk:
                    f.write(chunk)

        images.append({
            'name': image_name,
            'path': os.path.relpath(archive_path, package_root),
            'saved_as': save_name,
        })

    return images


def load_package_images(manifest, package_root):
    for image in manifest.get('images', []):
        image_name = image.get('name')
        image_path = image.get('path')
        if not image_name or not image_path:
            raise ValueError('invalid image entry in package manifest')

        path = package_path(package_root, image_path)
        if not os.path.isfile(path):
            raise ValueError('package image archive not found: {0}'.format(image_path))

        print('loading image {0}'.format(image_name))
        for _ in podman_client.client.images.load(file_path=path):
            pass


def save_package_systems(package_root, conf, args):
    systems = []
    systems_dir = os.path.join(package_root, PACKAGE_IMAGES_DIR)
    if not os.path.exists(systems_dir):
        os.makedirs(systems_dir)

    from nspawn import nspawn_manager
    for idx, image_name in enumerate(required_bench_images(conf, args), 1):
        archive_name = 'system-{0}-{1}.tar.gz'.format(idx, sanitize_archive_name(image_name.replace(':', '_')))
        archive_path = os.path.join(systems_dir, archive_name)
        print('saving nspawn system {0}'.format(image_name))
        try:
            nspawn_manager.pack_image(image_name, archive_path)
        except RuntimeError as e:
            raise ValueError(str(e))
        systems.append({
            'name': image_name,
            'path': os.path.relpath(archive_path, package_root),
        })
    return systems


def load_package_systems(manifest, package_root):
    from nspawn import nspawn_manager
    for system in manifest.get('systems', []):
        image_name = system.get('name')
        image_path = system.get('path')
        if not image_name or not image_path:
            raise ValueError('invalid system entry in package manifest')

        path = package_path(package_root, image_path)
        if not os.path.isfile(path):
            raise ValueError('package nspawn system archive not found: {0}'.format(image_path))

        print('loading nspawn system {0}'.format(image_name))
        try:
            nspawn_manager.load_image(image_name, path)
        except RuntimeError as e:
            raise ValueError(str(e))


def write_bench_package(conf, archive_path, args):
    archive_path = os.path.abspath(os.path.expanduser(archive_path))
    archive_dir = os.path.dirname(archive_path)
    if archive_dir and not os.path.exists(archive_dir):
        os.makedirs(archive_dir)

    package_root = tempfile.mkdtemp(prefix='bgperf-package-')
    try:
        packaged_conf = copy.deepcopy(conf)
        rewrite_file_refs_for_package(packaged_conf, package_root)

        manifest = {
            'format': 'bgperf-bench-package-v1',
            'runtime': runtime_config.name,
            'bench': {
                'target': args.target,
                'image': args.image,
            },
        }
        if runtime_config.name == 'nspawn':
            manifest['systems'] = save_package_systems(package_root, packaged_conf, args)
        else:
            manifest['images'] = save_package_images(package_root, packaged_conf, args)

        with open(os.path.join(package_root, PACKAGE_MANIFEST), 'w') as f:
            yaml.safe_dump(manifest, f, default_flow_style=False)

        with open(os.path.join(package_root, PACKAGE_SCENARIO), 'w') as f:
            yaml.safe_dump(packaged_conf, f, default_flow_style=False)

        with tarfile.open(archive_path, 'w:gz') as tar:
            for name in [PACKAGE_MANIFEST, PACKAGE_SCENARIO, PACKAGE_FILES_DIR, PACKAGE_IMAGES_DIR]:
                path = os.path.join(package_root, name)
                if os.path.exists(path):
                    tar.add(path, arcname=name)
    finally:
        shutil.rmtree(package_root)

    return archive_path


def safe_extract_package(archive_path, dest_dir):
    archive_path = os.path.abspath(os.path.expanduser(archive_path))
    dest_dir = os.path.abspath(dest_dir)

    if not tarfile.is_tarfile(archive_path):
        raise ValueError('not a tar archive: {0}'.format(archive_path))

    with tarfile.open(archive_path, 'r:*') as tar:
        for member in tar.getmembers():
            if not (member.isfile() or member.isdir()):
                raise ValueError('package contains unsupported member: {0}'.format(member.name))
            if member.issym() or member.islnk():
                raise ValueError('package contains unsupported link: {0}'.format(member.name))

            target = os.path.abspath(os.path.join(dest_dir, member.name))
            if target != dest_dir and not target.startswith(dest_dir + os.sep):
                raise ValueError('package member escapes destination: {0}'.format(member.name))

        tar.extractall(dest_dir)


def resolve_package_file_refs(conf, package_dir):
    def resolve(value):
        value = os.path.expanduser(value)
        if os.path.isabs(value):
            return value
        return os.path.abspath(os.path.join(package_dir, value))

    target_conf = conf.get('target', {})
    if 'config_path' in target_conf:
        target_conf['config_path'] = resolve(target_conf['config_path'])

    for tester in conf.get('testers', []):
        if 'mrt-file' in tester:
            tester['mrt-file'] = resolve(tester['mrt-file'])

        for neighbor in tester.get('neighbors', {}).values():
            if 'mrt-file' in neighbor:
                neighbor['mrt-file'] = resolve(neighbor['mrt-file'])


def load_bench_package(archive_path, config_dir):
    if os.path.exists(config_dir):
        shutil.rmtree(config_dir)
    os.makedirs(config_dir)

    safe_extract_package(archive_path, config_dir)

    manifest_path = os.path.join(config_dir, PACKAGE_MANIFEST)
    if os.path.isfile(manifest_path):
        with open(manifest_path) as f:
            manifest = yaml.safe_load(f) or {}
    else:
        manifest = {}

    scenario_path = os.path.join(config_dir, PACKAGE_SCENARIO)
    if not os.path.isfile(scenario_path):
        raise ValueError('package does not contain {0}'.format(PACKAGE_SCENARIO))

    with open(scenario_path) as f:
        conf = yaml.safe_load(f)

    if not isinstance(conf, dict):
        raise ValueError('package scenario is invalid')

    resolve_package_file_refs(conf, config_dir)
    return conf, manifest


def validate_bench_args(args):
    if args.package_only and args.from_package:
        raise ValueError('--package-only and --from-package cannot be used together')
    if args.from_package and args.file:
        raise ValueError('--from-package cannot be used together with --file')
    if args.package_only and args.repeat:
        raise ValueError('--package-only cannot be used together with --repeat')
    if args.from_package and args.repeat:
        raise ValueError('--from-package cannot be used together with --repeat')


def doctor(args):
    if runtime_config.name == 'nspawn':
        from nspawn import nspawn_manager
        print('systemd-nspawn ... {0}'.format(nspawn_manager.version()))
        print('nspawn runtime dir ... {0}'.format(runtime_config.runtime_dir))
    else:
        ver = podman_client.version()['Version']
        curr_version = version.parse(ver.replace('-ce', ''))
        min_version = version.parse('1.9.0')
        ok = curr_version >= min_version
        print('podman version ... {1} ({0})'.format(ver, 'ok' if ok else 'update to {} at least'.format(min_version)))

    print('bgperf image', end=' ')
    if img_exists('bgperf/exabgp'):
        print('... ok')
    else:
        print('... not found. run `bgperf prepare`')

    for name in TARGET_CHOICES:
        print('{0} image'.format(name), end=' ')
        if img_exists('bgperf/{0}'.format(name)):
            print('... ok')
        else:
            print('... not found. if you want to bench {0}, run `bgperf prepare`'.format(name))

    thresh = gc_thresh3()
    print('/proc/sys/net/ipv4/neigh/default/gc_thresh3 ... {0}'.format(
        thresh if thresh is not None else 'unavailable'
    ))


def prepare(args):
    ExaBGP.build_image(args.force, nocache=args.no_cache,
                       repo=args.exabgp_repo or ExaBGP.DEFAULT_REPO)
    ExaBGP_MRTParse.build_image(args.force, nocache=args.no_cache,
                                repo=args.exabgp_repo or ExaBGP_MRTParse.DEFAULT_REPO,
                                mrtparse_repo=args.mrtparse_repo or ExaBGP_MRTParse.DEFAULT_MRTPARSE_REPO)
    GoBGP.build_image(args.force, nocache=args.no_cache,
                      repo=args.gobgp_repo or GoBGP.DEFAULT_REPO)
    BIRD.build_image(args.force, nocache=args.no_cache,
                     repo=args.bird_repo or BIRD.DEFAULT_REPO)
    FRRouting.build_image(args.force, checkout='stable/3.0', nocache=args.no_cache,
                          repo=args.frr_repo or FRRouting.DEFAULT_REPO)


def update(args):
    if args.image == 'all' and args.repo:
        print('--repo cannot be used with `update all`; rebuild one image at a time '
              'or use `prepare` with per-image repository options.', file=sys.stderr)
        sys.exit(2)

    if args.image == 'all' or args.image == 'exabgp':
        ExaBGP.build_image(True, checkout=args.checkout, nocache=args.no_cache,
                           repo=args.repo or ExaBGP.DEFAULT_REPO)
    if args.image == 'all' or args.image == 'exabgp_mrtparse':
        ExaBGP_MRTParse.build_image(True, checkout=args.checkout, nocache=args.no_cache,
                                    repo=args.repo or ExaBGP_MRTParse.DEFAULT_REPO,
                                    mrtparse_repo=args.mrtparse_repo or ExaBGP_MRTParse.DEFAULT_MRTPARSE_REPO)
    if args.image == 'all' or args.image == 'gobgp':
        GoBGP.build_image(True, checkout=args.checkout, nocache=args.no_cache,
                          repo=args.repo or GoBGP.DEFAULT_REPO)
    if args.image == 'all' or args.image == 'bird':
        BIRD.build_image(True, checkout=args.checkout, nocache=args.no_cache,
                         repo=args.repo or BIRD.DEFAULT_REPO)
    if args.image == 'all' or args.image == 'frr':
        FRRouting.build_image(True, checkout=args.checkout, nocache=args.no_cache,
                              repo=args.repo or FRRouting.DEFAULT_REPO)


def bench(args):
    config_dir = '{0}/{1}'.format(args.dir, args.bench_name)
    network_name = args.podman_network_name or args.bench_name + '-br'

    try:
        validate_bench_args(args)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)

    if args.package_only:
        conf = render_conf(args, config_dir, write_generated_scenario=False)
        try:
            package_path = write_bench_package(conf, args.package_only, args)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            sys.exit(2)
        print('wrote benchmark package: {0}'.format(package_path))
        return

    if args.from_package:
        try:
            conf, manifest = load_bench_package(args.from_package, config_dir)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            sys.exit(2)
        package_runtime = manifest.get('runtime')
        if package_runtime:
            args.runtime = package_runtime
            configure_runtime(args, runtime_name=package_runtime)
        bench_manifest = manifest.get('bench', {})
        args.target = bench_manifest.get('target', args.target)
        if bench_manifest.get('image'):
            args.image = bench_manifest['image']
        if args.target not in TARGET_CHOICES:
            print('unsupported target in package: {0}'.format(args.target), file=sys.stderr)
            sys.exit(2)
        try:
            if runtime_config.name == 'nspawn':
                load_package_systems(manifest, config_dir)
            else:
                load_package_images(manifest, config_dir)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            sys.exit(2)
    else:
        if not args.repeat and os.path.exists(config_dir):
            shutil.rmtree(config_dir)
        conf = render_conf(args, config_dir, write_generated_scenario=True)

    for target_class in [BIRDTarget, GoBGPTarget, FRRoutingTarget]:
        if ctn_exists(target_class.CONTAINER_NAME):
            print('removing target container', target_class.CONTAINER_NAME)
            remove_ctn(target_class.CONTAINER_NAME, force=True)

    if not args.repeat:
        if ctn_exists(Monitor.CONTAINER_NAME):
            print('removing monitor container', Monitor.CONTAINER_NAME)
            remove_ctn(Monitor.CONTAINER_NAME, force=True)

        for ctn_name in get_ctn_names():
            if ctn_name.startswith(ExaBGPTester.CONTAINER_NAME_PREFIX) or \
                ctn_name.startswith(ExaBGPMrtTester.CONTAINER_NAME_PREFIX) or \
                ctn_name.startswith(GoBGPMRTTester.CONTAINER_NAME_PREFIX):
                print('removing tester container', ctn_name)
                remove_ctn(ctn_name, force=True)

    if runtime_config.name == 'nspawn':
        from nspawn import nspawn_manager
        subnet = conf['local_prefix']
        print('creating or reusing nspawn network "{}" with subnet {}'.format(network_name, subnet))
        network = nspawn_manager.ensure_network(network_name, subnet)
    else:
        bridge_found = False
        for network in podman_client.networks(names=[network_name]):
            if network['Name'] == network_name:
                print('Podman network "{}" already exists'.format(network_name))
                bridge_found = True
                break
        if not bridge_found:
            subnet = conf['local_prefix']
            print('creating Podman network "{}" with subnet {}'.format(network_name, subnet))
            ipam = IPAMConfig(pool_configs=[IPAMPool(subnet=subnet)])
            network = podman_client.create_network(network_name, driver='bridge', ipam=ipam)

    num_tester = sum(len(t.get('neighbors', [])) for t in conf.get('testers', []))
    thresh = gc_thresh3()
    if thresh is not None and num_tester > thresh:
        print('gc_thresh3({0}) is lower than the number of peer({1})'.format(thresh, num_tester))
        print('type next to increase the value')
        print('$ echo 16384 | sudo tee /proc/sys/net/ipv4/neigh/default/gc_thresh3')

    print('run monitor')
    m = Monitor(config_dir+'/monitor', conf['monitor'])
    m.run(conf, network_name)

    is_remote = True if 'remote' in conf['target'] and conf['target']['remote'] else False

    if is_remote:
        print('target is remote ({})'.format(conf['target']['local-address']))

        ip = IPRoute()

        # r: route to the target
        r = ip.get_routes(dst=conf['target']['local-address'], family=AF_INET)
        if len(r) == 0:
            print('no route to remote target {0}'.format(conf['target']['local-address']))
            sys.exit(1)

        # interface used to reach the target
        idx = [t[1] for t in r[0]['attrs'] if t[0] == 'RTA_OIF'][0]
        intf = ip.get_links(idx)[0]
        intf_name = intf.get_attr('IFLA_IFNAME')

        # Linux bridge name of the runtime bridge
        if runtime_config.name == 'nspawn':
            raw_bridge_name = args.bridge_name or network['BridgeName']
        else:
            raw_bridge_name = args.bridge_name or 'br-{}'.format(network['Id'][0:12])

        # list of Linux bridges that match raw_bridge_name
        raw_bridges = ip.link_lookup(ifname=raw_bridge_name)
        if len(raw_bridges) == 0:
            if not args.bridge_name:
                print('can\'t determine the Linux bridge interface name starting '
                      'from the runtime network {}'.format(network_name))
            else:
                print('the Linux bridge name provided ({}) seems nonexistent'.format(
                      raw_bridge_name))
            print('Since the target is remote, the host interface used to '
                    'reach the target ({}) must be part of the Linux bridge '
                    'used by the runtime network {}, but without the correct Linux '
                    'bridge name it\'s impossible to verify if that\'s true'.format(
                        intf_name, network_name))
            if not args.bridge_name:
                print('Please supply the Linux bridge name corresponding to the '
                      'runtime network {} using the --bridge-name argument.'.format(
                          network_name))
            sys.exit(1)

        # bridge interface that intf is already member of
        intf_bridge = intf.get_attr('IFLA_MASTER')

        # if intf is not member of the bridge, add it
        if intf_bridge not in raw_bridges:
            if intf_bridge is None:
                print('Since the target is remote, the host interface used to '
                      'reach the target ({}) must be part of the Linux bridge '
                      'used by the Podman network {}'.format(
                          intf_name, network_name))
                sys.stdout.write('Do you confirm to add the interface {} '
                                 'to the bridge {}? [yes/NO] '.format(
                                     intf_name, raw_bridge_name
                                    ))
                try:
                    answer = input()
                except Exception:
                    print('aborting')
                    sys.exit(1)
                answer = answer.strip()
                if answer.lower() != 'yes':
                    print('aborting')
                    sys.exit(1)

                print('adding interface {} to the bridge {}'.format(
                    intf_name, raw_bridge_name
                ))
                br = raw_bridges[0]

                try:
                    ip.link('set', index=idx, master=br)
                except Exception as e:
                    print('Something went wrong: {}'.format(str(e)))
                    print('Please consider running the following command to '
                          'add the {iface} interface to the {br} bridge:\n'
                          '   sudo brctl addif {br} {iface}'.format(
                              iface=intf_name, br=raw_bridge_name))
                    print('\n\n\n')
                    raise
            else:
                curr_bridge_name = ip.get_links(intf_bridge)[0].get_attr('IFLA_IFNAME')
                print('the interface used to reach the target ({}) '
                      'is already member of the bridge {}, which is not '
                      'the one used in this configuration'.format(
                          intf_name, curr_bridge_name))
                print('Please consider running the following command to '
                        'remove the {iface} interface from the {br} bridge:\n'
                        '   sudo brctl addif {br} {iface}'.format(
                            iface=intf_name, br=curr_bridge_name))
                sys.exit(1)
    else:
        if args.target == 'gobgp':
            target_class = GoBGPTarget
        elif args.target == 'bird':
            target_class = BIRDTarget
        elif args.target == 'frr':
            target_class = FRRoutingTarget

        print('run', args.target)
        if args.image:
            target = target_class('{0}/{1}'.format(config_dir, args.target), conf['target'], image=args.image)
        else:
            target = target_class('{0}/{1}'.format(config_dir, args.target), conf['target'])
        target.run(conf, network_name)

    time.sleep(1)

    print('waiting bgp connection between {0} and monitor'.format(args.target))
    m.wait_established(conf['target']['local-address'])

    testers = []
    if not args.repeat:
        for idx, tester in enumerate(conf['testers']):
            if 'name' not in tester:
                name = 'tester{0}'.format(idx)
            else:
                name = tester['name']
            if 'type' not in tester:
                tester_type = 'normal'
            else:
                tester_type = tester['type']
            if tester_type == 'normal':
                tester_class = ExaBGPTester
            elif tester_type == 'mrt':
                if 'mrt_injector' not in tester:
                    mrt_injector = 'gobgp'
                else:
                    mrt_injector = tester['mrt_injector']
                if mrt_injector == 'gobgp':
                    tester_class = GoBGPMRTTester
                elif mrt_injector == 'exabgp':
                    tester_class = ExaBGPMrtTester
                else:
                    print('invalid mrt_injector:', mrt_injector)
                    sys.exit(1)
            else:
                print('invalid tester type:', tester_type)
                sys.exit(1)
            t = tester_class(name, config_dir+'/'+name, tester)
            print('run tester', name, 'type', tester_type)
            t.run(conf['target'], network_name)
            testers.append(t)

    start = datetime.datetime.now()

    q = Queue()
    reporter = BenchReporter(config_dir, enabled=not args.no_report)
    reporter.start({
        'runtime': runtime_config.name,
        'target': args.target,
        'image': args.image,
        'remote': is_remote,
        'local_prefix': conf.get('local_prefix'),
        'neighbor_count': num_tester,
        'expected_routes': expected_routes(conf),
    })

    resource_containers = [m]
    if not is_remote:
        resource_containers.append(target)
    resource_containers.extend(testers)
    resource_order = [container.name for container in resource_containers]
    latest_resources = {}

    m.stats(q)
    for container in resource_containers:
        container.resource_stats(q)

    f = open(args.output, 'w') if args.output else None
    cooling = -1
    while True:
        info = q.get()
        now = datetime.datetime.now()
        elapsed = now - start
        elapsed_seconds = int(elapsed.total_seconds())

        if info.get('kind') == 'resource':
            latest_resources[info['who']] = {
                'cpu': info.get('cpu', 0.0),
                'mem': info.get('mem', 0),
            }
            reporter.record_resource(elapsed_seconds, info['who'], info.get('cpu', 0.0), info.get('mem', 0))
            continue

        if info.get('kind') != 'bgp':
            continue

        if info['who'] == m.name:
            recved = info['state']['adj-table']['accepted'] if 'accepted' in info['state']['adj-table'] else 0
            reporter.record_routes(elapsed_seconds, recved)
            if elapsed_seconds > 0:
                rm_line()
            target_resource = latest_resources.get(target.name, {}) if not is_remote else {}
            cpu = target_resource.get('cpu', 0.0)
            mem = target_resource.get('mem', 0)
            resource_status = format_resource_status(latest_resources, resource_order)
            print('elapsed: {0}sec, target_cpu: {1:>4.2f}%, target_mem: {2}, recved: {3}, containers: {4}'.format(
                elapsed_seconds, cpu, mem_human(mem), recved, resource_status
            ))
            f.write('{0}, {1}, {2}, {3}\n'.format(elapsed_seconds, cpu, mem, recved)) if f else None
            f.flush() if f else None

            if cooling == args.cooling:
                f.close() if f else None
                report_path = reporter.finish({}, args.report)
                if report_path:
                    print('report: {0}'.format(report_path))
                return

            if cooling >= 0:
                cooling += 1

            if info['checked']:
                cooling = 0

def gen_conf(args):
    neighbor_num = args.neighbor_num
    prefix = args.prefix_num
    as_path_list = args.as_path_list_num
    prefix_list = args.prefix_list_num
    community_list = args.community_list_num
    ext_community_list = args.ext_community_list_num

    local_address_prefix = netaddr.IPNetwork(args.local_address_prefix)

    if args.target_local_address:
        target_local_address = netaddr.IPAddress(args.target_local_address)
    else:
        target_local_address = local_address_prefix.broadcast - 1

    if args.monitor_local_address:
        monitor_local_address = netaddr.IPAddress(args.monitor_local_address)
    else:
        monitor_local_address = local_address_prefix.ip + 2

    if args.target_router_id:
        target_router_id = netaddr.IPAddress(args.target_router_id)
    else:
        target_router_id = target_local_address

    if args.monitor_router_id:
        monitor_router_id = netaddr.IPAddress(args.monitor_router_id)
    else:
        monitor_router_id = monitor_local_address

    conf = {}
    conf['local_prefix'] = str(local_address_prefix)
    conf['target'] = {
        'as': 1000,
        'router-id': str(target_router_id),
        'local-address': str(target_local_address),
        'single-table': args.single_table,
    }

    if args.target_config_file:
        conf['target']['config_path'] = args.target_config_file

    conf['monitor'] = {
        'as': 1001,
        'router-id': str(monitor_router_id),
        'local-address': str(monitor_local_address),
        'check-points': [prefix * neighbor_num],
    }

    offset = 0

    it = netaddr.iter_iprange('90.0.0.0', '100.0.0.0')

    conf['policy'] = {}

    assignment = []

    if prefix_list > 0:
        name = 'p1'
        conf['policy'][name] = {
            'match': [{
                'type': 'prefix',
                'value': list('{0}/32'.format(ip) for ip in islice(it, prefix_list)),
            }],
        }
        assignment.append(name)

    if as_path_list > 0:
        name = 'p2'
        conf['policy'][name] = {
            'match': [{
                'type': 'as-path',
                'value': list(range(10000, 10000 + as_path_list)),
            }],
        }
        assignment.append(name)

    if community_list > 0:
        name = 'p3'
        conf['policy'][name] = {
            'match': [{
                'type': 'community',
                'value': list('{0}:{1}'.format(i // (1 << 16), i % (1 << 16)) for i in range(community_list)),
            }],
        }
        assignment.append(name)

    if ext_community_list > 0:
        name = 'p4'
        conf['policy'][name] = {
            'match': [{
                'type': 'ext-community',
                'value': list('rt:{0}:{1}'.format(i // (1 << 16), i % (1 << 16)) for i in range(ext_community_list)),
            }],
        }
        assignment.append(name)

    neighbors = {}
    configured_neighbors_cnt = 0
    for i in range(3, neighbor_num+3+2):
        if configured_neighbors_cnt == neighbor_num:
            break
        curr_ip = local_address_prefix.ip + i
        if curr_ip in [target_local_address, monitor_local_address]:
            print('skipping tester\'s neighbor with IP {} because it collides with target or monitor'.format(curr_ip))
            continue
        router_id = str(local_address_prefix.ip + i)
        neighbors[router_id] = {
            'as': 1000 + i,
            'router-id': router_id,
            'local-address': router_id,
            'paths': '${{gen_paths({0})}}'.format(prefix),
            'filter': {
                args.filter_type: assignment,
            },
        }
        configured_neighbors_cnt += 1

    conf['testers'] = [{
        'name': 'tester',
        'type': 'normal',
        'neighbors': neighbors,
    }]
    return gen_mako_macro() + yaml.dump(conf, default_flow_style=False)


def config(args):
    conf = gen_conf(args)

    with open(args.output, 'w') as f:
        f.write(conf)


def report_cmd(args):
    config_dir = args.run_dir or '{0}/{1}'.format(args.dir, args.bench_name)
    try:
        report_path = write_report_from_run(config_dir, args.output)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)
    print('wrote report: {0}'.format(report_path))


def build_parser():
    parser = ArgumentParser(description='BGP performance measuring tool')
    parser.add_argument('-b', '--bench-name', default='bgperf')
    parser.add_argument('-d', '--dir', default='/tmp')
    parser.add_argument('--runtime', choices=['podman', 'nspawn'], default='podman',
                        help='container runtime backend')
    parser.add_argument('--runtime-dir', default='.bgperf-nspawn',
                        help='runtime state directory for systemd-nspawn rootfs and run metadata')
    parser.add_argument('--nspawn-debian-suite', default='trixie',
                        help='Debian suite used by debootstrap for nspawn rootfs')
    parser.add_argument('--nspawn-debian-mirror', default='http://deb.debian.org/debian',
                        help='Debian mirror used by debootstrap for nspawn rootfs')
    parser.add_argument('--nspawn-cpu-quota', default='100%',
                        help='systemd CPUQuota applied to each nspawn machine')
    parser.add_argument('--nspawn-memory-max', default='1G',
                        help='systemd MemoryMax applied to each nspawn machine')
    s = parser.add_subparsers()
    parser_doctor = s.add_parser('doctor', help='check env')
    parser_doctor.set_defaults(func=doctor)

    parser_prepare = s.add_parser('prepare', help='prepare env')
    parser_prepare.add_argument('-f', '--force', action='store_true', help='build even if the container already exists')
    parser_prepare.add_argument('-n', '--no-cache', action='store_true')
    parser_prepare.add_argument('--exabgp-repo', default=ExaBGP.DEFAULT_REPO,
                                help='ExaBGP git repository; fixed ExaBGP build template still applies')
    parser_prepare.add_argument('--mrtparse-repo', default=ExaBGP_MRTParse.DEFAULT_MRTPARSE_REPO,
                                help='MRTParse git repository; fixed ExaBGP MRTParse build template still applies')
    parser_prepare.add_argument('--gobgp-repo', default=GoBGP.DEFAULT_REPO,
                                help='GoBGP git repository; fixed GoBGP build template still applies')
    parser_prepare.add_argument('--bird-repo', default=BIRD.DEFAULT_REPO,
                                help='BIRD git repository; fixed BIRD build template still applies')
    parser_prepare.add_argument('--frr-repo', default=FRRouting.DEFAULT_REPO,
                                help='FRR git repository; fixed FRR build template still applies')
    parser_prepare.set_defaults(func=prepare)

    parser_update = s.add_parser('update', help='rebuild bgp container images')
    parser_update.add_argument('image', choices=UPDATE_IMAGE_CHOICES)
    parser_update.add_argument('-c', '--checkout', default='HEAD')
    parser_update.add_argument('-n', '--no-cache', action='store_true')
    parser_update.add_argument('--repo',
                               help='git repository for the selected image; fixed build template still applies')
    parser_update.add_argument('--mrtparse-repo', default=ExaBGP_MRTParse.DEFAULT_MRTPARSE_REPO,
                               help='MRTParse git repository when rebuilding exabgp_mrtparse')
    parser_update.set_defaults(func=update)

    def add_gen_conf_args(parser):
        parser.add_argument('-n', '--neighbor-num', default=100, type=int)
        parser.add_argument('-p', '--prefix-num', default=100, type=int)
        parser.add_argument('-l', '--filter-type', choices=['in', 'out'], default='in')
        parser.add_argument('-a', '--as-path-list-num', default=0, type=int)
        parser.add_argument('-e', '--prefix-list-num', default=0, type=int)
        parser.add_argument('-c', '--community-list-num', default=0, type=int)
        parser.add_argument('-x', '--ext-community-list-num', default=0, type=int)
        parser.add_argument('-s', '--single-table', action='store_true')
        parser.add_argument('--target-config-file', type=str,
                            help='target BGP daemon\'s configuration file')
        parser.add_argument('--local-address-prefix', type=str, default='10.10.0.0/16',
                            help='IPv4 prefix used for local addresses; default: 10.10.0.0/16')
        parser.add_argument('--target-local-address', type=str,
                            help='IPv4 address of the target; default: the last address of the '
                                 'local prefix given in --local-address-prefix')
        parser.add_argument('--target-router-id', type=str,
                            help='target\' router ID; default: same as --target-local-address')
        parser.add_argument('--monitor-local-address', type=str,
                            help='IPv4 address of the monitor; default: the second address of the '
                                 'local prefix given in --local-address-prefix')
        parser.add_argument('--monitor-router-id', type=str,
                            help='monitor\' router ID; default: same as --monitor-local-address')

    parser_bench = s.add_parser('bench', help='run benchmarks')
    parser_bench.add_argument('-t', '--target', choices=TARGET_CHOICES, default='gobgp')
    parser_bench.add_argument('-i', '--image', help='specify custom container image')
    parser_bench.add_argument('--podman-network-name', help='Podman network name; this is the name given by `podman network ls`')
    parser_bench.add_argument('--bridge-name', help='Linux bridge name of the '
                              'interface corresponding to the Podman network; '
                              'use this argument only if bgperf can\'t '
                              'determine the Linux bridge name starting from '
                              'the Podman network name in case of tests of '
                              'remote targets.')
    parser_bench.add_argument('-r', '--repeat', action='store_true', help='use existing tester/monitor container')
    parser_bench.add_argument('-f', '--file', metavar='CONFIG_FILE')
    parser_bench.add_argument('--package-only', metavar='PACKAGE',
                              help='write a compressed benchmark package and exit without running')
    parser_bench.add_argument('--from-package', metavar='PACKAGE',
                              help='load a compressed benchmark package and run without regenerating scenario')
    parser_bench.add_argument('--report', metavar='REPORT',
                              help='write a markdown benchmark report; default: report.md under the run directory')
    parser_bench.add_argument('--no-report', action='store_true',
                              help='disable automatic benchmark report generation')
    parser_bench.add_argument('-g', '--cooling', default=0, type=int)
    parser_bench.add_argument('-o', '--output', metavar='STAT_FILE')
    add_gen_conf_args(parser_bench)
    parser_bench.set_defaults(func=bench)

    parser_config = s.add_parser('config', help='generate config')
    parser_config.add_argument('-o', '--output', default='bgperf.yml', type=str)
    add_gen_conf_args(parser_config)
    parser_config.set_defaults(func=config)

    parser_report = s.add_parser('report', help='generate a benchmark report from collected metrics')
    parser_report.add_argument('--run-dir',
                               help='benchmark run directory; default: DIR/BENCH_NAME')
    parser_report.add_argument('-o', '--output',
                               help='report output path; default: report.md under the run directory')
    parser_report.set_defaults(func=report_cmd)

    parser_tui = s.add_parser('tui', help='interactive terminal UI')
    parser_tui.set_defaults(
        func=lambda args: __import__('bgperf_tui').run_tui(args, build_parser, __file__)
    )

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, 'func'):
        parser.print_help()
        return 2

    configure_runtime(args)
    result = args.func(args)
    return 0 if result is None else result


if __name__ == '__main__':
    sys.exit(main())
