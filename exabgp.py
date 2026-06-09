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

from base import *

class ExaBGP(Container):

    DEFAULT_REPO = 'https://github.com/Exa-Networks/exabgp'
    GUEST_DIR = '/root/config'

    def __init__(self, name, host_dir, conf, image='bgperf/exabgp'):
        super(ExaBGP, self).__init__('bgperf_exabgp_' + name, image, host_dir, self.GUEST_DIR, conf)

    @classmethod
    def build_image(cls, force=False, tag='bgperf/exabgp', checkout='HEAD', nocache=False,
                    repo=DEFAULT_REPO):
        if runtime_config.name == 'nspawn':
            from nspawn import nspawn_manager
            nspawn_manager.build_exabgp(tag, force, repo, checkout)
            return

        cls.containerfile = '''
FROM ubuntu:latest
WORKDIR /root
RUN apt-get update && apt-get install -qy git python3 python3-setuptools gcc python3-dev python3-pip
RUN git clone {repo} exabgp && \
(cd exabgp && git checkout {checkout} && python3 -m pip install --break-system-packages six && \
python3 -m pip install --break-system-packages -r requirements.txt && python3 setup.py install)
RUN ln -s /root/exabgp /exabgp
'''.format(repo=shell_quote(repo), checkout=shell_quote(checkout))
        super(ExaBGP, cls).build_image(force, tag, nocache)


class ExaBGP_MRTParse(Container):

    DEFAULT_REPO = ExaBGP.DEFAULT_REPO
    DEFAULT_MRTPARSE_REPO = 'https://github.com/t2mune/mrtparse.git'
    GUEST_DIR = '/root/config'

    def __init__(self, name, host_dir, conf, image='bgperf/exabgp_mrtparse'):
        super(ExaBGP_MRTParse, self).__init__('bgperf_exabgp_mrtparse_' + name, image, host_dir, self.GUEST_DIR, conf)

    @classmethod
    def build_image(cls, force=False, tag='bgperf/exabgp_mrtparse', checkout='HEAD', nocache=False,
                    repo=DEFAULT_REPO, mrtparse_repo=DEFAULT_MRTPARSE_REPO):
        if runtime_config.name == 'nspawn':
            from nspawn import nspawn_manager
            nspawn_manager.build_exabgp(tag, force, repo, checkout, mrtparse_repo=mrtparse_repo)
            return

        cls.containerfile = '''
FROM ubuntu:latest
WORKDIR /root
RUN apt-get update && apt-get install -qy git python3 python3-setuptools gcc python3-dev python3-pip
RUN git clone {repo} exabgp && \
(cd exabgp && git checkout {checkout} && python3 -m pip install --break-system-packages six && \
python3 -m pip install --break-system-packages -r requirements.txt && python3 setup.py install)
RUN ln -s /root/exabgp /exabgp
RUN git clone {mrtparse_repo} mrtparse && \
(cd mrtparse && python3 setup.py install)
'''.format(
            repo=shell_quote(repo),
            checkout=shell_quote(checkout),
            mrtparse_repo=shell_quote(mrtparse_repo),
        )
        super(ExaBGP_MRTParse, cls).build_image(force, tag, nocache)
