.. networking-calico documentation master file, created by
   sphinx-quickstart on Tue Jul  9 22:26:36 2013.
   You can adapt this file completely to your liking, but it should at least
   contain the root `toctree` directive.

networking-calico
=================

networking-calico is the Neutron 'stadium' sub-project that provides 'Calico'
connectivity and security in an OpenStack/Neutron cloud.

Calico (http://www.projectcalico.org/) uses IP routing to provide connectivity
between the workloads in a data center that provide or use IP-based services -
whether VMs, containers or bare metal appliances; and iptables, to impose any
desired fine-grained security policy between those workloads.  Calico thus
differs from most other Neutron backends, which use bridging and tunneling to
simulate L2-level connectivity between the VMs attached to a Neutron network.

.. toctree::
   :maxdepth: 2

   readme
   semantics
   floating-ips
   devstack
   implementation-notes
   host-routes
   dhcp-agent
   contributing

Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
