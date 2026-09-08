"""The leakage test suite -- the point of this phase (plan.md section 13 phase 4 acceptance: "the
agent cannot see past pre_fix_ref in either environment").

Each test boots a real LXD container wired up exactly as a benchmark run would be: the dedicated
routeless network, the egress proxy, the container-side `/etc/hosts`/`iptables` defence in depth
from phase 3 (:mod:`segbench.netpol.enforce`), and this phase's E1 seed or E2 git mirror + URL
rewrite. From inside, it proves the fix commit is unreachable, no post-cutoff object exists,
`git log` shows no future history, `fetch --unshallow`/`remote add && fetch` produce nothing new,
and (E2 only) a second, unrelated repository is servable but cut at the bug's report date.

Slow -- each test boots a real container -- and skipped when `lxc` is not installed, the same
convention as `test_runtime_smoke.py` and `segbench.netpol.verify`.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from segbench.config import Settings
from segbench.corpus.models import (
    Bug,
    BugManifest,
    BugRepo,
    BugSource,
    ChannelId,
    ChannelSpec,
    FixInfo,
    GroundTruth,
    LoadedChannel,
    Origin,
    Product,
    Tracker,
)
from segbench.netpol import enforce
from segbench.netpol.gitmirror import GitMirror, _upstream_cache_path, ensure_target_mirror, seed_e1
from segbench.netpol.policy import EgressPolicy, build_policies
from segbench.netpol.proxy import EgressProxy
from segbench.netpol.verify import _gateway_ip
from segbench.runtime.base import ContainerSpec
from segbench.runtime.base import NetworkPolicy as RuntimeNetworkPolicy
from segbench.runtime.images import load_definitions
from segbench.runtime.lxd import LXDRuntime, sanitise_name

pytestmark = pytest.mark.skipif(shutil.which("lxc") is None, reason="LXD is not installed")


def _git(repo: Path, *args: str, env: dict | None = None, check: bool = True):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=check, capture_output=True, text=True, env=env
    )


def _dated_commit(repo: Path, filename: str, content: str, message: str, date: str) -> str:
    (repo / filename).write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    _git(repo, "commit", "-q", "-m", message, env=env)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _make_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    return repo


def _seed_upstream_cache(cache_dir: Path, url: str, real_repo: Path) -> None:
    """Pre-populate the mirror's upstream cache at the exact path it would compute for `url`, so
    the tests below never touch real network even though `url` uses a real-looking scheme."""
    dest = _upstream_cache_path(cache_dir, url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(real_repo), str(dest)], check=True)


@pytest.fixture
def leakage_bug(tmp_path: Path) -> tuple[Bug, Path, str, str]:
    """Like the shared `mirror_bug` fixture, but with a real `https://` repo URL.

    Git's `insteadOf` matching requires a proper URL scheme -- confirmed by hand: a bare
    filesystem path is rejected with "fatal: invalid URL scheme name or missing '://' suffix"
    rather than ever being compared against the configured prefixes. `mirror_bug`'s plain-path
    URL is fine for the other gitmirror tests (they call `ensure_target_mirror` directly, never
    through a gitconfig rewrite), but this suite exercises the actual E2 rewrite, so the URL has
    to look real. ``.invalid`` (RFC 2606) guarantees it can never resolve for real, in case the
    rewrite or the network policy ever has a gap.

    Returns ``(bug, real_upstream_repo, fix_commit, fake_https_url)``.
    """
    real_repo = _make_repo(tmp_path, "real-upstream")
    _dated_commit(real_repo, "f.txt", "v1\n", "before the report", "2024-01-01T00:00:00Z")
    pre_fix_ref = _dated_commit(
        real_repo, "f.txt", "v2\n", "at report time", "2024-02-01T00:00:00Z"
    )
    fix_commit = _dated_commit(
        real_repo, "f.txt", "v3 - the fix\n", "the fix", "2024-03-01T00:00:00Z"
    )

    fake_url = "https://fixture.invalid/owner/target-repo.git"
    channel_path = tmp_path / "channels" / "customer_report.md"
    channel_path.parent.mkdir(parents=True, exist_ok=True)
    channel_path.write_text("something is wrong\n", encoding="utf-8")
    manifest = BugManifest(
        id="leakage-fixture",
        title="a fixture bug for the leakage suite",
        product=Product.OTHER,
        source=BugSource(
            tracker=Tracker.REPRODUCTION, reported_at=dt.datetime(2024, 2, 15, tzinfo=dt.UTC)
        ),
        repo=BugRepo(url=fake_url, pre_fix_ref=pre_fix_ref),
        channels=[
            ChannelSpec(
                id=ChannelId.CUSTOMER_REPORT,
                file=Path("channels/customer_report.md"),
                origin=Origin.VERBATIM,
            )
        ],
    )
    ground_truth = GroundTruth(root_cause="x", component="c", fix=FixInfo(commit=fix_commit))
    bug = Bug(
        directory=tmp_path,
        manifest=manifest,
        ground_truth=ground_truth,
        channels=[
            LoadedChannel(
                id=ChannelId.CUSTOMER_REPORT, origin=Origin.VERBATIM, path=channel_path, text="x"
            )
        ],
    )
    return bug, real_repo, fix_commit, fake_url


@pytest.fixture(scope="module")
def lxd() -> LXDRuntime:
    runtime = LXDRuntime()
    runtime.preflight()
    return runtime


@pytest.fixture(scope="module")
def base_image() -> str:
    return load_definitions(Path("images"))["base"].alias


def test_e1_seed_hides_all_future_history_and_denies_network(
    lxd: LXDRuntime, base_image: str, leakage_bug, tmp_path: Path
) -> None:
    bug, real_repo, fix_commit, fake_url = leakage_bug
    cache_dir = tmp_path / "cache"
    _seed_upstream_cache(cache_dir, fake_url, real_repo)

    settings = Settings()
    policy = build_policies(settings)["E1"]
    proxy = EgressProxy(settings, bind_host="0.0.0.0")
    network = enforce.ensure_network(lxd, policy)
    bridge_ip = _gateway_ip(lxd, network)
    run_id = f"leak-e1-{int(time.time())}"
    run_handle = proxy.start_run(
        run_id, policy, netlog_path=tmp_path / "netlog.jsonl", max_cost_usd=5.0
    )

    spec = ContainerSpec(
        image=base_image,
        name=sanitise_name("segbench-leak-e1"),
        network=RuntimeNetworkPolicy(
            name=policy.name, network=network, env=run_handle.env(advertise_host=bridge_ip)
        ),
        wait_for_network=True,
    )
    handle = None
    try:
        handle = lxd.create(spec)
        enforce.apply(
            lxd,
            handle,
            policy,
            proxy_host=bridge_ip,
            proxy_port=run_handle.port,
            ca_cert_pem=proxy.ca_cert_pem,
        )

        truncated = ensure_target_mirror(cache_dir, bug)
        # Raises MirrorError -- failing this test loudly -- if the in-container invariants (one
        # commit, no remotes, no shallow marker) do not hold; this is the "abort, don't degrade"
        # behaviour plan.md section 5 requires of E1 seeding.
        seed_e1(lxd, handle, truncated, host_workdir=tmp_path / "e1-workdir")

        commits = lxd.exec(
            handle, ["git", "-C", "/workspace/repo", "log", "--all", "--format=%H"], check=True
        ).stdout.split()
        assert len(commits) == 1
        assert fix_commit not in commits

        absent = lxd.exec(handle, ["git", "-C", "/workspace/repo", "cat-file", "-e", fix_commit])
        assert absent.returncode != 0

        remotes = lxd.exec(handle, ["git", "-C", "/workspace/repo", "remote", "-v"], check=True)
        assert remotes.stdout.strip() == ""

        unshallow = lxd.exec(handle, ["git", "-C", "/workspace/repo", "fetch", "--unshallow"])
        assert unshallow.returncode != 0

        lxd.exec(
            handle,
            [
                "git",
                "-C",
                "/workspace/repo",
                "remote",
                "add",
                "origin",
                "https://opendev.org/openstack/does-not-matter.git",
            ],
            check=True,
        )
        fetch = lxd.exec(handle, ["git", "-C", "/workspace/repo", "fetch", "origin"], timeout=30.0)
        assert fetch.returncode != 0  # no route out at all: E1 has no mirror and no egress
    finally:
        if handle is not None:
            lxd.destroy(handle)
        proxy.stop_run(run_id)
        proxy.shutdown()


def test_e2_clone_via_mirror_is_truncated_and_bypassing_the_rewrite_fails(
    lxd: LXDRuntime, base_image: str, leakage_bug, tmp_path: Path
) -> None:
    bug, real_repo, fix_commit, fake_url = leakage_bug
    cache_dir = tmp_path / "cache"
    _seed_upstream_cache(cache_dir, fake_url, real_repo)

    # A second, unrelated repository -- for "cloning a second, unrelated repository succeeds but
    # is cut at the report date" (plan.md section 13).
    other_repo = _make_repo(tmp_path, "other-upstream")
    _dated_commit(other_repo, "o.txt", "before cutoff\n", "before cutoff", "2024-01-10T00:00:00Z")
    _dated_commit(other_repo, "o.txt", "after cutoff\n", "after cutoff", "2024-06-01T00:00:00Z")
    other_fake_url = "https://otherfixture.invalid/owner/other-repo.git"

    settings = Settings()
    mirror = GitMirror(
        settings,
        cache_dir=cache_dir,
        bind_host="0.0.0.0",
        port=0,
        known_hosts={"fixture.invalid": "fx", "otherfixture.invalid": "ofx"},
    )
    mirror.start()
    _seed_upstream_cache(mirror.cache_dir, other_fake_url, other_repo)

    policy_defaults = build_policies(settings)["E2"]
    network = enforce.ensure_network(lxd, policy_defaults)
    bridge_ip = _gateway_ip(lxd, network)
    # The mirror's real reachable address is the bridge gateway, not the configured default --
    # same reasoning as the proxy's `advertise_host` (see EgressProxy / RunProxyHandle.env).
    policy = EgressPolicy(
        name="E2",
        inference_host=policy_defaults.inference_host,
        inference_port=policy_defaults.inference_port,
        allow_mirror=True,
        mirror_host=bridge_ip,
        mirror_port=mirror.port,
        description=policy_defaults.description,
    )

    proxy = EgressProxy(settings, bind_host="0.0.0.0")
    run_id = f"leak-e2-{int(time.time())}"
    netlog_path = tmp_path / "netlog.jsonl"
    run_handle = proxy.start_run(run_id, policy, netlog_path=netlog_path, max_cost_usd=5.0)
    mirror.start_run(run_id, bug, netlog_path=netlog_path)

    spec = ContainerSpec(
        image=base_image,
        name=sanitise_name("segbench-leak-e2"),
        network=RuntimeNetworkPolicy(
            name=policy.name, network=network, env=run_handle.env(advertise_host=bridge_ip)
        ),
        wait_for_network=True,
    )
    handle = None
    try:
        handle = lxd.create(spec)
        enforce.apply(
            lxd,
            handle,
            policy,
            proxy_host=bridge_ip,
            proxy_port=run_handle.port,
            ca_cert_pem=proxy.ca_cert_pem,
        )
        mirror.apply_e2_rewrite(lxd, handle, run_id, bug, advertise_host=bridge_ip)

        clone = lxd.exec(handle, ["git", "clone", "-q", fake_url, "/workspace/clone"], timeout=60.0)
        assert clone.returncode == 0, clone.stderr

        commits = lxd.exec(
            handle, ["git", "-C", "/workspace/clone", "log", "--all", "--format=%H"], check=True
        ).stdout.split()
        assert len(commits) == 1
        absent = lxd.exec(handle, ["git", "-C", "/workspace/clone", "cat-file", "-e", fix_commit])
        assert absent.returncode != 0
        content = lxd.exec(handle, ["cat", "/workspace/clone/f.txt"], check=True).stdout
        assert content == "v2\n"

        unshallow = lxd.exec(handle, ["git", "-C", "/workspace/clone", "fetch", "--unshallow"])
        assert unshallow.returncode != 0

        # Bypassing the rewrite (as an agent that works around it, or a host this deployment did
        # not anticipate, would): must fail on the network policy alone, never silently reach the
        # real upstream (plan.md section 13's explicit requirement).
        direct = lxd.exec(
            handle,
            [
                "git",
                "clone",
                "-q",
                "https://github.com/octocat/Hello-World.git",
                "/workspace/direct",
            ],
            env={"GIT_CONFIG_NOSYSTEM": "1"},
            timeout=30.0,
        )
        assert direct.returncode != 0

        clone_other = lxd.exec(
            handle, ["git", "clone", "-q", other_fake_url, "/workspace/other"], timeout=30.0
        )
        assert clone_other.returncode == 0, clone_other.stderr
        other_content = lxd.exec(handle, ["cat", "/workspace/other/o.txt"], check=True).stdout
        assert other_content == "before cutoff\n"
    finally:
        if handle is not None:
            lxd.destroy(handle)
        proxy.stop_run(run_id)
        proxy.shutdown()
        mirror.stop_run(run_id)
        mirror.stop()
