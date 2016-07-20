#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import socket

from oslo_config import cfg


# The node hostname is used as the default identity for leader election
_hostname = socket.gethostname()

SHARED_ETCD_OPTS = [
    # etcd connection information.
    cfg.StrOpt('etcd_host', default='127.0.0.1',
               help="The hostname or IP of the etcd node/proxy"),
    cfg.IntOpt('etcd_port', default=4001,
               help="The port to use for the etcd node/proxy"),
    cfg.StrOpt('etcd_protocol', default='http',
               help='The protocol scheme to be used for connections to etcd'),
    # etcd TLS-related options.
    cfg.StrOpt('etcd_key_file', default=None,
               help="The path to the TLS key file to use with etcd."),
    cfg.StrOpt('etcd_cert_file', default=None,
               help="The path to the TLS client certificate file to use with "
                    "etcd."),
    cfg.StrOpt('etcd_ca_cert_file', default=None,
               help="The path to the TLS CA certificate file to use with "
                    "etcd."),
    # Elector configuration.
    cfg.StrOpt('elector_name', default=_hostname,
               help="A unique name to identify this node in leader election"),
]


def register_options(conf):
    conf.register_opts(SHARED_ETCD_OPTS, 'calico')
