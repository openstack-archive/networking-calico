#!/bin/bash

set -x

# The Calico DHCP agent periodically stops itself, as a workaround for
# leaking connections to the etcd server.  So we run it in a loop.
while true; do
    /usr/local/bin/calico-dhcp-agent "$@"
done
