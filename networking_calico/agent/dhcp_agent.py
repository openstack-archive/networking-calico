# Copyright 2012 OpenStack Foundation
# Copyright 2015 Metaswitch Networks
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

import logging
import netaddr
import socket
import sys

import eventlet
eventlet.monkey_patch()

from oslo_config import cfg

from neutron.agent.common import config
from neutron.agent.dhcp.agent import DhcpAgent, NetworkCache
from neutron.agent.dhcp_agent import register_options
from neutron.agent.linux import dhcp
from neutron.common import config as common_config
from neutron.common import constants

from calico.datamodel_v1 import dir_for_host
from calico.etcdutils import EtcdWatcher
from calico.felix.fetcd import safe_decode_json

from networking_calico.agent.linux.dhcp import DnsmasqRouted

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.DEBUG)
LOG.addHandler(logging.StreamHandler())

NETWORK_ID = 'calico'


class FakePlugin(object):
    """Fake plugin class.

    This class exists to support various calls that
    neutron.agent.linux.dhcp.Dnsmasq makes to what it thinks is the
    Neutron database (aka the plugin).

    The calls are create_dhcp_port, update_dhcp_port and
    release_dhcp_port, and the docstring for each corresponding method
    below indicates how they are called.

    Because this class doesn't speak to the real Neutron database, it
    follows that the DHCP interface that we create on each compute
    host does not show up as a port in the Neutron database.  That
    doesn't matter, because we don't allocate a unique IP for each
    DHCP port, and hence don't consume any IPs that the Neutron
    database ought to know about.
    """

    def create_dhcp_port(self, port):
        """Support the following DHCP DeviceManager calls.

        dhcp_port = self.plugin.create_dhcp_port({'port': port_dict})
        """
        LOG.debug("create_dhcp_port: %s", port)
        port['port']['id'] = 'dhcp'
        port['port']['mac_address'] = '00:11:22:33:44:55'
        port['port']['device_owner'] = constants.DEVICE_OWNER_DHCP
        return dhcp.DictModel(port['port'])

    def update_dhcp_port(self, port_id, port):
        """Support the following DHCP DeviceManager calls.

        dhcp_port = self.plugin.update_dhcp_port(
            port.id, {'port': {'network_id': network.id,
                               'fixed_ips': port_fixed_ips}})

        dhcp_port = self.plugin.update_dhcp_port(
            port.id, {'port': {'network_id': network.id,
                               'device_id': device_id}})
        """
        LOG.debug("update_dhcp_port: %s %s", port_id, port)

    def release_dhcp_port(self, network_id, device_id):
        """Support the following DHCP DeviceManager calls.

        self.plugin.release_dhcp_port(network.id,
                                      self.get_device_id(network))
        """
        LOG.debug("release_dhcp_port: %s %s", network_id, device_id)


class CalicoEtcdWatcher(EtcdWatcher):

    def __init__(self, agent):
        super(CalicoEtcdWatcher, self).__init__(
            'localhost:4001',
            dir_for_host(socket.gethostname()) + "/workload"
        )
        self.agent = agent
        self.register_path(
            "/calico/v1/host/<hostname>/workload/<orchestrator>" +
            "/<workload_id>/endpoint/<endpoint_id>",
            on_set=self.on_endpoint_set,
            on_del=self.on_endpoint_delete
        )

    """
    Calico network ID.

    Although there can in general be multiple networks and multiple
    subnets per network that need DHCP on a particular compute host,
    there's actually nothing that depends on how the subnets are
    partitioned across networks, and a single instance of Dnsmasq is
    quite capable of providing DHCP for many subnets.

    Therefore we model the DHCP requirement using a single network
    with ID 'calico', and many subnets within that network.
    """
    NETWORK_ID = 'calico'

    def on_endpoint_set(self, response, hostname, orchestrator,
                        workload_id, endpoint_id):
        """Handler for endpoint creations and updates.

        Endpoint data is, for example:

        { 'state': 'active' or 'inactive',
          'name': port['interface_name'],
          'mac': port['mac_address'],
          'profile_ids': port['security_groups'],
          'ipv4_nets': ['10.28.0.2/32'],
          'ipv4_gateway': '10.28.0.1',
          'ipv6_nets': ['2001:db8:1::2/128'],
          'ipv6_gateway': '2001:db8:1::1' }

        Port properties needed by DHCP code are:

        { 'id': <unique ID>,
          'network_id': <network ID>,
          'device_owner': 'calico',
          'device_id': <Linux interface name>,
          'fixed_ips': [ { 'subnet_id': <subnet ID>,
                           'ip_address': '10.28.0.2' } ],
          'mac_address: <MAC address>,
          'extra_dhcp_opts': ... (optional) }

        Network properties are:

        { 'subnets': [ <subnet object> ],
          'id': <network ID>,
          'namespace': None,
          'ports: [ <port object> ],
          'tenant_id': ? }

        Subnet properties are:

        { 'enable_dhcp': True,
          'ip_version': 4 or 6,
          'cidr': '10.28.0.0/24',
          'dns_nameservers': [],
          'id': <subnet ID>,
          'gateway_ip': <gateway IP address>,
          'host_routes': [] }
        """
        # Get the global network object, creating it if it doesn't
        # already exist.
        net = self.agent.cache.get_network_by_id(NETWORK_ID)
        if not net:
            net = dhcp.NetModel(False,
                                {"id": NETWORK_ID,
                                 "subnets": [],
                                 "ports": []})
            self.agent.cache.put(net)

        # Get the endpoint data.
        endpoint = safe_decode_json(response.value, 'endpoint')

        # Set up subnet objects.
        subnets_changed = False
        fixed_ips = []
        for ip_version in [4, 6]:
            nets_key = 'ipv%s_nets' % ip_version
            gateway_key = 'ipv%s_gateway' % ip_version
            # FIXME: don't hardcode prefix length.
            subnet_suffix = '/24' if ip_version == 4 else '/80'

            if gateway_key in endpoint:
                # Construct the CIDR for this endpoint's IPv? subnet.
                cidr = str(netaddr.IPNetwork(endpoint[gateway_key] +
                                             subnet_suffix))

                # See if we already have this subnet.
                if [s for s in net.subnets if s.cidr == cidr]:
                    # Yes, no change needed to network object.
                    pass
                else:
                    # No, update the network object with a new subnet.
                    subnet = {'enable_dhcp': True,
                              'ip_version': ip_version,
                              'cidr': cidr,
                              'dns_nameservers': [],
                              'id': cidr,
                              'gateway_ip': endpoint[gateway_key],
                              'host_routes': []}
                    net = dhcp.NetModel(False,
                                        {"id": net.id,
                                         "subnets": net.subnets + [subnet],
                                         "ports": net.ports,
                                         "tenant_id": "calico"})
                    self.agent.cache.put(net)
                    subnets_changed = True

                # Generate the fixed IPs for the endpoint on this subnet.
                fixed_ips += [{'subnet_id': cidr,
                               'ip_address': n.split('/')[0]}
                              for n in endpoint[nets_key]]

        LOG.debug("net: %s", net)
        if subnets_changed:
            self.agent.call_driver('restart', net)

        # Construct port equivalent of endpoint data.
        port = {'id': endpoint_id,
                'network_id': NETWORK_ID,
                'device_owner': 'calico',
                'device_id': endpoint['name'],
                'fixed_ips': fixed_ips,
                'mac_address': endpoint['mac'],
                'extra_dhcp_opts': []}
        self.agent.cache.put_port(dhcp.DictModel(port))
        LOG.debug("port: %s", port)

        self.agent.call_driver('reload_allocations', net)

    def on_endpoint_delete(self, response, hostname, orchestrator,
                           workload_id, endpoint_id):
        """Handler for endpoint deletion."""
        # Find the corresponding port in the DHCP agent's cache.
        port = self.agent.cache.get_port_by_id(endpoint_id)
        if port:
            net = self.agent.cache.get_network_by_port_id(endpoint_id)
            self.agent.cache.remove_port(port)
            self.agent.call_driver('reload_allocations', net)

    def _on_snapshot_loaded(self, etcd_snapshot_response):
        """Called whenever a snapshot is loaded from etcd."""

        # Reset the cache.
        LOG.debug("Reset cache for new snapshot")
        self.agent.cache = NetworkCache()

        # Now pass each snapshot node through the dispatcher, which
        # means that on_endpoint_set will be called for each endpoint.
        for etcd_node in etcd_snapshot_response.leaves:
            etcd_node.action = 'set'
            self.dispatcher.handle_event(etcd_node)

        LOG.debug("End of new snapshot")


class CalicoDhcpAgent(DhcpAgent):
    """Calico DHCP agent.

    This DHCP agent subclasses and overrides the standard Neutron DHCP
    agent so as to be driven by etcd endpoint data - instead of by
    Neutron RPC network, subnet and port messages - and so as not to
    provide any agent status reporting back to the Neutron server.
    This is because we have observed that the RPC exchanges between
    DHCP agents and the Neutron server will exhaust the latter once
    there are more than a few hundred agents running.
    """
    def __init__(self):
        super(CalicoDhcpAgent, self).__init__(host=socket.gethostname())

        # Override settings that Calico's DHCP agent use requires.
        self.conf.set_override('enable_isolated_metadata', False)
        self.conf.set_override('use_namespaces', False)
        self.conf.set_override(
            'interface_driver',
            'networking_calico.agent.linux.interface.RoutedInterfaceDriver'
        )

        # Override the DHCP driver class - networking-calico's
        # DnsmasqRouted class.
        self.dhcp_driver_cls = DnsmasqRouted

        # Override the RPC plugin (i.e. proxy to the Neutron database)
        # with a fake plugin.  The DHCP driver code calls when it
        # wants to tell Neutron that it is creating, updating or
        # releasing the DHCP port.
        self.plugin_rpc = FakePlugin()

        # Watch etcd for any endpoint changes for this host.
        self.etcd = CalicoEtcdWatcher(self)

    def run(self):
        """Run the EtcdWatcher loop."""
        self.etcd.loop()


def main():
    register_options(cfg.CONF)
    common_config.init(sys.argv[1:])
    config.setup_logging()
    agent = CalicoDhcpAgent()
    agent.run()
