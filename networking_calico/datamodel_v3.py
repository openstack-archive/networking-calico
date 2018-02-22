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

import json
import uuid

from networking_calico.compat import log
from networking_calico import etcdv3
from networking_calico.timestamp import timestamp_now


# Particular JSON key strings.
CLUSTER_GUID = 'clusterGUID'
CLUSTER_TYPE = 'clusterType'
DATASTORE_READY = 'datastoreReady'
ENDPOINT_REPORTING_ENABLED = 'endpointReportingEnabled'
INTERFACE_PREFIX = 'interfacePrefix'


# Annotation keys.
ANN_KEY_PREFIX = 'openstack.projectcalico.org/'
ANN_KEY_FQDN = ANN_KEY_PREFIX + 'fqdn'
ANN_KEY_NETWORK_ID = ANN_KEY_PREFIX + 'network-id'


LOG = log.getLogger(__name__)


def put(resource_kind, name, spec, annotations={}, labels=None,
        mod_revision=None):
    """Write a Calico v3 resource to etcdv3.

    - resource_kind (string): E.g. WorkloadEndpoint, Profile, etc.

    - name (string): The resource's name.  This is used to form its etcd key,
      and also goes in its .Metadata.Name field.

    - spec (dict): Resource spec, as a dict with keys as specified by the
      'json:' comments in the relevant golang struct definition (for example,
      https://github.com/projectcalico/libcalico-go/blob/master/
      lib/apis/v3/workloadendpoint.go#L38).

    - annotations (dict): Annotations to set on the resource.  These are merged
      with existing annotations; i.e. existing annotations with other keys are
      unchanged, and existing annotations with the same keys are overwritten by
      these new values.

    - mod_revision (string): If specified, indicates that the write should only
      proceed if replacing an existing value with that mod_revision.

    Returns True if the write happened successfully; False if not.
    """
    key = _build_key(resource_kind, name)
    value = None
    try:
        # Get the existing resource so we can persist its metadata.
        value, _ = _get_with_metadata(resource_kind, name)
    except etcdv3.KeyNotFound:
        pass
    except ValueError:
        LOG.warning("etcd value not valid JSON, so ignoring")
    if value is None:
        # Build basic resource structure.
        value = {
            'kind': resource_kind,
            'apiVersion': 'projectcalico.org/v3',
            'metadata': {
                'name': name,
            },
        }
    # Ensure namespace set, for a namespaced resource.
    if _is_namespaced(resource_kind):
        value['metadata']['namespace'] = 'openstack'
    # Ensure that there is a creation timestamp.
    if 'creationTimestamp' not in value['metadata']:
        value['metadata']['creationTimestamp'] = timestamp_now()
    # Ensure that there is a UID.
    if 'uid' not in value['metadata']:
        value['metadata']['uid'] = uuid.uuid4().get_hex()
    # Set annotations and labels if specified.  (We previously used to merge
    # here, instead of overwriting, but (a) for annotations there is actually
    # no use case for that, because we only use annotations on endpoints for
    # which Neutron is the sole source of truth; and (b) for the use case where
    # labels are used to represent security group membership it is crucial that
    # we overwrite and don't merge; otherwise a VM could never be removed from
    # a security group.)
    if annotations:
        value['metadata']['annotations'] = annotations
    if labels:
        value['metadata']['labels'] = labels
    # Set the new spec (overriding whatever may already be there).
    value['spec'] = spec
    return etcdv3.put(key, json.dumps(value), mod_revision=mod_revision)


def get(resource_kind, name):
    """Read spec of a Calico v3 resource from etcdv3.

    - resource_kind (string): E.g. WorkloadEndpoint, Profile, etc.

    - name (string): The resource's name, which is used to form its etcd key.

    Returns (spec, mod_revision) where

    - spec is the resource spec as a dict with keys as specified by the 'json:'
      comments in the relevant golang struct definition (for example,
      https://github.com/projectcalico/libcalico-go/blob/master/
      lib/apis/v3/workloadendpoint.go#L38).

    - mod_revision is the etcdv3 revision at which the resource was last
      modified.

    Raises etcdv3.KeyNotFound if there is no resource with that kind and name.
    """
    value, mod_revision = _get_with_metadata(resource_kind, name)
    return value['spec'], mod_revision


def get_all(resource_kind, with_labels_and_annotations=False):
    """Read all Calico v3 resources of a certain kind from etcdv3.

    - resource_kind (string): E.g. WorkloadEndpoint, Profile, etc.

    - with_labels_and_annotations: If True, indicates to return the labels and
      annotations for each resource as well as the spec.

    Returns a list of tuples (name, spec, mod_revision) or (name, (spec,
    labels, annotations), mod_revision), one for each resource of the specified
    kind, in which:

    - name is the resource's name (a string)

    - spec is a dict with keys as specified by the 'json:' comments in the
      relevant golang struct definition (for example,
      https://github.com/projectcalico/libcalico-go/blob/master/
      lib/apis/v3/workloadendpoint.go#L38).

    - labels is a dict containing the resource's labels

    - annotations is a dict containing the resource's annotations

    - mod_revision is the revision at which that resource was last modified (an
      integer represented as a string).
    """
    prefix = _build_key(resource_kind, '')
    results = etcdv3.get_prefix(prefix)
    tuples = []
    for result in results:
        key, value, mod_revision = result
        name = key.split('/')[-1]

        # Decode the value.
        spec = labels = annotations = None
        try:
            value_dict = json.loads(value)
            LOG.debug("value dict: %s", value_dict)
            spec = value_dict['spec']
            labels = value_dict['metadata'].get('labels', {})
            annotations = value_dict['metadata'].get('annotations', {})
        except ValueError:
            # When the value is not valid JSON, we still return a tuple for
            # this key, with spec, labels and annotations all as None.  This is
            # so that the caller can correctly differentiate between
            # overwriting an existing value (which => a transaction with
            # specified mod_revision) and creating a key that did not exist
            # before (=> a transaction with version 0).
            LOG.warning("etcd value not valid JSON (%s)", value)

        if with_labels_and_annotations:
            t = (name, (spec, labels, annotations), mod_revision)
        else:
            t = (name, spec, mod_revision)
        tuples.append(t)
    return tuples


def delete(resource_kind, name, mod_revision=None):
    """Delete a Calico v3 resource from etcdv3.

    - resource_kind (string): E.g. WorkloadEndpoint, Profile, etc.

    - name (string): The resource's name, which is used to form its etcd key.

    Returns True if the deletion was successful; False if not.
    """
    key = _build_key(resource_kind, name)
    return etcdv3.delete(key, mod_revision=mod_revision)


def _is_namespaced(resource_kind):
    if resource_kind == "WorkloadEndpoint":
        return True
    if resource_kind == "NetworkPolicy":
        return True
    return False


def _plural(resource_kind):
    if resource_kind == "NetworkPolicy":
        return "NetworkPolicies"
    if resource_kind == "GlobalNetworkPolicy":
        return "GlobalNetworkPolicies"
    return resource_kind + "s"


def _build_key(resource_kind, name):
    if _is_namespaced(resource_kind):
        # Use 'openstack' as the namespace.
        template = "/calico/resources/v3/projectcalico.org/%s/openstack/%s"
    else:
        template = "/calico/resources/v3/projectcalico.org/%s/%s"
    return template % (_plural(resource_kind).lower(), name)


def _get_with_metadata(resource_kind, name):
    # Note: 'with_metadata' here means including the Calico data model
    # metadata, as well as the etcdv3 mod_revision.
    key = _build_key(resource_kind, name)
    value_as_string, mod_revision = etcdv3.get(key)
    value = json.loads(value_as_string)
    return value, mod_revision
