# Copyright (c) 2015 Cisco Systems.  All Rights Reserved.
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

from neutron.db import l3_db
from neutron.plugins.ml2.plugin import Ml2Plugin
from neutron_lib import exceptions as exc
from oslo_utils import excutils

try:
    # Icehouse, Juno
    from neutron.openstack.common import log
except ImportError:
    # Kilo
    from oslo_log import log

try:
    # Icehouse, Juno
    from oslo.config import cfg
except ImportError:
    # Kilo
    from oslo_config import cfg

LOG = log.getLogger(__name__)


class CalicoPlugin(Ml2Plugin, l3_db.L3_NAT_db_mixin):
    def __init__(self):
        # Add the ability to handle floating IPs.
        self._supported_extension_aliases.extend(["router"])

        # Set ML2 options so the user doesn't have to.
        LOG.info("Forcing ML2 mechanism_drivers to 'calico'")
        cfg.CONF.set_override('mechanism_drivers', ['calico'], group='ml2')
        LOG.info("Forcing ML2 type_drivers to 'local, flat'")
        cfg.CONF.set_override('type_drivers', ['local', 'flat'], group='ml2')
        LOG.info("Forcing ML2 tenant_network_types to 'local'")
        cfg.CONF.set_override('tenant_network_types', ['local'], group='ml2')

        super(CalicoPlugin, self).__init__()

    # Intercept floating IP associates/disassociates so we can trigger an
    # appropriate endpoint update.
    def _update_floatingip(self, context, id, floatingip):
        old_floatingip, new_floatingip = super(
            CalicoPlugin, self)._update_floatingip(context, id, floatingip)

        if new_floatingip['port_id']:
            context.fip_update_port_id = new_floatingip['port_id']
            self.mechanism_manager._call_on_drivers('update_floatingip',
                                                    context)
        if old_floatingip['port_id']:
            context.fip_update_port_id = old_floatingip['port_id']
            self.mechanism_manager._call_on_drivers('update_floatingip',
                                                    context)

        return old_floatingip, new_floatingip

    def _update_dhcp_port(self, context, subnet, delete=False):
        device_id = 'dhcp-' + subnet['network_id']

        dhcp_port = None
        ports = self.get_ports(context, filters={'device_id': device_id})
        if len(ports) > 1:
            raise exc.Conflict()
        elif ports:
            dhcp_port = ports[0]

        if dhcp_port:
            fixed_ips = dhcp_port['fixed_ips']
            if delete:
                fixed_ips = [fip for fip in fixed_ips
                             if fip['subnet_id'] != subnet['id']]
            else:
                fixed_ips.append({'subnet_id': subnet['id']})

            if fixed_ips:
                return self.update_port(
                    context, dhcp_port['id'], {'port': {
                        'fixed_ips': fixed_ips,
                    }})
            else:
                self.delete_port(dhcp_port['id'])
        else:
            port_dict = dict(
                name='',
                admin_state_up=True,
                device_id=device_id,
                network_id=subnet['network_id'],
                tenant_id=subnet['tenant_id'],
                fixed_ips=[dict(subnet_id=subnet['id'])],
            )
            return self.create_port(context, {'port': port_dict})

    def create_subnet(self, context, subnet):
        subnet = super(CalicoPlugin, self).create_subnet(
            context, subnet)
        try:
            self._update_dhcp_port(context, subnet['network_id'])
        except Exception:
            with excutils.save_and_reraise_exception():
                self.delete_subnet(context, subnet['id'])
        return subnet

    def update_subnet(self, context, id_, subnet):
        old_subnet = self.get_subnet(context, id_)
        new_subnet = super(CalicoPlugin, self).update_subnet(
            context, id_, subnet)
        if new_subnet['enable_dhcp'] != old_subnet['enable_dhcp']:
            self._update_dhcp_port(context, new_subnet,
                                   delete=old_subnet['enable_dhcp'])
        return new_subnet

    def delete_subnet(self, context, id_):
        subnet = self.get_subnet(context, id_)
        self._update_dhcp_port(context, subnet, delete=True)
        return super(CalicoPlugin, self).delete_subnet(context, id_)
