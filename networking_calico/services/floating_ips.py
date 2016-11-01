# Copyright (c) 2013 OpenStack Foundation.
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

from neutron_lib import constants as n_const
from oslo_config import cfg
from oslo_log import helpers as log_helpers
from oslo_utils import importutils

from neutron.db import common_db_mixin
from neutron.db import l3_db
from neutron.plugins.common import constants
from neutron.quota import resource_registry
from neutron import service
from neutron.services.l3_router.service_providers import driver_controller
from neutron.services import service_base


class CalicoFloatingIpsPlugin(service_base.ServicePluginBase,
                              common_db_mixin.CommonDbMixin,
                              l3_db.L3_NAT_dbonly_mixin):

    """Service plugin for floating IPs with Calico mechanism driver.

    This class implements a L3 service plugin that provides router and
    floatingip resources and manages associated request/response.

    All DB related work is implemented by l3_db.L3_NAT_dbonly_mixin.  Actual
    implementation of 'router' objects is a no-op for Calico.  Actual
    implementation of floating IPs is by adding the floating->fixed IP mapping
    to the Calico endpoint data for the target instance.
    """
    supported_extension_aliases = ["router"]

    @resource_registry.tracked_resources(router=l3_db.Router,
                                         floatingip=l3_db.FloatingIP)
    def __init__(self):
        super(CalicoFloatingIpsPlugin, self).__init__()

    @classmethod
    def get_plugin_type(cls):
        return 'calico-floating-ips'

    def get_plugin_description(self):
        return ("Service plugin for floating IPs with Calico.")

    def create_floatingip(self, context, floatingip,
            initial_status=lib_constants.FLOATINGIP_STATUS_ACTIVE):

        # Do the creation in the Neutron DB.
        floatingip_dict = super(CalicoFloatingIpsPlugin,
                                self).create_floatingip(context,
                                                        id,
                                                        floatingip,
                                                        initial_status)

        # Get the port the floating IP is pointing to.
        new_port_id = floatingip_dict.get('port_id')

        # Notify mech drivers to process floating IPs for the new port.
        if new_port_id:
            self._notify_mech_drivers(context, new_port_id)

        return floatingip_dict

    def update_floatingip(self, context, id, floatingip):

        # Get the port that the floating IP is currently pointing to.
        floatingip_dict = self.get_floatingip(context, id)
        old_port_id = floatingip_dict.get('port_id')

        # Do the update in the Neutron DB.
        floatingip_dict = super(CalicoFloatingIpsPlugin,
                                self).update_floatingip(context,
                                                        id,
                                                        floatingip)

        # Get the port the floating IP is pointing to now.
        new_port_id = floatingip_dict.get('port_id')

        # Notify mech drivers to process floating IPs for the new port.
        if new_port_id:
            self._notify_mech_drivers(context, new_port_id)

        # If the port has changed, also notify mech drivers to process floating
        # IPs for the old port.
        if old_port_id and old_port_id != new_port_id:
            self._notify_mech_drivers(context, old_port_id)

        return floatingip_dict

    def _notify_mech_drivers(self, context, port_id):
        context.fip_update_port_id = port_id
        self.mechanism_manager._call_on_drivers('update_floatingip', context)
