# -*- coding: utf-8 -*-
#
# Copyright (c) 2019 Salesforce.com
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

import eventlet
from neutron_lib import worker as neutron_worker
import psutil

from networking_calico.common import config as calico_config
from networking_calico.compat import cfg
from networking_calico.compat import log
from networking_calico import datamodel_v2
from networking_calico.plugins.ml2.drivers.calico.election import Elector
from networking_calico.plugins.ml2.drivers.calico import status

LOG = log.getLogger(__name__)

MASTER_CHECK_INTERVAL_SECS = 5

# Set a low refresh interval on the master key.  This reduces the chance of
# the etcd event buffer wrapping while non-masters are waiting for the key to
# be refreshed.
MASTER_REFRESH_INTERVAL = 10
MASTER_TIMEOUT = 60


# Elector configuration;
worker_opt = cfg.BoolOpt(
    'fork_workers', default=True,
    help="Fork plugin workers instead of using eventlet threads"
)

# Register Calico related configuration options
calico_config.register_options(cfg.CONF, additional_options=[worker_opt])


def _num_workers():
    # On systems with <4GB of ram, use eventlet threads
    if psutil.virtual_memory() < (4*1024*1024*1024):
        return 0
    # Else, use fork
    return 1


class CalicoWorker(neutron_worker.BaseWorker):

    def __init__(self, mech_driver):
        LOG.debug('calling CalicoWorker init')
        eventlet.greenthread.GreenThread.cancel(mech_driver._post_init_thread)
        super(CalicoWorker, self).__init__(
            worker_process_count=int(cfg.CONF.calico.fork_workers)
        )
        self._name = "neutron-calico"
        self._desc = self.__class__.__name__
        self._mech_driver = mech_driver
        self._time_to_stop = False
        self._etcd_watcher = None
        self._etcd_watcher_thread = None
        self._elector = None

    def start(self):
        LOG.debug('calling CalicoWorker start')
        try:
            super(CalicoWorker, self).start(name=self._name,
                                            desc=self._desc)
        except TypeError:
            super(CalicoWorker, self).start()

        self._mech_driver._post_fork_init(worker_mode=self._worker_mode)
        if self._elector is None:
            self._elector = Elector(cfg.CONF.calico.elector_name,
                                    self._election_key,
                                    interval=MASTER_REFRESH_INTERVAL,
                                    ttl=MASTER_TIMEOUT)

        LOG.debug('CalicoWorker beginning election loop')
        while not self._time_to_stop:
            # Only handle updates if we are the master node.
            if self._elector.master():
                if self._etcd_watcher is None:
                    self._start()
                elif not self._etcd_watcher_thread:
                    LOG.error("%s %s died", self._name, self._etcd_watcher)
                    self._stop()
                    eventlet.sleep(0.01)
                    self._start()
            else:
                if self._etcd_watcher is not None:
                    LOG.warning("No longer master, stopping %s", self._name)
                    self._stop()
            # Short sleep interval before we check if we've become
            # the master.
            eventlet.sleep(MASTER_CHECK_INTERVAL_SECS)
        self._stop()

    def _start(self):
        LOG.info("Became the master, starting %s", self._desc)
        self._etcd_watcher = self._thread_class(self._mech_driver)
        self._etcd_watcher_thread = eventlet.spawn(
            self._etcd_watcher.start
        )
        LOG.info("Started %s as %s", self._etcd_watcher,
                 self._etcd_watcher_thread)

    def wait(self):
        while self._etcd_watcher is not None:
            eventlet.sleep(0.1)

    def _stop(self):
        if self._etcd_watcher:
            self._etcd_watcher.stop()
            self._etcd_watcher = None

    def stop(self):
        self._time_to_stop = True
        self._stop()

    @staticmethod
    def reset():
        pass


class AgentCalicoWorker(CalicoWorker):

    def __init__(self, mech_driver):
        super(AgentCalicoWorker, self).__init__(mech_driver)
        self._thread_class = status.AgentStatusWatcher
        self._desc = "alive-status worker"
        self._worker_mode = "agent"
        self._election_key = datamodel_v2.neutron_election_key(
            calico_config.get_region_string()) + "_agent_worker"


class PortCalicoWorker(CalicoWorker):

    def __init__(self, mech_driver):
        super(PortCalicoWorker, self).__init__(mech_driver)
        self._thread_class = status.PortStatusWatcher
        self._desc = "port-status worker"
        self._worker_mode = "port"
        self._election_key = datamodel_v2.neutron_election_key(
            calico_config.get_region_string()) + "_port_worker"
