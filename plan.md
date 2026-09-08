# segbench — design and implementation plan

Version 1.0. This document is the source of truth for the design. Code that disagrees with it is
a bug in one of the two; fix the mismatch explicitly.

---

## 1. Goal and scope

Measure how well an LLM agent can diagnose real defects in Canonical products, given the kinds of
information that actually arrive with a bug report, and identify **which of those pieces of
information carry the diagnostic signal**.

The benchmark answers three questions:

1. **Capability.** Which models diagnose these bugs, and how well?
2. **Environment sensitivity.** How much does source access help? Is a model that can read the
   repository meaningfully better than one working from memory alone?
3. **Information value.** Which channel of a bug report — the customer's description, the
   engineer's triage note, the stack trace, the version string, the logs — actually moves the
   needle? This is the occlusion study, and it is the part of this benchmark that is novel.

Out of scope for v1: patch generation and test-passing verification, multi-bug agentic sessions,
interactive clarification, and human-in-the-loop grading.

## 2. The shape of one task

The agent boots into a container with a task prompt and a `/workspace` directory. The prompt
contains a fixed preamble, the visible channels for that run, the environment's rules, and the
required output schema. The agent investigates however it likes within the environment's limits,
then writes `/workspace/answer.json`:

```json
{
  "root_cause": "prose, 1-6 sentences, what is actually wrong and why",
  "component": "package or project the defect lives in, e.g. 'nova' or 'juju/juju'",
  "suspect_files": ["path/from/repo/root.py", "..."],
  "suspect_symbols": ["ClassName.method_name", "..."],
  "proposed_fix": "prose, what change would resolve it",
  "confidence": 0.0,
  "evidence": ["which visible channels or files led you here"],
  "uncertain_about": ["what you could not determine and what would resolve it"]
}
```

`confidence` and `uncertain_about` are not scored in v1 but are recorded — calibration analysis is
cheap to add later and impossible to add retroactively.

The run ends when the agent exits, the 300 s wall clock expires, or the cost ceiling trips.
Timeout is a normal outcome: if `answer.json` exists and validates at the moment of the kill, it
is graded, with `truncated: true` on the record.

## 3. Corpus

### 3.1 Layout

```
corpus/bugs/<bug_id>/
  bug.yaml              metadata, provenance, channel inventory
  ground_truth.yaml     what a correct diagnosis looks like
  channels/
    customer_report.md
    engineer_notes.md
    error_trace.txt
    version_manifest.yaml
    service_logs.txt
    deployment_config.yaml
    reproduction_steps.md
    system_environment.md
  attachments/          large artefacts referenced by channels (sosreport excerpts etc.)
```

`<bug_id>` is a stable slug, e.g. `lp-2048221` or `gh-juju-16712`.

### 3.2 `bug.yaml`

```yaml
id: lp-2048221
title: short neutral title, must not name the root cause
product: sunbeam | charmed-openstack | juju | microk8s | kernel | other
source:
  tracker: launchpad | github | reproduction
  url: https://bugs.launchpad.net/...          # null for reproductions
  reported_at: 2024-01-05T00:00:00Z            # the cutoff date for git truncation
repo:
  url: https://opendev.org/openstack/nova
  pre_fix_ref: 7f3a9c1...                      # parent of the fix commit
  build_setup: null                            # optional shell to make the tree usable
difficulty_hint: low | medium | high           # maintainer's guess, never shown to the agent
tags: [networking, ovn, upgrade]
channels:
  - id: customer_report
    file: channels/customer_report.md
    origin: verbatim | paraphrased | synthesised
  - id: error_trace
    file: channels/error_trace.txt
    origin: verbatim
notes: free text for the maintainer, never shown to the agent
```

`origin` matters for interpreting occlusion results. A synthesised customer report reflects how a
maintainer imagines customers write, not how they do. Any conclusion about a channel whose corpus
is mostly synthesised must be reported with that caveat, and the dashboard surfaces the mix.

### 3.3 `ground_truth.yaml`

```yaml
root_cause: |
  Prose description of the actual defect. This is what the judge compares against.
component: nova
fix:
  commit: 9b2e0f4...
  url: https://opendev.org/openstack/nova/commit/9b2e0f4
  files:                    # derived from the commit, do not hand-write
    - nova/virt/libvirt/driver.py
  symbols:
    - LibvirtDriver._get_guest_config
acceptable_components: [nova, nova-compute]   # aliases that count as correct
also_acceptable_root_causes:                  # alternate correct framings, optional
  - |
    Equivalent diagnosis phrased at the ovs layer.
provenance: authored | inferred               # inferred = agent-derived, needs review
reviewed_by: <name>                           # required before a bug is marked ready
```

`fix.files` and `fix.symbols` are generated by `segbench corpus derive`, which fetches the commit
from the mirror and extracts changed paths and, for Python and Go, changed top-level symbols.
Never hand-maintain them.

### 3.4 Adding bugs

`segbench corpus add` scaffolds a directory from a Launchpad or GitHub URL: it pulls title,
description, comments and attachment list, drops them into a `raw/` staging area, and writes a
skeleton `bug.yaml`. Turning raw tracker text into channels is a human-supervised step, because
the split is a judgement call and because tracker text mixes channels freely — a Launchpad
description will often contain the customer's account, the trace, and the version in one blob.

Rules for splitting:

- **Never let a channel contain the answer.** Bug reports frequently include a comment where a
  developer says exactly what is wrong. That text belongs in `ground_truth.root_cause`, not in
  `engineer_notes`. `engineer_notes` is triage-stage observation: what was checked, what was ruled
  out, which subsystem is suspected — not the resolved cause.
- **Never let a channel cite the fix.** Strip review URLs, patch links, "fixed by" references.
- **Keep the version realistic.** If the reporter gave a vague version, keep it vague.

`segbench corpus validate` enforces the mechanical part: schema conformance, referenced files
exist, no channel contains the fix commit hash or the fix URL, no channel contains high-similarity
overlap with `ground_truth.root_cause` (token-level, flagged above a threshold for human review),
and the scrubber finds no credentials, IPs, MACs, hostnames matching customer patterns, or cloud
account IDs.

### 3.5 Validation decisions (phase 1)

Detail settled while implementing §3.4, recorded here so the code and the plan agree.

**Severity.** Checks report findings at one of two severities, and `corpus validate` exits non-zero
on any `error`. Hard failures: every scrubber detector, the fix commit hash, the fix URL, a review
or patch link, and any bug that fails to load. Warnings, reported but not fatal: the root-cause
similarity flag, an unreviewed ground truth, and an attachment that is not UTF-8 text and therefore
could not be scrubbed. Similarity is deliberately advisory — a channel legitimately shares
vocabulary with the diagnosis, so the score is a suspicion for a human, never proof.

**Similarity is containment, not Jaccard.** The score is the fraction of `root_cause`'s content
vocabulary (stopwords stripped) present in the channel. Oriented this way round because the question
is "how much of the answer sits in this channel", and Jaccard would let a long channel dilute a
total leak into a harmless-looking number.

**Readiness.** A bug whose `ground_truth.reviewed_by` is unset loads and validates, but is marked
not-ready and excluded from campaigns unless `--include-unreviewed` is passed. The maintainer has to
be able to iterate on a bug before it is finished; what must not happen is an unreviewed bug
silently contributing numbers to an aggregate.

**Scrubber allowlists.** Every detector is paired with an allowlist of values safe by construction —
non-global IP space (RFC1918, loopback, link-local, the RFC5737/RFC3849 documentation ranges),
reserved MACs, and the RFC2606 documentation domains — extensible from the `[corpus]` config
section. A checker that flags `127.0.0.1` gets switched off within a day, and a switched-off checker
protects nothing. Customer hostname patterns are configured, not guessed: the default list is empty.

**Findings never quote secrets.** A finding's excerpt keeps at most three leading and trailing
characters of the match. The scrubber must not print a credential it just found into a terminal or
a log file.

**`corpus derive` writes in place.** `fix.files` and `fix.symbols` are rewritten by a line-level
edit of `ground_truth.yaml` rather than a YAML round-trip, because these files are hand-maintained
and their comments are load-bearing. In phase 1 the command reads the commit from a local clone
given as `--repo`; phase 4 routes the same extraction through the mirror.

### 3.6 Corpus size

v1 targets 25–40 bugs, spread across products, with a `smoke` tag on 3–5 cheap ones for pipeline
testing. Occlusion needs a reasonable per-channel sample, and with leave-one-out at 1 repeat, the
per-channel estimate is only as good as the bug count.

## 4. Channels

The channel vocabulary is closed. Adding one is a schema change requiring migration of existing
bugs, because occlusion results are only comparable across bugs that share a vocabulary.

| id | what it is |
|---|---|
| `customer_report` | End user's description of the symptom. Imprecise, symptom-level, often wrong about cause. |
| `engineer_notes` | Support or engineering triage observations. What was checked and ruled out. Never the resolved cause. |
| `error_trace` | Stack trace, panic, oops, or the error block from the logs. |
| `version_manifest` | Product/package/charm/snap versions, channel/track, revision. |
| `service_logs` | Surrounding log context, not the trace itself. |
| `deployment_config` | `juju status`, bundle, sunbeam manifest, cloud-init, relevant config. |
| `reproduction_steps` | How to trigger it. |
| `system_environment` | Kernel, OS release, arch, hypervisor, hardware, scale. |

Not every bug has every channel. Missing channels are simply absent, and leave-one-out only
generates a cell for channels the bug actually has. The aggregation layer must therefore compute
per-channel information gain over the subset of bugs possessing that channel, and the dashboard
must show that denominator — a channel present in 6 of 30 bugs is a much weaker claim than one
present in 28, and displaying them at equal visual weight would be misleading.

## 5. Environments

All three environments run the same image and the same prompt scaffolding, and all three route
egress through the proxy. They differ in repository access.

### E0 — knowledge only

No repository. `/workspace` contains only the channel files. Egress: inference API only. Measures
what the model knows about these codebases from training, and how well it reasons from symptoms
alone.

### E1 — pre-seeded repository

`/workspace/repo` contains the target repository, checked out at `pre_fix_ref`, with history
rewritten so that the checkout is a **single root commit with no parents, no tags, no branches
beyond `main`, and no remotes**. The agent sees a working tree exactly as it existed immediately
before the fix, with no timeline to mine and nothing to fetch. Egress: inference API only; git
network access is denied.

Rewriting rather than shallow-cloning matters: `git clone --depth 1` still leaves a remote, and an
agent that runs `git fetch --unshallow` or reads `.git/shallow` gets more than intended. The
rewrite is done host-side in the mirror and the resulting bare repo is pushed into the container
as a plain directory with `.git` present but inert.

E1 answers: does having the code in front of it help, if it cannot choose what to look at?

### E2 — free cloning

`/workspace` starts empty of source. The agent may clone anything it wants. All git traffic is
transparently rewritten to the **host-side truncating mirror** via `insteadOf` rules plus a
proxy-level deny on any direct git host. No web browsing, no tracker access, no package indexes
beyond what the image already contains.

The mirror serves:

- the **target repository** truncated to `pre_fix_ref`;
- **any other repository** the agent requests, truncated to the last commit at or before
  `source.reported_at`, fetched lazily on first request and cached.

Lazy fetching means the mirror needs upstream network access, which it has, because it runs on the
host outside the container's network namespace. The container itself can reach only the mirror.

E2 answers: does the ability to *choose* what source to read — including dependencies, and
including repositories the maintainer never anticipated — help beyond having the primary repo?

### 5.1 Network enforcement

This is the load-bearing part of the design. Prompt-level restriction is not enforcement.

Topology: each run gets a dedicated LXD network (or a shared one with per-container ACLs, if the
LXD version supports network ACLs cleanly — prefer ACLs, they are much cheaper to set up). The
container has **no default route to the internet**. Its only reachable destinations are:

1. the **egress proxy** on the host, an HTTP CONNECT proxy with an allowlist;
2. the **git mirror** on the host, plain HTTP on a loopback-bound port exposed to the container
   network (E2 only; denied in E0 and E1).

`http_proxy`, `https_proxy` and `ALL_PROXY` are set in the container environment, and the proxy is
the only thing that can reach outward. Anything not on the allowlist gets a 403 and an entry in
the run's `netlog.jsonl`.

The allowlist is exactly:

- the configured inference endpoint (`openrouter.ai` or the Copilot endpoint);
- nothing else.

Explicitly denied and logged as **leak attempts** even though they would fail anyway:
`launchpad.net`, `bugs.launchpad.net`, `github.com`, `opendev.org`, `review.opendev.org`, any
search engine, any package index. A run with leak attempts is still valid — the attempt failed —
but the count is recorded and shown in the dashboard, because a model that spends its budget
trying to browse is behaving differently from one that reasons.

Belt and braces: the container's `/etc/hosts` pins the denied hosts to `127.0.0.1`, and iptables
in the container drops outbound traffic not destined for the proxy or mirror. Defence in depth,
because a single misconfigured LXD profile should not silently invalidate a week of runs.

### 5.1.1 Network enforcement decisions (phase 3)

Detail settled while implementing §5.1, recorded here so the code and the plan agree.

**Dedicated networks, not ACLs.** §5.1 hedged between a shared network with per-container ACLs and
one network per run, preferring ACLs "if the LXD version supports network ACLs cleanly." It
doesn't, here: `security.acls` is rejected as an invalid device option on this host's LXD (5.21
LTS) for a bridge NIC, in both the classic `nictype=bridged,parent=<net>` form and the modern
`network=<net>` shorthand — confirmed by hand against a real container before writing any
enforcement code. `segbench.netpol.enforce` therefore takes the plan's own explicit fallback: one
dedicated LXD network per named policy (`segbench-e0`, `segbench-e1`, `segbench-e2`), created with
`ipv4.address=auto` (no subnet to guess or collide with the host's other networks),
`ipv4.nat=false` and `ipv4.routing=false` (no path in or out of the bridge at all — confirmed by
hand: a container on such a network cannot reach a public IP, but can still reach the host's own
gateway address on that bridge, which is exactly where the per-run proxy listener binds) and no
IPv6. The container's only NIC is this dedicated network; `NetworkPolicy.acls` stays a real,
documented mechanism on the runtime protocol for a backend where it does work, but nothing in
`netpol` relies on it today.

**`lxc launch --device` takes exactly one key/value pair per occurrence.** Its own `--help` says so
("New key/value to apply to a specific device," singular), but it is easy to write
`--device eth0,type=nic,network=foo,name=eth0` once and have it work by accident on some LXD
versions; on others it silently mis-parses everything after the first `=` as one value and fails
with a confusing "Invalid device type" error. `segbench.runtime.lxd.LXDRuntime.create` repeats the
flag once per key. Repeating it is also what turns `eth0` from a profile-inherited device into an
instance-level override — required regardless of ACLs, since a profile-inherited device cannot be
modified in place (`lxc config device set` on it fails with "cannot be modified for individual
instance").

### 5.2 Cost enforcement

The proxy sits in the inference path, so it can parse usage from responses. It maintains a
per-run token and cost tally using a static price table keyed on model string, and once the tally
crosses the ceiling it rejects further requests with a 402. The agent will then fail; the harness
records `outcome: "cost_exceeded"` and grades whatever answer file exists.

Streaming responses need the usage block from the terminating SSE event; if a provider omits
usage, fall back to a `tiktoken`-based estimate and mark the cost `estimated: true`.

### 5.3 Git mirror decisions (phase 4)

Detail settled while implementing the truncating git mirror and E1/E2 seeding (`segbench.netpol
.gitmirror`), recorded here so the code and the plan agree.

**Truncation is export-and-recommit, never a graft.** The obvious "quick" truncation is a graft or
filter-branch on a clone of the real history: point a new parentless commit at the old tree and
delete the other refs. That leaves the *objects* of every later commit sitting in the pack,
unreachable from any ref but not gone — `git cat-file` still reads them, and a `gc` that fails to
prune leaves the fix sitting right there in `.git/objects`. Instead, `truncate_to_rootless` `git
archive`s the tree at the cutoff commit into a directory that was never seeded from the source
repository's object database, commits it fresh (fixed author/committer identity and timestamp —
the snapshot commit's own metadata carries no information), and pushes that into a brand-new bare
repository. There is nothing to accidentally retain because nothing from after the cutoff was ever
copied in. Cached per source commit via a marker file in the destination, so a repeat request for
an unchanged `pre_fix_ref` (the common case — one clone touches the mirror at least twice, for
`info/refs` then `git-upload-pack`) never re-touches the network.

**`git http-backend` as a CGI subprocess, not `git daemon`.** Both speak the smart protocol; the
choice is about what surrounds it. `git daemon` is its own long-lived process with its own
export-list and access model, orthogonal to HTTP. `git http-backend` is a CGI script, so the exact
request plumbing phase 3 already built (`segbench.netpol.proxy`'s plain-HTTP handler) fronts it
directly: the resolve-and-cache step runs as ordinary Python before the CGI subprocess is even
invoked, so an unresolvable repository gets a clear, human-readable denial instead of an opaque
404. It also means mirror traffic is proxied through the *same* egress proxy as inference traffic —
git's HTTP transport honours `http_proxy`/`https_proxy` like any libcurl client — with
`segbench.netpol.enforce`'s direct `iptables` allow rule for `mirror_host:mirror_port` as the
defence-in-depth fallback for a client that bypasses the proxy env vars, not the primary path.

**One long-lived mirror listener for the whole campaign, run-scoped state in the URL.** Unlike the
proxy (a fresh ephemeral listener per run, because per-run cost metering needs unconditional
attribution), the mirror matches `NetpolConfig.mirror_port`'s single configured port. What varies
per run is which bug's cutoff applies, registered by `GitMirror.start_run` and threaded through the
URL path (`/run/<run_id>/target.git`, `/run/<run_id>/other/<host-slug>/<owner>/<repo>.git`) rather
than through the socket.

**The E2 rewrite is a narrow, explicit host table, not a generic reversible encoding.** `insteadOf`
rules are generated for a fixed set of known git-hosting hosts (github.com, opendev.org,
review.opendev.org, code.launchpad.net, git.launchpad.net) plus one exact-match rule for the bug's
own `repo.url`. Git's own longest-prefix-wins rule for multiple matching `insteadOf` entries is what
lets the exact target-repo mapping win over the more general host-prefix mapping when both apply.
Anything outside the table simply has no rewrite rule and falls through to the network deny, which
is a safe failure, not a gap — confirmed by the leakage suite, which bypasses the rewrite entirely
(`GIT_CONFIG_NOSYSTEM=1`) against a real, resolvable host and shows the proxy still denies it.
Rules are installed to `/etc/gitconfig` (`--system`, not `--global`), so they apply regardless of
which user the agent's git commands run as.

**`git insteadOf` requires a proper URL scheme.** A bare filesystem path is rejected with "fatal:
invalid URL scheme name or missing '://' suffix" rather than ever being matched against configured
prefixes — confirmed by hand. Not a concern for real corpus bugs, whose `repo.url` is always an
`https://` URL, but it means any fixture exercising the E2 rewrite needs a real-looking scheme too
(`https://*.invalid/...`, RFC 2606, is used in the leakage tests for exactly this).

**Two latent runtime-layer bugs, surfaced by actually pushing a directory and then running git
against it — the first time either had been exercised for real:**

- `LXDRuntime.push` did not previously handle directories correctly: `lxc file push --recursive SRC
  TARGET` lands the tree at `TARGET/<basename of SRC>`, never at `TARGET` itself. There is no `lxc`
  option for "contents only", so `push` now pushes into the parent of `remote` and renames the
  result into place.
- git 2.35.2+ refuses to operate on a repository it does not own ("detected dubious ownership")
  unless told otherwise, and a file pushed via `lxc file push` need not land owned by whichever uid
  later runs git in it. `seed_e1` marks the seeded path as a system-wide `safe.directory` (same
  `/etc/gitconfig` mechanism as the E2 rewrite) immediately after pushing, before any verification
  runs.

## 6. The run matrix

```
cells = bugs × environments × channel_sets(bug) × models
runs  = cells × repeats
```

`channel_sets(bug)` for v1 is `{full} ∪ {loo:c for c in bug.channels}`. Everything else —
single-channel-only, minimal baselines, powerset sampling — is future work (§12).

With 30 bugs averaging 6 channels, 3 environments, 5 models, 1 repeat, that is
`30 × 3 × 7 × 5 = 3150` runs. At 5 minutes worst case and meaningful parallelism this is a
multi-day campaign, so the orchestrator must support:

- `--dry-run` printing the matrix, run count, and an estimated cost range;
- arbitrary subsetting by bug, product, tag, environment, channel set, and model;
- **resumption**, skipping cells that already have a successful run record;
- bounded parallelism (`--concurrency`, default 4), respecting provider rate limits;
- a global campaign cost ceiling that halts cleanly.

A pragmatic budget shape: run `full` across all bugs, environments and models first, since that is
the capability and environment-sensitivity result and it is only `30 × 3 × 5 = 450` runs. Run the
leave-one-out sweep second, and consider restricting it to E1 plus a subset of models, since
information gain is a property of the information more than of the environment. The orchestrator
should make this staging easy, and `plan.md` recommends it, but does not hardcode it.

## 7. Grading

Grading is a separate command from execution, so a scoring change can be re-applied to existing
runs without paying for inference again. Run records and grade records are separate files joined
on `run_id`.

### 7.1 Deterministic checks

- **component_match** — normalised comparison of `answer.component` against
  `ground_truth.component` plus `acceptable_components`. Boolean.
- **file_hit** — did `answer.suspect_files` intersect `fix.files`? Boolean.
- **file_f1** — precision/recall F1 over the file sets, with path normalisation (leading repo name
  stripped, case-sensitive otherwise). An agent that lists forty files to guarantee a hit should
  not score like one that names two.
- **symbol_hit** — intersection with `fix.symbols`. Boolean, and null when symbols are unavailable
  for that bug's language.

In E0 the agent has no repository, so file paths come from memory. Deterministic file scoring is
still meaningful there — naming the right file from memory is a real signal — but the dashboard
must never compare a raw composite score across environments without noting that the file terms
are far harder in E0.

### 7.2 LLM judge

Model: pinned in config, default DeepSeek V4 Flash via OpenRouter. Never a model under test.
Temperature 0. The judge is given `ground_truth.root_cause`, the acceptable alternates, and the
agent's `root_cause` and `proposed_fix` — and nothing else. It returns strict JSON:

```json
{
  "root_cause_score": 0,
  "root_cause_rationale": "...",
  "fix_score": 0,
  "fix_rationale": "...",
  "contradicts_ground_truth": false,
  "unsupported_specifics": 0
}
```

`root_cause_score`, 0–3:

- **0** — wrong, or so vague it identifies nothing.
- **1** — right general area, wrong mechanism.
- **2** — correct mechanism, imprecise or missing a necessary condition.
- **3** — correct mechanism, would let an engineer go straight to the fix.

`fix_score`, 0–3: 0 no fix or a harmful one; 1 addresses the symptom; 2 plausible fix in the right
place; 3 substantively the fix that was made.

`unsupported_specifics` counts confident, checkable, wrong specifics — a named function that does
not exist, a version that does not exist. This is the hallucination signal and it is worth
reporting on its own axis, because a model that is usefully vague and a model that is confidently
wrong fail very differently in a support workflow.

The judge prompt is versioned (`judge_prompt_version`) and stamped on every grade record. Changing
it requires a re-grade of anything being compared against.

### 7.3 Composite score

```
diagnosis  = root_cause_score / 3
localisation = 0.5 * component_match + 0.3 * file_f1 + 0.2 * symbol_hit   # symbol term
                                                                          # redistributed if null
remedy     = fix_score / 3

score = 0.50 * diagnosis + 0.30 * localisation + 0.20 * remedy
penalty = min(0.15, 0.05 * unsupported_specifics)
final  = max(0, score - penalty)
```

Weights live in config, not code, and the weight vector is stamped on every grade record so a
dashboard can never mix scoring regimes silently. The dashboard shows the three components
separately as well as the composite, because "identified the right subsystem but misdiagnosed the
mechanism" is a genuinely different failure from "described the mechanism but could not locate it",
and a single number hides that.

`outcome` is one of `ok`, `no_answer`, `invalid_answer`, `timeout`, `cost_exceeded`,
`harness_error`. Only `harness_error` is excluded from aggregates; the rest score zero and are
reported, because failing to produce a parseable answer within the budget is a real failure mode.

### 7.4 Judge validation

Before trusting the judge, hand-grade 30–50 runs spanning the score range and report agreement
(exact match and within-one, plus Cohen's κ) in `docs/judge-validation.md`. If agreement is poor,
the rubric is at fault more often than the model. Do this once early, on the smoke corpus, and
repeat whenever the judge model or prompt version changes.

## 8. Information gain (the occlusion analysis)

For channel `c`, over the set of bugs `B_c` that possess it:

```
gain(c) = mean over b in B_c [ score(b, full) - score(b, loo:c) ]
```

Positive gain means removing the channel hurt, so the channel carried signal. Gain near zero means
the channel was redundant given the others. **Negative gain means removing it helped**, which is
the most interesting result the benchmark can produce: a channel that actively misleads. Customer
reports are the obvious candidate, since customers describe symptoms in terms of their mental
model of the system. If that shows up, it is a finding worth writing up on its own.

Reporting requirements, because this is the part most easily overread:

- Report `n = |B_c|` alongside every gain figure, always.
- With `repeats = 1`, per-cell noise is uncontrolled. Report gain as a point estimate, show the
  per-bug distribution (a strip or box plot beside each bar, not just the mean), and mark any
  channel with `n < 10` as low confidence in the UI.
- Report gain per environment as well as pooled. A channel might matter enormously in E0 and not
  at all in E2, where the agent can go read the code instead.
- Do not compute significance tests off `repeats = 1` data and present them as if they were
  powered. If the maintainer later raises repeats, the aggregation layer computes bootstrap CIs
  and the dashboard shows them; until then it shows the raw spread and says so.

## 9. Container image

One base image, built once, cached, digest-stamped onto every run record.

Contents: Ubuntu 24.04, Python 3.12, git, ripgrep, fd, jq, build-essential, the standard text
tooling an investigating agent reaches for, and `opencode`. Deliberately **no** curl-to-internet
utility expectations, no browser, no package index access at runtime — if the agent needs a
package it does not have, that is part of the task's difficulty and should be recorded rather than
worked around.

Product-specific extras (e.g. `crash`, `linux-tools` for kernel bugs) go in optional overlay
layers selected by `bug.product`, so the base stays small.

Build: an LXD image built from a `cloud-init` profile, published locally with an alias and digest.
`segbench image build` is idempotent and refuses to run a campaign against a stale image unless
`--allow-stale`.

Kernel bugs may eventually want an LXD **VM** rather than a container. The runtime protocol should
not assume container semantics, so a VM backend can be added without touching the orchestrator.
Not implemented in v1.

### 9.1 Runtime and image decisions (phase 2)

Detail settled while implementing §9, recorded here so the code and the plan agree.

**Provisioning is execs, not cloud-init.** §9 said "an LXD image built from a `cloud-init`
profile". It is built by launching a container, running `apt-get` and a provisioning script as
foreground execs, then `lxc publish`. cloud-init's failure mode is a container that boots, reports
success, and is quietly missing half its packages; that surfaces days later as an unexplained agent
failure. A foreground exec that fails, fails at the step that broke, with that step's stderr. The
build still waits for the cloud image's own `cloud-init` to finish before touching apt, because
racing first-boot `unattended-upgrades` produces an intermittent dpkg lock error that looks like a
harness bug.

**Idempotence is a definition hash.** A build is skipped when the definition file and its
provisioning script hash to the value recorded in the image cache *and* the alias still resolves to
the recorded fingerprint. The upstream Ubuntu image is deliberately not part of the hash — it moves
underneath the harness, and `--force` is the way to pick up a refresh. The cache records what was
built; LXD remains the authority on what exists, so fingerprints are re-read on every status and
every build decision, and an image deleted behind the harness's back is detected rather than
assumed. `image status` reports three distinct stale states: missing, definition changed, and
rebuilt outside segbench.

**Overlays are layered images, not composable ones.** An overlay names a `parent` and is built by
provisioning on top of the parent's published alias, so a base rebuild invalidates everything
derived from it. Overlays are selected by `bug.product`, which is the key they declare. A bug
cannot get two overlays at once; if that is ever needed the answer is a combined definition, not a
composition mechanism.

**Privilege is not expressible in an image definition.** `ContainerSpec` carries a `privileged`
flag because LXD offers it, but nothing in the harness sets it, and the image definition schema has
no key for it — granting it requires a deliberate schema change rather than a line in a YAML file.
The kernel overlay in particular does not need it: reading a dump or a decoded oops from a bug's
attachments needs the analysis tools, not host capabilities.

**Cleanup is a process-wide registry, and it runs on a worker thread.** Every backend registers a
container the moment it exists — before the readiness wait, since a container that starts and then
fails to become ready is the one most likely to be orphaned — and the registry destroys anything
outstanding on SIGINT, SIGTERM and interpreter exit. The destroys run on a non-daemon worker
thread: `Ctrl-C` at a terminal delivers SIGINT to the whole process group and the harness reliably
sees two, and the second one interrupting the first one's delete leaves exactly the orphan the
registry exists to prevent. Signals reach only the main thread, so a worker cannot be interrupted
that way.

`destroy` additionally retries while LXD reports the instance busy. Interrupting the harness
mid-`create` kills the `lxc launch` client but leaves the server-side create operation running, and
LXD refuses to delete an instance with an operation in flight.

**Background processes are killed by an environment marker.** Killing the `lxc exec` client does
not kill the process inside the container, so `exec_background` injects a unique token into the
command's environment and `wait_or_kill` matches it against `/proc/*/environ` to kill the agent and
everything it spawned. Without this the wall-clock cap is advisory and a run can keep spending
budget after the harness has moved on.

**The podman backend has documented gaps.** It cannot build images and cannot enforce network
ACLs, and raises rather than degrading, because a run that believes it was policed and was not is
the failure the invariants exist to prevent. Records carry the backend that produced them; runs
from the two backends are not interchangeable.

## 10. Agent invocation

`opencode` runs headless in the container as an unprivileged user with `/workspace` as cwd. Its
config is generated per run: provider (OpenRouter or Copilot), model string, API key from the
environment, and a proxy setting pointing at the egress proxy.

The task prompt is assembled from a versioned template (`prompt_version` stamped on the record):

1. Role and objective — diagnose, do not fix; you cannot run the product.
2. Environment rules — what access exists, stated plainly, with the fact that browsing is blocked
   stated as fact rather than instruction, because the model will otherwise waste turns.
3. The visible channels, each in a labelled block.
4. The required output schema and the file path to write it to.
5. Budget notice — approximate time remaining, and the instruction to write a best-effort answer
   file early and refine it rather than risking producing nothing.

Point 5 matters more than it looks. Without it, a slow, careful model scores zero on timeout while
a shallow one scores 0.4, and the benchmark measures haste. Telling every agent to checkpoint its
answer equalises that, and "did it improve on its first answer" becomes measurable later if the
harness snapshots the answer file periodically. Do snapshot it — cheap now, impossible to add
retroactively.

The transcript is extracted from the `opencode` session directory and written verbatim to
`results/runs/<run_id>/transcript.jsonl`, along with `netlog.jsonl`, `answer.json`, the rendered
prompt, and a `meta.json`. Transcripts stay on disk and out of the dashboard.

## 11. Dashboard

Static Vite + React + Recharts build, fed by a single `data.json` from `segbench export`. No
server, no transcripts. Aggregates only, with per-run rows drillable down to the answer and the
judge's rationale.

Views:

**Overview / leaderboard.** One row per model: composite score, the three subscores, mean cost,
mean latency, timeout rate, no-answer rate, leak-attempt rate. Sortable. This is the "who won"
view and it should be honest about the noise — a `repeats = 1` badge sits at the top.

**Model × environment heatmap.** Composite by model and environment, with the delta E1−E0 and
E2−E1 called out, since "how much does source access buy you" is a headline question. Colour scale
diverging around the grand mean, not a rainbow.

**Information gain.** The centrepiece. Horizontal diverging bar chart, one bar per channel, sorted
by gain, zero line emphasised so negative-gain channels read immediately as harmful. Each bar
carries `n` and a strip plot of per-bug deltas behind it so the spread is visible. Facetable by
environment and by model. A toggle switches between pooled and per-environment.

**Bug × model matrix.** Small-multiple grid, bugs as rows, models as columns, cell colour by
score. Reveals which bugs are trivial (drop them; they cost budget and discriminate nothing) and
which are impossible (check them; often the ground truth is wrong rather than the bug being hard).
Sortable by mean difficulty, groupable by product.

**Failure-mode breakdown.** Stacked bars per model over outcome categories plus a hallucination
axis from `unsupported_specifics`. Answers "how does this model fail", which for a support
workflow matters as much as how often.

**Cost/score frontier.** Scatter, mean cost per run against mean score, Pareto front marked. The
practical procurement question is which model is worth running, not which is best.

**Run drilldown.** Filterable table down to the individual run: its cell coordinates, outcome,
scores, the agent's `root_cause` and `proposed_fix`, the judge's rationales, the ground truth, and
the netlog summary. Not the transcript — that stays on disk, and the row shows the path.

Design principles: neutral palette, one accent, colour-blind-safe diverging scale; sample size on
every aggregate; never a bare mean without its spread; the `repeats = 1` caveat visible on any view
that could be mistaken for a significance claim.

## 12. Future work

Explicitly deferred, recorded so they are not silently forgotten:

- **Richer occlusion.** Single-channel-only runs, minimal baselines, sampled powersets, and
  Shapley-style attribution over channels. Leave-one-out measures marginal value given everything
  else, which understates redundant-but-sufficient channels; a trace and a log excerpt may each be
  sufficient alone, and leave-one-out will score both as worthless.
- **Repeats and confidence intervals.** Bootstrap CIs, variance decomposition across bug/model/env.
- **Degraded channels** rather than removed ones — a truncated trace, a vague version, a customer
  report rewritten to be less coherent. Closer to reality than absence.
- **Patch generation and verification** on the subset of bugs with runnable reproductions.
- **Interactive clarification**, where the agent may ask a simulated reporter for a specific
  channel and pays a budget cost for it. This measures whether a model knows what it is missing,
  which is arguably the most useful support-agent skill and is invisible in the current design.
- **VM backend** for kernel work.
- **Contamination checks** — for each bug, whether models can recall the fix when asked directly,
  as a per-bug contamination flag.

---

## 13. Implementation phases

Each phase ends lint-clean, tested, and demonstrating its acceptance check. `prompts.md` holds the
prompt for each.

| # | Phase | Acceptance check |
|---|---|---|
| 0 | Scaffolding: repo, tooling, config models, CLI skeleton | `segbench --help`, `pytest` green |
| 1 | Corpus schema, loader, validator, scrubber, `corpus add` scaffold | Two sample bugs validate; a deliberately leaky bug fails |
| 2 | Container runtime abstraction + LXD backend + image build | `segbench image build` then exec a command in a fresh container |
| 3 | Network policy: egress proxy, allowlist, netlog, cost meter | Container reaches the inference API and nothing else; denial logged |
| 4 | Truncating git mirror; E1 seeding and E2 rewriting | Agent cannot see past `pre_fix_ref` in either environment |
| 5 | Agent layer: prompt assembly, opencode config, invocation, transcript + answer capture | One end-to-end run producing a valid `answer.json` |
| 6 | Grading: deterministic checks, judge, composition, `grade` command | Graded records for phase-5 runs; scoring config change re-grades without re-running |
| 7 | Orchestrator: matrix, subsetting, resumption, concurrency, budget ceiling | `--dry-run` matrix correct; interrupted campaign resumes without duplicates |
| 8 | Export + aggregation, information-gain computation | `data.json` with correct gains and `n` on a synthetic fixture |
| 9 | Dashboard | All views render from real results |
| 10 | Judge validation, docs, smoke campaign | `docs/judge-validation.md` with agreement stats; smoke campaign end to end |