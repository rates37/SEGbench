# Environment

- Ubuntu 22.04.4 LTS on all hypervisors and controllers.
- Kernel 5.15.0-101-generic, x86_64.
- KVM on Intel Xeon Gold 6338, two sockets, 64 threads per host.
- 9 hypervisors before the failure, 8 after.
- Storage is a separate Ceph cluster reached over a dedicated 25G storage network; the compute
  hosts mount nothing locally beyond their root filesystem.
- Roughly 400 servers in the cloud, of which about 60 are volume-backed.
- Three controllers behind a virtual IP, all healthy throughout the incident.
- The failed hypervisor was powered off and removed from the cluster before the evacuation was
  started, so nothing was running on it at the time.
