# Triage

Confirmed on the customer's cluster and reproduced in the lab on the same release.

Scope:

- Only servers booted from a volume are affected. Image-backed servers on the same hypervisor
  evacuated and started without any trouble.
- The affected servers do move: the placement records and the hypervisor assignment are correct,
  and the scheduler picked a healthy destination.
- The volumes themselves are healthy. The storage service reports them as available and their
  data is readable from a rescue server on a different hypervisor.

Checked and ruled out:

- Storage backend connectivity from the destination hypervisor. A fresh server on the same
  hypervisor attaches a new volume and boots from it without issue.
- Credentials and endpoint configuration on the destination. Unchanged, and the service catalogue
  resolves correctly from that host.
- Free capacity, both compute and storage.

Observations:

- The failure is fast and deterministic — thirty seconds, same message, every attempt.
- The identifier the destination hypervisor presents to the storage service on power-on does not
  appear anywhere in the storage service's current records. It does appear in its audit log, from
  before the evacuation.
- Deleting the server's stored disk mapping row by hand and re-creating the attachment through the
  API lets the server boot. That is not a supportable workaround but it narrows things down.

Suspect the compute side rather than storage. Not yet determined which write path drops the value.
