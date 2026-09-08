# Methodology

This document explains what `segbench` measures, how the no-leakage invariant is enforced and
verified, how a run is scored, how the occlusion (information-gain) study works, and — honestly —
where the results stop being trustworthy. See [plan.md](../plan.md) for the full design rationale;
this is the summary a reader needs before trusting a number in the dashboard.

## What this measures

`segbench` measures how well an LLM agent **diagnoses** a real defect in a Canonical product, given
the kind of information that actually arrives with a bug report. It is not a patch-generation
benchmark and does not check whether a fix compiles or a test passes — it checks whether the agent
identified the right component, the right code, and the right root cause, and proposed a plausible
remedy. See [plan.md §1](../plan.md#1-goal-and-scope) for the full scope statement.

Each task is one real bug. The agent is dropped into a container with a subset of the bug report
and, in two of three environments, some amount of source access, and must write a structured
`answer.json` (root cause, component, suspect files/symbols, proposed fix) within a wall-clock and
cost budget. Nothing about the fix, the fix commit, or any post-report discussion is reachable.

## The three environments

All three environments run the same container image, the same prompt scaffolding, and route all
egress through a policy-enforced proxy (below). They differ only in repository access:

| environment | repository access | answers |
|---|---|---|
| **E0** — knowledge only | none; `/workspace` has only the channel files | what does the model know from training, and how well does it reason from symptoms alone? |
| **E1** — pre-seeded repository | `/workspace/repo`, checked out at `pre_fix_ref`, history rewritten to a single parentless commit (no tags, branches, or remotes) | does having the code in front of it help, if it can't choose what to look at? |
| **E2** — free cloning | empty; the agent may clone anything, rewritten through a host-side truncating mirror | does the ability to *choose* what to read (including dependencies) help beyond the primary repo? |

Full design in [plan.md §5](../plan.md#5-environments).

## How leakage is prevented — and how that's verified, not just asserted

The load-bearing invariant (CLAUDE.md #1–#2) is that the agent can never observe the fix, the fix
commit, the tracker page, or anything postdating the bug report. This is enforced structurally:

- **E1**'s repository is truncated by *export-and-recommit*, never a graft: the tree at the cutoff
  commit is `git archive`d into a directory that was never seeded from the source repo's object
  database, then committed fresh. There is nothing to un-delete, because nothing from after the
  cutoff was ever copied in ([plan.md §5.3](../plan.md#53-git-mirror-decisions-phase-4)).
- **E2** rewrites all git traffic through a host-side mirror via `insteadOf` rules; the mirror
  itself only ever serves history truncated at-or-before `source.reported_at`.
- **Every environment** has a dedicated, routeless LXD network — no default route to the internet
  at all — and the *only* reachable destinations are the egress proxy (inference API only) and, in
  E2, the git mirror. Defence in depth: `/etc/hosts` pins denied hosts to `127.0.0.1`, and
  container-side `iptables` drops anything not addressed to the proxy or mirror.

This is proven against a real container by `segbench netpol verify`, which boots a container wired
up exactly as a benchmark run would be and asserts that the inference endpoint is reachable while a
representative set of tracker, search-engine, package-index, and DNS-exfiltration destinations are
not — several of which deliberately bypass the proxy's environment variables entirely, to prove the
`/etc/hosts`/`iptables` layers hold on their own rather than merely trusting the proxy. Real output,
captured on this development box, one table per environment:

```
netpol verify — E0
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ check                      ┃ expected  ┃ result ┃ detail                     ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ inference endpoint (via    │ reachable │ pass   │ exit=0 reachable=True      │
│ proxy)                     │           │        │ (expected True)            │
│ search engine (via proxy)  │ blocked   │ pass   │ exit=56 reachable=False    │
│ launchpad (via proxy)      │ blocked   │ pass   │ exit=56 reachable=False    │
│ github (via proxy)         │ blocked   │ pass   │ exit=56 reachable=False    │
│ opendev (via proxy)        │ blocked   │ pass   │ exit=56 reachable=False    │
│ pypi (via proxy)           │ blocked   │ pass   │ exit=56 reachable=False    │
│ raw IP over HTTP           │ blocked   │ pass   │ exit=28 reachable=False    │
│ (bypassing the proxy)      │           │        │                            │
│ DNS exfiltration via a     │ blocked   │ pass   │ exit=124 reachable=False   │
│ direct resolver (bypassing │           │        │                            │
│ proxy)                     │           │        │                            │
└────────────────────────────┴───────────┴────────┴────────────────────────────┘
PASS — E0
```

E1 and E2 produce the identical table (same checks, same `pass` results — E2 additionally allows
the mirror, which is not exercised by this generic check but is covered by the leakage test suite,
`tests/test_gitmirror_leakage.py`). All three environments passed every check when last run
(2026-09-09, against `segbench-base` image digest `6e0243ea2c5c...`). Re-run
`segbench netpol verify` before trusting a campaign against a new image or LXD version — it is
cheap and it is the actual evidence, not the prompt telling the model not to browse.

## Scoring rubric

Grading is a separate step from execution (`segbench grade`), so a scoring change can be re-applied
to existing runs without paying for inference again. Full spec in
[plan.md §7](../plan.md#7-grading); summary:

**Deterministic checks** (`src/segbench/grade/deterministic.py`): `component_match` (normalised
against `ground_truth.component` plus `acceptable_components`), `file_f1` (precision/recall over
`answer.suspect_files` vs `fix.files`, path-normalised), `symbol_hit` (intersection with
`fix.symbols`, `null` when unavailable for the bug's language).

**LLM judge** (`src/segbench/grade/judge.py`): a model pinned in config (default DeepSeek V4 Flash),
never a model under test (CLAUDE.md #3, enforced in code — `assert_judge_not_under_test` hard-fails
if the judge model appears in the model matrix), temperature 0, given only `ground_truth.root_cause`,
`also_acceptable_root_causes`, and the agent's `root_cause`/`proposed_fix` — nothing else, so the
judge never sees the transcript or reasons about the bug tracker (CLAUDE.md #5). Returns
`root_cause_score` (0–3), `fix_score` (0–3), and `unsupported_specifics` (a hallucination count,
reported on its own axis rather than folded silently into the score).

**Composition**:

```
diagnosis    = root_cause_score / 3
localisation = 0.5·component_match + 0.3·file_f1 + 0.2·symbol_hit   # symbol term redistributed
                                                                     # over the other two if null
remedy       = fix_score / 3
score        = 0.50·diagnosis + 0.30·localisation + 0.20·remedy
penalty      = min(0.15, 0.05·unsupported_specifics)
final        = max(0, score - penalty)
```

Weights live in `[scoring]` in `segbench.toml` and are stamped on every grade record, so results
computed under different weight vectors can never be silently mixed in one view.

`outcome` is one of `ok`, `no_answer`, `invalid_answer`, `timeout`, `cost_exceeded`,
`harness_error`. Only `harness_error` (a harness bug, not an agent failure) is excluded from
aggregates — everything else scores zero and is counted, because failing to produce a parseable
answer within budget is a real failure mode a support workflow cares about.

In E0 the agent has no repository, so `file_f1`/`symbol_hit` come from memory; the dashboard must
never compare a raw composite across environments without noting the file terms are far harder in
E0 than E1/E2 — the current dashboard filters allow slicing by environment for exactly this reason.

## The occlusion design (information gain)

For channel `c`, over the set of bugs `B_c` that actually have it:

```
gain(c) = mean over b in B_c [ score(b, full) − score(b, loo:c) ]
```

`channel_sets(bug) = {full} ∪ {loo:c for c in bug.channels}` — full-context, then leave-one-out per
channel the bug actually has (missing channels never generate a cell). Positive gain: removing the
channel hurt, so it carried signal. Near zero: redundant given the rest. **Negative gain: removing
it *helped*** — the most interesting result the benchmark can produce, since it means a channel
actively misleads (customer reports are the obvious suspect, since customers describe symptoms in
terms of their own mental model of the system, not the mechanism).

Every gain figure is reported with `n = |B_c|` and, per plan.md §8, the per-bug distribution
alongside the mean (never just a bare average), faceted by environment as well as pooled, and with
any channel at `n < 10` marked low-confidence. No significance test is computed off `repeats = 1`
data (see limitations below).

## Limitations

Read this section before citing a result from this benchmark. It exists because a benchmark's
numbers are only as trustworthy as the caveats attached to them, and every one of these is a real,
current property of the corpus and the run configuration — not a hypothetical.

- **`repeats = 1` noise.** Every cell in the default configuration is a single run. LLM outputs are
  not deterministic even at temperature 0 in practice (routing, sampling backends, minor prompt
  non-determinism from timestamps), so any single score is a point estimate with unknown variance.
  The dashboard never computes or displays a confidence interval off `repeats = 1` data (`ci95` is
  `null` in the export whenever `repeats == 1`) and shows a `repeats = 1` banner on every view that
  could otherwise be mistaken for a significance claim. Raise `caps.repeats` before treating a
  leaderboard ordering, or a gain figure, as more than a hypothesis.
- **Leave-one-out understates redundant channels.** Gain, as computed here, is the *marginal* value
  of a channel given every other channel is still visible. If a stack trace and a log excerpt are
  each independently sufficient to diagnose a bug, leave-one-out will score *both* as
  approximately zero gain, because removing either one still leaves the other. A channel with
  measured gain near zero may be redundant-but-valuable, not worthless — richer occlusion designs
  (single-channel-only baselines, powerset sampling, Shapley attribution) are deferred future work
  (plan.md §12) that would disambiguate this, and are not implemented in v1.
- **Synthesised channel origins.** Every channel in the corpus carries an `origin`:
  `verbatim` (taken from the real report unmodified), `paraphrased` (a real observation, reworded to
  strip identifying detail or the fix), or `synthesised` (invented to fill a channel the real report
  never provided). A synthesised `customer_report` reflects how a maintainer imagines a customer
  writes, not how they actually do, and any conclusion drawn about a channel whose corpus
  contribution is mostly synthesised must carry that caveat explicitly. The dashboard's
  information-gain view carries the origin mix (`origin_counts`) alongside every gain bar for this
  reason — check it before generalising.
- **Potential training-data contamination.** Every bug in this corpus is drawn from a public
  Launchpad or GitHub report with a public fix commit. A model with that report or that commit in
  its training data could recall the diagnosis rather than derive it, especially in E0, which
  otherwise measures "what the model knows from training" by design — the two are not
  distinguishable from the outside. A per-bug contamination flag (ask the model directly whether it
  recalls the fix, independent of the benchmark prompt) is deferred future work (plan.md §12) and is
  not implemented; until it is, treat a suspiciously high E0 score on an old, well-known bug with
  scepticism, and prefer bugs recent enough, or obscure enough, that this is less likely.
- **Small corpus.** At the time of writing the corpus contains a single reviewed bug
  (`lp-2012647`, tagged `smoke`), used to exercise the harness end to end (see
  [operations.md](operations.md) for the resulting smoke campaign numbers). Every aggregate,
  leaderboard position, and information-gain figure in the current `dashboard/public/data.json` is
  therefore a demonstration that the pipeline computes the right thing on real data, **not** a
  capability or information-value claim — `n` is 1 bug, not the 25–40 target in plan.md §3.6. Do
  not read anything into the current leaderboard ordering beyond "the harness ran end to end and
  produced sane numbers." Growing the corpus is the highest-leverage thing that would make future
  runs of this same pipeline citable.
