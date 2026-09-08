# Deferred work

The items explicitly deferred out of v1, per [plan.md §12](../plan.md#12-future-work). Recorded here
as a checklist so they stay visible rather than silently forgotten. Each is a real gap, not a
hypothetical — see [docs/methodology.md](methodology.md#limitations) for how the current gaps affect
what today's results can and can't claim.

- [ ] **Richer occlusion.** Single-channel-only runs, minimal baselines, sampled powersets, and
      Shapley-style attribution over channels. Leave-one-out measures marginal value given
      everything else, which understates redundant-but-sufficient channels — a trace and a log
      excerpt may each be independently sufficient, and leave-one-out scores both as worthless.
- [ ] **Repeats and confidence intervals.** Bootstrap CIs, variance decomposition across
      bug/model/environment, once `caps.repeats > 1` is actually used for a campaign. The
      aggregation layer already computes bootstrap CIs when repeats allow it
      (`src/segbench/aggregate.py`); what's missing is a campaign run at `repeats > 1` to feed it.
- [ ] **Degraded channels**, rather than removed ones — a truncated trace, a vague version, a
      customer report rewritten to be less coherent. Closer to how real reports actually degrade
      than outright absence.
- [ ] **Patch generation and verification** on the subset of bugs with runnable reproductions.
- [ ] **Interactive clarification** — let the agent ask a simulated reporter for a specific channel,
      at a budget cost. Measures whether a model knows what it's missing, which is invisible in the
      current fixed-channel-set design.
- [ ] **VM backend** for kernel work (LXD VMs rather than containers). The runtime protocol is
      already written to not assume container semantics; no VM backend implements it yet.
- [ ] **Per-bug contamination checks** — ask each model directly whether it can recall the fix,
      independent of the benchmark prompt, and record a per-bug contamination flag. Every bug in the
      corpus is a public tracker report with a public fix, so this is a real, currently-unmeasured
      risk (see methodology.md's limitations section).

## Also tracked here (not in plan.md §12, found during phase 10)

- [ ] **Grow the corpus past a single reviewed bug.** Plan.md §3.6 targets 25–40 bugs; the corpus
      currently has one (`lp-2012647`). Every aggregate in the current dashboard export is a
      pipeline demonstration, not a capability result, purely because of `n`.
- [ ] **A real judge-validation sample.** The current `docs/judge-validation.md` sample is `n=2`
      (all the smoke campaign produced), far below the 30–50 target — needs redoing once the corpus
      has grown and a real campaign has been graded.
- [ ] **Fill in `[netpol.pricing]`.** Shipped empty by design, so the proxy's cost meter currently
      reports `$0.00` for every run rather than an estimate — real per-model prices need adding
      before `mean_cost_usd` in the dashboard means anything.
- [ ] **Investigate the `qwen/qwen-2.5-coder-32b-instruct` no-answer rate** seen in the smoke
      campaign (3 of 4 attempts produced no valid `answer.json`) before drawing any capability
      conclusion from it — could be a prompt/agent-harness interaction specific to that model rather
      than a capability signal, and is exactly the kind of thing a larger corpus and repeats would
      disambiguate.
