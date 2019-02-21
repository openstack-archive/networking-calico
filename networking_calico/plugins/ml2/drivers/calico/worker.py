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

import os
import time

import eventlet
from neutron.common import config
from neutron_lib import worker as neutron_worker

from networking_calico.common import config as calico_config
from networking_calico.compat import log
from networking_calico import datamodel_v2
from networking_calico.plugins.ml2.drivers.calico import status

LOG = log.getLogger(__name__)

MASTER_CHECK_INTERVAL_SECS = 5


class CalicoWorker(neutron_worker.BaseWorker):

    def __init__(self, mech_driver, worker_process_count=1):
        LOG.debug('calling CalicoWorker init')
        # TODO(dougwig) -- very simple 0 vs 1 here, based on running
        # a tiny machine or other.
        super(CalicoWorker, self).__init__(
            worker_process_count=worker_process_count
        )
        self._name = self.__class__.__name__
        self._mech_driver = mech_driver
        self._time_to_stop = False
        self._etcd_watcher = None
        self._etcd_watcher_thread = None

    def start(self):
        if os.getpid() == self._mech_driver._my_pid:
            raise WhatTheFuckError()

        LOG.debug("%s start" % self._name)  # xx: remove
        try:
            super(CalicoWorker, self).start(name="neutron-calico",
                                            desc=self._desc)
        except TypeError:
            super(CalicoWorker, self).start()

        LOG.debug("%s before post-fork init" % self._name)  # xx: remove
        self._mech_driver._post_fork_init(worker_mode=True,
                                          election_key=self._election_key)
        LOG.debug("%s after post-fork init" % self._name)  # xx: remove

        while not self._time_to_stop:
            LOG.debug("%s hello world" % self._name)  # xx: remove

            # Only handle updates if we are the master node.
            if self._mech_driver.elector.master():
                if self._etcd_watcher is None:
                    self._start()
                elif not self._etcd_watcher_thread:
                    LOG.error("%s %s died", self._name, self._etcd_watcher)
                    self._stop()
            else:
                if self._etcd_watcher is not None:
                    LOG.warning("No longer master, stopping %s", self._name)
                    self._stop()
            # Short sleep interval before we check if we've become
            # the master.
            eventlet.sleep(MASTER_CHECK_INTERVAL_SECS)

    def _start(self):
        LOG.info("Became the master, starting %s", self._name)
        self._etcd_watcher = self._thread_class(self._mech_driver)
        self._etcd_watcher_thread = eventlet.spawn(
            self._etcd_watcher.start
        )
        LOG.info("Started %s as %s", self._etcd_watcher,
                 self._etcd_watcher_thread)

    def wait(self):
        while self._etcd_watcher is not None:
            time.sleep(0.1)

    def _stop(self):
        if self._etcd_watcher:
            self._etcd_watcher.stop()
            self._etcd_watcher = None

    def stop(self):
        self._time_to_stop = True
        self._stop()

    @staticmethod
    def reset():
        config.reset_service()


class AgentCalicoWorker(CalicoWorker):

    def __init__(self, mech_driver):
        self._thread_class = status.AgentStatusWatcher
        self._desc = "alive-status worker"
        self._election_key = datamodel_v2.neutron_election_key(
            calico_config.get_region_string()) + "_agent_worker"
        super(AgentCalicoWorker, self).__init__(mech_driver)


class PortCalicoWorker(CalicoWorker):

    def __init__(self, mech_driver):
        self._thread_class = status.PortStatusWatcher
        self._desc = "port-status worker"
        self._election_key = None
        super(PortCalicoWorker, self).__init__(mech_driver)
