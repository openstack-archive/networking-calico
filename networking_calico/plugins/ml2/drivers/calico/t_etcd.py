# -*- coding: utf-8 -*-
#
# Copyright (c) 2015 Metaswitch Networks
# All Rights Reserved.
#
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

# Etcd-based transport for the Calico/OpenStack Plugin.

import collections
import functools
import json
import netaddr
import re
import socket
import uuid
import weakref

import etcd
from eventlet.semaphore import Semaphore

from networking_calico.common import config as calico_config
from networking_calico.compat import cfg
from networking_calico.compat import log
from networking_calico import datamodel_v1
from networking_calico import datamodel_v3
from networking_calico import etcdutils
from networking_calico.plugins.ml2.drivers.calico.election import Elector


# The node hostname is used as the default identity for leader election
_hostname = socket.gethostname()


# Elector configuration;
elector_opt = cfg.StrOpt(
    'elector_name', default=_hostname,
    help="A unique name to identify this node in leader election"
)

# Register Calico related configuration options
calico_config.register_options(cfg.CONF, additional_options=[elector_opt])


# The amount of time in seconds to wait for etcd responses.
ETCD_TIMEOUT = 5

OPENSTACK_ENDPOINT_RE = re.compile(
    r'^' + datamodel_v1.HOST_DIR +
    r'/(?P<hostname>[^/]+)/.*openstack.*/endpoint/(?P<endpoint_id>[^/]+)')

LOG = log.getLogger(__name__)

# Set a low refresh interval on the master key.  This reduces the chance of
# the etcd event buffer wrapping while non-masters are waiting for the key to
# be refreshed.
MASTER_REFRESH_INTERVAL = 10
MASTER_TIMEOUT = 60

# Objects for lightly wrapping etcd return values for use in the mechanism
# driver.
# These namedtuples are getting pretty heavyweight at this point. If you find
# yourself wanting to add more fields to them, consider rewriting them as full
# classes. Note that several of the properties of namedtuples are desirable for
# these objects (immutability being the biggest), so if you rewrite as classes
# attempt to preserve those properties.
Endpoint = collections.namedtuple(
    'Endpoint', ['id', 'key', 'modified_index', 'host', 'data']
)
Profile = collections.namedtuple(
    'Profile',
    [
        'id',                   # Note: _without_ any OPENSTACK_SG_PREFIX.
        'modified_index',
        'spec',
    ]
)
Subnet = collections.namedtuple(
    'Subnet', ['id', 'modified_index', 'data']
)


# The ID of every profile that this driver writes into etcd will be prefixed
# with the following, and this driver only regards itself as the owner of
# profiles that begin with this prefix.  Specifically this means that, when
# resyncing the content of the Neutron DB against etcd, it will not clean up
# (or even touch) profiles whose ID does not begin with this prefix.  This
# means that profiles established by this driver can happily coexist with those
# established by other Calico orchestrators.
OPENSTACK_SG_PREFIX = 'openstack-sg-'


def with_openstack_sg_prefix(openstack_sg_id):
    assert not openstack_sg_id.startswith(OPENSTACK_SG_PREFIX)
    return OPENSTACK_SG_PREFIX + openstack_sg_id


def without_openstack_sg_prefix(etcd_sg_id):
    assert etcd_sg_id.startswith(OPENSTACK_SG_PREFIX)
    return etcd_sg_id[len(OPENSTACK_SG_PREFIX):]


def _handling_etcd_exceptions(fn):
    """_handling_etcd_exceptions

    Decorator for methods of CalicoTransportEtcd only; implements some
    common EtcdException handling.
    """
    @functools.wraps(fn)
    def wrapped(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except (etcd.EtcdCompareFailed,
                etcd.EtcdKeyError,
                etcd.EtcdValueError) as e:
            # The caller should be expecting this, re-raise it.
            LOG.warning("Expected etcd error, re-raising: %r", e)
            raise
        except etcd.EtcdException:
            # Other exceptions we can't be sure about, so be defensive and
            # reconnect.
            LOG.exception("Request to etcd failed, refreshing our connection.")
            self._on_etcd_request_failed()
            raise
    return wrapped


class CalicoTransportEtcd(object):
    """Calico transport implementation based on etcd."""

    def __init__(self, driver):
        # Explicitly store the driver as a weakreference. This prevents
        # the reference loop between transport and driver keeping the objects
        # alive.
        self.driver = weakref.proxy(driver)

        # Prepare client for accessing etcd data.
        self.client = None

        # Elector, for performing leader election.
        self.elector = None

        # Lock prevents concurrent re-initialisations which could leave us with
        # inconsistent client and elector.
        self._init_lock = Semaphore()
        self._init_count = 0
        self._initialise()

    def _on_etcd_request_failed(self):
        LOG.warning("Request to etcd failed, reinitialising our connection.")
        self._initialise()

    def _initialise(self):
        # Optimisation: only run the most recently scheduled _initialise()
        # call. This increment and copy is atomic because there are no yield
        # points.  The lock prevents inconsistency anyway.
        self._init_count += 1
        expected_count = self._init_count

        with self._init_lock:
            if self._init_count != expected_count:
                LOG.info("Skipping duplicate _initialise() call.")
                return
            LOG.info("(Re)initialising the etcd transport.")
            if self.elector:
                LOG.warning("There was already an elector, shutting it down.")
                self.elector.stop()
            calico_cfg = cfg.CONF.calico
            tls_config_params = [
                calico_cfg.etcd_key_file,
                calico_cfg.etcd_cert_file,
                calico_cfg.etcd_ca_cert_file,
            ]
            if any(tls_config_params):
                LOG.info("TLS to etcd is enabled with key file %s; "
                         "cert file %s; CA cert file %s", *tls_config_params)
                # Etcd client expects cert and key as a tuple.
                tls_cert = (calico_cfg.etcd_cert_file,
                            calico_cfg.etcd_key_file)
                tls_ca_cert = calico_cfg.etcd_ca_cert_file
                protocol = "https"
            else:
                LOG.info("TLS disabled, using HTTP to connect to etcd.")
                tls_cert = None
                tls_ca_cert = None
                protocol = "http"
            client = etcd.Client(host=calico_cfg.etcd_host,
                                 port=calico_cfg.etcd_port,
                                 protocol=protocol,
                                 cert=tls_cert,
                                 ca_cert=tls_ca_cert)
            elector = Elector(
                client=client,
                server_id=calico_cfg.elector_name,
                election_key=datamodel_v1.NEUTRON_ELECTION_KEY,
                interval=MASTER_REFRESH_INTERVAL,
                ttl=MASTER_TIMEOUT,
            )
            # Since normal reading threads don't take the lock, save the
            # client and elector off together atomically.  This is atomic
            # because we're in a green thread.
            self.client = client
            self.elector = elector

    @property
    def is_master(self):
        """Whether this node is currently the Neutron master."""
        return self.elector.master()

    @_handling_etcd_exceptions
    def write_profile_to_etcd(self, profile, prev_index=None):
        """Convert and write a SecurityProfile to etcdv3."""
        LOG.debug("Writing profile %s", profile)
        name = with_openstack_sg_prefix(profile.id)
        datamodel_v3.put("Profile",
                         name,
                         profile_spec(profile),
                         prev_index=prev_index)

    @_handling_etcd_exceptions
    def subnet_created(self, subnet, prev_index=None):
        """Write data to etcd to describe a DHCP-enabled subnet."""
        LOG.info("Write subnet %s %s to etcd", subnet['id'], subnet['cidr'])
        data = subnet_etcd_data(subnet)

        # python-etcd doesn't keyword argument properly.
        kwargs = {}
        if prev_index is not None:
            kwargs['prevIndex'] = prev_index

        self.client.write(datamodel_v1.key_for_subnet(subnet['id']),
                          json.dumps(data),
                          **kwargs)

    @_handling_etcd_exceptions
    def subnet_deleted(self, subnet_id):
        """Delete data from etcd for a subnet that is no longer wanted."""
        LOG.info("Deleting subnet %s", subnet_id)
        # Delete the etcd key for this subnet.
        key = datamodel_v1.key_for_subnet(subnet_id)
        try:
            self.client.delete(key)
        except etcd.EtcdKeyNotFound:
            # Already gone, treat as success.
            LOG.debug("Key %s, which we were deleting, disappeared", key)

    @_handling_etcd_exceptions
    def endpoint_created(self, port):
        """Write appropriate data to etcd for an endpoint creation event."""
        # Write etcd data for the new endpoint.
        self.write_port_to_etcd(port)

    @_handling_etcd_exceptions
    def endpoint_deleted(self, port):
        """Delete data from etcd for an endpoint deleted event."""
        LOG.info("Deleting port %s", port)
        # TODO(nj): What do we do about profiles here?
        # Delete the etcd key for this endpoint.
        key = port_etcd_key(port)
        try:
            self.client.delete(key)
        except etcd.EtcdKeyNotFound:
            # Already gone, treat as success.
            LOG.debug("Key %s, which we were deleting, disappeared", key)

        self._cleanup_workload_tree(key)

    @_handling_etcd_exceptions
    def write_port_to_etcd(self, port, prev_index=None):
        """Writes a given port dictionary to etcd."""
        LOG.info("Write port %s to etcd", port)
        data = port_etcd_data(port)

        # python-etcd doesn't keyword argument properly.
        kwargs = {}
        if prev_index is not None:
            kwargs['prevIndex'] = prev_index

        self.client.write(
            port_etcd_key(port), json.dumps(data), **kwargs
        )

    @_handling_etcd_exceptions
    def provide_felix_config(self):
        """provide_felix_config

        Specify the prefix of the TAP interfaces that Felix should
        look for and work with.  This config setting does not have a
        default value, because different cloud systems will do
        different things.  Here we provide the prefix that Neutron
        uses.
        """
        LOG.info("Providing Felix configuration")

        # Get existing global ClusterInformation and FelixConfiguration.  We
        # will add to these, rather than trampling on anything that may already
        # be there.
        cluster_info = datamodel_v3.get("ClusterInformation", "default")
        rewrite_cluster_info = False
        LOG.info("Read ClusterInformation: %s", cluster_info)
        felix_config = datamodel_v3.get("FelixConfiguration", "default")
        rewrite_felix_config = False
        LOG.info("Read FelixConfiguration: %s", felix_config)

        # Generate a cluster GUID if there isn't one already.
        if not cluster_info.get(datamodel_v3.CLUSTER_GUID):
            cluster_info[datamodel_v3.CLUSTER_GUID] = uuid.uuid4().get_hex()
            rewrite_cluster_info = True

        # Add "openstack" to the cluster type, unless there already.
        cluster_type = cluster_info.get(datamodel_v3.CLUSTER_TYPE, "")
        if cluster_type:
            if "openstack" not in cluster_type:
                cluster_info[datamodel_v3.CLUSTER_TYPE] = \
                    cluster_type + ",openstack"
                rewrite_cluster_info = True
        else:
            cluster_info[datamodel_v3.CLUSTER_TYPE] = "openstack"
            rewrite_cluster_info = True

        # TODO(nj): Do we need to ensure that the Calico version field is set?
        # It may be that we don't need this for Calico/OpenStack operation.

        # Set the datastore to ready, if it isn't already.
        if not cluster_info.get(datamodel_v3.DATASTORE_READY, False):
            cluster_info[datamodel_v3.DATASTORE_READY] = True
            rewrite_cluster_info = True

        # Enable endpoint reporting.
        if not felix_config.get(datamodel_v3.ENDPOINT_REPORTING_ENABLED,
                                False):
            felix_config[datamodel_v3.ENDPOINT_REPORTING_ENABLED] = True
            rewrite_felix_config = True

        # Ensure that interface prefixes include 'tap'.
        interface_prefix = felix_config.get(datamodel_v3.INTERFACE_PREFIX)
        prefixes = interface_prefix.split(',') if interface_prefix else []
        if 'tap' not in prefixes:
            prefixes.append('tap')
            felix_config[datamodel_v3.INTERFACE_PREFIX] = ','.join(prefixes)
            rewrite_felix_config = True

        # Rewrite FelixConfiguration, if we changed anything above.
        if rewrite_felix_config:
            LOG.info("New FelixConfiguration: %s", felix_config)
            datamodel_v3.put("FelixConfiguration", "default", felix_config)

        # Rewrite ClusterInformation, if we changed anything above.
        if rewrite_cluster_info:
            LOG.info("New ClusterInformation: %s", cluster_info)
            datamodel_v3.put("ClusterInformation", "default", cluster_info)

    @_handling_etcd_exceptions
    def get_subnet_data(self, subnet):
        """Get data for an subnet out of etcd.

        This should be used on subnets returned from functions like
        ``get_subnets``.

        :param subnet: A ``Subnet`` class.
        :return: A ``Subnet`` class with ``data`` not None.
        """
        LOG.debug("Getting subnet %s", subnet.id)

        result = self.client.read(datamodel_v1.key_for_subnet(subnet.id),
                                  timeout=ETCD_TIMEOUT)
        return Subnet(
            id=subnet.id,
            modified_index=result.modifiedIndex,
            data=result.value,
        )

    @_handling_etcd_exceptions
    def get_subnets(self):
        """Get information about every subnet in etcd.

        Returns a generator of ``Subnet`` objects.
        """
        LOG.info("Scanning etcd for all subnets")

        try:
            result = self.client.read(
                datamodel_v1.SUBNET_DIR, recursive=True, timeout=ETCD_TIMEOUT
            )
        except etcd.EtcdKeyNotFound:
            # No key yet, which is totally fine: just exit.
            LOG.info("No subnet key present.")
            return

        nodes = result.children

        for node in nodes:
            subnet_id = node.key.split("/")[-1]
            LOG.debug("Found subnet %s", subnet_id)
            yield Subnet(
                id=subnet_id,
                modified_index=node.modifiedIndex,
                data=None,
            )

    @_handling_etcd_exceptions
    def atomic_delete_subnet(self, subnet):
        """Atomically delete a given subnet.

        This method tolerates attempting to delete keys that are already
        missing, otherwise allows exceptions from etcd to bubble up.
        """
        LOG.info(
            "Atomically deleting subnet id %s, modified %s",
            subnet.id,
            subnet.modified_index
        )

        try:
            self.client.delete(
                datamodel_v1.key_for_subnet(subnet.id),
                prevIndex=subnet.modified_index,
                timeout=ETCD_TIMEOUT,
            )
        except etcd.EtcdKeyNotFound:
            # Trying to delete stuff that doesn't exist is ok, but log it.
            LOG.info(
                "Subnet %s was already deleted, nothing to do.",
                subnet.id
            )

    @_handling_etcd_exceptions
    def get_endpoint_data(self, endpoint):
        """get_endpoint_data

        Get data for an endpoint out of etcd. This should be used on endpoints
        returned from functions like ``get_endpoints``.

        :param endpoint: An ``Endpoint`` class.
        :return: A ``Endpoint`` class with ``data`` not None.
        """
        LOG.debug("Getting endpoint %s", endpoint.id)

        result = self.client.read(endpoint.key, timeout=ETCD_TIMEOUT)

        return Endpoint(
            id=endpoint.id,
            key=endpoint.key,
            modified_index=result.modifiedIndex,
            host=endpoint.host,
            data=result.value,
        )

    @_handling_etcd_exceptions
    def get_endpoints(self):
        """get_endpoints

        Gets information about every endpoint in etcd. Returns a generator of
        ``Endpoint`` objects.
        """
        LOG.info("Scanning etcd for all endpoints")

        try:
            result = self.client.read(
                datamodel_v1.HOST_DIR, recursive=True, timeout=ETCD_TIMEOUT
            )
        except etcd.EtcdKeyNotFound:
            # No key yet, which is totally fine: just exit.
            LOG.info("No endpoint key present.")
            return

        nodes = result.children

        for node in nodes:
            match = OPENSTACK_ENDPOINT_RE.match(node.key)
            if match is None:
                continue

            endpoint_id = match.group('endpoint_id')
            host = match.group('hostname')

            LOG.debug("Found endpoint %s", endpoint_id)
            yield Endpoint(
                id=endpoint_id,
                key=node.key,
                modified_index=node.modifiedIndex,
                host=host,
                data=None,
            )

    @_handling_etcd_exceptions
    def atomic_delete_endpoint(self, endpoint):
        """Atomically delete a given endpoint.

        This method tolerates attempting to delete keys that are already
        missing, otherwise allows exceptions from etcd to bubble up.

        This also attempts to clean up the containing directory, but doesn't
        worry too much if it fails.
        """
        LOG.info(
            "Atomically deleting endpoint id %s, modified %s",
            endpoint.id,
            endpoint.modified_index
        )

        try:
            self.client.delete(
                endpoint.key,
                prevIndex=endpoint.modified_index,
                timeout=ETCD_TIMEOUT,
            )
        except etcd.EtcdKeyNotFound:
            # Trying to delete stuff that doesn't exist is ok, but log it.
            LOG.info(
                "Key %s was already deleted, nothing to do.",
                endpoint.key
            )

        self._cleanup_workload_tree(endpoint.key)

    @_handling_etcd_exceptions
    def get_profiles(self):
        """get_profiles

        Gets every OpenStack profile in etcdv3. Returns a generator of
        ``Profile`` objects.
        """
        LOG.info("Scanning etcdv3 for all profiles")

        for result in datamodel_v3.get_all("Profile"):
            name, spec, modified_index = result
            if name.startswith(OPENSTACK_SG_PREFIX):
                LOG.debug("Found profile %s", name)
                yield Profile(
                    id=without_openstack_sg_prefix(name),
                    modified_index=modified_index,
                    spec=spec,
                )

    @_handling_etcd_exceptions
    def atomic_delete_profile(self, profile):
        """atomic_delete_profile

        Atomically delete a profile.
        """
        LOG.info(
            "Deleting profile %s, modified %s",
            profile.id,
            profile.modified_index,
        )
        name = with_openstack_sg_prefix(profile.id)

        datamodel_v3.delete("Profile", name)

    def _cleanup_workload_tree(self, endpoint_key):
        """_cleanup_workload_tree

        Attempts to delete any remaining etcd directories after an endpoint has
        been deleted. This needs to recurse up the tree until the workload
        level to ensure that all directories really are pruned.
        """
        key_parts = endpoint_key.split('/')

        # This will return [-1, -2], which means we'll attempt to delete two
        # directories above the endpoint key. That means we'll clean up the
        # workload directory.
        for i in range(-1, -3, -1):
            delete_key = '/'.join(key_parts[:i])
            try:
                self.client.delete(delete_key, dir=True, timeout=ETCD_TIMEOUT)
            except etcd.EtcdException as e:
                LOG.debug("Failed to delete %s (%r), skipping.", delete_key, e)

    def stop(self):
        LOG.info("Stopping transport %s", self)
        self.elector.stop()


class CalicoEtcdWatcher(etcdutils.EtcdWatcher):
    """An EtcdWatcher that watches our status-reporting subtree.

    Responsible for parsing the events and passing the updates to the
    mechanism driver.

    We deliberately do not share an etcd client with the transport.
    The reason is that, if we share a client then managing the lifecycle
    of the client becomes an awkward shared responsibility (complicated
    by the EtcdClusterIdChanged exception, which is only thrown once).
    """

    def __init__(self, calico_driver):
        calico_cfg = cfg.CONF.calico
        host = calico_cfg.etcd_host
        port = calico_cfg.etcd_port
        LOG.info("CalicoEtcdWatcher created for %s:%s", host, port)
        tls_config_params = [
            calico_cfg.etcd_key_file,
            calico_cfg.etcd_cert_file,
            calico_cfg.etcd_ca_cert_file,
        ]
        if any(tls_config_params):
            LOG.info("TLS to etcd is enabled with key file %s; "
                     "cert file %s; CA cert file %s", *tls_config_params)
            protocol = "https"
        else:
            LOG.info("TLS disabled, using HTTP to connect to etcd.")
            protocol = "http"
        super(CalicoEtcdWatcher, self).__init__(
            "%s:%s" % (host, port),
            datamodel_v1.FELIX_STATUS_DIR,
            etcd_scheme=protocol,
            etcd_key=calico_cfg.etcd_key_file,
            etcd_cert=calico_cfg.etcd_cert_file,
            etcd_ca=calico_cfg.etcd_ca_cert_file
        )
        self.calico_driver = calico_driver

        # Track the set of endpoints that are on each host so we can generate
        # deletes for parent dirs being deleted.
        self._endpoints_by_host = collections.defaultdict(set)

        # Register for felix uptime updates.
        self.register_path(datamodel_v1.FELIX_STATUS_DIR +
                           "/<hostname>/status",
                           on_set=self._on_status_set,
                           on_del=self._on_status_del)
        # Register for per-port status updates.
        self.register_path(datamodel_v1.FELIX_STATUS_DIR +
                           "/<hostname>/workload/openstack/"
                           "<workload>/endpoint/<endpoint>",
                           on_set=self._on_ep_set,
                           on_del=self._on_ep_delete)
        self.register_path(datamodel_v1.FELIX_STATUS_DIR +
                           "/<hostname>/workload/openstack/"
                           "<workload>/endpoint",
                           on_del=self._on_per_host_dir_delete)
        self.register_path(datamodel_v1.FELIX_STATUS_DIR +
                           "/<hostname>/workload/openstack/"
                           "<workload>",
                           on_del=self._on_per_host_dir_delete)
        self.register_path(datamodel_v1.FELIX_STATUS_DIR +
                           "/<hostname>/workload/openstack",
                           on_del=self._on_per_host_dir_delete)
        self.register_path(datamodel_v1.FELIX_STATUS_DIR +
                           "/<hostname>/workload",
                           on_del=self._on_per_host_dir_delete)
        self.register_path(datamodel_v1.FELIX_STATUS_DIR + "/<hostname>",
                           on_del=self._on_per_host_dir_delete)
        self.register_path(datamodel_v1.FELIX_STATUS_DIR,
                           on_del=self._force_resync)

    def _on_snapshot_loaded(self, etcd_snapshot_response):
        """Called whenever a snapshot is loaded from etcd.

        Updates the driver with the current state.
        """
        LOG.info("Started processing status-reporting snapshot from etcd")
        endpoints_by_host = collections.defaultdict(set)
        hosts_with_live_felix = set()

        # First pass: find all the Felixes that are alive.
        for etcd_node in etcd_snapshot_response.leaves:
            key = etcd_node.key
            felix_hostname = datamodel_v1.hostname_from_status_key(key)
            if felix_hostname:
                # Defer to the code for handling an event.
                hosts_with_live_felix.add(felix_hostname)
                self._on_status_set(etcd_node, felix_hostname)
                continue

        # Second pass: find all the endpoints associated with a live Felix.
        for etcd_node in etcd_snapshot_response.leaves:
            key = etcd_node.key
            endpoint_id = datamodel_v1.get_endpoint_id_from_key(key)
            if endpoint_id:
                if endpoint_id.host in hosts_with_live_felix:
                    LOG.debug("Endpoint %s is on a host with a live Felix.",
                              endpoint_id)
                    self._report_status(
                        endpoints_by_host,
                        endpoint_id,
                        etcd_node.value
                    )
                else:
                    LOG.debug("Endpoint %s is not on a host with live Felix;"
                              "marking it down.",
                              endpoint_id)
                    self.calico_driver.on_port_status_changed(
                        endpoint_id.host,
                        endpoint_id.endpoint,
                        None,
                    )
                continue

        # Find any removed endpoints.
        for host, endpoints in self._endpoints_by_host.items():
            current_endpoints = endpoints_by_host.get(host, set())
            removed_endpoints = endpoints - current_endpoints
            for endpoint_id in removed_endpoints:
                LOG.debug("Endpoint %s removed by resync.")
                self.calico_driver.on_port_status_changed(
                    host,
                    endpoint_id.endpoint,
                    None,
                )

        # Swap in the newly-loaded state.
        self._endpoints_by_host = endpoints_by_host
        LOG.info("Finished processing status-reporting snapshot from etcd")

    def _on_status_set(self, response, hostname):
        """Called when a felix uptime report is inserted/updated."""
        try:
            value = json.loads(response.value)
            new = bool(value.get("first_update"))
        except (ValueError, TypeError):
            LOG.warning("Bad JSON data for key %s: %s",
                        response.key, response.value)
        else:
            self.calico_driver.on_felix_alive(
                hostname,
                new=new,
            )

    def _on_status_del(self, response, hostname):
        """Called when Felix's status key expires.  Implies felix is dead."""
        LOG.error("Felix on host %s failed to check in.  Marking the "
                  "ports it was managing as in-error.", hostname)
        for endpoint_id in self._endpoints_by_host[hostname]:
            # Flag all the ports as being in error.  They're no longer
            # receiving security updates.
            self.calico_driver.on_port_status_changed(
                hostname,
                endpoint_id.endpoint,
                None,
            )
        # Then discard our cache of endpoints.  If felix comes back up, it will
        # repopulate.
        self._endpoints_by_host.pop(hostname)

    def _on_ep_set(self, response, hostname, workload, endpoint):
        """Called when the status key for a particular endpoint is updated.

        Reports the status to the driver and caches the existence of the
        endpoint.
        """
        ep_id = datamodel_v1.get_endpoint_id_from_key(response.key)
        if not ep_id:
            LOG.error("Failed to extract endpoint ID from: %s.  Ignoring "
                      "update!", response.key)
            return
        self._report_status(self._endpoints_by_host,
                            ep_id,
                            response.value)

    def _report_status(self, endpoints_by_host, endpoint_id, raw_json):
        try:
            status = json.loads(raw_json)
        except (ValueError, TypeError):
            LOG.error("Bad JSON data for %s: %s", endpoint_id, raw_json)
            status = None  # Report as error
            endpoints_by_host[endpoint_id.host].discard(endpoint_id)
            if not endpoints_by_host[endpoint_id.host]:
                del endpoints_by_host[endpoint_id.host]
        else:
            endpoints_by_host[endpoint_id.host].add(endpoint_id)
        LOG.debug("Port %s updated to status %s", endpoint_id, status)
        self.calico_driver.on_port_status_changed(
            endpoint_id.host,
            endpoint_id.endpoint,
            status,
        )

    def _on_ep_delete(self, response, hostname, workload, endpoint):
        """Called when the status key for an endpoint is deleted.

        This typically means the endpoint has been deleted.  Reports
        the deletion to the driver.
        """
        LOG.debug("Port %s/%s/%s deleted", hostname, workload, endpoint)
        endpoint_id = datamodel_v1.get_endpoint_id_from_key(response.key)
        self._endpoints_by_host[hostname].discard(endpoint_id)
        if not self._endpoints_by_host[hostname]:
            del self._endpoints_by_host[hostname]
        self.calico_driver.on_port_status_changed(
            hostname,
            endpoint,
            None,
        )

    def _on_per_host_dir_delete(self, response, hostname, workload=None):
        """_on_per_host_dir_delete

        Called when one of the directories that may contain endpoint
        statuses is deleted.  Cleans up either the specific workload
        or the whole host.
        """
        LOG.debug("One of the per-host directories for host %s, workload "
                  "%s deleted.", hostname, workload)
        endpoints_on_host = self._endpoints_by_host[hostname]
        for endpoint_id in [ep_id for ep_id in endpoints_on_host if
                            workload is None or workload == ep_id.workload]:
            LOG.info("Directory containing status report for %s deleted;"
                     "updating port status",
                     endpoint_id)
            endpoints_on_host.discard(endpoint_id)
            self.calico_driver.on_port_status_changed(
                hostname,
                endpoint_id.endpoint,
                None
            )
        if not endpoints_on_host:
            del self._endpoints_by_host[hostname]

    def _force_resync(self, response, **kwargs):
        LOG.warning("Forcing a resync due to %s to key %s",
                    response.action, response.key)
        raise etcdutils.ResyncRequired()


def _neutron_rule_to_etcd_rule(rule):
    """_neutron_rule_to_etcd_rule

    Translate a single Neutron rule dict to a single dict in our
    etcd format.
    """
    ethertype = rule['ethertype']
    etcd_rule = {'action': 'Allow'}
    # Map the ethertype field from Neutron to etcd format.
    etcd_rule['ipVersion'] = {'IPv4': 4,
                              'IPv6': 6}[ethertype]
    # Map the protocol field from Neutron to etcd format.
    if rule['protocol'] is None or rule['protocol'] == -1:
        pass
    elif rule['protocol'] == 'icmp':
        etcd_rule['protocol'] = {'IPv4': 'ICMP',
                                 'IPv6': 'ICMPv6'}[ethertype]
    elif isinstance(rule['protocol'], int):
        etcd_rule['protocol'] = rule['protocol']
    else:
        etcd_rule['protocol'] = rule['protocol'].upper()

    port_spec = None
    if rule['protocol'] == 'icmp':
        # OpenStack stashes the ICMP match criteria in
        # port_range_min/max.
        icmp_fields = {}
        icmp_type = rule['port_range_min']
        if icmp_type is not None and icmp_type != -1:
            icmp_fields['type'] = icmp_type
        icmp_code = rule['port_range_max']
        if icmp_code is not None and icmp_code != -1:
            icmp_fields['code'] = icmp_code
        if icmp_fields:
            etcd_rule['icmp'] = icmp_fields
    else:
        # src/dst_ports is a list in which each entry can be a
        # single number, or a string describing a port range.
        if rule['port_range_min'] == -1:
            port_spec = ['1:65535']
        elif rule['port_range_min'] == rule['port_range_max']:
            if rule['port_range_min'] is not None:
                port_spec = [rule['port_range_min']]
        else:
            port_spec = ['%s:%s' % (rule['port_range_min'],
                                    rule['port_range_max'])]

    entity_rule = {}
    if rule['remote_group_id'] is not None:
        entity_rule['selector'] = 'has(%s)' % rule['remote_group_id']
    if rule['remote_ip_prefix'] is not None:
        entity_rule['nets'] = [rule['remote_ip_prefix']]
    if port_spec is not None:
        entity_rule['ports'] = port_spec
    LOG.debug("=> Entity rule %s" % entity_rule)

    # Store in source or destination field of the overall rule.
    if entity_rule:
        if rule['direction'] == 'ingress':
            etcd_rule['source'] = entity_rule
        else:
            etcd_rule['destination'] = entity_rule

    LOG.debug("=> %s Calico rule %s" % (rule['direction'], etcd_rule))

    return etcd_rule


def subnet_etcd_data(subnet):
    data = {'network_id': subnet['network_id'],
            'cidr': str(netaddr.IPNetwork(subnet['cidr'])),
            'host_routes': subnet['host_routes'],
            'gateway_ip': subnet['gateway_ip']}
    if subnet['dns_nameservers']:
        data['dns_servers'] = subnet['dns_nameservers']
    return data


def port_etcd_key(port):
    """Determine what the etcd key is for a port."""
    return datamodel_v1.key_for_endpoint(port['binding:host_id'],
                                         "openstack",
                                         port['device_id'],
                                         port['id'])


def port_etcd_data(port):
    """port_etcd_data

    Build the dictionary of data that will be written into etcd for a port.
    """

    # Construct the simpler port data.
    data = {'state': 'active' if port['admin_state_up'] else 'inactive',
            'name': port['interface_name'],
            'mac': port['mac_address'],
            'profile_ids': [with_openstack_sg_prefix(sg_id)
                            for sg_id in port['security_groups']]}
    # TODO(MD4) Check the old version writes 'profile_id' in a form
    # that translation code in common.validate_endpoint() will work.

    # Collect IPv4 and IPv6 addresses and subnet IDs.  On the way, also set the
    # corresponding gateway fields.  If there is more than one IPv4 or IPv6
    # gateway, the last one (in port['fixed_ips']) wins.
    ipv4_nets = []
    ipv6_nets = []
    ipv4_subnet_ids = []
    ipv6_subnet_ids = []
    for ip in port['fixed_ips']:
        if ':' in ip['ip_address']:
            ipv6_nets.append(ip['ip_address'] + '/128')
            ipv6_subnet_ids.append(ip['subnet_id'])
            if ip['gateway'] is not None:
                data['ipv6_gateway'] = ip['gateway']
        else:
            ipv4_nets.append(ip['ip_address'] + '/32')
            ipv4_subnet_ids.append(ip['subnet_id'])
            if ip['gateway'] is not None:
                data['ipv4_gateway'] = ip['gateway']
    data['ipv4_nets'] = ipv4_nets
    data['ipv6_nets'] = ipv6_nets
    data['ipv4_subnet_ids'] = ipv4_subnet_ids
    data['ipv6_subnet_ids'] = ipv6_subnet_ids

    # Propagate the port's FQDN.
    dns_assignment = port.get('dns_assignment')
    if dns_assignment:
        # Note: the Neutron server generates a list of assignment entries, one
        # for each fixed IP, but all with the same FQDN, for slightly
        # historical reasons.  We're fine getting the FQDN from the first
        # entry.
        data['fqdn'] = dns_assignment[0]['fqdn']

    ipv4_nat = []
    ipv6_nat = []
    for ip in port['floating_ips']:
        if ':' in ip['int_ip']:
            ipv6_nat.append(ip)
        else:
            ipv4_nat.append(ip)
    if ipv4_nat:
        data['ipv4_nat'] = ipv4_nat
    if ipv6_nat:
        data['ipv6_nat'] = ipv6_nat

    # Return that data.
    return data


def profile_spec(profile):
    """profile_spec

    Generate JSON ProfileSpec for the given SecurityProfile.
    """
    inbound_rules = [
        _neutron_rule_to_etcd_rule(rule) for rule in profile.inbound_rules
    ]
    outbound_rules = [
        _neutron_rule_to_etcd_rule(rule) for rule in profile.outbound_rules
    ]
    labels_to_apply = {}
    for tag in profile.id.split('_'):
        labels_to_apply[tag] = ''

    return {
        'ingress': inbound_rules,
        'egress': outbound_rules,
        'labelsToApply': labels_to_apply,
    }
