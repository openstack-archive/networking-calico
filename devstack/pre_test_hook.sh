
echo "Hi, this is networking-calico's pre-test hook"

function gate_hook {
    echo "Hi, this is networking-calico's gate hook"

    # Run the normal gate hook and record its status.
    $BASE/new/devstack-gate/devstack-vm-gate.sh
    status=$?

    # Do things to get at the Felix logs.
    ls -lrt /var/log/calico
    cat /var/log/calico/felix.log

    # Return the normal gate hook's status.
    return $status
}
export -f gate_hook
