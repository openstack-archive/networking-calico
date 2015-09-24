#!/bin/bash

set -ex

# Automatically generate full mesh BIRD config for a multi-node
# Calico/DevStack deployment.

MY_ETCD_DIR=/calico-bird

# In a sub-shell, periodically write our own IP into etcd.
(
    while true; do
	etcdctl set ${MY_ETCD_DIR}/$HOST_IP $HOST_IP --ttl 60
	sleep 30
    done
) &

# Watch for all changes under /calico-bird.
while true; do
    etcdctl watch ${MY_ETCD_DIR} --recursive

    # Something has changed.  Get all the IPs that are there now.
    peer_ips=
    for key in `etcdctl ls ${MY_ETCD_DIR}`; do
	key=`basename $key`
	if [ $key != $HOST_IP ]; then
	    peer_ips="$peer_ips $key"
	fi
    done

    # Regenerate BIRD config, and restart BIRD.
    /opt/stack/calico/etc/calico-gen-bird-mesh-conf.sh $HOST_IP 65411 $peer_ips
done
