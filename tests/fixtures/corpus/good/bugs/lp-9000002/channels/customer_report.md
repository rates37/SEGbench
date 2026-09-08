# Summary

One of our hypervisors died on Tuesday (hardware, the BMC logs show a DIMM failure). We followed
the runbook and evacuated everything off it onto the rest of the cluster. Most of the workloads
came back on their own.

The ones that did not are all our database servers. They all boot from storage rather than from a
local image, if that matters. They show up in the dashboard as if they moved, but they will not
power on. Pressing start in the dashboard gives an error after about thirty seconds and the machine
goes back to the same state.

We tried starting one of them four or five times over the afternoon with the same result. Detaching
and reattaching its disk from the dashboard did not help either.

Our stateless workloads on the same hypervisor were all fine, which is why we think it is something
to do with the storage rather than the evacuation itself.
