"""The host-side egress proxy (plan.md sections 5.1 and 5.2).

One :class:`EgressProxy` per campaign, matching the phase-3 spec. **Per-run identification is by
listening port, not by a header.** The alternative — a run token injected as an env var and sent
back as a header on every request — was considered and rejected: it requires every HTTP client we
might ever point at this proxy (``opencode``, whatever OpenRouter/Copilot client it embeds) to
forward a custom header unmodified on both plain requests and the ``CONNECT`` line, which is not
something we can audit for a closed-source or third-party client, and a client that silently drops
it would misattribute another run's traffic without any visible failure. A dedicated ephemeral
listener per run costs nothing — the OS hands out the port — and makes attribution unconditional:
whichever socket a byte arrived on says which run it belongs to. "One instance per campaign" is
satisfied at the process level: one :class:`EgressProxy` object owns every run's listener and the
one shared CA.

**Cost metering requires decrypting the one allow-listed host.** A blind ``CONNECT`` tunnel can
enforce the allowlist (it only needs to see the ``CONNECT`` line) but cannot show us the response
body, and plan.md section 5.2 requires parsing ``usage`` out of that body, including the
terminating SSE event for streaming. So the proxy terminates TLS for the inference host only,
using a throwaway CA generated fresh per campaign (:class:`CertificateAuthority`), and
:mod:`segbench.netpol.enforce` installs that CA into the container's trust store. Every other host
is rejected before a TLS handshake would even begin — nothing else is ever decrypted.

Both the ``CONNECT`` (MITM) path and the plain-HTTP path (used by the git mirror in E2, and handy
for testing the metering logic without a TLS round trip) end up funnelled through the same
:meth:`_ProxyHandler._forward`, so the usage-parsing and cost-tallying logic is exercised
identically either way.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import http.client
import http.server
import ipaddress
import json
import socketserver
import ssl
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from segbench.config import ModelPrice, Settings
from segbench.logging import get_logger
from segbench.netpol.policy import EgressPolicy, is_leak_target

log = get_logger(__name__)

#: Headers stripped before forwarding upstream: proxy/hop-by-hop framing plus ``Host``, which
#: ``http.client`` derives itself from the connection's own host/port and would otherwise send
#: twice.
_STRIP_HEADERS = frozenset(
    {
        "connection",
        "proxy-connection",
        "keep-alive",
        "transfer-encoding",
        "upgrade",
        "te",
        "trailer",
        "proxy-authenticate",
        "proxy-authorization",
        "host",
        "content-length",
    }
)

_TIKTOKEN_ENCODING = None


def _encoding():
    """Lazily load the tiktoken encoding used for the estimate fallback (plan.md section 5.2)."""
    global _TIKTOKEN_ENCODING
    if _TIKTOKEN_ENCODING is None:
        import tiktoken

        _TIKTOKEN_ENCODING = tiktoken.get_encoding("cl100k_base")
    return _TIKTOKEN_ENCODING


def _estimate_usage(request_body: bytes, response_text: str) -> dict[str, int]:
    """Fall back to a tiktoken token count when a provider omits ``usage`` entirely."""
    enc = _encoding()
    prompt = len(enc.encode(request_body.decode("utf-8", errors="replace"))) if request_body else 0
    completion = len(enc.encode(response_text)) if response_text else 0
    return {"prompt_tokens": prompt, "completion_tokens": completion}


def _path_prefix(path: str, max_segments: int = 2) -> str:
    """Truncate a request path to its first ``max_segments`` segments, query string dropped.

    A query string can carry an API key or other sensitive parameter; the netlog only ever needs
    enough of the path to tell "this looked like a chat completions call" from "this looked like a
    models list", not the full request.
    """
    clean = path.split("?", 1)[0]
    parts = [p for p in clean.split("/") if p]
    return "/" + "/".join(parts[:max_segments])


def _looks_like_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


class CertificateAuthority:
    """A throwaway CA generated fresh per campaign, used only to MITM the inference host.

    Never persisted to disk outside a per-instance temp directory, never reused across campaigns.
    Compromising the ability to decrypt *one specific, allow-listed* host is the deliberate
    trade-off that makes cost metering over HTTPS possible at all (see the module docstring).
    """

    def __init__(self) -> None:
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        common_name = "segbench egress proxy (do not trust globally)"
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
        now = dt.datetime.now(dt.UTC)
        self._cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(self._key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=7))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    key_cert_sign=True,
                    crl_sign=True,
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .sign(self._key, hashes.SHA256())
        )
        self._tmpdir = tempfile.TemporaryDirectory(prefix="segbench-proxy-ca-")
        self._ca_cert_path = Path(self._tmpdir.name) / "ca.pem"
        self._ca_cert_path.write_bytes(self.cert_pem)
        self._leaf_cache: dict[str, tuple[str, str]] = {}
        self._lock = threading.Lock()

    @property
    def cert_pem(self) -> bytes:
        return self._cert.public_bytes(serialization.Encoding.PEM)

    @property
    def cert_path(self) -> Path:
        return self._ca_cert_path

    def leaf_files_for(self, host: str) -> tuple[str, str]:
        """Return ``(cert_path, key_path)`` for a leaf certificate covering ``host``, generating
        and caching it on first use."""
        with self._lock:
            cached = self._leaf_cache.get(host)
            if cached:
                return cached
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
            san = x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address(host))]
                if _looks_like_ip(host)
                else [x509.DNSName(host)]
            )
            now = dt.datetime.now(dt.UTC)
            cert = (
                x509.CertificateBuilder()
                .subject_name(name)
                .issuer_name(self._cert.subject)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(minutes=5))
                .not_valid_after(now + dt.timedelta(days=7))
                .add_extension(san, critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .sign(self._key, hashes.SHA256())
            )
            cert_path = Path(self._tmpdir.name) / f"{host}.crt"
            key_path = Path(self._tmpdir.name) / f"{host}.key"
            cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            key_path.write_bytes(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.TraditionalOpenSSL,
                    serialization.NoEncryption(),
                )
            )
            result = (str(cert_path), str(key_path))
            self._leaf_cache[host] = result
            return result

    def close(self) -> None:
        self._tmpdir.cleanup()


class NetlogWriter:
    """Appends one JSON line per proxied or denied request to ``netlog.jsonl`` for one run.

    Never writes a request or response body, and never a header — only method, host, a truncated
    path, status, byte count, timestamp and (for metered inference calls) the numeric usage block.
    The API key travels in an ``Authorization`` header on the *upstream* leg the netlog never
    touches.
    """

    def __init__(self, path: Path, run_id: str) -> None:
        self._path = path
        self._run_id = run_id
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        *,
        method: str,
        host: str,
        path_prefix: str,
        status: int,
        bytes_: int,
        allowed: bool,
        leak_attempt: bool = False,
        usage: Mapping[str, object] | None = None,
        extra: Mapping[str, object] | None = None,
    ) -> None:
        entry: dict[str, object] = {
            "ts": dt.datetime.now(dt.UTC).isoformat(),
            "run_id": self._run_id,
            "method": method,
            "host": host,
            "path": path_prefix,
            "status": status,
            "bytes": bytes_,
            "allowed": allowed,
            "leak_attempt": leak_attempt,
        }
        if usage is not None:
            entry["usage"] = dict(usage)
        if extra:
            entry.update(extra)
        line = json.dumps(entry, default=str)
        with self._lock, self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


class CostMeter:
    """Per-run token and USD tally (plan.md section 5.2).

    ``tripped`` latches once the tally reaches ``max_cost_usd``; the handler checks it before
    forwarding each new request and returns 402 without ever making the upstream call, so cost
    cannot overshoot the ceiling by more than one in-flight request.
    """

    def __init__(
        self, *, run_id: str, max_cost_usd: float, pricing: Mapping[str, ModelPrice]
    ) -> None:
        self.run_id = run_id
        self.max_cost_usd = max_cost_usd
        self.pricing = pricing
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.usd = 0.0
        self.tripped = False
        self.tripped_at: float | None = None
        self._lock = threading.Lock()

    def check_before_request(self) -> bool:
        """False once the ceiling has been reached; the caller must 402 instead of forwarding."""
        return not self.tripped

    def add_usage(
        self, model: str | None, usage: Mapping[str, object] | None, *, estimated: bool
    ) -> dict[str, object]:
        prompt = int((usage or {}).get("prompt_tokens", 0) or 0)
        completion = int((usage or {}).get("completion_tokens", 0) or 0)
        price = self.pricing.get(model) if model else None
        usd = 0.0
        if price is not None:
            usd = (prompt / 1_000_000) * price.input_per_million_usd
            usd += (completion / 1_000_000) * price.output_per_million_usd
        else:
            estimated = True  # no price on file: tokens are real, the dollar figure is a guess

        with self._lock:
            self.prompt_tokens += prompt
            self.completion_tokens += completion
            self.usd += usd
            if self.usd >= self.max_cost_usd and not self.tripped:
                self.tripped = True
                self.tripped_at = time.time()

        return {
            "model": model,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "usd": round(usd, 6),
            "estimated": estimated,
        }


class _UsageExtractor:
    """Pulls the ``usage`` block out of a response as it streams past, line by line.

    Handles both a single-shot JSON body (compact JSON has no embedded newline, so it arrives as
    one "line") and an SSE stream, where OpenAI/OpenRouter-style APIs put ``usage`` on the final
    ``data: {...}`` event when the request asked for it. Also accumulates the response text so a
    caller can fall back to a token estimate when no provider-supplied usage is found at all.
    """

    def __init__(self, *, is_sse: bool) -> None:
        self.is_sse = is_sse
        self.usage: dict[str, object] | None = None
        self._chunks: list[str] = []

    def feed_line(self, line: bytes) -> None:
        text = line.decode("utf-8", errors="replace")
        self._chunks.append(text)
        if not self.is_sse:
            return
        stripped = text.strip()
        if not stripped.startswith("data:"):
            return
        payload = stripped[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            return
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            return
        usage = obj.get("usage")
        if usage:
            self.usage = usage

    def finish(self) -> dict[str, object] | None:
        if self.usage is not None:
            return self.usage
        if self.is_sse:
            return None
        try:
            obj = json.loads("".join(self._chunks))
        except json.JSONDecodeError:
            return None
        usage = obj.get("usage")
        return usage if isinstance(usage, dict) else None

    @property
    def text(self) -> str:
        return "".join(self._chunks)


def _peek_model_and_stream(body: bytes) -> tuple[str | None, bool]:
    if not body:
        return None, False
    try:
        obj = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, False
    if not isinstance(obj, dict):
        return None, False
    return obj.get("model"), bool(obj.get("stream", False))


def _read_request(rfile) -> tuple[str, str, http.client.HTTPMessage, bytes] | None:
    """Parse one HTTP/1.1 request off a buffered stream. ``None`` on a closed/empty connection."""
    request_line = rfile.readline(65536)
    if not request_line:
        return None
    try:
        method, path, _version = request_line.decode("latin-1").strip().split(" ", 2)
    except ValueError:
        return None
    headers = http.client.parse_headers(rfile)
    length = int(headers.get("Content-Length", "0") or "0")
    body = rfile.read(length) if length else b""
    return method, path, headers, body


def _write_status_and_headers(
    wfile, status: int, reason: str, headers: list[tuple[str, str]]
) -> None:
    wfile.write(f"HTTP/1.1 {status} {reason}\r\n".encode("latin-1"))
    for key, value in headers:
        wfile.write(f"{key}: {value}\r\n".encode("latin-1", errors="replace"))
    wfile.write(b"\r\n")
    wfile.flush()


class _ProxyHandler(http.server.BaseHTTPRequestHandler):
    """One connection's worth of proxying. Attributes on ``self.server`` carry the per-run state
    (:class:`_RunServer`) since a fresh handler instance is built per connection."""

    protocol_version = "HTTP/1.1"
    server: _RunServer

    def log_message(self, format: str, *args: object) -> None:
        pass  # netlog + segbench's own logger are the record; suppress BaseHTTPRequestHandler's.

    # ------------------------------------------------------------------ CONNECT (HTTPS)

    def do_CONNECT(self) -> None:
        host, _, port_s = self.path.partition(":")
        port = int(port_s) if port_s else 443
        server = self.server
        self.close_connection = True

        if not server.policy.permits(host, port):
            self._deny(host, port, method="CONNECT", path="")
            return

        self.send_response(200, "Connection Established")
        self.end_headers()
        cert_path, key_path = server.ca.leaf_files_for(host)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert_path, key_path)
        try:
            tls = ctx.wrap_socket(self.connection, server_side=True)
        except ssl.SSLError as exc:
            log.warning(
                "TLS handshake with the container failed",
                extra={"run_id": server.run_id, "host": host, "error": str(exc)},
            )
            return

        self.connection = tls
        self.rfile = tls.makefile("rb")
        self.wfile = tls.makefile("wb")
        try:
            parsed = _read_request(self.rfile)
            if parsed is None:
                return
            method, path, headers, body = parsed
            self._forward(
                method=method,
                path=path,
                headers=headers,
                body=body,
                host=host,
                port=port,
                use_tls=True,
            )
        finally:
            with contextlib.suppress(Exception):
                tls.unwrap()

    # ------------------------------------------------------------------ plain HTTP

    def _do_plain(self, method: str) -> None:
        self.close_connection = True
        parsed = urlsplit(self.path)
        host = parsed.hostname or ""
        port = parsed.port or 80
        server = self.server

        if not server.policy.permits(host, port):
            self._deny(host, port, method=method, path=parsed.path)
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b""
        target = parsed.path or "/"
        if parsed.query:
            target += f"?{parsed.query}"
        self._forward(
            method=method,
            path=target,
            headers=self.headers,
            body=body,
            host=host,
            port=port,
            use_tls=False,
        )

    def do_GET(self) -> None:
        self._do_plain("GET")

    def do_POST(self) -> None:
        self._do_plain("POST")

    def do_PUT(self) -> None:
        self._do_plain("PUT")

    def do_PATCH(self) -> None:
        self._do_plain("PATCH")

    def do_DELETE(self) -> None:
        self._do_plain("DELETE")

    # ------------------------------------------------------------------ shared

    def _deny(self, host: str, port: int, *, method: str, path: str) -> None:
        # Logged *before* the response is written: a caller must never be able to observe a
        # denial that the netlog does not yet contain, so there is no window in which a test (or
        # an auditor) reading the netlog right after the client sees a 403 could find it empty.
        leak = is_leak_target(host)
        self.server.netlog.write(
            method=method,
            host=host,
            path_prefix=_path_prefix(path),
            status=403,
            bytes_=0,
            allowed=False,
            leak_attempt=leak,
        )
        (log.warning if leak else log.info)(
            "egress denied",
            extra={"run_id": self.server.run_id, "host": host, "port": port, "leak_attempt": leak},
        )
        self._write_simple(403, b'{"error":"forbidden"}')

    def _write_simple(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def _forward(
        self,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
        host: str,
        port: int,
        use_tls: bool,
    ) -> None:
        """Forward one request to ``host:port``, buffer and meter the response, then relay it."""
        server = self.server
        model, requested_stream = _peek_model_and_stream(body)

        if not server.meter.check_before_request():
            server.netlog.write(
                method=method,
                host=host,
                path_prefix=_path_prefix(path),
                status=402,
                bytes_=0,
                allowed=True,
            )
            self._write_simple(402, b'{"error":"run cost ceiling exceeded"}')
            return

        out_headers = {k: v for k, v in headers.items() if k.lower() not in _STRIP_HEADERS}
        out_headers["Connection"] = "close"
        conn: http.client.HTTPConnection
        try:
            if use_tls:
                # Non-default only for tests, against a fake upstream with a self-signed cert.
                ctx = (
                    ssl.create_default_context()
                    if server.verify_upstream
                    else ssl._create_unverified_context()
                )
                conn = http.client.HTTPSConnection(host, port, timeout=280.0, context=ctx)
            else:
                conn = http.client.HTTPConnection(host, port, timeout=280.0)
            conn.request(method, path, body=body or None, headers=out_headers)
            resp = conn.getresponse()
        except OSError as exc:
            log.warning(
                "upstream unreachable",
                extra={"run_id": server.run_id, "host": host, "error": str(exc)},
            )
            server.netlog.write(
                method=method,
                host=host,
                path_prefix=_path_prefix(path),
                status=502,
                bytes_=0,
                allowed=True,
            )
            self._write_simple(502, b'{"error":"upstream unreachable"}')
            return

        content_type = resp.getheader("Content-Type") or ""
        is_sse = requested_stream or "text/event-stream" in content_type
        extractor = _UsageExtractor(is_sse=is_sse)

        # Read the whole upstream response before writing anything back to the client. This
        # costs true incremental streaming to the agent, but buys a hard guarantee that matters
        # more here: the netlog entry (and the cost tally) for this request always exists before
        # the client can possibly have seen any part of the response, with no race window.
        chunks: list[bytes] = []
        while True:
            chunk = resp.readline()
            if not chunk:
                break
            chunks.append(chunk)
            extractor.feed_line(chunk)
        conn.close()
        total = sum(len(c) for c in chunks)

        usage = extractor.finish()
        estimated = False
        if usage is None:
            if resp.status < 400:
                usage = _estimate_usage(body, extractor.text)
                estimated = True
            else:
                usage = {"prompt_tokens": 0, "completion_tokens": 0}
        metered = server.meter.add_usage(model, usage, estimated=estimated)
        server.netlog.write(
            method=method,
            host=host,
            path_prefix=_path_prefix(path),
            status=resp.status,
            bytes_=total,
            allowed=True,
            usage=metered,
        )

        response_headers = [
            (k, v)
            for k, v in resp.getheaders()
            if k.lower() not in _STRIP_HEADERS and k.lower() != "content-length"
        ]
        response_headers.append(("Connection", "close"))
        _write_status_and_headers(self.wfile, resp.status, resp.reason, response_headers)
        for chunk in chunks:
            self.wfile.write(chunk)
        self.wfile.flush()


class _RunServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """One ephemeral listener dedicated to a single run's traffic."""

    daemon_threads = True
    allow_reuse_address = True

    policy: EgressPolicy
    meter: CostMeter
    netlog: NetlogWriter
    run_id: str
    ca: CertificateAuthority
    verify_upstream: bool


@dataclass
class RunProxyHandle:
    """A running per-run listener, returned by :meth:`EgressProxy.start_run`."""

    run_id: str
    bind_host: str
    port: int
    meter: CostMeter
    netlog_path: Path
    _server: _RunServer = field(repr=False)
    _thread: threading.Thread = field(repr=False)

    def env(self, advertise_host: str | None = None) -> dict[str, str]:
        """The ``http_proxy``/``https_proxy``/``ALL_PROXY`` env vars for this run's container.

        ``advertise_host`` is the address the *container* reaches the proxy on — usually the LXD
        bridge's gateway IP, never ``0.0.0.0`` (that is only meaningful as a bind address).
        """
        host = advertise_host or self.bind_host
        if host in ("0.0.0.0", "", "::"):
            raise ValueError(
                f"{host!r} is a bind address, not something a container can connect to; "
                f"pass advertise_host explicitly."
            )
        url = f"http://{host}:{self.port}"
        no_proxy = "localhost,127.0.0.1"
        return {
            "http_proxy": url,
            "https_proxy": url,
            "ALL_PROXY": url,
            "HTTP_PROXY": url,
            "HTTPS_PROXY": url,
            "NO_PROXY": no_proxy,
            "no_proxy": no_proxy,
        }


class EgressProxy:
    """The campaign-wide egress proxy. See the module docstring for the design rationale."""

    def __init__(
        self, settings: Settings, *, bind_host: str | None = None, verify_upstream: bool = True
    ) -> None:
        self._settings = settings
        self._bind_host = bind_host or settings.netpol.bind_host
        self._verify_upstream = verify_upstream
        self._ca = CertificateAuthority()
        self._runs: dict[str, RunProxyHandle] = {}
        self._lock = threading.Lock()

    @property
    def ca_cert_pem(self) -> bytes:
        """The CA certificate to install into a container's trust store (see ``netpol.enforce``)."""
        return self._ca.cert_pem

    def start_run(
        self, run_id: str, policy: EgressPolicy, *, netlog_path: Path, max_cost_usd: float
    ) -> RunProxyHandle:
        pricing = dict(self._settings.netpol.pricing)
        meter = CostMeter(run_id=run_id, max_cost_usd=max_cost_usd, pricing=pricing)
        netlog = NetlogWriter(netlog_path, run_id)

        server = _RunServer((self._bind_host, 0), _ProxyHandler)
        server.policy = policy
        server.meter = meter
        server.netlog = netlog
        server.run_id = run_id
        server.ca = self._ca
        server.verify_upstream = self._verify_upstream

        thread = threading.Thread(
            target=server.serve_forever, name=f"segbench-proxy-{run_id}", daemon=True
        )
        thread.start()

        handle = RunProxyHandle(
            run_id=run_id,
            bind_host=self._bind_host,
            port=server.server_address[1],
            meter=meter,
            netlog_path=netlog_path,
            _server=server,
            _thread=thread,
        )
        with self._lock:
            self._runs[run_id] = handle
        log.info(
            "proxy listening for run",
            extra={"run_id": run_id, "port": handle.port, "policy": policy.name},
        )
        return handle

    def stop_run(self, run_id: str) -> None:
        with self._lock:
            handle = self._runs.pop(run_id, None)
        if handle is None:
            return
        handle._server.shutdown()
        handle._server.server_close()
        handle._thread.join(timeout=5.0)

    def shutdown(self) -> None:
        for run_id in list(self._runs):
            self.stop_run(run_id)
        self._ca.close()
