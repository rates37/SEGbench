# SEGbench

A benchmark harness measuring how well an LLM agent can **diagnose** real defects in Canonical
products — Sunbeam, Charmed OpenStack, Juju, MicroK8s, the Ubuntu kernel. Each task is one real
bug; the agent is scored on whether it identifies the right component, the right code, the right
root cause and a plausible fix, not on producing a passing test.

`plan.md` is the design document and the source of truth. `CLAUDE.md` is the working context for
coding agents.

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

## Development

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

The test suite does not need LXD; the backends are exercised through fakes, and
`segbench runtime smoke` is the acceptance check against the real thing.
