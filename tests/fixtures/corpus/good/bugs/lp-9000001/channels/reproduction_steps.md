# Reproduction

Environment: a three-controller, two-hypervisor deployment on the release in the version manifest.

1. Deploy the bundle and let the model settle to active/idle.
2. Create a tenant network and subnet, and boot one server on it. Confirm the server finishes its
   first boot and has its SSH key installed. This is the "before" case and it works.
3. Refresh the ovn-chassis and neutron-api charms to the current revision in the same channel, and
   wait for the units to settle again.
4. Boot a second server on the same network.
5. Observe that the second server's first boot stalls, retries the instance data service and gives
   up. The first server is still reachable and still healthy.

Consistency: reproduced on five of five attempts. Booting on a second network reproduces it too, so
it is not specific to one network.

Recovery: restarting the agent on the hypervisor clears it for every network on that hypervisor
until the next charm refresh.
