# -*- coding: utf-8 -*-
# Copyright (c) 2017 Tigera, Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import etcd
from etcd3gw.client import Etcd3Client
from etcd3gw.utils import _encode

from networking_calico.compat import cfg
from networking_calico.compat import log


# Particular JSON key strings.
CLUSTER_GUID = 'clusterGUID'
CLUSTER_TYPE = 'clusterType'
DATASTORE_READY = 'datastoreReady'
ENDPOINT_REPORTING_ENABLED = 'endpointReportingEnabled'
INTERFACE_PREFIX = 'interfacePrefix'


LOG = log.getLogger(__name__)


def put(resource_kind, name, spec, prev_index=None):
    client = _get_client()
    key = _build_key(resource_kind, name)
    value = {
        'kind': resource_kind,
        'apiVersion': 'projectcalico.org/v3',
        'metadata': {
            'name': name,
        },
        'spec': spec,
    }
    if _is_namespaced(resource_kind):
        value['metadata']['namespace'] = 'openstack'
    value_as_string = json.dumps(value)
    LOG.debug("etcdv3 put key=%s value=%s", key, value)
    if prev_index:
        base64_key = _encode(key)
        base64_value = _encode(value)
        result = client.transaction({
            'compare': [{
                'key': base64_key,
                'result': 'EQUAL',
                'target': 'VERSION',
                'version': prev_index,
            }],
            'success': [{
                'request_put': {
                    'key': base64_key,
                    'value': base64_value,
                },
            }],
            'failure': [],
        })
        LOG.debug("transaction result %s", result)
        succeeded = result.get('succeeded', False)
    else:
        succeeded = client.put(key, value)
    return succeeded


def get(resource_kind, name):
    client = _get_client()
    key = _build_key(resource_kind, name)
    results = client.get(key, metadata=False)
    LOG.debug("etcdv3 get key=%s results=%s", key, results)
    if len(results) != 1:
        raise etcd.EtcdKeyNotFound()
    value = json.loads(results[0])
    return value['spec']


def get_all(resource_kind):
    client = _get_client()
    prefix = _build_key(resource_kind, '')
    results = client.get_prefix(prefix)
    LOG.debug("etcdv3 get_prefix key=%s results=%s", key, len(results))
    tuples = []
    for result in results:
        value, item = result
        value_dict = json.loads(value)
        LOG.debug("value dict: %s", value_dict)
        tuple = (
            value_dict['metadata']['name'],
            value_dict['spec'],
            item['version']
        )
        tuples.append(tuple)
    return tuples


def delete(resource_kind, name):
    client = _get_client()
    key = _build_key(resource_kind, name)
    deleted = client.delete(key)
    LOG.debug("etcdv3 delete key=%s deleted=%s", key, deleted)
    return deleted


# Internals.
_client = None


def _get_client():
    global _client
    if not _client:
        calico_cfg = cfg.CONF.calico
        tls_config_params = [
            calico_cfg.etcd_key_file,
            calico_cfg.etcd_cert_file,
            calico_cfg.etcd_ca_cert_file,
        ]
        if any(tls_config_params):
            LOG.info("TLS to etcd is enabled with key file %s; "
                     "cert file %s; CA cert file %s", *tls_config_params)
            _client = Etcd3Client(host=calico_cfg.etcd_host,
                                  port=calico_cfg.etcd_port,
                                  protocol="https",
                                  ca_cert=calico_cfg.etcd_ca_cert_file,
                                  cert_key=calico_cfg.etcd_key_file,
                                  cert_cert=calico_cfg.etcd_cert_file)
        else:
            LOG.info("TLS disabled, using HTTP to connect to etcd.")
            _client = Etcd3Client(host=calico_cfg.etcd_host,
                                  port=calico_cfg.etcd_port,
                                  protocol="http")
    return _client


def _is_namespaced(resource_kind):
    if resource_kind == "WorkloadEndpoint":
        return True
    return False


def _build_key(resource_kind, name):
    if _is_namespaced(resource_kind):
        # Use 'openstack' as the namespace.
        template = "/calico/resources/v3/projectcalico.org/%ss/openstack/%s"
    else:
        template = "/calico/resources/v3/projectcalico.org/%ss/%s"
    return template % (resource_kind.lower(), name)
