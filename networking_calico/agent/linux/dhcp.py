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

import copy
import re
import sys
import time

from neutron.agent.linux import dhcp
from oslo_log import log as logging

LOG = logging.getLogger(__name__)


class DnsmasqRouted(dhcp.Dnsmasq):
    """Dnsmasq DHCP driver for routed virtual interfaces."""

    def __init__(self, conf, network, process_monitor,
                 version=None, plugin=None):
        super(DnsmasqRouted, self).__init__(conf, network, process_monitor,
                                            version, plugin)
        self.device_manager = CalicoDeviceManager(self.conf, plugin)

    def _build_cmdline_callback(self, pid_file):
        cmd = super(DnsmasqRouted, self)._build_cmdline_callback(pid_file)

        # Replace 'static' by 'static,off-link' in all IPv6
        # --dhcp-range options.
        prog = re.compile('(--dhcp-range=set:[^,]+,[0-9a-f:]+),static,(.*)')
        for option in copy.copy(cmd):
            m = prog.match(option)
            if m:
                cmd.remove(option)
                cmd.append(m.group(1) + ',static,off-link,' + m.group(2))

        # Add '--enable-ra'.
        cmd.append('--enable-ra')

        # Enumerate precisely the TAP interfaces to listen on.
        cmd.remove('--interface=tap*')
        cmd.remove('--bridge-interface=%s,tap*' % self.interface_name)
        bridge_option = '--bridge-interface=%s' % self.interface_name
        for port in self.network.ports:
            if port.device_id.startswith('tap'):
                LOG.debug('Listen on %s', port.device_id)
                cmd.append('--interface=%s' % port.device_id)
                bridge_option = bridge_option + ',' + port.device_id
        cmd.append(bridge_option)

        return cmd

    def _destroy_namespace_and_port(self):
        try:
            self.device_manager.destroy(self.network, self.interface_name)
        except RuntimeError:
            LOG.warning('Failed trying to delete interface: %s',
                        self.interface_name)

    def _spawn_or_reload_process(self, reload_with_HUP):
        self.device_manager.fill_dhcp_udp_checksums(None)
        super(DnsmasqRouted, self)._spawn_or_reload_process(reload_with_HUP)


class CalicoDeviceManager(dhcp.DeviceManager):
    """Device manager for the default namespace that Calico operates in."""

    def _set_default_route(self, network, device_name):
        pass

    def _cleanup_stale_devices(self, network, dhcp_port):
        pass

    def fill_dhcp_udp_checksums(self, *args, **kwargs):
        retries = 10
        while retries > 0:
            try:
                super(CalicoDeviceManager, self).fill_dhcp_udp_checksums(
                    *args, **kwargs)
            except RuntimeError:
                # fill_dhcp_udp_checksums() can fail transiently if iptables
                # is modified concurrently, especially with an aggressive
                # iptables writer such as Felix running.
                LOG.exception("Failed to insert checksum rule, may retry...")
                time.sleep(0.1)
                retries -= 1
            else:
                LOG.debug("Inserted DHCP checksum rule.")
                break
        else:
            LOG.error("Failed to insert DHCP checksum rule. Exiting...")
            sys.exit(1)
