# -*- coding: utf-8 -*-
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

from networking_calico import datamodel_v3
from networking_calico.plugins.ml2.drivers.calico.syncer import ResourceSyncer


class V3ResourceSyncer(ResourceSyncer):
    """Common logic for syncing Calico v3 data model resources.

    For v3 resources, the name is the metadata name field (which is also the
    last part of the full etcd key), and the data is the spec as a JSON-decoded
    dict.
    """

    def __init__(self, db, txn_from_context, resource_kind):
        super(V3ResourceSyncer, self).__init__(db,
                                               txn_from_context,
                                               resource_kind)

    def get_all_from_etcd(self):
        return datamodel_v3.get_all(self.resource_kind)

    def create_in_etcd(self, name, spec):
        return datamodel_v3.put(self.resource_kind, name, spec, mod_revision=0)

    def update_in_etcd(self, name, spec, mod_revision=None):
        return datamodel_v3.put(self.resource_kind, name, spec,
                                mod_revision=mod_revision)

    def delete_from_etcd(self, name, mod_revision):
        return datamodel_v3.delete(self.resource_kind, name,
                                   mod_revision=mod_revision)
