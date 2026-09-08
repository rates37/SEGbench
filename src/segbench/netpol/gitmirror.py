"""The truncating git mirror (plan.md section 5, section 5.1's "git mirror" line, and section 13
phase 4).

CLAUDE.md invariant 1 — no future leakage — is enforced here structurally, not by asking a model
nicely: the agent must never be able to observe the fix commit, anything that postdates it, or the
tracker discussion around it. Two environments, two truncation rules:

* **E1** gets a single working tree, seeded once, checked out at ``pre_fix_ref``.
* **E2** gets this module's HTTP server: the *only* thing in the whole harness with real upstream
  network access (plan.md section 5.1 — "the mirror is the only component with upstream network
  access. It runs on the host, outside the container's namespace"), because someone has to be able
  to fetch a repository the agent asks for.

**Why exporting the tree into a fresh repository, not grafting the existing one.** The obvious
"quick" truncation is a graft or a filter-branch on a clone of the real history: point a new
parentless commit at the old tree and delete the other refs. That leaves the *objects* of every
later commit sitting in the pack, unreachable from any ref but not gone — `git cat-file` still
reads them, and a `gc` that fails to prune (or a caller who forgets to run one) leaves the fix
sitting right there in `.git/objects`. `truncate_to_rootless` sidesteps the entire category: it
`git archive`s the tree at the cutoff commit into a bare directory that was never seeded from the
source repository's object database in the first place, then commits it fresh. There is nothing to
accidentally retain because nothing from after the cutoff was ever copied in.

**Why `git http-backend`, not `git daemon`.** Both speak the smart protocol; the choice is about
what surrounds it. `git daemon` is its own long-lived process with its own export-list and access
model, orthogonal to HTTP. `git http-backend` is a CGI script, which means the exact same request
plumbing this codebase already has (:mod:`segbench.netpol.proxy`'s ``_ProxyHandler`` — a plain
``http.server`` handler) can front it: the resolve-and-cache step in :meth:`GitMirror._serve` runs
as ordinary Python before the CGI subprocess is even invoked, so an unresolvable repository gets a
clear, human-readable denial instead of `git http-backend`'s own opaque 404, and the exact same
handler that would otherwise need a second protocol implementation just shells out. It also means
mirror traffic can be — and, in a real deployment, is — proxied through the same egress proxy as
inference traffic: git's HTTP transport honours ``http_proxy``/``https_proxy`` like any libcurl
client, so a plain ``http://`` clone URL naturally routes through
:class:`segbench.netpol.proxy.EgressProxy`, which already has a plain-HTTP forwarding path built
for exactly this (see that module's docstring). :mod:`segbench.netpol.enforce`'s direct
``iptables`` allow rule for ``mirror_host:mirror_port`` is the defence-in-depth fallback for a
client that bypasses the proxy env vars, not the primary path.

**Run-scoped state, one long-lived listener.** Unlike the proxy (a fresh ephemeral listener per
run, because per-run cost metering needs unconditional attribution — see ``proxy.py``), the mirror
is one process for the whole campaign, matching ``NetpolConfig.mirror_port``'s single configured
port. What varies per run is *which bug's cutoff applies*, so that context is registered by
:meth:`GitMirror.start_run` and threaded through the URL path (``/run/<run_id>/...``) rather than
through the socket.
"""

from __future__ import annotations

import datetime as dt
import http.server
import os
import re
import shutil
import socketserver
import subprocess
import tempfile
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from segbench.config import Settings
from segbench.corpus.models import Bug
from segbench.logging import get_logger
from segbench.netpol.proxy import NetlogWriter
from segbench.runtime.base import Handle, Runtime

log = get_logger(__name__)

#: Upstream git hosts the E2 environment may lazily clone from, and the short path segment each is
#: rewritten to (kept short: it appears in every clone URL the agent's git client constructs).
#: Deliberately a narrow, explicit list rather than a generic reversible URL encoding — plan.md
#: section 5.1 accepts that anything outside it simply has no `insteadOf` rule and therefore falls
#: through to the network deny, which is a safe failure, not a gap.
KNOWN_GIT_HOSTS: dict[str, str] = {
    "github.com": "gh",
    "opendev.org": "od",
    "review.opendev.org": "review-od",
    "code.launchpad.net": "lp",
    "git.launchpad.net": "lp-git",
}

#: Name of the marker file recording which upstream commit a truncated bare repo was built from,
#: so a repeat request for the same (repo, ref) — the common case, since one clone touches this
#: mirror at least twice (`info/refs` then `git-upload-pack`) — skips rebuilding and, more
#: importantly, skips re-touching the network at all.
MARKER_FILE = "segbench-source-commit"


class MirrorError(Exception):
    """A repository could not be resolved, fetched, or truncated.

    Raised all the way out to the HTTP handler for an unresolvable request (rendered as a 404 with
    this exception's message as the body — plan.md section 13: "reject requests for repositories
    that cannot be resolved, with a clear error the agent can read") and out to the CLI for
    ``segbench mirror sync``.
    """


# --------------------------------------------------------------------------------------- plumbing


def _run(
    argv: Sequence[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    log.debug("mirror git", extra={"argv": list(argv), "cwd": str(cwd) if cwd else None})
    return subprocess.run(
        list(argv), cwd=cwd, capture_output=True, text=True, env=dict(env) if env else None
    )


def _git(*args: str, cwd: Path | None = None, env: Mapping[str, str] | None = None) -> str:
    result = _run(["git", *args], cwd=cwd, env=env)
    if result.returncode != 0:
        raise MirrorError(
            f"git {' '.join(args)} failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def _slug(url: str) -> str:
    """A filesystem-safe cache key for a repository URL, stable across the https/git@/git:// forms
    that all name the same repository."""
    import hashlib

    cleaned = re.sub(r"^\w+://", "", url)
    cleaned = re.sub(r"^git@", "", cleaned)
    cleaned = cleaned.replace(":", "/")
    cleaned = re.sub(r"\.git$", "", cleaned)
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", cleaned).strip("-").lower()
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned}-{digest}" if cleaned else digest


# ------------------------------------------------------------------------------ upstream fetching


def _upstream_cache_path(cache_dir: Path, url: str) -> Path:
    return cache_dir / "upstream" / f"{_slug(url)}.git"


def ensure_upstream(url: str, cache_dir: Path) -> Path:
    """Ensure a local bare mirror clone of ``url`` exists, returning its path.

    The only function in this module (indeed, in this benchmark — CLAUDE.md invariant 2) that
    reaches real upstream network. Always called host-side, never from inside a container.
    """
    dest = _upstream_cache_path(Path(cache_dir), url)
    if dest.exists():
        try:
            _git(
                "-C",
                str(dest),
                "fetch",
                "-q",
                "--prune",
                "origin",
                "+refs/heads/*:refs/heads/*",
                "+refs/tags/*:refs/tags/*",
            )
        except MirrorError as exc:
            log.warning(
                "upstream refresh failed; using the cached copy",
                extra={"url": url, "error": str(exc)},
            )
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        _git("clone", "-q", "--bare", "--", url, str(dest))
    except MirrorError as exc:
        if dest.exists():
            shutil.rmtree(dest)
        raise MirrorError(f"could not resolve or fetch repository {url!r}: {exc}") from exc
    return dest


def _resolve_commit(bare_repo: Path, ref: str) -> str:
    try:
        return _git("-C", str(bare_repo), "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    except MirrorError as exc:
        raise MirrorError(f"{ref!r} does not resolve to a commit in {bare_repo}: {exc}") from exc


def _default_branch(bare_repo: Path) -> str:
    try:
        return _git("-C", str(bare_repo), "symbolic-ref", "--short", "HEAD").strip()
    except MirrorError as exc:
        raise MirrorError(f"could not determine the default branch of {bare_repo}: {exc}") from exc


def _resolve_commit_before(bare_repo: Path, cutoff: dt.datetime) -> str:
    """The last commit on the default branch at or before ``cutoff`` (plan.md section 5, E2)."""
    branch = _default_branch(bare_repo)
    out = _git(
        "-C", str(bare_repo), "log", "-1", "--format=%H", f"--before={cutoff.isoformat()}", branch
    ).strip()
    if not out:
        raise MirrorError(
            f"no commit at or before {cutoff.isoformat()} on {branch!r} in {bare_repo}"
        )
    return out


# --------------------------------------------------------------------------------- the truncation


def _cached_source_commit(dest: Path) -> str | None:
    marker = dest / MARKER_FILE
    return marker.read_text(encoding="utf-8").strip() if marker.exists() else None


def _extract_tree(bare_repo: Path, commit: str, dest_dir: Path) -> None:
    archive = subprocess.run(
        ["git", "-C", str(bare_repo), "archive", "--format=tar", commit], capture_output=True
    )
    if archive.returncode != 0:
        raise MirrorError(
            f"git archive failed for {commit} in {bare_repo}: "
            f"{archive.stderr.decode(errors='replace').strip()}"
        )
    extract = subprocess.run(
        ["tar", "-x", "-C", str(dest_dir)], input=archive.stdout, capture_output=True
    )
    if extract.returncode != 0:
        raise MirrorError(
            f"failed to extract the snapshot for {commit}: "
            f"{extract.stderr.decode(errors='replace').strip()}"
        )


def truncate_to_rootless(bare_upstream: Path, commit: str, dest: Path) -> str:
    """Build (or reuse) a bare repository at ``dest`` holding exactly one commit: a fresh root
    commit, no parents, whose tree equals ``commit``'s tree in ``bare_upstream`` — no tags, no
    branches but the one, no remotes, no notes (plan.md section 5, E1 and E2). See the module
    docstring for why this is an export-and-recommit rather than a graft.

    Cached via ``dest``'s marker file: a rebuild is skipped once the resolved source commit stops
    changing, which is what makes a repeated ``segbench mirror sync`` or a repeat clone of an
    already-served E2 repository cheap and network-free.
    """
    resolved = _resolve_commit(bare_upstream, commit)
    if dest.exists() and _cached_source_commit(dest) == resolved:
        return _git("-C", str(dest), "rev-parse", "HEAD").strip()

    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="segbench-mirror-export-") as tmp:
        worktree = Path(tmp) / "wt"
        worktree.mkdir()
        _extract_tree(bare_upstream, resolved, worktree)

        # Fixed identity and timestamp: the snapshot commit's own metadata carries no information
        # (who ran the mirror, when), only its tree does.
        commit_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "segbench truncating mirror",
            "GIT_AUTHOR_EMAIL": "segbench@localhost",
            "GIT_AUTHOR_DATE": "1970-01-01T00:00:00Z",
            "GIT_COMMITTER_NAME": "segbench truncating mirror",
            "GIT_COMMITTER_EMAIL": "segbench@localhost",
            "GIT_COMMITTER_DATE": "1970-01-01T00:00:00Z",
        }
        _git("init", "-q", "-b", "main", cwd=worktree)
        _git("add", "-A", cwd=worktree)
        _git(
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            f"segbench: truncated snapshot of {resolved}",
            cwd=worktree,
            env=commit_env,
        )
        new_head = _git("rev-parse", "HEAD", cwd=worktree).strip()

        # A fresh bare repo plus a direct push, rather than `git clone --bare` of the worktree:
        # a bare clone still writes a `[remote "origin"]` stanza (confirmed by hand), which would
        # have to be stripped anyway. A push to a bare repo by URL, with no named remote, adds one
        # nowhere.
        _git("init", "-q", "--bare", "-b", "main", str(dest))
        _git("push", "-q", str(dest.resolve()), f"{new_head}:refs/heads/main", cwd=worktree)

    _git("-C", str(dest), "gc", "-q", "--prune=now")
    (dest / MARKER_FILE).write_text(resolved + "\n", encoding="utf-8")
    return _git("-C", str(dest), "rev-parse", "HEAD").strip()


def _target_dest(cache_dir: Path, bug_id: str) -> Path:
    return cache_dir / "truncated" / "target" / f"{bug_id}.git"


def ensure_target_mirror(cache_dir: Path, bug: Bug) -> Path:
    """The bug's own repository, truncated to ``repo.pre_fix_ref``. Cached per bug id."""
    cache_dir = Path(cache_dir)
    dest = _target_dest(cache_dir, bug.id)
    ref = bug.manifest.repo.pre_fix_ref
    cached = _cached_source_commit(dest) if dest.exists() else None
    if cached and (cached == ref or cached.startswith(ref)):
        return dest
    upstream = ensure_upstream(bug.manifest.repo.url, cache_dir)
    truncate_to_rootless(upstream, ref, dest)
    return dest


def _other_dest(cache_dir: Path, url: str, cutoff: dt.datetime) -> Path:
    return cache_dir / "truncated" / "other" / _slug(url) / f"{cutoff.date().isoformat()}.git"


def ensure_other_mirror(cache_dir: Path, url: str, cutoff: dt.datetime) -> Path:
    """Any other repository the agent asks for, truncated to the last commit at or before
    ``cutoff``. Cached per (repo, cutoff) (plan.md section 5, E2)."""
    cache_dir = Path(cache_dir)
    dest = _other_dest(cache_dir, url, cutoff)
    if dest.exists() and _cached_source_commit(dest):
        return dest
    upstream = ensure_upstream(url, cache_dir)
    commit = _resolve_commit_before(upstream, cutoff)
    truncate_to_rootless(upstream, commit, dest)
    return dest


@dataclass(frozen=True)
class SyncResult:
    bug_id: str
    repo_url: str
    pre_fix_ref: str
    commit: str
    path: Path


def sync_bug(cache_dir: Path, bug: Bug, *, force: bool = False) -> SyncResult:
    """``segbench mirror sync --bug ID``: populate (or refresh) the target mirror for one bug."""
    cache_dir = Path(cache_dir)
    if force:
        dest = _target_dest(cache_dir, bug.id)
        if dest.exists():
            shutil.rmtree(dest)
        upstream = _upstream_cache_path(cache_dir, bug.manifest.repo.url)
        if upstream.exists():
            shutil.rmtree(upstream)
    path = ensure_target_mirror(cache_dir, bug)
    commit = _git("-C", str(path), "rev-parse", "HEAD").strip()
    return SyncResult(
        bug_id=bug.id,
        repo_url=bug.manifest.repo.url,
        pre_fix_ref=bug.manifest.repo.pre_fix_ref,
        commit=commit,
        path=path,
    )


@dataclass(frozen=True)
class MirrorStatus:
    bug_id: str
    cached: bool
    path: Path
    source_commit: str | None


def mirror_status_for(cache_dir: Path, bug: Bug) -> MirrorStatus:
    """``segbench mirror status``'s per-bug row."""
    dest = _target_dest(Path(cache_dir), bug.id)
    return MirrorStatus(
        bug_id=bug.id, cached=dest.exists(), path=dest, source_commit=_cached_source_commit(dest)
    )


# ------------------------------------------------------------------------------------- E1 seeding


def _check_worktree_invariants(log_all: str, remote_v: str) -> None:
    commits = [line for line in log_all.splitlines() if line.strip()]
    if len(commits) != 1:
        raise MirrorError(
            f"E1 seed verification failed: expected exactly one commit from `git log --all`, "
            f"found {len(commits)}: {commits!r}"
        )
    if remote_v.strip():
        raise MirrorError(
            f"E1 seed verification failed: expected no remotes, `git remote -v` reported: "
            f"{remote_v.strip()!r}"
        )


def build_e1_worktree(bare_repo: Path, dest: Path) -> str:
    """Materialise ``bare_repo`` (already truncated to a single rootless commit) as a working tree
    with an inert ``.git`` at ``dest``: no remotes, no shallow marker, no reflog referencing
    anything removed (plan.md section 5, E1). Returns the seeded commit hash.
    """
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _git("clone", "-q", "--", str(bare_repo), str(dest))
    _git("-C", str(dest), "remote", "remove", "origin")
    for stray in ("FETCH_HEAD", "ORIG_HEAD", "shallow"):
        stray_path = dest / ".git" / stray
        if stray_path.exists():
            stray_path.unlink()
    _git("-C", str(dest), "reflog", "expire", "--expire=now", "--all")
    _git("-C", str(dest), "gc", "-q", "--prune=now")
    return _git("-C", str(dest), "rev-parse", "HEAD").strip()


def verify_e1_worktree_on_host(path: Path) -> None:
    """Host-side equivalent of :func:`verify_e1_worktree_in_container`, for testing the seed
    logic without a container in the loop."""
    log_all = _git("-C", str(path), "log", "--all", "--format=%H")
    remote_v = _git("-C", str(path), "remote", "-v")
    _check_worktree_invariants(log_all, remote_v)
    if (path / ".git" / "shallow").exists():
        raise MirrorError("E1 seed verification failed: a shallow marker is present")


def verify_e1_worktree_in_container(
    runtime: Runtime, handle: Handle, *, path: str = "/workspace/repo"
) -> None:
    """Prove the seeded tree holds the invariants plan.md section 5 requires, from *inside* the
    container. A failure here must abort the run, never degrade it silently — the caller is
    expected to let :class:`MirrorError` propagate."""
    log_result = runtime.exec(
        handle, ["git", "-C", path, "log", "--all", "--format=%H"], check=True
    )
    remote_result = runtime.exec(handle, ["git", "-C", path, "remote", "-v"], check=True)
    _check_worktree_invariants(log_result.stdout, remote_result.stdout)
    shallow_check = runtime.exec(handle, ["test", "-f", f"{path}/.git/shallow"])
    if shallow_check.returncode == 0:
        raise MirrorError(
            "E1 seed verification failed: a shallow marker is present in the container"
        )


def seed_e1(
    runtime: Runtime,
    handle: Handle,
    bare_repo: Path,
    *,
    host_workdir: Path,
    container_path: str = "/workspace/repo",
) -> str:
    """Build the truncated working tree on the host, push it into the container, and verify it
    from inside before returning. Raises :class:`MirrorError` on a failed verification; the caller
    must treat that as an aborted run (plan.md section 5, E1)."""
    commit = build_e1_worktree(bare_repo, host_workdir)
    runtime.push(handle, host_workdir, container_path)
    # A pushed file's owning uid need not match whichever uid later runs git in it (root for this
    # function's own verification; the unprivileged agent user for everything after) -- git 2.35.2+
    # refuses to operate on a repository it does not own ("detected dubious ownership") unless told
    # otherwise. `--system`, like the E2 rewrite's `/etc/gitconfig`, applies regardless of user.
    runtime.exec(
        handle,
        ["git", "config", "--system", "--add", "safe.directory", container_path],
        user="root",
        check=True,
        timeout=30.0,
    )
    verify_e1_worktree_in_container(runtime, handle, path=container_path)
    return commit


# --------------------------------------------------------------------------------- the E2 rewrite


def _instead_of_pairs(
    *, base_url: str, target_url: str, known_hosts: Mapping[str, str]
) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = [(f"{base_url}/target.git", target_url)]
    for host, slug in known_hosts.items():
        other_base = f"{base_url}/other/{slug}/"
        pairs += [
            (other_base, f"https://{host}/"),
            (other_base, f"http://{host}/"),
            (other_base, f"git://{host}/"),
            (other_base, f"ssh://git@{host}/"),
            (other_base, f"git@{host}:"),
        ]
    return pairs


def render_gitconfig(pairs: list[tuple[str, str]]) -> str:
    """Render ``[url "<rewrite>"] insteadOf = <prefix>`` blocks. Longest-prefix-wins is git's own
    rule for multiple matching ``insteadOf`` entries, which is what lets the exact target-repo
    mapping win over the more general host-prefix mapping when both apply to the same URL."""
    return "".join(f'[url "{rewrite}"]\n\tinsteadOf = {prefix}\n' for rewrite, prefix in pairs)


# --------------------------------------------------------------------------------- the HTTP mirror

_PATH_RE = re.compile(r"^/run/(?P<run_id>[^/]+)/(?P<rest>.+)$")
_OTHER_RE = re.compile(r"^other/(?P<slug>[^/]+)/(?P<rest>.+)\.git$")
_SMART_SUFFIX_RE = re.compile(r"^(.*?)(/info/refs|/git-upload-pack|/HEAD)$")


def _split_repo_path(rest: str) -> tuple[str, str]:
    match = _SMART_SUFFIX_RE.match(rest)
    if not match:
        raise MirrorError(f"unsupported git protocol request path: {rest!r}")
    repo_part, suffix = match.group(1), match.group(2)
    if not repo_part.endswith(".git"):
        repo_part += ".git"
    return repo_part, suffix


def _invoke_http_backend(
    *, project_root: Path, path_info: str, method: str, query: str, content_type: str, body: bytes
) -> tuple[int, list[tuple[str, str]], bytes]:
    """Run ``git http-backend`` as a one-shot CGI process and parse its response.

    CGI's own contract: headers, a blank line, then the body, over stdout; a ``Status:`` header
    names a non-200 response and is otherwise absent (confirmed by hand against the installed
    ``git http-backend`` for both a valid ``info/refs`` request and a request for a repository that
    does not exist).
    """
    env = {
        **os.environ,
        "GIT_PROJECT_ROOT": str(project_root),
        "GIT_HTTP_EXPORT_ALL": "1",
        "REQUEST_METHOD": method,
        "PATH_INFO": path_info,
        "QUERY_STRING": query,
        "CONTENT_TYPE": content_type,
        "CONTENT_LENGTH": str(len(body)),
        "REMOTE_ADDR": "127.0.0.1",
    }
    proc = subprocess.run(["git", "http-backend"], input=body, capture_output=True, env=env)
    sep = b"\r\n\r\n" if b"\r\n\r\n" in proc.stdout else b"\n\n"
    header_blob, _, payload = proc.stdout.partition(sep)
    status = 200
    headers: list[tuple[str, str]] = []
    for line in header_blob.decode("latin-1").splitlines():
        if not line.strip():
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key.lower() == "status":
            status = int(value.split()[0])
        else:
            headers.append((key, value))
    return status, headers, payload


@dataclass
class _RunState:
    bug: Bug
    netlog: NetlogWriter


@dataclass
class MirrorRunHandle:
    """Returned by :meth:`GitMirror.start_run`; identifies one run's namespace on the mirror."""

    run_id: str
    bug_id: str


class _MirrorServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    mirror: GitMirror


class _MirrorHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _MirrorServer

    def log_message(self, format: str, *args: object) -> None:
        pass  # segbench's own logger and the run netlog are the record.

    def _handle(self, method: str) -> None:
        parsed = urlsplit(self.path)
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b""
        try:
            status, headers, payload = self.server.mirror._serve(
                method=method,
                path=parsed.path,
                query=parsed.query,
                content_type=self.headers.get("Content-Type", ""),
                body=body,
            )
        except MirrorError as exc:
            log.info(
                "git mirror request could not be resolved",
                extra={"path": self.path, "error": str(exc)},
            )
            status, headers, payload = (
                404,
                [("Content-Type", "text/plain; charset=utf-8")],
                str(exc).encode("utf-8"),
            )

        self.send_response(status)
        for key, value in headers:
            if key.lower() == "content-length":
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")


class GitMirror:
    """The campaign-wide E2 git mirror. See the module docstring for the design rationale."""

    def __init__(
        self,
        settings: Settings,
        *,
        cache_dir: Path | None = None,
        bind_host: str | None = None,
        port: int | None = None,
        known_hosts: Mapping[str, str] | None = None,
    ) -> None:
        self._cache_dir = (
            Path(cache_dir) if cache_dir is not None else Path(settings.paths.mirror_cache)
        )
        self._bind_host = bind_host or settings.netpol.bind_host
        self._port = port if port is not None else settings.netpol.mirror_port
        self._known_hosts = dict(known_hosts) if known_hosts is not None else dict(KNOWN_GIT_HOSTS)
        self._host_by_slug = {slug: host for host, slug in self._known_hosts.items()}
        self._serve_root = self._cache_dir / "serve"
        self._runs: dict[str, _RunState] = {}
        self._build_locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()
        self._server: _MirrorServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def cache_dir(self) -> Path:
        return self._cache_dir

    @property
    def port(self) -> int:
        if self._server is None:
            raise MirrorError("the git mirror is not running")
        return self._server.server_address[1]

    def start(self) -> None:
        if self._server is not None:
            return
        self._serve_root.mkdir(parents=True, exist_ok=True)
        server = _MirrorServer((self._bind_host, self._port), _MirrorHandler)
        server.mirror = self
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever, name="segbench-gitmirror", daemon=True
        )
        self._thread.start()
        log.info("git mirror listening", extra={"host": self._bind_host, "port": self.port})

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._server = None
        self._thread = None

    def start_run(self, run_id: str, bug: Bug, *, netlog_path: Path) -> MirrorRunHandle:
        with self._lock:
            self._runs[run_id] = _RunState(bug=bug, netlog=NetlogWriter(netlog_path, run_id))
        return MirrorRunHandle(run_id=run_id, bug_id=bug.id)

    def stop_run(self, run_id: str) -> None:
        with self._lock:
            self._runs.pop(run_id, None)
        link_dir = self._serve_root / "run" / run_id
        if link_dir.exists():
            shutil.rmtree(link_dir, ignore_errors=True)

    def base_url(self, run_id: str, *, advertise_host: str) -> str:
        """The URL prefix this run's ``insteadOf`` rules rewrite onto. ``advertise_host`` is the
        address the *container* reaches this listener on — never ``0.0.0.0``, which is only
        meaningful as a bind address (mirrors :meth:`segbench.netpol.proxy.RunProxyHandle.env`)."""
        return f"http://{advertise_host}:{self.port}/run/{run_id}"

    def instead_of_config(self, run_id: str, bug: Bug, *, advertise_host: str) -> str:
        """This run's ``[url "..."] insteadOf = ...`` blocks, ready to append to a gitconfig."""
        base = self.base_url(run_id, advertise_host=advertise_host)
        pairs = _instead_of_pairs(
            base_url=base, target_url=bug.manifest.repo.url, known_hosts=self._known_hosts
        )
        return render_gitconfig(pairs)

    def apply_e2_rewrite(
        self, runtime: Runtime, handle: Handle, run_id: str, bug: Bug, *, advertise_host: str
    ) -> None:
        """Install this run's URL rewrite system-wide (``/etc/gitconfig``), so it applies
        regardless of which user the agent's git commands run as (plan.md section 5.1)."""
        content = self.instead_of_config(run_id, bug, advertise_host=advertise_host)
        script = f"cat >> /etc/gitconfig <<'SEGBENCH_GITCONFIG'\n{content}SEGBENCH_GITCONFIG\n"
        runtime.exec(handle, ["sh", "-c", script], user="root", check=True, timeout=30.0)
        log.info(
            "E2 git URL rewriting installed", extra={"container": handle.name, "run_id": run_id}
        )

    # ------------------------------------------------------------------------------------ serving

    def _lock_for(self, key: str) -> threading.Lock:
        with self._lock:
            return self._build_locks.setdefault(key, threading.Lock())

    def _resolve_repo(self, run: _RunState, repo_rel: str) -> tuple[Path, dt.datetime | None, str]:
        if repo_rel == "target.git":
            with self._lock_for(f"target:{run.bug.id}"):
                path = ensure_target_mirror(self._cache_dir, run.bug)
            return path, None, run.bug.manifest.repo.url

        match = _OTHER_RE.match(repo_rel)
        if not match:
            raise MirrorError(f"cannot resolve mirrored repository for path: {repo_rel!r}")
        slug, rest = match.group("slug"), match.group("rest")
        host = self._host_by_slug.get(slug)
        if host is None:
            raise MirrorError(f"no upstream host registered for mirror slug {slug!r}")
        url = f"https://{host}/{rest}.git"
        cutoff = run.bug.manifest.source.reported_at
        with self._lock_for(f"other:{url}:{cutoff.isoformat()}"):
            path = ensure_other_mirror(self._cache_dir, url, cutoff)
        return path, cutoff, url

    def _link_repo(self, run_id: str, repo_rel: str, target: Path) -> None:
        link = self._serve_root / "run" / run_id / repo_rel
        link.parent.mkdir(parents=True, exist_ok=True)
        resolved_target = target.resolve()
        if link.is_symlink():
            if link.resolve() == resolved_target:
                return
            link.unlink()
        elif link.exists():
            shutil.rmtree(link)
        link.symlink_to(resolved_target)

    def _serve(
        self, *, method: str, path: str, query: str, content_type: str, body: bytes
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        match = _PATH_RE.match(path)
        if not match:
            raise MirrorError(f"not a mirror path: {path!r}")
        run_id, rest = match.group("run_id"), match.group("rest")
        with self._lock:
            run = self._runs.get(run_id)
        if run is None:
            raise MirrorError(f"unknown or expired run: {run_id!r}")

        repo_rel, suffix = _split_repo_path(rest)
        resolved_path, cutoff, url = self._resolve_repo(run, repo_rel)
        self._link_repo(run_id, repo_rel, resolved_path)

        status, headers, payload = _invoke_http_backend(
            project_root=self._serve_root,
            path_info=f"/run/{run_id}/{repo_rel}{suffix}",
            method=method,
            query=query,
            content_type=content_type,
            body=body,
        )
        run.netlog.write(
            method=method,
            host=url,
            path_prefix=f"/{repo_rel}",
            status=status,
            bytes_=len(payload),
            allowed=True,
            extra={"resolved_cutoff": cutoff.isoformat() if cutoff else None},
        )
        return status, headers, payload
