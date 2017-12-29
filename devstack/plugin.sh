
# Devstack plugin code for Calico
# ===============================

mode=$1				# stack, unstack or clean
phase=$2			# pre-install, install, post-config or extra

if [ "${Q_AGENT}" = calico-felix ]; then

    case $mode in

	stack)
	    # Called by stack.sh four times for different phases of
	    # its run.
	    echo Calico plugin: stack

	    case $phase in

		pre-install)
		    # Called after system (OS) setup is complete and
		    # before project source is installed.
		    echo Calico plugin: pre-install

		    # Add Calico master PPA as a package source.
		    sudo apt-add-repository -y ppa:project-calico/master
		    REPOS_UPDATED=False

		    # Also add BIRD project PPA as a package source.
		    LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 sudo add-apt-repository -y ppa:cz.nic-labs/bird

		    ;;

		install)
		    # Called after the layer 1 and 2 projects source
		    # and their dependencies have been installed.
		    echo Calico plugin: install

		    # Upgrade dnsmasq.
		    install_package dnsmasq-base dnsmasq-utils

		    # Install ipset.
		    install_package ipset

		    # Install BIRD.
		    install_package bird

		    # Install the Calico agent.
		    install_package calico-felix

		    # Install Calico common code, that includes BIRD templates.
		    install_package calico-common

		    # Install networking-calico.
		    pip_install "${GITDIR['networking-calico']}"

		    ;;

		post-config)
		    # Called after the layer 1 and 2 services have
		    # been configured. All configuration files for
		    # enabled services should exist at this point.
		    echo Calico plugin: post-config


		    # Update qemu configuration (shouldn't be anything
		    # in there so safe to blow away)
		    sudo sh -c "cat > /etc/libvirt/qemu.conf" << EOF
user = "root"
group = "root"
cgroup_device_acl = [
    "/dev/null", "/dev/full", "/dev/zero",
    "/dev/random", "/dev/urandom",
    "/dev/ptmx", "/dev/kvm", "/dev/kqemu",
    "/dev/rtc", "/dev/hpet", "/dev/net/tun",
]
EOF

		    # Use the Calico plugin.  We make this change here, instead
		    # of putting 'Q_PLUGIN=calico' in the settings file,
		    # because the latter would require adding Calico plugin
		    # support to the core DevStack repository.
		    iniset $NEUTRON_CONF DEFAULT core_plugin calico

		    # Reset service_plugins to be empty, as the Calico plugin
		    # itself supports the 'router' extension.
		    inidelete $NEUTRON_CONF DEFAULT service_plugins

		    # Propagate ENABLE_DEBUG_LOG_LEVEL to neutron.conf, so that
		    # it applies to the Calico DHCP agent on each compute node.
		    iniset $NEUTRON_CONF DEFAULT debug $ENABLE_DEBUG_LOG_LEVEL
		    ;;

		extra)
		    # Called near the end after layer 1 and 2 services
		    # have been started.
		    echo Calico plugin: extra

		    # Run script to automatically generate and
		    # maintain BIRD config for the cluster.
		    run_process calico-bird \
                      "${DEST}/networking-calico/devstack/auto-bird-conf.sh ${HOST_IP}"

		    # Run the Calico DHCP agent.
		    sudo mkdir /var/log/neutron || true
		    sudo chown `whoami` /var/log/neutron
		    run_process calico-dhcp \
		      "/usr/local/bin/calico-dhcp-agent --config-file $NEUTRON_CONF"

		    ;;

		*)
		    echo Calico plugin: unexpected phase $phase
		    ;;

	    esac
	    ;;

	unstack)
	    # Called by unstack.sh before other services are shut
	    # down.
	    echo Calico plugin: unstack
	    ;;

	clean)
	    # Called by clean.sh before other services are cleaned,
	    # but after unstack.sh has been called.
	    echo Calico plugin: clean
	    ;;

	*)
	    echo Calico plugin: unexpected mode $mode
	    ;;

    esac
fi
