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

from gobgp import GoBGP
import os
import yaml
import json
from threading import Thread
import time


def dict_get_any(values, names, default=None):
    for name in names:
        if name in values:
            return values[name]
    return default


def is_established(neigh):
    state = neigh.get('state', {})
    session_state = dict_get_any(state, ['session-state', 'session_state'])
    if isinstance(session_state, int):
        return session_state == 6
    if session_state is None:
        return False
    return str(session_state).lower().endswith('established')


def accepted_paths(neigh):
    state = neigh.get('state', {})
    adj_table = dict_get_any(state, ['adj-table', 'adj_table'], {})
    if 'accepted' in adj_table:
        return int(adj_table['accepted'])

    accepted = 0
    for afi_safi in dict_get_any(neigh, ['afi-safis', 'afi_safis'], []):
        accepted += int(afi_safi.get('state', {}).get('accepted', 0))
    return accepted


class Monitor(GoBGP):

    CONTAINER_NAME = 'bgperf_monitor'

    def run(self, conf, network_name=''):
        ctn = super(GoBGP, self).run(network_name)
        config = {}
        config['global'] = {
            'config': {
                'as': conf['monitor']['as'],
                'router-id': conf['monitor']['router-id'],
            },
        }
        config ['neighbors'] = [{'config': {'neighbor-address': conf['target']['local-address'],
                                            'peer-as': conf['target']['as']},
                                 'transport': {'config': {'local-address': conf['monitor']['local-address']}},
                                 'timers': {'config': {'connect-retry': 10}}}]
        with open('{0}/{1}'.format(self.host_dir, 'gobgpd.conf'), 'w') as f:
            f.write(yaml.dump(config))
        self.config_name = 'gobgpd.conf'
        startup = '''#!/bin/bash
ulimit -n 65536
gobgpd -t yaml -f {1}/{2} -l {3} > {1}/gobgpd.log 2>&1
'''.format(conf['monitor']['local-address'], self.guest_dir, self.config_name, 'info')
        filename = '{0}/start.sh'.format(self.host_dir)
        with open(filename, 'w') as f:
            f.write(startup)
        os.chmod(filename, 0o777)
        self.local('{0}/start.sh'.format(self.guest_dir), detach=True)
        self.config = conf
        return ctn

    def wait_established(self, neighbor):
        while True:
            neigh = json.loads(self.local('gobgp neighbor {0} -j'.format(neighbor)))
            if is_established(neigh):
                return
            time.sleep(1)

    def stats(self, queue):
        def stats():
            cps = self.config['monitor']['check-points'] if 'check-points' in self.config['monitor'] else []
            while True:
                info = json.loads(self.local('gobgp neighbor -j'))[0]
                info['kind'] = 'bgp'
                info['who'] = self.name
                info.setdefault('state', {})['adj-table'] = {'accepted': accepted_paths(info)}
                state = info['state']
                if len(cps) > 0 and int(cps[0]) == int(state['adj-table']['accepted']):
                    cps.pop(0)
                    info['checked'] = True
                else:
                    info['checked'] = False
                queue.put(info)
                time.sleep(1)

        t = Thread(target=stats)
        t.daemon = True
        t.start()
