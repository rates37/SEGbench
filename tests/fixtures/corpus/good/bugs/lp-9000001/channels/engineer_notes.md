# Triage notes

Reproduced on the customer's staging deployment and on a fresh internal lab deployment of the same
release, so this is not specific to their site.

Checked and ruled out:

- Security groups. The relevant egress and ingress rules are unchanged from before the window, and
  packet captures on the hypervisor show the request leaving the tap interface.
- Nova. `openstack server show` reports ACTIVE, and the server's config drive is absent as expected
  for this deployment (config drive is disabled cloud-wide), so the guest is right to use the
  network path.
- DNS and NTP inside the tenant network. Both resolve and answer normally from the same guest.
- The API tier. Requests issued by hand from a network namespace on the controller succeed.

Observations:

- Every affected network has a haproxy process running for it, so the proxy is being spawned; it
  simply is not answering on the address the guest talks to.
- Restarting the agent on a hypervisor makes every network on that hypervisor start working again,
  including networks that were broken a moment earlier. The fix does not survive the next upgrade.
- Suspect the agent's handling of its own restart rather than anything in the data plane. Not yet
  clear which part of the startup sequence the restart path skips.

Not yet checked: whether a live migration of a working server onto an affected hypervisor breaks
it. Worth doing, since it would separate "wrong at spawn" from "wrong per hypervisor".
