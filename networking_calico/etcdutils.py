# Copyright (c) 2016, 2018 Tigera, Inc. All rights reserved.

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

import collections
import eventlet
import functools
import json
import logging
import re
from types import StringTypes

from networking_calico import etcdv3
from networking_calico.monotonic import monotonic_time

_log = logging.getLogger(__name__)

# Map etcd event actions to the effects we care about.
ACTION_MAPPING = {
    "set": "set",
    "compareAndSwap": "set",
    "create": "set",
    "update": "set",

    "delete": "delete",
    "compareAndDelete": "delete",
    "expire": "delete",
}
WATCH_TIMEOUT_SECS = 10


class PathDispatcher(object):
    def __init__(self):
        self.handler_root = {}

    def register(self, path, on_set=None, on_del=None):
        _log.info("Registering path %s set=%s del=%s", path, on_set, on_del)
        parts = path.strip("/").split("/")
        node = self.handler_root
        for part in parts:
            m = re.match(r'<(.*)>', part)
            if m:
                capture_name = m.group(1)
                name, node = node.setdefault("capture", (capture_name, {}))
                assert name == capture_name, (
                    "Conflicting capture name %s vs %s" % (name, capture_name)
                )
            else:
                node = node.setdefault(part, {})
        if on_set:
            node["set"] = on_set
        if on_del:
            node["delete"] = on_del

    def handle_event(self, response):
        """handle_event

        :param EtcdResponse: A python-etcd response object for a watch.
        """
        _log.debug("etcd event %s for key %s", response.action, response.key)
        key_parts = response.key.strip("/").split("/")
        self._handle(key_parts, response, self.handler_root, {})

    def _handle(self, key_parts, response, handler_node, captures):
        while key_parts:
            next_part = key_parts.pop(0)
            if "capture" in handler_node:
                capture_name, handler_node = handler_node["capture"]
                captures[capture_name] = next_part
            elif next_part in handler_node:
                handler_node = handler_node[next_part]
            else:
                _log.debug("No matching sub-handler for %s", response.key)
                return
        # We've reached the end of the key.
        action = ACTION_MAPPING.get(response.action)
        if action in handler_node:
            _log.debug("Found handler for event %s for %s, captures: %s",
                       action, response.key, captures)
            handler_node[action](response, **captures)
        else:
            _log.debug("No handler for event %s on %s. Handler node %s.",
                       action, response.key, handler_node)


Response = collections.namedtuple(
    'Response', ['action', 'key', 'value']
)


class EtcdWatcher(object):
    """A class that watches an etcdv3 subtree.

    Entrypoints:
    - EtcdWatcher() (constructor)
    - watcher.start()
    - watcher.stop()
    """

    def __init__(self, prefix):
        _log.debug("Creating EtcdWatcher for %s", prefix)
        self.prefix = prefix
        self.dispatcher = PathDispatcher()
        self._stopped = False

    def register_path(self, *args, **kwargs):
        self.dispatcher.register(*args, **kwargs)

    def _pre_snapshot_hook(self):
        return None

    def _post_snapshot_hook(self, _):
        pass

    def start(self):
        _log.info("Start watching %s", self.prefix)
        self._stopped = False

        while not self._stopped:
            # Get the current etcdv3 cluster ID and revision, so (a) we can
            # detect if the cluster ID changes, and (b) we know when to start
            # watching from.
            cluster_id, last_revision = etcdv3.get_status()
            last_revision = int(last_revision)
            _log.info("Current cluster_id %s, revision %d",
                      cluster_id, last_revision)

            # Allow subclass to do pre-snapshot processing, and to return any
            # data that it will need for reconciliation after the snapshot.
            snapshot_data = self._pre_snapshot_hook()

            # Get all existing values and process them through the dispatcher.
            for result in etcdv3.get_prefix(self.prefix):
                key, value, _ = result
                # Convert to what the dispatcher expects - see below.
                response = Response(
                    action='set',
                    key=key,
                    value=value,
                )
                _log.info("status event: %s", response)
                self.dispatcher.handle_event(response)

            # Allow subclass to do post-snapshot reconciliation.
            self._post_snapshot_hook(snapshot_data)

            # Now watch for any changes, starting after the revision above.
            while not self._stopped:
                try:
                    # Check for cluster ID changing.
                    cluster_id_now, _ = etcdv3.get_status()
                    _log.debug("Now cluster_id is %s", cluster_id_now)
                    if cluster_id_now != cluster_id:
                        _log.info("Cluster ID changed, resync")
                        break

                    # Start a watch from just after the last known revision.
                    event_stream, cancel = etcdv3.watch_subtree(
                        self.prefix,
                        str(last_revision + 1))
                except Exception:
                    # Log and handle by breaking out to the wider loop, which
                    # means we'll get the tree again and then try watching
                    # again.  E.g. it could be that the DB has just been
                    # compacted and so the revision is no longer available that
                    # we asked to start watching from.
                    _log.exception("Exception watching status tree")
                    break

                # Record time of last activity on the successfully created
                # watch.  (This is updated below as we see watch events.)
                last_event_time = monotonic_time()

                def _cancel_watch_if_inactive():
                    # Loop until we should cancel the watch, either because of
                    # inactivity or because of stop() having been called.
                    while not self._stopped:
                        time_to_next_timeout = (last_event_time +
                                                WATCH_TIMEOUT_SECS -
                                                monotonic_time())
                        _log.debug("Time to next timeout is %ds",
                                   time_to_next_timeout)
                        if time_to_next_timeout < 1:
                            break
                        else:
                            # Sleep until when we might next have to cancel
                            # (but won't if a watch event has occurred in the
                            # meantime).
                            eventlet.sleep(time_to_next_timeout)

                    # Cancel the watch
                    cancel()
                    return

                # Spawn a greenlet to cancel the watch if it's inactive, or if
                # stop() is called.  Cancelling the watch adds None to the
                # event stream, so the following for loop will see that.
                eventlet.spawn(_cancel_watch_if_inactive)

                for event in event_stream:
                    _log.debug("Event: %s", event)
                    last_event_time = monotonic_time()

                    # If the EtcdWatcher has been stopped, return from the
                    # whole loop.
                    if self._stopped:
                        _log.info("EtcdWatcher has been stopped")
                        return

                    # Otherwise a None event means that the watch has been
                    # cancelled owing to inactivity.  In that case we break out
                    # from this loop, and the watch will be restarted.
                    if event is None:
                        _log.debug("Watch cancelled owing to inactivity")
                        break

                    # Convert v3 event to form that the dispatcher expects;
                    # namely an object response, with:
                    # - response.key giving the etcd key
                    # - response.action being "set" or "delete"
                    # - whole response being passed on to the handler method.
                    # Handler methods here expect
                    # - response.key
                    # - response.value
                    response = Response(
                        action=event.get('type', 'SET').lower(),
                        key=event['kv']['key'],
                        value=event['kv'].get('value', ''),
                    )
                    _log.info("Event: %s", response)
                    self.dispatcher.handle_event(response)

                    # Update last known revision.
                    mod_revision = int(event['kv'].get('mod_revision', '0'))
                    if mod_revision > last_revision:
                        last_revision = mod_revision
                        _log.info("Last known revision is now %d",
                                  last_revision)

    """
    Example status events:

    status event: {u'kv': {
        u'mod_revision': u'4',
        u'value': '{
            "time":"2017-12-31T14:09:29Z",
            "uptime":392.5231995,
            "first_update":true
        }',
        u'create_revision': u'4',
        u'version': u'1',
        u'key': '/calico/felix/v1/host/ubuntu-xenial-rax-dfw-0001640133/status'
    }}

    status event: {u'type': u'DELETE',
                   u'kv': {
        u'mod_revision': u'88',
        u'key': '/calico/felix/v1/host/ubuntu-xenial-rax-dfw-0001640133/' +
                'workload/openstack/' +
                'openstack%2f84a5e464-c2be-4bfd-926b-96030421999d/endpoint/' +
                '84a5e464-c2be-4bfd-926b-96030421999d'
    }}

    status event: {u'kv': {
        u'mod_revision': u'113',
        u'value': '{"status":"down"}',
        u'create_revision': u'113',
        u'version': u'1',
        u'key': '/calico/felix/v1/host/ubuntu-xenial-rax-dfw-0001640133/' +
                'workload/openstack/' +
                'openstack%2f8ae2181b-8aab-4b49-8242-346f6a0b21e5/endpoint/' +
                '8ae2181b-8aab-4b49-8242-346f6a0b21e5'
    }}
    """

    def stop(self):
        _log.info("Stop watching status tree")
        self._stopped = True


def intern_dict(d, fields_to_intern=None):
    """intern_dict

    Return a copy of the input dict where all its string/unicode keys
    are interned, optionally interning some of its values too.

    Caveat: assumes that it is safe to convert the keys and interned values
    to str by calling .encode("utf8") on each string.

    :param dict[StringTypes,...] d: Input dict.
    :param set[StringTypes] fields_to_intern: set of field names whose values
        should also be interned.
    :return: new dict with interned keys/values.
    """
    fields_to_intern = fields_to_intern or set()
    out = {}
    for k, v in d.iteritems():
        # We can't intern unicode strings, as returned by etcd but all our
        # keys should be ASCII anyway.  Use the utf8 encoding just in case.
        k = intern(k.encode("utf8"))
        if k in fields_to_intern:
            if isinstance(v, StringTypes):
                v = intern(v.encode("utf8"))
            elif isinstance(v, list):
                v = intern_list(v)
        out[k] = v
    return out


def intern_list(l):
    """intern_list

    Returns a new list with interned versions of the input list's contents.

    Non-strings are copied to the new list verbatim.  Returned strings are
    encoded using .encode("utf8").
    """
    out = []
    for item in l:
        if isinstance(item, StringTypes):
            item = intern(item.encode("utf8"))
        out.append(item)
    return out


# Intern JSON keys as we load them to reduce occupancy.
FIELDS_TO_INTERN = set([
    # Endpoint dicts.  It doesn't seem worth interning items like the MAC
    # address or TAP name, which are rarely (if ever) shared.
    "profile_id",
    "profile_ids",
    "state",
    "ipv4_gateway",
    "ipv6_gateway",

    # Rules dicts.
    "protocol",
    "!protocol",
    "src_tag",
    "!src_tag",
    "dst_tag",
    "!dst_tag",
    "action",
])
json_decoder = json.JSONDecoder(
    object_hook=functools.partial(intern_dict,
                                  fields_to_intern=FIELDS_TO_INTERN)
)


def safe_decode_json(raw_json, log_tag=None):
    try:
        return json_decoder.decode(raw_json)
    except (TypeError, ValueError):
        _log.warning("Failed to decode JSON for %s: %r.  Returning None.",
                     log_tag, raw_json)
        return None
