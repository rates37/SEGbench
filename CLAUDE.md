# CLAUDE.md — segbench

Context file for any coding agent working in this repository. Read this before touching code,
and read `plan.md` for the full design rationale.

## What this project is

`segbench` is a benchmark harness that measures how well an LLM agent can **diagnose** real
defects in Canonical products (Sunbeam / Canonical OpenStack, Charmed OpenStack, Juju, MicroK8s,
the Ubuntu kernel, and similar). It is a diagnosis benchmark, not a patch-generation benchmark:
the agent is scored on whether it identifies the right component, the right code, the right root
cause, and a plausible fix — not on producing a passing test.

Each **task** is one real bug, drawn from public Launchpad bugs, public GitHub/OpenDev issues, or
reproductions on the maintainer's own systems. Each task is run under several **environments**
(how much repository access the agent gets) and several **channel sets** (which pieces of the bug
report the agent is shown). Results are graded, aggregated, and rendered into a static dashboard.

## Non-negotiable invariants

These are the things that make the benchmark valid. Breaking one silently invalidates every
number the harness has ever produced. Treat them as tests, not preferences.

1. **No future leakage.** The agent must never be able to observe the fix, the commit that
   introduced the fix, the bug tracker page, or any discussion postdating the bug report. This is
   enforced structurally (network policy + truncated git history), never by asking the model
   nicely in the prompt.
2. **No web browsing, ever, in any environment.** The only permitted egress is (a) the inference
   API endpoint and (b) `git` traffic to the local truncating mirror. Everything else is denied at
   the proxy and the denial is logged as an event on the run record.
3. **The judge is never a model under test.** The judge model is pinned in config and recorded on
   every grade record.
4. **Every run is reproducible from its record.** A run record must contain enough information —
   image digest, corpus revision, channel set, environment, model string, prompt hash, seed — to
   re-run it exactly.
5. **Grading never sees the transcript's reasoning about the bug tracker.** The grader receives
   the structured answer and the ground truth, nothing else. This prevents the judge from
   rewarding an agent that guessed well for bad reasons and stops transcript length from biasing
   scores.
6. **Claude is not a model under test.** The harness is developed with Claude Code, but the
   in-container agent is `opencode` driving OpenRouter or GitHub Copilot. Never add a Claude model
   to the default model matrix; the maintainer will add one deliberately if they ever want a
   baseline.

## Repository layout

```
segbench/
  CLAUDE.md               this file
  plan.md                 full design document — the source of truth
  prompts.md              implementation prompt series (for the human driving the agent)
  pyproject.toml
  src/segbench/
    config.py             pydantic settings + run-matrix models
    corpus/               bug loading, validation, sanitisation checks
    runtime/              container backends (lxd primary, podman fallback)
    netpol/               egress proxy, git mirror, allowlist policy
    agent/                opencode config generation, invocation, transcript capture
    grade/                deterministic checks, LLM judge, score composition
    orchestrator.py       builds the run matrix, executes it, writes records
    export.py             results -> dashboard data
    cli.py                `segbench` entry point
  corpus/
    bugs/<bug_id>/        bug.yaml, ground_truth.yaml, channels/, attachments/
    schema/
  images/                 container image build definitions
  dashboard/              Vite + React static site
  results/                run records, transcripts, logs (gitignored except .gitkeep)
  tests/
```

## Core vocabulary

Use these terms exactly; they are the field names in the data model.

- **bug** — one real defect, one directory under `corpus/bugs/`.
- **channel** — one category of information from the bug report. The vocabulary is closed; see
  `plan.md` §4. Adding a channel is a schema change and needs a migration of existing bugs.
- **channel set** — the subset of channels shown to the agent in a given run. `full` shows all
  available channels; `loo:<channel>` shows all but one.
- **environment** — `E0` knowledge-only, `E1` pre-seeded truncated repo, `E2` free cloning via the
  truncating mirror. See `plan.md` §5.
- **cell** — the tuple (bug, environment, channel set, model). One cell is executed `repeats`
  times; each execution is a **run**.
- **run record** — the JSON object describing one execution, appended to `results/runs.jsonl`.

## Technical decisions already made

Do not relitigate these without asking the maintainer.

- **Python 3.12**, `uv` for dependency management, `ruff` for lint and format, `pytest` for tests,
  `pydantic` v2 for every schema that crosses a process or file boundary.
- **LXD is the primary container backend**, driven through the `lxc` CLI via `subprocess`, not
  `pylxd`. The CLI is more stable across LXD versions and far easier to debug from a shell.
  Privileged containers are available. A `podman` backend implements the same protocol as a
  fallback, and the runtime layer must not leak backend-specific concepts upward.
- **`opencode` is the in-container agent**, configured against OpenRouter (primary) or GitHub
  Copilot (secondary). The model string is per-run config. `opencode` runs headless, writes its
  session to disk, and the harness extracts the transcript from that session directory.
- **The answer is a file, not stdout.** The agent must write `/workspace/answer.json` conforming
  to the schema in `src/segbench/agent/schema.py`. A run that produces no valid answer file scores
  zero with `outcome: "no_answer"`, which is distinct from scoring zero on a bad answer.
- **Caps**: 300 s wall clock per run, unlimited turns within it, 5.00 USD hard ceiling per run
  enforced at the proxy by tallying usage from inference responses. Timeout is a normal outcome,
  not an error.
- **Dashboard is a static build.** No server, no database. `segbench export` writes a single
  `data.json`; the Vite build produces a directory that can be served from anywhere or opened over
  `file://` if possible.

## Working agreements

- **`plan.md` is the source of truth.** If the code and the plan disagree, that is a bug in one of
  them; fix the mismatch explicitly rather than letting it drift. If you make a design decision
  the plan doesn't cover, add it to the plan in the same commit.
- **Every phase ends green.** Lint clean, tests passing, and the phase's stated acceptance check
  demonstrably working before moving on.
- **Prefer boring.** This harness will be debugged at 11pm when a container won't start. Explicit
  subprocess calls with logged command lines beat clever abstractions.
- **Fail loudly on ambiguity in the corpus.** A bug with a malformed manifest, a missing ground
  truth field, or a channel file referencing a nonexistent attachment must fail validation at load
  time, not silently produce a degraded task.
- **Never fabricate corpus content.** If asked to fill gaps in ground truth, derive fields
  mechanically from the fix commit where possible and mark anything inferred with
  `provenance: inferred` so the maintainer can review it. Do not invent root-cause prose for a bug
  whose fix you cannot see.
- **Cost awareness.** The maintainer's token budget is limited. Default to `--repeats 1`, offer a
  `--dry-run` that prints the matrix and estimated cost without executing, and make partial
  execution and resumption first-class.

## Common commands

```bash
uv sync                                   # install
uv run ruff check . && uv run ruff format --check .
uv run pytest

segbench corpus validate                  # schema + leakage checks over all bugs
segbench corpus add --launchpad 2048221   # scaffold a bug directory from a tracker
segbench image build                      # build/refresh the base container image
segbench mirror sync --bug <id>           # populate the truncating git mirror for a bug
segbench run --dry-run                    # print matrix + cost estimate
segbench run --models <a,b> --bugs <id>   # execute a subset
segbench grade --pending                  # grade ungraded runs
segbench export && (cd dashboard && npm run build)
```

## Safety and data hygiene

The corpus is drawn from public sources and the maintainer's own reproductions, but attachments
such as sosreports and log dumps routinely contain hostnames, IPs, MAC addresses, cloud account
identifiers and credentials. `segbench corpus validate` runs a scrubber check over every channel
file and attachment and fails on a hit. Never commit an attachment that has not passed it.