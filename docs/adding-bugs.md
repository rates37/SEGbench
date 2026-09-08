# Adding bugs to the corpus

Turning a tracker bug into a corpus entry is a supervised workflow: the mechanical parts are
automated, but the actual split of raw tracker text into channels is a human judgement call, on
purpose (plan.md §3.4). Auto-splitting would be the single easiest way to leak the answer into a
channel and invalidate the benchmark, so `segbench corpus add` deliberately stops short of it.

## Workflow

1. **Scaffold from the tracker.**
   ```bash
   segbench corpus add --launchpad 2012647
   # or: segbench corpus add --github openstack/nova#12345
   ```
   This fetches the title, description, comments and attachment list, drops a readable transcript
   into `corpus/bugs/<slug>/raw/tracker.md` (plus the raw JSON in `raw/tracker.json`), and writes
   skeleton `bug.yaml`/`ground_truth.yaml` files with `TODO` markers.

2. **Read the raw transcript and decide which channels this bug actually has.** The channel
   vocabulary is closed (plan.md §4: `customer_report`, `engineer_notes`, `error_trace`,
   `version_manifest`, `service_logs`, `deployment_config`, `reproduction_steps`,
   `system_environment`). Not every bug has every channel — **omit the ones it lacks**. Do not
   invent a channel's content to fill a gap; leave-one-out only generates a cell for a channel that
   exists, and a synthesised channel where the real report had nothing is exactly the kind of
   corpus content plan.md's `origin: synthesised` tag exists to flag as weaker evidence.

3. **Split, enforcing the three rules below**, writing each channel to `channels/<id>.<ext>` and
   listing it in `bug.yaml` with its `origin` (`verbatim` | `paraphrased` | `synthesised`).

4. **Author `ground_truth.yaml`** from the fix commit: `root_cause` (prose — what is actually wrong
   and why), `component`, `acceptable_components` (aliases), `also_acceptable_root_causes`
   (alternate correct framings, optional). Set `provenance: authored`.

5. **Derive `fix.files`/`fix.symbols` mechanically — never hand-write them:**
   ```bash
   segbench corpus derive --bug <slug> --repo <path to a local clone>
   ```
   This rewrites `ground_truth.yaml` in place (a line-level edit, not a YAML round-trip, so the
   hand-written comments survive) with the changed files and, for Python and Go, changed top-level
   symbols from the fix commit.

6. **Set `reviewed_by`** once a human — ideally not the same person who wrote the ground truth —
   has checked it against the actual fix. Until `reviewed_by` is set the bug loads and validates but
   is marked not-ready and excluded from campaigns (`segbench run campaign` skips it unless
   `--include-unreviewed` is passed to `corpus validate`/loading).

7. **Validate:**
   ```bash
   segbench corpus validate --bug <slug>
   ```
   This is the mechanical safety net: schema conformance, referenced files exist, no channel
   contains the fix commit hash or fix URL, no channel has high token-overlap with
   `ground_truth.root_cause` (a warning above the configured `similarity_threshold`, never a hard
   failure — some legitimate vocabulary overlap is expected), and the scrubber finds no
   credentials, real IPs/MACs, or hostnames matching configured customer patterns. See
   [plan.md §3.5](../plan.md#35-validation-decisions-phase-1) for exactly which findings are
   errors vs warnings.

8. **Tag `smoke`** if this bug should be part of the cheap pipeline-smoke-test rotation
   (plan.md §3.6 targets 3–5 such bugs).

## The three splitting rules (plan.md §3.4)

### 1. Never let a channel contain the answer

Bug reports frequently include a comment — sometimes the reporter's *own* comment — that states
exactly what is wrong. That text belongs in `ground_truth.root_cause`, never in a channel.
`engineer_notes` in particular is triage-stage observation only: what was checked, what was ruled
out, which subsystem is suspected — **not** the resolved cause.

**Worked example (good split), from the real bug `lp-2012647`:** the Launchpad description itself
contained a `[Root Cause / Proposed Fix]` section, written by the reporter, that named the exact
missing guard clause. That entire section was excluded from every channel and became the seed for
`ground_truth.root_cause` instead. What remained — the symptom, the repro steps, the traceback —
became `customer_report`, `reproduction_steps`, and `error_trace`.

**What a bad split looks like:** copying the Launchpad description verbatim into `customer_report`
because "that's what the customer wrote." It's what the customer wrote, but it also *is* the
diagnosis — a channel is not automatically safe just because it is attributed to the original
reporter.

### 2. Never let a channel cite the fix

Strip review URLs (`review.opendev.org/c/...`), commit links, "Committed:"/"Fixed by:" comments,
and any prose that references a specific patch or merge proposal — even indirectly ("see the
review for details").

**Worked example, `lp-2012647`:** the tracker thread's later comments were almost entirely Gerrit
bot noise (`Fix proposed to branch: stable/2024.1`, `Committed: <commit-url>`, backport review
links for five stable branches). None of that became a channel. The one comment with actual
diagnostic content (an engineer identifying which port/process was involved, and a workaround)
was paraphrased into `engineer_notes` with the surrounding review/commit links removed, keeping
only the observation.

**What a bad split looks like:** including "Comment 3" verbatim because it "has useful debugging
detail," without first stripping the `Reviewed: ...` / `Committed: ...` boilerplate that comment
happened to be wrapped in.

### 3. Keep the version realistic

If the reporter gave a vague version ("the next OpenStack release," no charm revision, no track),
`version_manifest` stays exactly that vague. Do not backfill a precise version from the fix commit's
metadata or from investigating what the *actual* affected release was — that reintroduces
information the reporter never had, and inflates how specific a real bug report actually is.

**Worked example, `lp-2012647`:** the reporter never stated a charm-keystone version or track. The
`version_manifest` channel for this bug says exactly that ("channel/track: not stated by the
reporter") rather than inferring a release from the stable branches the fix was later backported to.

**What a bad split looks like:** writing `version_manifest` as "charm-keystone 2023.1, based on the
backport branches in the fix history" — using information from *after* the cutoff to make the
version channel look more complete than the real report was.

## A worked bad-vs-good example, side by side

Given this raw tracker excerpt (illustrative, not from a real bug):

> "We think this is the same issue as the connection pool leak — nova-compute stops responding
> after a few days under load. Roman looked at it and says it's almost certainly the same root
> cause as bug #1990001 (`fixed in Ie3f8a2...`), where the RPC client wasn't closing sockets on
> timeout."

- **Bad split** — dropped verbatim into `engineer_notes`: names another bug number, a Gerrit
  Change-Id, and states the resolved mechanism ("RPC client wasn't closing sockets on timeout").
  This both cites a fix and states the answer.
- **Good split** — `engineer_notes`: "Engineering suspects this may be related to a previously seen
  connection-handling issue in the RPC client, not yet confirmed for this report." The symptom
  ("stops responding after a few days under load") goes in `customer_report`. The suspected
  mechanism, the bug number, and the Change-Id are all withheld — if that suspicion is in fact
  correct, it becomes `ground_truth.also_acceptable_root_causes`, not a channel.

## Corpus size and `smoke`

v1 targets 25–40 bugs, spread across products (plan.md §3.6). `smoke`-tagged bugs are the cheap
subset used for `segbench run campaign --tags smoke` pipeline checks (see
[operations.md](operations.md)) — pick real bugs with a small, cheap-to-diagnose footprint, not
synthetic fixtures, so the smoke run still exercises real judge/deterministic scoring behaviour.
