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

### 3.5 Corpus size

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

### 5.2 Cost enforcement

The proxy sits in the inference path, so it can parse usage from responses. It maintains a
per-run token and cost tally using a static price table keyed on model string, and once the tally
crosses the ceiling it rejects further requests with a 402. The agent will then fail; the harness
records `outcome: "cost_exceeded"` and grades whatever answer file exists.

Streaming responses need the usage block from the terminating SSE event; if a provider omits
usage, fall back to a `tiktoken`-based estimate and mark the cost `estimated: true`.

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