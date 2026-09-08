# SEGbench

SEGbench measures how well an LLM agent can **diagnose** real bugs in Canonical products — Sunbeam,
Charmed OpenStack, Juju, MicroK8s, the Ubuntu kernel. Each task is one real bug. The agent isn't
asked to write a patch or pass a test — it's scored on whether it names the right component, the
right code, and the right root cause, and proposes a plausible fix.

Each bug is run under three levels of source access (E0/E1/E2, see below) and several "channel
sets" — which parts of the original bug report the agent gets to see. Removing one channel at a
time (leave-one-out) and comparing the score to the full-context run tells you which piece of
information actually mattered. This is the same idea as an occlusion sensitivity map for a CNN —
blank out part of the input and see how much the output changes — just applied to pieces of a bug
report instead of patches of an image, and to an LLM's diagnosis instead of a classifier's
confidence.

The three environments answer a related but separate question: how much does *source access*
help?

| | access | question it answers |
|---|---|---|
| **E0** | none, just the bug report | what does the model already know from training? |
| **E1** | repo pinned to right before the fix, can't clone anything else | does having the code in front of it help, if it can't go looking? |
| **E2** | can clone anything (through a mirror that still hides the fix) | does being able to *choose* what to read help further? |

`plan.md` is the full design doc. `CLAUDE.md` is the working context for coding agents working on
this repo.

## Setup

```bash
uv sync
cp segbench.example.toml segbench.toml     # then edit
uv run segbench --help
```

## Container images

The harness runs each task in a container built from a definition under [images/](images/):

| definition | alias | contents |
|---|---|---|
| [base.yaml](images/base.yaml) | `segbench-base` | Ubuntu 24.04, Python 3.12, git, ripgrep, fd, jq, build-essential, standard text tooling, `opencode` |
| [overlays/kernel.yaml](images/overlays/kernel.yaml) | `segbench-kernel` | `crash`, `linux-tools`, `trace-cmd`, `pahole`, cscope/ctags — selected by `bug.product: kernel` |

```bash
segbench image build                    # base image
segbench image build --overlay kernel   # kernel overlay (builds the base first)
segbench image build --force            # rebuild even if the definition is unchanged
segbench image status                   # aliases, digests, staleness
```

**How long it takes.** From cold, the base image is roughly **3 minutes** and the kernel overlay
another **3 minutes** on a reasonable connection; most of that is `apt-get` and the ~180 MB
`opencode` download. A no-op rebuild is under a second. Times are dominated by network, so a slow
archive mirror can easily double them.

**What the build depends on.**

- **LXD**, initialised, with this user in the `lxd` group (`snap install lxd && lxd init`).
  `segbench image build` and `segbench runtime smoke` fail with an explicit message if the daemon
  is unreachable.
- **Outbound network in the build container** — the Ubuntu archive and `opencode.ai`. The build
  container uses the default LXD bridge and is the only container in the harness with real egress;
  it never runs agent code. Benchmark runs have no egress at all beyond the phase 3 proxy.
- **The upstream `ubuntu:24.04` LXD image**, pulled on first build and cached by LXD.

Builds are idempotent: an image whose definition and provisioning script are unchanged, and whose
alias still resolves to the recorded fingerprint, is left alone. The digest is recorded in
`.cache/images/images.json` and is stamped onto every run record.

## Checking the runtime

```bash
segbench runtime smoke                          # against the base image
segbench runtime smoke --image segbench-kernel   # against an overlay
```

Creates a container, execs as root and as the unprivileged agent user, verifies the image's
tooling, round-trips a file through push and pull, proves a background process is killed inside the
container when its deadline passes, destroys the container, and prints per-step timings. A full
pass is around 25 seconds, of which about 20 is container creation.

Interrupting it with `Ctrl-C` at any point leaves no container behind: every backend registers each
container with a cleanup registry that fires on SIGINT, SIGTERM and interpreter exit.

## Corpus

```bash
segbench corpus validate                  # schema, leakage and scrubber checks over all bugs
segbench corpus add --launchpad 2048221   # scaffold a bug directory from a tracker
segbench corpus derive --bug <id> --repo <path>
```

See [docs/adding-bugs.md](docs/adding-bugs.md) for the full supervised workflow, including the
channel-splitting rules with worked good/bad examples.

## Quickstart: run a campaign and view the dashboard

```bash
export OPENROUTER_API_KEY=...
segbench netpol verify                          # prove the no-leakage invariant against a real container
segbench run campaign --tags smoke --dry-run     # see the matrix and a cost estimate first
segbench run campaign --tags smoke               # execute it
segbench grade                                   # deterministic checks + LLM judge
segbench export                                  # writes dashboard/public/data.json
cd dashboard && npm install && npm run build && npm run preview
```

Open the printed preview URL to see the leaderboard, model×environment heatmap, bug×model matrix,
failure-mode breakdown, cost/score frontier, and a drillable run table — all rendered from
`data.json`, no server or database involved.

![segbench dashboard — overview/leaderboard view, rendered from a real end-to-end smoke campaign](docs/dashboard-screenshot.png)

The only bug in the corpus right now is a real one:
[LP #2012647](https://bugs.launchpad.net/charm-keystone/+bug/2012647), a charm-keystone bug where
`openstack-upgrade` fails on a paused unit. That's not enough bugs to say anything about which
model is "better" — it's there to prove the pipeline runs end to end on a real report. See
[docs/methodology.md](docs/methodology.md) for what the benchmark measures and its current
limitations, and [docs/operations.md](docs/operations.md) for running a full campaign.

## Documentation

- [plan.md](plan.md) — the full design document; source of truth.
- [docs/methodology.md](docs/methodology.md) — what's measured, environments, leakage prevention
  (with real `netpol verify` output), scoring rubric, occlusion design, and an honest limitations
  section.
- [docs/adding-bugs.md](docs/adding-bugs.md) — the supervised workflow for turning a tracker bug
  into a corpus entry.
- [docs/operations.md](docs/operations.md) — running a campaign, budget planning, resumption,
  troubleshooting, recommended staging, and measured smoke-campaign numbers.
- [docs/judge-validation.md](docs/judge-validation.md) — the judge hand-grading/agreement process.
- [docs/todo.md](docs/todo.md) — deferred work (plan.md §12), tracked so it stays visible.

## Development

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

The test suite does not need LXD; the backends are exercised through fakes, and
`segbench runtime smoke` is the acceptance check against the real thing.
