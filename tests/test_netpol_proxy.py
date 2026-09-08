"""Drives the egress proxy directly: the allowlist, leak tagging, and the cost meter tripping.

The "allowed" path is exercised over plain HTTP against a fake upstream rather than a full
CONNECT/TLS round trip, deliberately: the usage-parsing and cost-metering logic in
``_ProxyHandler._forward`` is shared verbatim between the CONNECT (MITM) path and the plain-HTTP
path (see proxy.py's module docstring), so a plain-HTTP test exercises exactly the same code
without needing a client that trusts the proxy's throwaway CA. One additional test proves the
CONNECT path itself denies before ever touching TLS, which is the part that is different.
"""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from segbench.config import ModelPrice, NetpolConfig, Settings
from segbench.netpol.policy import EgressPolicy
from segbench.netpol.proxy import EgressProxy


class _FakeUpstreamHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b""
        self.server.requests.append({"body": body, "headers": dict(self.headers.items())})
        self.server.response_fn(self, body)


class _FakeUpstream(http.server.HTTPServer):
    def __init__(self, response_fn: Callable[[_FakeUpstreamHandler, bytes], None]) -> None:
        super().__init__(("127.0.0.1", 0), _FakeUpstreamHandler)
        self.response_fn = response_fn
        self.requests: list[dict[str, object]] = []


@pytest.fixture
def fake_upstream():
    servers: list[_FakeUpstream] = []

    def start(response_fn: Callable[[_FakeUpstreamHandler, bytes], None]) -> _FakeUpstream:
        server = _FakeUpstream(response_fn)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        return server

    yield start

    for server in servers:
        server.shutdown()
        server.server_close()


def _json_usage_response(prompt: int = 10, completion: int = 20):
    def respond(handler: _FakeUpstreamHandler, body: bytes) -> None:
        payload = json.dumps(
            {
                "id": "cmpl-1",
                "choices": [{"message": {"content": "hello"}}],
                "usage": {
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "total_tokens": prompt + completion,
                },
            }
        ).encode()
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    return respond


def _sse_usage_response(handler: _FakeUpstreamHandler, body: bytes) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.end_headers()
    for chunk in (
        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
        b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":7,'
        b'"total_tokens":12}}\n\n',
        b"data: [DONE]\n\n",
    ):
        handler.wfile.write(chunk)
        handler.wfile.flush()


@pytest.fixture
def proxy_settings() -> Settings:
    return Settings(
        netpol=NetpolConfig(
            bind_host="127.0.0.1",
            pricing={
                "test-model": ModelPrice(input_per_million_usd=1.0, output_per_million_usd=2.0)
            },
        )
    )


@pytest.fixture
def proxy(proxy_settings: Settings):
    p = EgressProxy(proxy_settings, verify_upstream=False)
    yield p
    p.shutdown()


def _read_netlog(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_allowed_request_is_forwarded_and_metered(fake_upstream, proxy, tmp_path: Path) -> None:
    upstream = fake_upstream(_json_usage_response(prompt=10, completion=20))
    policy = EgressPolicy(
        name="TEST", inference_host="127.0.0.1", inference_port=upstream.server_port
    )
    netlog_path = tmp_path / "netlog.jsonl"
    handle = proxy.start_run("run-allowed", policy, netlog_path=netlog_path, max_cost_usd=5.0)

    client = httpx.Client(proxy=f"http://127.0.0.1:{handle.port}")
    response = client.post(
        f"http://127.0.0.1:{upstream.server_port}/v1/chat/completions",
        json={"model": "test-model", "messages": []},
        headers={"Authorization": "Bearer sk-super-secret-key"},
    )

    assert response.status_code == 200
    assert response.json()["usage"]["prompt_tokens"] == 10

    entries = _read_netlog(netlog_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["allowed"] is True
    assert entry["host"] == "127.0.0.1"
    assert entry["usage"]["prompt_tokens"] == 10
    assert entry["usage"]["completion_tokens"] == 20
    assert entry["usage"]["estimated"] is False
    assert entry["usage"]["usd"] == pytest.approx(10 / 1_000_000 * 1.0 + 20 / 1_000_000 * 2.0)

    raw_netlog_text = netlog_path.read_text()
    assert "sk-super-secret-key" not in raw_netlog_text
    assert "hello" not in raw_netlog_text  # response body content never logged, only usage counts

    proxy.stop_run("run-allowed")


def test_streaming_usage_is_extracted_from_the_terminating_sse_event(
    fake_upstream, proxy, tmp_path: Path
) -> None:
    upstream = fake_upstream(_sse_usage_response)
    policy = EgressPolicy(
        name="TEST", inference_host="127.0.0.1", inference_port=upstream.server_port
    )
    netlog_path = tmp_path / "netlog.jsonl"
    handle = proxy.start_run("run-stream", policy, netlog_path=netlog_path, max_cost_usd=5.0)

    client = httpx.Client(proxy=f"http://127.0.0.1:{handle.port}")
    with client.stream(
        "POST",
        f"http://127.0.0.1:{upstream.server_port}/v1/chat/completions",
        json={"model": "test-model", "messages": [], "stream": True},
    ) as response:
        chunks = list(response.iter_lines())

    assert any("hi" in c for c in chunks)
    entries = _read_netlog(netlog_path)
    assert entries[0]["usage"]["prompt_tokens"] == 5
    assert entries[0]["usage"]["completion_tokens"] == 7
    assert entries[0]["usage"]["estimated"] is False

    proxy.stop_run("run-stream")


def test_unpriced_model_is_metered_in_tokens_and_marked_estimated(
    fake_upstream, proxy, tmp_path: Path
) -> None:
    upstream = fake_upstream(_json_usage_response(prompt=3, completion=4))
    policy = EgressPolicy(
        name="TEST", inference_host="127.0.0.1", inference_port=upstream.server_port
    )
    netlog_path = tmp_path / "netlog.jsonl"
    handle = proxy.start_run("run-unpriced", policy, netlog_path=netlog_path, max_cost_usd=5.0)

    client = httpx.Client(proxy=f"http://127.0.0.1:{handle.port}")
    client.post(
        f"http://127.0.0.1:{upstream.server_port}/v1/chat/completions",
        json={"model": "some/unpriced-model", "messages": []},
    )

    entry = _read_netlog(netlog_path)[0]
    assert entry["usage"]["estimated"] is True
    assert entry["usage"]["usd"] == 0.0
    assert entry["usage"]["prompt_tokens"] == 3

    proxy.stop_run("run-unpriced")


def test_non_allowlisted_plain_host_is_denied_and_not_a_leak(
    fake_upstream, proxy, tmp_path: Path
) -> None:
    """A host absent from the allowlist but *not* on the tracker/search/index watchlist is denied
    like everything else, but must not be tagged as an attempted leak."""
    upstream = fake_upstream(_json_usage_response())
    policy = EgressPolicy(
        name="TEST", inference_host="127.0.0.1", inference_port=upstream.server_port
    )
    netlog_path = tmp_path / "netlog.jsonl"
    handle = proxy.start_run("run-denied", policy, netlog_path=netlog_path, max_cost_usd=5.0)

    client = httpx.Client(proxy=f"http://127.0.0.1:{handle.port}")
    response = client.post("http://internal.example.test:8080/anything", json={})

    assert response.status_code == 403
    entry = _read_netlog(netlog_path)[0]
    assert entry["allowed"] is False
    assert entry["leak_attempt"] is False

    proxy.stop_run("run-denied")


def test_watchlisted_host_denial_via_connect_is_tagged_as_a_leak_attempt(
    fake_upstream, proxy, tmp_path: Path
) -> None:
    """CONNECT to a tracker/search/index host must be denied before any TLS handshake, and tagged
    distinctly from an ordinary denial (plan.md section 5.1)."""
    upstream = fake_upstream(_json_usage_response())
    policy = EgressPolicy(
        name="TEST", inference_host="127.0.0.1", inference_port=upstream.server_port
    )
    netlog_path = tmp_path / "netlog.jsonl"
    handle = proxy.start_run("run-leak", policy, netlog_path=netlog_path, max_cost_usd=5.0)

    client = httpx.Client(proxy=f"http://127.0.0.1:{handle.port}")
    with pytest.raises(httpx.HTTPError):
        client.get("https://github.com/", timeout=5.0)

    entry = _read_netlog(netlog_path)[0]
    assert entry["allowed"] is False
    assert entry["leak_attempt"] is True
    assert entry["host"] == "github.com"

    proxy.stop_run("run-leak")


def test_cost_meter_trips_at_the_ceiling_and_rejects_further_requests(
    fake_upstream, proxy, tmp_path: Path
) -> None:
    upstream = fake_upstream(_json_usage_response(prompt=10, completion=20))
    policy = EgressPolicy(
        name="TEST", inference_host="127.0.0.1", inference_port=upstream.server_port
    )
    netlog_path = tmp_path / "netlog.jsonl"
    # Each request costs 10/1e6*1.0 + 20/1e6*2.0 = 0.00005 USD; the ceiling trips on the 2nd.
    handle = proxy.start_run("run-cost", policy, netlog_path=netlog_path, max_cost_usd=0.00007)

    client = httpx.Client(proxy=f"http://127.0.0.1:{handle.port}")

    def call() -> httpx.Response:
        return client.post(
            f"http://127.0.0.1:{upstream.server_port}/v1/chat/completions",
            json={"model": "test-model", "messages": []},
        )

    first, second, third = call(), call(), call()

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 402
    assert handle.meter.tripped is True
    assert len(upstream.requests) == 2  # the 3rd request never reached upstream

    entries = _read_netlog(netlog_path)
    assert [e["status"] for e in entries] == [200, 200, 402]

    proxy.stop_run("run-cost")
