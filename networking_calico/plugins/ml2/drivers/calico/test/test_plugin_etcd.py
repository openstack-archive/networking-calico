# -*- coding: utf-8 -*-
# Copyright 2015 Metaswitch Networks
# Copyright (c) 2018 Tigera, Inc. All rights reserved.
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
"""
networking_calico.plugins.ml2.drivers.calico.test.test_plugin_etcd
~~~~~~~~~~~

Unit test for the Calico/OpenStack Plugin using etcd transport.
"""
import copy
import json
import unittest

from etcd3gw.utils import _decode
import eventlet
import logging
import mock

import networking_calico.plugins.ml2.drivers.calico.test.lib as lib

from networking_calico import datamodel_v1
from networking_calico import etcdv3
from networking_calico.monotonic import monotonic_time
from networking_calico.plugins.ml2.drivers.calico import mech_calico
from networking_calico.plugins.ml2.drivers.calico import policy
from networking_calico.plugins.ml2.drivers.calico import status

_log = logging.getLogger(__name__)
logging.getLogger().addHandler(logging.NullHandler())


class _TestEtcdBase(lib.Lib, unittest.TestCase):

    def setUp(self):
        """Setup before each test case."""
        super(_TestEtcdBase, self).setUp()

        # Start with an empty etcd database.
        self.etcd_data = {}

        # Hook the (mock) etcdv2 client.
        lib.m_etcd.Client.reset_mock()
        self.client = lib.m_etcd.Client.return_value
        self.client.read.side_effect = self.etcd_read
        self.client.write.side_effect = self.check_etcd_write
        self.client.delete.side_effect = self.check_etcd_delete

        # Insinuate a mock etcd3gw client.
        etcdv3._client = self.clientv3 = mock.Mock()
        self.clientv3.put.side_effect = self.check_etcd_write
        self.clientv3.transaction.side_effect = self.etcd3_transaction
        self.clientv3.delete.side_effect = self.etcd3_delete
        self.clientv3.get.side_effect = self.etcd3_get
        self.clientv3.get_prefix.side_effect = self.etcd3_get_prefix
        self.clientv3.status.return_value = {
            'header': {'revision': '10', 'cluster_id': '1234abcd'},
        }

        # Start with an empty set of recent writes and deletes.
        self.recent_writes = {}
        self.recent_deletes = set()

        # Reset the counter for when we'll reset the etcd database.
        self.reset_etcd_after = None
        self.assert_etcd_writes_deletes = True

    def maybe_reset_etcd(self):
        if self.reset_etcd_after is not None:
            self.reset_etcd_after -= 1
            if self.reset_etcd_after == 0:
                self.etcd_data = {}
                self.reset_etcd_after = None
                _log.info("etcd reset")
                self.assert_etcd_writes_deletes = False

    def check_etcd_write(self, key, value, **kwargs):
        """check_etcd_write

        Print each etcd write as it occurs, and save into the accumulated etcd
        database.
        """
        self.maybe_reset_etcd()

        # Confirm that, if prevIndex is provided, its value is not None.
        self.assertTrue(kwargs.get('prevIndex', 0) is not None)

        # Check if this key is already in etcd.  If it is, and if it represents
        # a Calico v3 resource, we want to check that its metadata is not
        # changed by the new write.
        existing_v3_metadata = None
        if key in self.etcd_data:
            try:
                existing = json.loads(self.etcd_data[key])
                existing_v3_metadata = existing.get('metadata')
            except ValueError:
                pass

        _log.info("etcd write: %s = %s", key, value)
        self.etcd_data[key] = value
        try:
            self.recent_writes[key] = json.loads(value)
        except ValueError:
            self.recent_writes[key] = value

        if 'metadata' in self.recent_writes[key]:
            # If this is an update, check that the metadata other than labels
            # is unchanged.
            if existing_v3_metadata:
                if 'labels' in self.recent_writes[key]['metadata']:
                    existing_v3_metadata['labels'] = \
                        self.recent_writes[key]['metadata']['labels']
                self.assertEqual(existing_v3_metadata,
                                 self.recent_writes[key]['metadata'])
            # Now delete not-easily-predictable metadata fields from the data
            # that test code checks against.
            if 'creationTimestamp' in self.recent_writes[key]['metadata']:
                del self.recent_writes[key]['metadata']['creationTimestamp']
            if 'uid' in self.recent_writes[key]['metadata']:
                del self.recent_writes[key]['metadata']['uid']

        return True

    def check_etcd_delete(self, key, **kwargs):
        """Print each etcd delete as it occurs."""
        self.maybe_reset_etcd()
        _log.info("etcd delete: %s", key)
        if kwargs.get('recursive', False):
            keylen = len(key) + 1
            for k in self.etcd_data.keys():
                if k == key or k[:keylen] == key + '/':
                    del self.etcd_data[k]
            self.recent_deletes.add(key + '(recursive)')
        else:
            try:
                del self.etcd_data[key]
            except KeyError:
                raise lib.EtcdKeyNotFound()
            self.recent_deletes.add(key)

    def assertEtcdWrites(self, expected):
        if self.assert_etcd_writes_deletes:
            self.assertEqual(expected, self.recent_writes)
        self.recent_writes = {}

    def assertEtcdDeletes(self, expected):
        if self.assert_etcd_writes_deletes:
            self.assertEqual(expected, self.recent_deletes)
        self.recent_deletes = set()

    def etcd3_get(self, key, metadata=False):
        self.maybe_reset_etcd()
        if key in self.etcd_data:
            value = self.etcd_data[key]

            # Print and return the result.
            _log.info("etcd3 get: %s; value: %s", key, value)
            if metadata:
                item = {'key': key, 'mod_revision': '10'}
                return [(value, item)]
            else:
                return [value]
        else:
            return []

    def etcd3_get_prefix(self, prefix):
        self.maybe_reset_etcd()
        results = []
        for key, value in self.etcd_data.items():
            if key.startswith(prefix):
                result = (value, {'mod_revision': 0, 'key': key})
                results.append(result)

        # Print and return the result.
        _log.info("etcd3 get_prefix: %s; results: %s", prefix, results)
        return results

    def etcd3_delete(self, key, **kwargs):
        try:
            self.check_etcd_delete(key, **kwargs)
            return True
        except lib.EtcdKeyNotFound:
            return False

    def etcd3_transaction(self, txn):
        if 'request_put' in txn['success'][0]:
            put_request = txn['success'][0]['request_put']
            succeeded = self.check_etcd_write(_decode(put_request['key']),
                                              _decode(put_request['value']))
        elif 'request_delete_range' in txn['success'][0]:
            del_request = txn['success'][0]['request_delete_range']
            succeeded = self.etcd3_delete(_decode(del_request['key']))
        return {'succeeded': succeeded}

    def etcd_read(self, key, wait=False, waitIndex=None, recursive=False,
                  timeout=None):
        """Read from the accumulated etcd database."""
        self.maybe_reset_etcd()

        # Slow down reading from etcd status subtree to allow threads to run
        # more often
        if wait and key == datamodel_v1.FELIX_STATUS_DIR:
            eventlet.sleep(30)
            self.driver.db.create_or_update_agent = mock.Mock()

        self.etcd_data[datamodel_v1.FELIX_STATUS_DIR + "/vm1/status"] = \
            json.dumps({
                "time": "2015-08-14T10:37:54"
            })

        # Prepare a read result object.
        read_result = mock.Mock()
        read_result.modifiedIndex = 123
        read_result.key = key
        read_result.etcd_index = 0

        # Set the object's value - i.e. the value, if any, of exactly the
        # specified key.
        if key in self.etcd_data:
            read_result.value = self.etcd_data[key]
        else:
            read_result.value = None
            if not recursive:
                raise lib.m_etcd.EtcdKeyNotFound()

        # Print and return the result object.
        _log.info("etcd read: %s; value: %s", key, read_result.value)

        if recursive:
            # Also see if this key has any children, and read those.
            read_result.children = []
            read_result.leaves = []
            keylen = len(key) + 1
            for k in self.etcd_data.keys():
                if k[:keylen] == key + '/':
                    child = mock.Mock()
                    child.key = k
                    child.value = self.etcd_data[k]
                    read_result.children.append(child)
                    read_result.leaves.append(child)
            if read_result.value is None and read_result.children == []:
                raise lib.m_etcd.EtcdKeyNotFound(self.etcd_data)
            # Actual direct children of the dir in etcd response.
            # Needed for status_dir, where children are dirs and
            # needs to be iterated.
            read_result._children = []
            list_of_statuses = [{"key": K} for K in self.etcd_data.keys()]
            read_result._children.append({"nodes": list_of_statuses})
        else:
            read_result.children = None

        return read_result


class TestPluginEtcd(_TestEtcdBase):
    # Tests for the Calico mechanism driver.  This covers the mainline function
    # and the periodic resync thread.

    def setUp(self):
        """Setup before each test case."""
        # Do common plugin test setup.
        lib.m_compat.cfg.CONF.core_plugin = 'ml2'
        super(TestPluginEtcd, self).setUp()

        # Mock out the status updating thread.  These tests were originally
        # written before that was added and they do not support the interleaved
        # requests from the status thread.  The status-reporting thread is
        # tested separately.
        self.driver._status_updating_thread = mock.Mock(
            spec=self.driver._status_updating_thread
        )

        # Mock out config.
        lib.m_compat.cfg.CONF.calico.etcd_host = "localhost"
        lib.m_compat.cfg.CONF.calico.etcd_port = 4001
        lib.m_compat.cfg.CONF.calico.etcd_cert_file = None
        lib.m_compat.cfg.CONF.calico.etcd_ca_cert_file = None
        lib.m_compat.cfg.CONF.calico.etcd_key_file = None
        lib.m_compat.cfg.CONF.calico.num_port_status_threads = 4

    sg_default_key_v3 = (
        '/calico/resources/v3/projectcalico.org/networkpolicies/' +
        'openstack/ossg.default.SGID-default')
    sg_default_value_v3 = {
        'apiVersion': 'projectcalico.org/v3',
        'kind': 'NetworkPolicy',
        'metadata': {
            'namespace': 'openstack',
            'name': 'ossg.default.SGID-default'
        },
        'spec': {
            'egress': [
                {'action': 'Allow',
                 'ipVersion': 4},
                {'action': 'Allow',
                 'ipVersion': 6}],
            'ingress': [
                {'action': 'Allow',
                 'ipVersion': 4,
                 'source': {
                     'selector':
                     'has(sg.projectcalico.org/openstack-SGID-default)'}},
                {'action': 'Allow',
                 'ipVersion': 6,
                 'source': {
                     'selector':
                     'has(sg.projectcalico.org/openstack-SGID-default)'}}
            ],
            'selector': 'has(sg.projectcalico.org/openstack-SGID-default)'}}

    initial_etcd3_writes = {
        '/calico/resources/v3/projectcalico.org/clusterinformations/default': {
            'apiVersion': 'projectcalico.org/v3',
            'kind': 'ClusterInformation',
            'metadata': {'name': 'default'},
            'spec': {'clusterGUID': 'uuid-start-no-ports',
                     'clusterType': 'openstack',
                     'datastoreReady': True}},
        '/calico/resources/v3/projectcalico.org/felixconfigurations/default': {
            'apiVersion': 'projectcalico.org/v3',
            'kind': 'FelixConfiguration',
            'metadata': {'name': 'default'},
            'spec': {'endpointReportingEnabled': True,
                     'interfacePrefix': 'tap'}},
        sg_default_key_v3: sg_default_value_v3
    }

    def test_start_no_ports(self):
        """Startup with no ports or existing etcd data."""
        # Allow the etcd transport's resync thread to run. The last thing it
        # does is write the Felix config, so let it run three reads.

        with lib.FixedUUID('uuid-start-no-ports'):
            self.give_way()
            self.simulated_time_advance(31)

        self.assertEtcdWrites(self.initial_etcd3_writes)

    def test_etcd_reset(self):
        for n in range(1, 20):
            _log.info("Reset etcd data after %s reads/writes/deletes", n)
            self.reset_etcd_after = n
            self.test_start_two_ports()
            self.etcd_data = {}

    def test_start_two_ports(self):
        """Startup with two existing ports but no existing etcd data."""
        # Provide two Neutron ports.
        self.osdb_ports = [lib.port1, lib.port2]

        # Allow the etcd transport's resync thread to run.
        with lib.FixedUUID('uuid-start-two-ports'):
            self.give_way()
            self.simulated_time_advance(31)

        ep_deadbeef_key_v3 = (
            '/calico/resources/v3/projectcalico.org/workloadendpoints/' +
            'openstack/felix--host--1-openstack-' +
            'instance--1-DEADBEEF--1234--5678')
        ep_facebeef_key_v3 = (
            '/calico/resources/v3/projectcalico.org/workloadendpoints/' +
            'openstack/felix--host--1-openstack-' +
            'instance--2-FACEBEEF--1234--5678')
        ep_deadbeef_value_v3 = {
            'apiVersion': 'projectcalico.org/v3',
            'kind': 'WorkloadEndpoint',
            'metadata': {
                'annotations': {
                    'openstack.projectcalico.org/network-id':
                    'calico-network-id'
                },
                'name': ('felix--host--1-openstack-instance' +
                         '--1-DEADBEEF--1234--5678'),
                'namespace': 'openstack',
                'labels': {
                    'sg.projectcalico.org/openstack-SGID-default': '',
                    'projectcalico.org/namespace': 'openstack',
                    'projectcalico.org/orchestrator': 'openstack'
                }
            },
            'spec': {'endpoint': 'DEADBEEF-1234-5678',
                     'interfaceName': 'tapDEADBEEF-12',
                     'ipNATs': [{'externalIP': '192.168.0.1',
                                 'internalIP': '10.65.0.2'}],
                     'ipNetworks': ['10.65.0.2/32'],
                     'ipv4Gateway': '10.65.0.1',
                     'mac': '00:11:22:33:44:55',
                     'node': 'felix-host-1',
                     'orchestrator': 'openstack',
                     'workload': 'instance-1'}}
        ep_facebeef_value_v3 = {
            'apiVersion': 'projectcalico.org/v3',
            'kind': 'WorkloadEndpoint',
            'metadata': {
                'annotations': {
                    'openstack.projectcalico.org/network-id':
                    'calico-network-id'
                },
                'name': ('felix--host--1-openstack-instance' +
                         '--2-FACEBEEF--1234--5678'),
                'namespace': 'openstack',
                'labels': {
                    'sg.projectcalico.org/openstack-SGID-default': '',
                    'projectcalico.org/namespace': 'openstack',
                    'projectcalico.org/orchestrator': 'openstack'
                }
            },
            'spec': {'endpoint': 'FACEBEEF-1234-5678',
                     'interfaceName': 'tapFACEBEEF-12',
                     'ipNetworks': ['10.65.0.3/32'],
                     'ipv4Gateway': '10.65.0.1',
                     'mac': '00:11:22:33:44:66',
                     'node': 'felix-host-1',
                     'orchestrator': 'openstack',
                     'workload': 'instance-2'}}

        expected_writes = copy.deepcopy(self.initial_etcd3_writes)
        expected_writes[
            '/calico/resources/v3/projectcalico.org/clusterinformations/' +
            'default']['spec']['clusterGUID'] = 'uuid-start-two-ports'
        expected_writes.update({
            ep_deadbeef_key_v3: ep_deadbeef_value_v3,
            ep_facebeef_key_v3: ep_facebeef_value_v3,
        })
        self.assertEtcdWrites(expected_writes)

        # Allow it to run again, this time auditing against the etcd data that
        # was written on the first iteration.
        _log.info("Resync with existing etcd data")
        self.simulated_time_advance(mech_calico.RESYNC_INTERVAL_SECS)
        self.assertEtcdWrites({})
        self.assertEtcdDeletes(set())

        # Delete lib.port1.
        context = mock.MagicMock()
        context._port = lib.port1
        context._plugin_context.session.query.return_value.filter_by.\
            side_effect = self.port_query
        self.driver.delete_port_postcommit(context)
        self.assertEtcdWrites({})
        self.assertEtcdDeletes(set([ep_deadbeef_key_v3]))
        self.osdb_ports = [lib.port2]

        # Do another resync - expect no changes to the etcd data.
        _log.info("Resync with existing etcd data")
        self.simulated_time_advance(mech_calico.RESYNC_INTERVAL_SECS)
        self.assertEtcdWrites({})
        self.assertEtcdDeletes(set())

        # Add lib.port1 back again.
        self.osdb_ports = [lib.port1, lib.port2]
        self.driver.create_port_postcommit(context)
        self.assertEtcdWrites({
            ep_deadbeef_key_v3: ep_deadbeef_value_v3,
            self.sg_default_key_v3: self.sg_default_value_v3,
        })
        self.assertEtcdDeletes(set())

        # Migrate port1 to a different host.
        context._port = lib.port1.copy()
        context.original = lib.port1.copy()
        context._port['binding:host_id'] = 'new-host'
        self.osdb_ports[0]['binding:host_id'] = 'new-host'
        self.driver.update_port_postcommit(context)

        self.assertEtcdDeletes(set([ep_deadbeef_key_v3]))
        ep_deadbeef_key_v3 = ep_deadbeef_key_v3.replace('felix--host--1',
                                                        'new--host')
        ep_deadbeef_value_v3['metadata']['name'] = \
            ep_deadbeef_value_v3['metadata']['name'].replace('felix--host--1',
                                                             'new--host')
        ep_deadbeef_value_v3['spec']['node'] = \
            ep_deadbeef_value_v3['spec']['node'].replace('felix-host-1',
                                                         'new-host')
        self.assertEtcdWrites({
            ep_deadbeef_key_v3: ep_deadbeef_value_v3,
            self.sg_default_key_v3: self.sg_default_value_v3,
        })

        # Now resync again, moving self.osdb_ports to move port 1 back to the
        # old host felix-host-1.  The effect will be as though we've
        # missed a further update that moved port1 back to felix-host-1; this
        # resync will now discover that.
        _log.info("Resync with existing etcd data")
        self.osdb_ports[0]['binding:host_id'] = 'felix-host-1'
        self.simulated_time_advance(mech_calico.RESYNC_INTERVAL_SECS)

        self.assertEtcdDeletes(set([ep_deadbeef_key_v3]))
        ep_deadbeef_key_v3 = ep_deadbeef_key_v3.replace('new--host',
                                                        'felix--host--1')
        ep_deadbeef_value_v3['metadata']['name'] = \
            ep_deadbeef_value_v3['metadata']['name'].replace('new--host',
                                                             'felix--host--1')
        ep_deadbeef_value_v3['spec']['node'] = \
            ep_deadbeef_value_v3['spec']['node'].replace('new-host',
                                                         'felix-host-1')
        self.assertEtcdWrites({
            ep_deadbeef_key_v3: ep_deadbeef_value_v3,
        })

        # Add another port with an IPv6 address.
        context._port = copy.deepcopy(lib.port3)
        self.osdb_ports.append(context._port)
        self.driver.create_port_postcommit(context)

        ep_hello_key_v3 = (
            '/calico/resources/v3/projectcalico.org/workloadendpoints/' +
            'openstack/felix--host--2-openstack-' +
            'instance--3-HELLO--1234--5678')
        ep_hello_value_v3 = {
            'apiVersion': 'projectcalico.org/v3',
            'kind': 'WorkloadEndpoint',
            'metadata': {
                'annotations': {
                    'openstack.projectcalico.org/network-id':
                    'calico-network-id'
                },
                'name': ('felix--host--2-openstack-instance' +
                         '--3-HELLO--1234--5678'),
                'namespace': 'openstack',
                'labels': {
                    'sg.projectcalico.org/openstack-SGID-default': '',
                    'projectcalico.org/namespace': 'openstack',
                    'projectcalico.org/orchestrator': 'openstack'
                }
            },
            'spec': {'endpoint': 'HELLO-1234-5678',
                     'interfaceName': 'tapHELLO-1234-',
                     'ipNetworks': ['2001:db8:a41:2::12/128'],
                     'ipv6Gateway': '2001:db8:a41:2::1',
                     'mac': '00:11:22:33:44:66',
                     'node': 'felix-host-2',
                     'orchestrator': 'openstack',
                     'workload': 'instance-3'}}

        expected_writes = {
            ep_hello_key_v3: ep_hello_value_v3,
            self.sg_default_key_v3: self.sg_default_value_v3,
        }
        self.assertEtcdWrites(expected_writes)
        self.assertEtcdDeletes(set())
        self.osdb_ports = [lib.port1, lib.port2, context._port]

        # Create a new security group.
        # Update what the DB's queries should now return.
        self.db.get_security_groups.return_value = [
            {'id': 'SGID-default',
             'security_group_rules': [
                 {'remote_group_id': 'SGID-default',
                  'remote_ip_prefix': None,
                  'protocol': -1,
                  'direction': 'ingress',
                  'ethertype': 'IPv4',
                  'port_range_min': -1},
                 {'remote_group_id': 'SGID-default',
                  'remote_ip_prefix': None,
                  'protocol': -1,
                  'direction': 'ingress',
                  'ethertype': 'IPv6',
                  'port_range_min': -1},
                 {'remote_group_id': None,
                  'remote_ip_prefix': None,
                  'protocol': -1,
                  'direction': 'egress',
                  'ethertype': 'IPv4',
                  'port_range_min': -1},
                 {'remote_group_id': None,
                  'remote_ip_prefix': None,
                  'protocol': -1,
                  'direction': 'egress',
                  'ethertype': 'IPv6',
                  'port_range_min': -1}
             ]},
            {'id': 'SG-1',
             'security_group_rules': [
                 {'remote_group_id': 'SGID-default',
                  'remote_ip_prefix': None,
                  'protocol': -1,
                  'direction': 'ingress',
                  'ethertype': 'IPv4',
                  'port_range_min': 5060,
                  'port_range_max': 5061}
             ]}
        ]
        self.db.get_security_group_rules.return_value = [
            {'remote_group_id': 'SGID-default',
             'remote_ip_prefix': None,
             'protocol': -1,
             'direction': 'ingress',
             'ethertype': 'IPv4',
             'security_group_id': 'SGID-default',
             'port_range_min': -1},
            {'remote_group_id': 'SGID-default',
             'remote_ip_prefix': None,
             'protocol': -1,
             'direction': 'ingress',
             'ethertype': 'IPv6',
             'security_group_id': 'SGID-default',
             'port_range_min': -1},
            {'remote_group_id': None,
             'remote_ip_prefix': None,
             'protocol': -1,
             'direction': 'egress',
             'ethertype': 'IPv4',
             'security_group_id': 'SGID-default',
             'port_range_min': -1},
            {'remote_group_id': None,
             'remote_ip_prefix': None,
             'protocol': -1,
             'direction': 'egress',
             'security_group_id': 'SGID-default',
             'ethertype': 'IPv6',
             'port_range_min': -1},
            {'remote_group_id': 'SGID-default',
             'remote_ip_prefix': None,
             'protocol': -1,
             'direction': 'ingress',
             'ethertype': 'IPv4',
             'security_group_id': 'SG-1',
             'port_range_min': 5060,
             'port_range_max': 5061}
        ]

        # Then, send in an update.
        self.notify_security_group_update(
            'SG-1',
            [
                {'remote_group_id': 'SGID-default',
                 'remote_ip_prefix': None,
                 'protocol': -1,
                 'direction': 'ingress',
                 'ethertype': 'IPv4',
                 'port_range_min': 5060,
                 'port_range_max': 5061}
            ],
            None,
            'rule'
        )

        sg_1_key_v3 = (
            '/calico/resources/v3/projectcalico.org/networkpolicies/' +
            'openstack/ossg.default.SG-1')
        sg_1_value_v3 = {
            'apiVersion': 'projectcalico.org/v3',
            'kind': 'NetworkPolicy',
            'metadata': {
                'namespace': 'openstack',
                'name': 'ossg.default.SG-1'
            },
            'spec': {
                'egress': [],
                'ingress': [{
                    'action': 'Allow',
                    'destination': {'ports': ['5060:5061']},
                    'ipVersion': 4,
                    'source': {
                        'selector':
                        'has(sg.projectcalico.org/openstack-SGID-default)'}
                }],
                'selector': 'has(sg.projectcalico.org/openstack-SG-1)'}}

        self.assertEtcdWrites({sg_1_key_v3: sg_1_value_v3})

        # Now change the security group for that port.
        context.original = copy.deepcopy(context._port)
        context.original['security_groups'] = ['SGID-default']
        context._port['security_groups'] = ['SG-1']
        self.port_security_group_bindings.pop(2)
        self.port_security_group_bindings.append({
            'port_id': 'HELLO-1234-5678', 'security_group_id': 'SG-1'
        })
        self.driver.update_port_postcommit(context)

        del ep_hello_value_v3['metadata']['labels'][
            'sg.projectcalico.org/openstack-SGID-default'
        ]
        ep_hello_value_v3['metadata']['labels'][
            'sg.projectcalico.org/openstack-SG-1'] = ''
        expected_writes = {
            ep_hello_key_v3: ep_hello_value_v3,
            sg_1_key_v3: sg_1_value_v3
        }
        self.assertEtcdWrites(expected_writes)

        # Resync with all latest data - expect no etcd writes or deletes.
        _log.info("Resync with existing etcd data")
        self.simulated_time_advance(mech_calico.RESYNC_INTERVAL_SECS)
        self.assertEtcdWrites({})
        self.assertEtcdDeletes(set([]))

        # Change SG-1 to allow only port 5060.
        self.db.get_security_groups.return_value[-1] = {
            'id': 'SG-1',
            'security_group_rules': [
                {'remote_group_id': 'SGID-default',
                 'remote_ip_prefix': None,
                 'protocol': -1,
                 'direction': 'ingress',
                 'ethertype': 'IPv4',
                 'port_range_min': 5060,
                 'port_range_max': 5061}]
        }
        self.db.get_security_group_rules.return_value[-1] = {
            'remote_group_id': 'SGID-default',
            'remote_ip_prefix': None,
            'protocol': -1,
            'direction': 'ingress',
            'ethertype': 'IPv4',
            'security_group_id': 'SG-1',
            'port_range_min': 5060,
            'port_range_max': 5060
        }
        self.notify_security_group_update(
            'SG-1',
            [
                {'remote_group_id': 'SGID-default',
                 'remote_ip_prefix': None,
                 'protocol': -1,
                 'direction': 'ingress',
                 'ethertype': 'IPv4',
                 'port_range_min': 5060,
                 'port_range_max': 5060}
            ],
            None,
            'rule'
        )

        # Expect an etcd write
        sg_1_value_v3['spec']['ingress'][0]['destination']['ports'] = [5060]
        self.assertEtcdWrites({
            sg_1_key_v3: sg_1_value_v3
        })

        # Resync with only the last port.  Expect the first two ports to be
        # cleaned up.
        self.osdb_ports = [context.original]
        _log.info("Resync with existing etcd data")
        self.simulated_time_advance(mech_calico.RESYNC_INTERVAL_SECS)
        self.assertEtcdWrites({})
        self.assertEtcdDeletes(set([
            ep_deadbeef_key_v3,
            ep_facebeef_key_v3,
        ]))

        # Change a small amount of information about the port and the security
        # group. Expect a resync to fix it up.
        self.db.get_security_groups.return_value[-1] = {
            'id': 'SG-1',
            'security_group_rules': [
                {'remote_group_id': 'SGID-default',
                 'remote_ip_prefix': None,
                 'protocol': -1,
                 'direction': 'ingress',
                 'ethertype': 'IPv4',
                 'port_range_min': 5070,
                 'port_range_max': 5071}]
        }
        self.db.get_security_group_rules.return_value[-1] = {
            'remote_group_id': 'SGID-default',
            'remote_ip_prefix': None,
            'protocol': -1,
            'direction': 'ingress',
            'ethertype': 'IPv4',
            'security_group_id': 'SG-1',
            'port_range_min': 5070,
            'port_range_max': 5070
        }
        old_ips = self.osdb_ports[0]['fixed_ips']
        self.osdb_ports[0]['fixed_ips'] = [
            {'subnet_id': 'subnet-id-10.65.0--24',
             'ip_address': '10.65.0.188'}
        ]
        _log.info("Resync with edited data")
        self.simulated_time_advance(mech_calico.RESYNC_INTERVAL_SECS)

        ep_hello_value_v3['spec']['ipNetworks'] = ["10.65.0.188/32"]
        ep_hello_value_v3['spec']['ipv4Gateway'] = "10.65.0.1"
        del ep_hello_value_v3['spec']['ipv6Gateway']
        sg_1_value_v3['spec']['ingress'][0]['destination']['ports'] = [5070]
        expected_writes = {
            ep_hello_key_v3: ep_hello_value_v3,
            sg_1_key_v3: sg_1_value_v3,
        }
        self.assertEtcdWrites(expected_writes)
        self.assertEtcdDeletes(set())

        # Reset the state for safety.
        self.osdb_ports[0]['fixed_ips'] = old_ips

        self.db.get_security_groups.return_value[-1] = {
            'id': 'SG-1',
            'security_group_rules': [
                {'remote_group_id': 'SGID-default',
                 'remote_ip_prefix': None,
                 'protocol': -1,
                 'direction': 'ingress',
                 'ethertype': 'IPv4',
                 'port_range_min': 5060,
                 'port_range_max': 5061}]
        }
        self.db.get_security_group_rules.return_value[-1] = {
            'remote_group_id': 'SGID-default',
            'remote_ip_prefix': None,
            'protocol': -1,
            'direction': 'ingress',
            'ethertype': 'IPv4',
            'security_group_id': 'SG-1',
            'port_range_min': 5060,
            'port_range_max': 5060
        }

    def test_noop_entry_points(self):
        """test_noop_entry_points

        Call the mechanism driver entry points that are currently
        implemented as no-ops (because Calico function does not need
        them).
        """
        self.driver.update_network_postcommit(None)
        self.driver.delete_network_postcommit(None)
        self.driver.create_network_postcommit(None)
        self.driver.update_network_postcommit(None)

    def test_subnet_hooks(self):
        """Test subnet creation, update and deletion hooks."""

        # Allow the etcd transport's resync thread to run.  Expect the usual
        # writes.
        with lib.FixedUUID('uuid-subnet-hooks'):
            self.give_way()
            self.simulated_time_advance(31)

        expected_writes = copy.deepcopy(self.initial_etcd3_writes)
        expected_writes[
            '/calico/resources/v3/projectcalico.org/clusterinformations/' +
            'default']['spec']['clusterGUID'] = 'uuid-subnet-hooks'
        self.assertEtcdWrites(expected_writes)

        # Define two subnets.
        subnet1 = {'network_id': 'net-id-1',
                   'enable_dhcp': True,
                   'id': 'subnet-id-10.65.0--24',
                   'cidr': '10.65.0/24',
                   'gateway_ip': '10.65.0.1',
                   'host_routes': [{'destination': '11.11.0.0/16',
                                    'nexthop': '10.65.0.1'}],
                   'dns_nameservers': []}
        subnet2 = {'network_id': 'net-id-2',
                   'enable_dhcp': False,
                   'id': 'subnet-id-10.28.0--24',
                   'cidr': '10.28.0/24',
                   'gateway_ip': '10.28.0.1',
                   'host_routes': [],
                   'dns_nameservers': ['172.18.10.55']}
        self.osdb_subnets = [subnet1, subnet2]

        # Notify creation of subnet1, and expect corresponding etcd write.
        context = mock.MagicMock()
        context.current = subnet1
        self.driver.create_subnet_postcommit(context)
        self.assertEtcdWrites({
            '/calico/dhcp/v1/subnet/subnet-id-10.65.0--24': {
                'network_id': 'net-id-1',
                'cidr': '10.65.0.0/24',
                'gateway_ip': '10.65.0.1',
                'host_routes': [{'destination': '11.11.0.0/16',
                                 'nexthop': '10.65.0.1'}]
            }
        })

        # Notify creation of subnet2, and expect no etcd write as this subnet
        # is not DHCP-enabled.
        context.current = subnet2
        self.driver.create_subnet_postcommit(context)
        self.assertEtcdWrites({})

        # Allow the etcd transport's resync thread to run again.  Expect no
        # change in etcd subnet data.
        self.give_way()
        self.simulated_time_advance(mech_calico.RESYNC_INTERVAL_SECS)
        self.assertEtcdWrites({})
        self.assertEtcdDeletes(set())

        # Update subnet1 so as not to be DHCP-enabled.
        subnet1['enable_dhcp'] = False
        context.current = subnet1
        self.driver.update_subnet_postcommit(context)
        self.assertEtcdDeletes(set([
            '/calico/dhcp/v1/subnet/subnet-id-10.65.0--24'
        ]))

        # Update subnet2 to be DHCP-enabled.
        subnet2['enable_dhcp'] = True
        context.current = subnet2
        self.driver.update_subnet_postcommit(context)
        self.assertEtcdWrites({
            '/calico/dhcp/v1/subnet/subnet-id-10.28.0--24': {
                'network_id': 'net-id-2',
                'cidr': '10.28.0.0/24',
                'gateway_ip': '10.28.0.1',
                'host_routes': [],
                'dns_servers': ['172.18.10.55']
            }
        })

        # Do a resync where we simulate the etcd data having been lost.
        with lib.FixedUUID('uuid-subnet-hooks-2'):
            self.give_way()
            self.etcd_data = {}
            self.simulated_time_advance(mech_calico.RESYNC_INTERVAL_SECS)

        expected_writes[
            '/calico/resources/v3/projectcalico.org/clusterinformations/' +
            'default']['spec']['clusterGUID'] = 'uuid-subnet-hooks-2'
        expected_writes.update({
            '/calico/dhcp/v1/subnet/subnet-id-10.28.0--24': {
                'network_id': 'net-id-2',
                'cidr': '10.28.0.0/24',
                'gateway_ip': '10.28.0.1',
                'host_routes': [],
                'dns_servers': ['172.18.10.55']},
        })
        self.assertEtcdWrites(expected_writes)
        self.assertEtcdDeletes(set())

        # Do a resync where we simulate having missed a dynamic update that
        # changed which subnets where DHCP-enabled.
        subnet1['enable_dhcp'] = True
        subnet2['enable_dhcp'] = False
        self.give_way()
        self.simulated_time_advance(mech_calico.RESYNC_INTERVAL_SECS)
        self.assertEtcdWrites({
            '/calico/dhcp/v1/subnet/subnet-id-10.65.0--24': {
                'network_id': 'net-id-1',
                'cidr': '10.65.0.0/24',
                'gateway_ip': '10.65.0.1',
                'host_routes': [{'destination': '11.11.0.0/16',
                                 'nexthop': '10.65.0.1'}]}
        })
        self.assertEtcdDeletes(set([
            '/calico/dhcp/v1/subnet/subnet-id-10.28.0--24'
        ]))

        # Do a resync where we simulate having missed a dynamic update that
        # changed a Calico-relevant property of a DHCP-enabled subnet.
        subnet1['gateway_ip'] = '10.65.0.2'
        self.give_way()
        self.simulated_time_advance(mech_calico.RESYNC_INTERVAL_SECS)
        self.assertEtcdWrites({
            '/calico/dhcp/v1/subnet/subnet-id-10.65.0--24': {
                'network_id': 'net-id-1',
                'cidr': '10.65.0.0/24',
                'gateway_ip': '10.65.0.2',
                'host_routes': [{'destination': '11.11.0.0/16',
                                 'nexthop': '10.65.0.1'}]}
        })
        self.assertEtcdDeletes(set())

        # Delete subnet2.  No etcd effect because it was already deleted from
        # etcd above.
        context.current = subnet2
        self.driver.delete_subnet_postcommit(context)
        self.assertEtcdDeletes(set())

        # Delete subnet1.
        context.current = subnet1
        self.driver.delete_subnet_postcommit(context)
        self.assertEtcdDeletes(set([
            '/calico/dhcp/v1/subnet/subnet-id-10.65.0--24'
        ]))

    def test_check_segment_for_agent(self):
        """Test the mechanism driver's check_segment_for_agent entry point."""
        # Simulate ML2 asking the driver if it can handle a port.
        self.assertTrue(self.driver.check_segment_for_agent(
            {mech_calico.api.NETWORK_TYPE: 'flat',
             mech_calico.api.ID: 'shiny'},
            mech_calico.constants.AGENT_TYPE_DHCP
        ))

        # Simulate ML2 asking the driver if it can handle a port that
        # it can't handle.
        self.assertFalse(self.driver.check_segment_for_agent(
            {mech_calico.api.NETWORK_TYPE: 'vlan',
             mech_calico.api.ID: 'not-shiny'},
            mech_calico.constants.AGENT_TYPE_DHCP
        ))

    def test_neutron_rule_to_etcd_rule_icmp(self):
        # No type/code specified
        self.assertNeutronToEtcd(_neutron_rule_from_dict({
            "ethertype": "IPv4",
            "protocol": "icmp",
        }), {'action': 'Allow', 'ipVersion': 4, 'protocol': 'ICMP'})
        # Type/code wildcarded, same as above.
        self.assertNeutronToEtcd(_neutron_rule_from_dict({
            "ethertype": "IPv4",
            "protocol": "icmp",
            "port_range_min": -1,
            "port_range_max": -1,
        }), {'action': 'Allow', 'ipVersion': 4, 'protocol': 'ICMP'})
        # Type and code.
        self.assertNeutronToEtcd(_neutron_rule_from_dict({
            "ethertype": "IPv4",
            "protocol": "icmp",
            "port_range_min": 123,
            "port_range_max": 100,
        }), {'icmp': {'code': 100, 'type': 123},
             'protocol': 'ICMP',
             'ipVersion': 4,
             'action': 'Allow'})
        # Numeric type.
        self.assertNeutronToEtcd(_neutron_rule_from_dict({
            "ethertype": "IPv4",
            "protocol": 123,
            "direction": "egress",
            "remote_group_id": "foobar",
        }), {
            'ipVersion': 4,
            'protocol': 123,
            'destination': {'selector':
                            'has(sg.projectcalico.org/openstack-foobar)'},
            'action': 'Allow',
        })
        # Type and code, IPv6.
        self.assertNeutronToEtcd(_neutron_rule_from_dict({
            "ethertype": "IPv6",
            "protocol": "icmp",
            "port_range_min": 123,
            "port_range_max": 100,
        }), {
            'ipVersion': 6,
            'protocol': 'ICMPv6',
            'icmp': {'type': 123, 'code': 100},
            'action': 'Allow',
        })

    def test_not_master_does_not_resync(self):
        """Test that a driver that is not master does not resync."""
        # Initialize the state early to put the elector in place, then override
        # it to claim that the driver is not master.
        self.driver._post_fork_init()

        with mock.patch.object(self.driver, "elector") as m_elector:
            m_elector.master.return_value = False

            # Allow the etcd transport's resync thread to run. Nothing will
            # happen.
            self.give_way()
            self.simulated_time_advance(31)
            self.assertEtcdWrites({})

    def assertNeutronToEtcd(self, neutron_rule, exp_etcd_rule):
        etcd_rule = policy._neutron_rule_to_etcd_rule(neutron_rule)
        self.assertEqual(exp_etcd_rule, etcd_rule)

    def test_profile_prefixing(self):
        """Startup with existing profile data from another orchestrator."""

        # Check that we don't delete the other orchestrator's profile data.
        self.etcd_data = {
            '/calico/resources/v3/projectcalico.org/networkpolicies/' +
            'mesos/profile-1': json.dumps({
                'apiVersion': 'projectcalico.org/v3',
                'kind': 'NetworkPolicy',
                'metadata': {'name': 'mesos-profile-1'},
                'spec': {
                    'egress': [
                        {'action': 'Allow',
                         'ipVersion': 4},
                        {'action': 'Allow',
                         'ipVersion': 6}],
                    'ingress': [
                        {'action': 'Allow',
                         'ipVersion': 4,
                         'source': {
                             'selector':
                             'has(sg.projectcalico.org/openstack-SGID-default)'
                         }},
                        {'action': 'Allow',
                         'ipVersion': 6,
                         'source': {
                             'selector':
                             'has(sg.projectcalico.org/openstack-SGID-default)'
                         }}],
                    'selector':
                    'has(sg.projectcalico.org/openstack-SGID-default)'}}
            )}
        with lib.FixedUUID('uuid-profile-prefixing'):
            self.give_way()
            self.simulated_time_advance(31)

        expected_writes = copy.deepcopy(self.initial_etcd3_writes)
        expected_writes[
            '/calico/resources/v3/projectcalico.org/clusterinformations/' +
            'default']['spec']['clusterGUID'] = 'uuid-profile-prefixing'
        self.assertEtcdWrites(expected_writes)
        self.assertEtcdDeletes(set())

    def test_policy_coexistence(self):
        """Coexistence with other policy data in the 'openstack' namespace.

        Check that we _do_ clean up old policy data that has our prefix, but
        _don't_ touch policies without our prefix.
        """

        # Start up with two existing policies in the 'openstack' namespace.
        self.etcd_data = {
            '/calico/resources/v3/projectcalico.org/networkpolicies/' +
            'openstack/customer-policy-1': json.dumps({
                'apiVersion': 'projectcalico.org/v3',
                'kind': 'NetworkPolicy',
                'metadata': {
                    'name': 'customer-policy-2',
                    'namespace': 'openstack',
                },
                'spec': {
                    'selector':
                    'has(sg.projectcalico.org/openstack-SGID-default)'}}
            ),
            '/calico/resources/v3/projectcalico.org/networkpolicies/' +
            'openstack/ossg.default.SOME_OLD_SG': json.dumps({
                'apiVersion': 'projectcalico.org/v3',
                'kind': 'NetworkPolicy',
                'metadata': {
                    'name': 'ossg.default.SOME_OLD_SG',
                    'namespace': 'openstack',
                },
                'spec': {
                    'selector':
                    'has(sg.projectcalico.org/openstack-SOME_OLD_SG)'}}
            ),
        }
        with lib.FixedUUID('uuid-profile-prefixing'):
            self.give_way()
            self.simulated_time_advance(31)

        expected_writes = copy.deepcopy(self.initial_etcd3_writes)
        expected_writes[
            '/calico/resources/v3/projectcalico.org/clusterinformations/' +
            'default']['spec']['clusterGUID'] = 'uuid-profile-prefixing'
        self.assertEtcdWrites(expected_writes)

        # We should clean up the old 'ossg.default.' policy, but not the
        # customer one.
        self.assertEtcdDeletes(set([
            '/calico/resources/v3/projectcalico.org/networkpolicies/' +
            'openstack/ossg.default.SOME_OLD_SG'
        ]))

    def test_old_openstack_data(self):
        """Startup with existing but old OpenStack profile data."""

        # Check that we clean it up.
        self.etcd_data = {
            '/calico/resources/v3/projectcalico.org/networkpolicies/' +
            'openstack/ossg.default.OLD': json.dumps({
                'apiVersion': 'projectcalico.org/v3',
                'kind': 'NetworkPolicy',
                'metadata': {
                    'namespace': 'openstack',
                    'name': 'ossg.default.OLD'
                },
                'spec': {
                    'egress': [
                        {'action': 'Allow',
                         'ipVersion': 4},
                        {'action': 'Allow',
                         'ipVersion': 6}],
                    'ingress': [
                        {'action': 'Allow',
                         'ipVersion': 4,
                         'source': {
                             'selector':
                             'has(sg.projectcalico.org/openstack-OLD)'}},
                        {'action': 'Allow',
                         'ipVersion': 6,
                         'source': {
                             'selector':
                             'has(sg.projectcalico.org/openstack-OLD)'}}],
                    'selector': 'has(sg.projectcalico.org/openstack-OLD)'}}
            )}
        with lib.FixedUUID('uuid-old-data'):
            self.give_way()
            self.simulated_time_advance(31)

        expected_writes = copy.deepcopy(self.initial_etcd3_writes)
        expected_writes[
            '/calico/resources/v3/projectcalico.org/clusterinformations/' +
            'default']['spec']['clusterGUID'] = 'uuid-old-data'
        self.assertEtcdWrites(expected_writes)
        self.assertEtcdDeletes(set([
            '/calico/resources/v3/projectcalico.org/networkpolicies/' +
            'openstack/ossg.default.OLD'
        ]))


class TestDriverStatusReporting(lib.Lib, unittest.TestCase):
    """Tests of the driver's status reporting function."""
    def setUp(self):
        super(TestDriverStatusReporting, self).setUp()

        # Mock out config.
        lib.m_compat.cfg.CONF.calico.etcd_host = "localhost"
        lib.m_compat.cfg.CONF.calico.etcd_port = 4001

    def test_felix_agent_state(self):
        self.assertEqual(
            {
                "agent_type": "Calico per-host agent (felix)",
                "binary": "calico-felix",
                "host": "host",
                "start_flag": True,
                'topic': mech_calico.constants.L2_AGENT_TOPIC,
            },
            mech_calico.felix_agent_state("host", True)
        )
        self.assertEqual(
            {
                "agent_type": "Calico per-host agent (felix)",
                "binary": "calico-felix",
                "host": "host2",
                'topic': mech_calico.constants.L2_AGENT_TOPIC,
            },
            mech_calico.felix_agent_state("host2", False)
        )

    def test_status_thread_epoch(self):
        self.driver._epoch = 2
        self.driver._status_updating_thread(1)

    @mock.patch("networking_calico.plugins.ml2.drivers.calico.mech_calico."
                "StatusWatcher",
                autospec=True)
    def test_status_thread_mainline(self, m_StatusWatcher):
        count = [0]

        with mock.patch.object(self.driver, "elector") as m_elector:
            m_elector.master.return_value = True

            def maybe_end_loop(*args, **kwargs):
                if count[0] == 2:
                    # Thread dies, should be restarted.
                    self.driver._etcd_watcher_thread = False
                if count[0] == 4:
                    # After a few loops, stop being the master...
                    m_elector.master.return_value = False
                if count[0] > 6:
                    # Then terminate the loop after a few more...
                    self.driver._epoch += 1
                count[0] += 1

            with mock.patch("eventlet.spawn") as m_spawn:
                with mock.patch("eventlet.sleep") as m_sleep:
                    m_sleep.side_effect = maybe_end_loop
                    self.driver._status_updating_thread(0)

        m_watcher = m_StatusWatcher.return_value
        self.assertEqual(
            [
                mock.call(m_watcher.start),
                mock.call(m_watcher.start),
            ],
            [c for c in m_spawn.mock_calls if c[0] == ""]
        )
        self.assertEqual(2, len(m_watcher.stop.mock_calls))
        self.assertIsNone(self.driver._etcd_watcher)

    def test_on_felix_alive(self):
        self.driver._get_db()
        self.driver._agent_update_context = mock.Mock()
        with mock.patch.object(self.driver, "state_report_rpc") as m_rpc:
            self.driver.on_felix_alive("hostfoo", True)
        self.assertEqual(
            [
                mock.call(
                    self.driver._agent_update_context,
                    {
                        "agent_type": "Calico per-host agent (felix)",
                        "binary": "calico-felix",
                        "host": "hostfoo",
                        "start_flag": True,
                        'topic': mech_calico.constants.L2_AGENT_TOPIC,
                    },
                    use_call=False
                )
            ],
            m_rpc.report_state.mock_calls
        )

    def test_on_port_status_changed(self):
        self.driver._last_status_queue_log_time = monotonic_time() - 100
        with mock.patch.object(self.driver, "_port_status_queue") as m_queue:
            m_queue.qsize.return_value = 100
            self.driver.on_port_status_changed("host", "port_id",
                                               {"status": "up"})
            self.assertEqual(
                "up",
                self.driver._port_status_cache[("host", "port_id")]

            )
            self.assertEqual([mock.call(("host", "port_id"))],
                             m_queue.put.mock_calls)
            m_queue.put.reset_mock()
            # Send a duplicate change.
            self.driver.on_port_status_changed("host", "port_id",
                                               {"status": "up"})
            # Should have no effect on the cache.
            self.assertEqual(
                "up",
                self.driver._port_status_cache[("host", "port_id")]
            )
            # And the queue update should be skipped.
            self.assertEqual([], m_queue.put.mock_calls)
            m_queue.put.reset_mock()
            # Deletion takes a different code path.
            self.driver.on_port_status_changed("host", "port_id", None)
            self.assertEqual({}, self.driver._port_status_cache)
            # Unknown value should be treated as deletion.
            self.driver.on_port_status_changed("host", "port_id",
                                               {"status": "unknown"})
            self.assertEqual({}, self.driver._port_status_cache)
            # One queue put for each deletion.
            self.assertEqual(
                [
                    mock.call(("host", "port_id")),
                    mock.call(("host", "port_id")),
                ],
                m_queue.put.mock_calls
            )

    def test_loop_writing_port_statuses(self):
        with mock.patch.object(self.driver, "_port_status_queue") as m_queue:
            with mock.patch.object(self.driver,
                                   "_try_to_update_port_status") as m_try_upd:
                m_queue.get.side_effect = iter([("host", "port")])
                self.assertRaises(StopIteration,
                                  self.driver._loop_writing_port_statuses,
                                  self.driver._epoch)
        self.assertEqual(
            [
                mock.call(mock.ANY, ("host", "port")),
            ],
            m_try_upd.mock_calls
        )

    def test_try_to_update_port_status(self):
        # New OpenStack releases have a host parameter.
        self.driver._get_db()

        # Driver uses reflection to check the function sig so we have to put
        # a real function here.
        mock_calls = []

        def m_update_port_status(context, port_id, status):
            """Older version of OpenStack; no host parameter"""
            mock_calls.append(mock.call(context, port_id, status))

        self.db.update_port_status = m_update_port_status
        context = mock.Mock()
        with mock.patch("eventlet.spawn_after", autospec=True) as m_spawn:
            self.driver._try_to_update_port_status(context, ("host", "p1"))
        self.assertEqual([mock.call(context, "p1",
                                    mech_calico.constants.PORT_STATUS_ERROR)],
                         mock_calls)
        self.assertEqual([], m_spawn.mock_calls)  # No retry on success

    def test_try_to_update_port_status_fail(self):
        # New OpenStack releases have a host parameter.
        self.driver._get_db()

        # Driver uses reflection to check the function sig so we have to put
        # a real function here.
        mock_calls = []

        def m_update_port_status(context, port_id, status, host=None):
            """Newer version of OpenStack; host parameter"""
            mock_calls.append(mock.call(context, port_id, status, host=host))
            raise lib.DBError()

        self.db.update_port_status = m_update_port_status
        self.driver._port_status_cache[("host", "p1")] = "up"
        context = mock.Mock()
        with mock.patch("eventlet.spawn_after", autospec=True) as m_spawn:
            self.driver._try_to_update_port_status(context, ("host", "p1"))
        self.assertEqual([mock.call(context, "p1",
                                    mech_calico.constants.PORT_STATUS_ACTIVE,
                                    host="host")],
                         mock_calls)
        self.assertEqual(
            [
                mock.call(5, self.driver._retry_port_status_update,
                          ("host", "p1"))
            ],
            m_spawn.mock_calls)

    def test_retry_port_status_update(self):
        with mock.patch.object(self.driver, "_port_status_queue") as m_queue:
            self.driver._retry_port_status_update(("host", "port"))
        self.assertEqual([mock.call(("host", "port"))], m_queue.put.mock_calls)


class TestStatusWatcher(_TestEtcdBase):
    def setUp(self):
        # Mock out config.
        lib.m_compat.cfg.CONF.calico.etcd_host = "localhost"
        lib.m_compat.cfg.CONF.calico.etcd_port = 4001
        lib.m_compat.cfg.CONF.calico.etcd_key_file = None
        lib.m_compat.cfg.CONF.calico.etcd_cert_file = None
        lib.m_compat.cfg.CONF.calico.etcd_ca_cert_file = None
        super(TestStatusWatcher, self).setUp()
        self.driver = mock.Mock(spec=mech_calico.CalicoMechanismDriver)
        self.watcher = status.StatusWatcher(self.driver)

    def test_tls(self):
        lib.m_compat.cfg.CONF.calico.etcd_cert_file = "cert-file"
        lib.m_compat.cfg.CONF.calico.etcd_ca_cert_file = "ca-cert-file"
        lib.m_compat.cfg.CONF.calico.etcd_key_file = "key-file"
        self.watcher = status.StatusWatcher(self.driver)

    def test_snapshot(self):
        # Populate initial status tree data, for initial snapshot testing.

        felix_status_key = '/calico/felix/v1/host/hostname/status'
        felix_last_reported_status_key = \
            '/calico/felix/v1/host/hostname/last_reported_status'
        ep_on_that_host_key = ('/calico/felix/v1/host/hostname/workload/' +
                               'openstack/wlid/endpoint/ep1')
        ep_on_unknown_host_key = ('/calico/felix/v1/host/unknown/workload/' +
                                  'openstack/wlid/endpoint/ep2')

        self.etcd_data = {
            # An agent status key to ignore.
            felix_last_reported_status_key: json.dumps({
                "uptime": 10,
                "first_update": True}),
            # An agent status key to take notice of.
            felix_status_key: json.dumps({
                "uptime": 10,
                "first_update": True}),
            # A port status key to take notice of.
            ep_on_that_host_key: '{"status": "up"}',
            # A port status key to ignore.
            ep_on_unknown_host_key: '{"status": "up"}',
        }

        # Arrange that the first watch call, on that status tree, will stop the
        # watcher.
        def _iterator():
            _log.info("Stop watcher now")
            self.watcher.stop()
            yield None

        def _cancel():
            pass

        self.clientv3.watch_prefix.return_value = _iterator(), _cancel

        # Start the watcher.  It will do initial snapshot processing, then stop
        # when it tries to watch for further changes.
        self.watcher.start()

        self.driver.on_felix_alive.assert_called_once_with("hostname",
                                                           new=True)
        self.driver.on_port_status_changed.assert_has_calls([
            mock.call("unknown", "ep2", {"status": "up"}),
            mock.call("hostname", "ep1", {"status": "up"}),
        ], any_order=True)

        # Start the watcher again, with the same etcd data.  We should see the
        # same status callbacks.
        self.driver.on_felix_alive.reset_mock()
        self.driver.on_port_status_changed.reset_mock()
        self.clientv3.watch_prefix.return_value = _iterator(), _cancel
        self.watcher.start()
        self.driver.on_felix_alive.assert_not_called()
        self.driver.on_port_status_changed.assert_has_calls([
            mock.call("unknown", "ep2", {"status": "up"}),
            mock.call("hostname", "ep1", {"status": "up"}),
        ], any_order=True)

        # Resync after deleting the unknown host endpoint.  We should see that
        # endpoint reported with status None.
        del self.etcd_data[ep_on_unknown_host_key]
        self.driver.on_felix_alive.reset_mock()
        self.driver.on_port_status_changed.reset_mock()
        self.clientv3.watch_prefix.return_value = _iterator(), _cancel
        self.watcher.start()
        self.driver.on_felix_alive.assert_not_called()
        self.driver.on_port_status_changed.assert_has_calls([
            mock.call("unknown", "ep2", None),
            mock.call("hostname", "ep1", {"status": "up"}),
        ], any_order=True)

        # Resync after deleting the Felix status.  We should see the other
        # endpoint reported with status None.
        del self.etcd_data[felix_status_key]
        self.driver.on_felix_alive.reset_mock()
        self.driver.on_port_status_changed.reset_mock()
        self.clientv3.watch_prefix.return_value = _iterator(), _cancel
        self.watcher.start()
        self.driver.on_felix_alive.assert_not_called()
        self.driver.on_port_status_changed.assert_has_calls([
            mock.call("hostname", "ep1", None),
        ], any_order=True)

    def test_endpoint_status_add_delete(self):
        m_port_status_node = self._add_test_endpoint()
        m_port_status_node.action = "delete"
        self.watcher._on_ep_delete(m_port_status_node,
                                   "hostname", "wlid", "ep1")

        self.assertEqual(
            [
                mock.call("hostname", "ep1", {"status": "up"}),
                mock.call("hostname", "ep1", None),
            ],
            self.driver.on_port_status_changed.mock_calls)
        self.assertEqual({}, self.watcher._endpoints_by_host)

    def test_endpoint_status_add_bad_json(self):
        m_port_status_node = mock.Mock()
        m_port_status_node.key = "/calico/felix/v1/host/hostname/workload/" \
                                 "openstack/wlid/endpoint/ep1"
        m_port_status_node.value = '{"status": "up"'
        self.watcher._on_ep_set(m_port_status_node, "hostname", "wlid", "ep1")

        self.assertEqual(
            [
                mock.call("hostname", "ep1", None),
            ],
            self.driver.on_port_status_changed.mock_calls)
        self.assertEqual({}, self.watcher._endpoints_by_host)

    def test_endpoint_status_add_bad_id(self):
        m_port_status_node = mock.Mock()
        m_port_status_node.key = "/calico/felix/v1/host/hostname/workload/" \
                                 "openstack/wlid/endpoint"
        self.watcher._on_ep_set(m_port_status_node, "hostname", "wlid", "ep1")
        self.assertEqual(
            [],
            self.driver.on_port_status_changed.mock_calls)
        self.assertEqual({}, self.watcher._endpoints_by_host)

    def _add_test_endpoint(self):
        # Add a workload to be deleted
        m_port_status_node = mock.Mock()
        m_port_status_node.key = "/calico/felix/v1/host/hostname/workload/" \
                                 "openstack/wlid/endpoint/ep1"
        m_port_status_node.value = '{"status": "up"}'
        self.watcher._on_ep_set(m_port_status_node, "hostname", "wlid", "ep1")
        ep_id = datamodel_v1.WloadEndpointId("hostname", "openstack",
                                             "wlid", "ep1")
        self.assertEqual({"hostname": set([ep_id])},
                         self.watcher._endpoints_by_host)
        return m_port_status_node

    def test_status_bad_json(self):
        for value in ["{", 10, "foo"]:
            m_response = mock.Mock()
            m_response.key = "/calico/felix/v1/host/hostname/status"
            m_response.value = value
            self.watcher._on_status_set(m_response, "foo")
        self.assertFalse(self.driver.on_felix_alive.called)

    def test_felix_status_expiry(self):
        # Put an endpoint in the cache to find later...
        m_response = mock.Mock()
        m_response.key = "/calico/felix/v1/host/hostname/workload/" \
                         "openstack/wlid/endpoint/epid"
        m_response.value = '{"status": "up"}'
        self.watcher._on_ep_set(m_response, "hostname", "wlid", "epid")

        # Then note that felix is down.
        m_response = mock.Mock()
        m_response.key = "/calico/felix/v1/host/hostname/status"
        self.watcher._on_status_del(m_response, "hostname")

        self.assertEqual(
            [
                mock.call("hostname", "epid", {"status": "up"}),
                mock.call("hostname", "epid", None),
            ],
            self.driver.on_port_status_changed.mock_calls)


def _neutron_rule_from_dict(overrides):
    rule = {
        "ethertype": "IPv4",
        "protocol": None,
        "remote_ip_prefix": None,
        "remote_group_id": None,
        "direction": "ingress",
        "port_range_min": None,
        "port_range_max": None,
    }
    rule.update(overrides)
    return rule
