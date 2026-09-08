# Operations

Running a campaign, planning its budget, resuming it, and troubleshooting the two things that
actually go wrong in practice (LXD and the egress proxy/mirror). See
[plan.md §6](../plan.md#6-the-run-matrix) for the run-matrix design this operationalises.

## Prerequisites

```bash
uv sync
cp segbench.example.toml segbench.toml   # edit models, judge, [netpol.pricing]
export OPENROUTER_API_KEY=...            # never written to segbench.toml
uv run segbench image build              # once, or after an image definition changes
uv run segbench netpol verify            # confirm the leakage invariant before spending anything
```

`segbench image status` shows whether the cached image is stale (definition changed, or the alias
was rebuilt outside segbench) — a campaign will refuse to run against a stale image unless
`--allow-stale` is added to `image build`.

## Running a campaign

```bash
segbench run campaign --dry-run                 # print the matrix and a cost estimate; execute nothing
segbench run campaign --tags smoke               # a small, cheap subset
segbench run campaign --bugs lp-2012647 --envs E0,E1
segbench run campaign --models deepseek/deepseek-chat --channel-sets full
```

Every subsetting flag (`--bugs`, `--products`, `--tags`, `--envs`, `--channel-sets`, `--models`)
narrows the matrix independently; combine as needed. `--limit N` caps the number of *pending* runs
actually submitted, which is the way to say "just run 20 of these and stop" without otherwise
changing the matrix.

### Recommended staging (plan.md §6)

Run the **full-context sweep first** — every bug × environment × model at `channel_sets=full` — since
that alone answers the capability and environment-sensitivity questions and is a fraction of the
full matrix (`bugs × envs × models`, vs. `bugs × envs × channel_sets × models` once leave-one-out is
included):

```bash
segbench run campaign --channel-sets full
```

Then run the leave-one-out sweep second, once the full sweep's results look sane. Information gain
is a property of the information more than of the environment, so consider restricting the LOO
sweep to E1 (or E1+E2) and a subset of models if budget is tight:

```bash
segbench run campaign --envs E1 --channel-sets loo:customer_report,loo:engineer_notes,...
```

The orchestrator makes this staging easy but does not hard-code it — decide the split per campaign.

## Budget planning

`--dry-run` prints the pending cell/run count and a cost *range* (min/max across the price table in
`[netpol.pricing]` for the models in the matrix). Fill in `[netpol.pricing."<model id>"]` with real
per-million-token input/output prices before trusting that range — a model absent from the price
table is still metered in tokens, but its reported cost is `estimated: true` (or, if the table is
entirely empty, silently `$0.00` — see the measured-cost caveat below).

Hard caps that bound worst case regardless of the estimate: `caps.wall_clock_s` per run (default
300s), `caps.max_cost_usd` per run (default $5, enforced by the proxy's token/cost meter, which
returns HTTP 402 once tripped — the run then scores as `cost_exceeded`, not a harness error), and
optionally `caps.max_campaign_cost_usd` to stop the whole campaign cleanly once the running total of
completed runs' metered cost crosses it.

## Resumption

Resumption is automatic and requires no flag: a cell with `caps.repeats` non-`harness_error` run
records already in `results/runs.jsonl` is skipped. `--force` re-runs every repeat regardless,
ignoring existing records. `Ctrl-C` stops submitting new cells, drains whatever is already in
flight (their containers are destroyed by the runtime registry, so they finish fast with
`outcome: harness_error`, which correctly leaves that repeat pending), and leaves a campaign
manifest under `results/campaigns/<id>.json` plus whatever run records did complete — re-running the
identical command resumes exactly the missing work. `segbench run status` re-reads a manifest's
filters and reports current pending/completed counts without re-executing anything.

## Troubleshooting

### LXD

- **`lxc list` hangs or errors** — the LXD daemon (snap) isn't running or the current user isn't in
  the `lxd` group; `newgrp lxd` or a re-login after `usermod -aG lxd $USER` is the usual fix. This is
  outside segbench's control.
- **Image build fails partway through provisioning** — provisioning is foreground `exec`s, not
  cloud-init (see [plan.md §9.1](../plan.md#91-runtime-and-image-decisions-phase-2)), so a failure
  points at the exact apt/script step that broke, with that step's stderr. Re-run
  `segbench image build`; it is idempotent and safe to retry.
- **A container is orphaned after a crash or `kill -9` of the harness process** — the runtime
  registry only destroys containers on a clean SIGINT/SIGTERM/interpreter-exit path; a `kill -9`
  bypasses it. `lxc list | grep segbench-` and `lxc delete -f <name>` clean up by hand.
- **`Address already in use` from the git mirror** during a multi-run campaign — this was a real
  bug found and fixed while writing this document: each `execute_run` used to start its own
  `GitMirror` listener on the campaign's single configured port, which collided the moment more than
  one E2 run was in flight under `--concurrency > 1`. Fixed in `orchestrator.run_campaign`, which now
  starts one campaign-wide mirror (matching plan.md §5.3's "one long-lived mirror listener for the
  whole campaign") and threads it into every `execute_run` call. If you see this error, you are on a
  version predating that fix.

### Egress proxy

- **Every run times out with no answer and no leak attempts logged** — check the proxy actually
  installed its MITM CA into the container's trust store (`proxy CA installed in container trust
  store` in the logs); a mismatch between the CA and what `opencode` trusts manifests as every
  outbound inference call silently failing.
- **`egress denied` for the inference endpoint itself** — check `[provider].base_url`'s host is on
  the allowlist the policy was built from; this should not happen with the default OpenRouter/Copilot
  config, but a custom `base_url` needs the policy to recognise it.
- **Diagnosing a specific policy** — `segbench netpol verify --env E2` reproduces the exact
  container/proxy/mirror wiring a real run uses and prints a pass/fail table; run it against the
  environment you're debugging before suspecting the model.

## Smoke campaign results

A real smoke campaign was run end to end on 2026-09-09 against the one reviewed `smoke`-tagged bug
in the corpus at the time (`lp-2012647`), all three environments, `channel_sets=full`, two cheap
models (`deepseek/deepseek-chat`, `qwen/qwen-2.5-coder-32b-instruct`), `repeats=1` — 6 cells, 7 run
attempts (one `harness_error` retried automatically by resumption).

| | measured |
|---|---|
| cells (bug × env × model, `channel_sets=full`) | 6 |
| run attempts | 7 (1 `harness_error`, retried; 6 valid cell records) |
| outcomes | 2 `ok`, 4 `no_answer`, 1 `harness_error` |
| total wall-clock across all invocations (`time`, including the crash/fix/retry below) | ≈5m10s |
| sum of individual run durations (`duration_s`, concurrency 3) | 569.6s |
| per-run duration range | 29.5s – 168.2s |
| total tokens metered | 154,134 prompt + 1,891 completion |
| metered cost reported | $0.00 at the time (see cost-fix note below) |
| `segbench grade` | 7 graded, 0 failed |
| `segbench export` → `segbench.toml` [scoring] weights → `dashboard/public/data.json` | built and rendered; see screenshot in [README.md](../README.md) |

**Cost fix (found after this run):** `[netpol.pricing]` was left empty in `segbench.toml` (the
example config ships it empty by design — plan.md never assumes prices), so the proxy's own cost
meter reported `$0.00` for this run rather than an actual dollar figure. `opencode` itself, however,
already computes a real, provider-accounted dollar cost per session (`session.json`'s `info.cost`),
so `execute_run` now reads that value and uses it as the authoritative `cost.usd` whenever a session
was captured, falling back to the proxy meter (still gated on `[netpol.pricing]`) only when it
wasn't. The run records above were backfilled from their already-saved `session.json` files once
this was found — the dashboard's cost figures reflect real spend, not token counts converted by
hand. A follow-up campaign against three more models (`deepseek/deepseek-v4-flash-0731`,
`qwen/qwen3.8-flash`, `google/gemini-3.8-flash`, 9 more runs, all `channel_sets=full`) confirmed real
non-zero costs now appear directly in `dashboard/public/data.json` without any manual
pricing-table maintenance.

**What this smoke run is (and isn't) evidence of:** it is evidence the full pipeline — corpus
loading → container provisioning → network enforcement → agent invocation → transcript/answer
capture → grading (deterministic + judge) → export → dashboard rendering — works end to end against
a real bug, real containers, and a real model provider, including catching and fixing a real
concurrency bug in the git mirror along the way. It is **not** a capability result: `n = 1` bug is
far below any threshold for comparing models, and one of the two `ok` runs happened to be the only
one that produced a valid answer for `qwen/qwen-2.5-coder-32b-instruct` across all three
environments (three `no_answer` outcomes for that model) — this reads much more like a prompt/agent
robustness issue worth investigating than a capability signal. See
[methodology.md's limitations section](methodology.md#limitations).
