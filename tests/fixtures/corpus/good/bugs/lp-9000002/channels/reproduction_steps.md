# Reproduction

Lab: four hypervisors on the release in the version manifest, Ceph-backed storage.

1. Create a volume and boot a server from it. Confirm it reaches ACTIVE and that the guest can
   read and write its root filesystem.
2. Stop the compute service on the hypervisor hosting that server, then power the host off so the
   service is genuinely unreachable rather than merely stopped.
3. Wait for the service to be marked down, then evacuate the server onto a healthy hypervisor.
4. Observe the server arrives on the destination and settles in a stopped state, as expected.
5. Start the server.
6. Observe the start fails after about thirty seconds and the server goes to an error state.

Control: repeat the whole sequence with a server booted from an image rather than a volume. It
evacuates and starts normally, every time.

Consistency: 6 of 6 attempts for the volume-backed case, 0 of 6 for the image-backed control.
