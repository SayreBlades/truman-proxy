"""
Secret-injection MITM forward proxy.

Runs as a sidecar container alongside the pi agent. Intercepts HTTPS for
configured hosts (injecting real API credentials), blind-tunnels everything
else, and forwards plain HTTP as-is.

Uses raw asyncio for the proxy server (needed for CONNECT / TLS upgrade)
and aiohttp ClientSession for upstream HTTPS requests.

See docs/plan-phase-2.md for full design.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import json as json_mod
import logging
import os
import queue as queue_mod
import re
import socket as socket_mod
import ssl
import tempfile
import time
from pathlib import Path
from typing import Optional

import aiohttp
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

# ── Logging ──────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("gateway")

# ── Anthropic OAuth Token Manager ────────────────────────────────────

ANTHROPIC_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
ANTHROPIC_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
# Refresh 5 minutes before actual expiry
TOKEN_REFRESH_MARGIN_S = 5 * 60


class AnthropicTokenManager:
    """Manages Anthropic OAuth access tokens via refresh token.

    Holds a long-lived refresh token (from .env) and uses it to obtain
    short-lived access tokens.  Tokens are refreshed proactively before
    expiry and reactively on 401 responses.

    The latest refresh token is persisted to disk so that gateway restarts
    survive token rotation (Anthropic invalidates old refresh tokens when
    issuing new ones).
    """

    def __init__(self, refresh_token: str) -> None:
        # Try to load a previously-persisted (rotated) token first
        persisted = self._load_persisted()
        if persisted:
            log.info("Loaded persisted refresh token from %s", self._token_path())
            self._refresh_token = persisted
        else:
            self._refresh_token = refresh_token
        self._access_token: Optional[str] = None
        self._expires_at: float = 0  # unix timestamp (seconds)
        self._lock = asyncio.Lock()

    async def get_access_token(self, http_session: aiohttp.ClientSession) -> str:
        """Return a valid access token, refreshing if necessary."""
        if self._access_token and time.time() < self._expires_at:
            return self._access_token
        return await self._refresh(http_session)

    async def force_refresh(self, http_session: aiohttp.ClientSession) -> str:
        """Force a token refresh (e.g. after a 401)."""
        return await self._refresh(http_session)

    async def _refresh(self, http_session: aiohttp.ClientSession) -> str:
        async with self._lock:
            # Double-check after acquiring lock (another coroutine may have refreshed)
            if self._access_token and time.time() < self._expires_at:
                return self._access_token

            log.info("Refreshing Anthropic OAuth access token...")
            async with http_session.post(
                ANTHROPIC_TOKEN_URL,
                json={
                    "grant_type": "refresh_token",
                    "client_id": ANTHROPIC_CLIENT_ID,
                    "refresh_token": self._refresh_token,
                },
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            ) as resp:
                body = await resp.text()
                if not resp.ok:
                    log.error("Token refresh failed: %d %s", resp.status, body)
                    raise RuntimeError(f"Anthropic token refresh failed: {resp.status} {body}")
                data = json_mod.loads(body)

            self._access_token = data["access_token"]
            expires_in = data.get("expires_in", 3600)
            self._expires_at = time.time() + expires_in - TOKEN_REFRESH_MARGIN_S
            # Update and persist refresh token if rotated
            if data.get("refresh_token"):
                self._refresh_token = data["refresh_token"]
                self._persist()
            log.info("Anthropic OAuth token refreshed (expires in %ds)", expires_in)
            return self._access_token

    @staticmethod
    def _token_path() -> Path:
        return Path(os.environ.get("CA_DIR", "/data")) / "anthropic_token.json"

    def _persist(self) -> None:
        """Save the current refresh token to disk for restart survival."""
        try:
            p = self._token_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json_mod.dumps({"refresh_token": self._refresh_token}))
            log.info("Persisted rotated refresh token to %s", p)
        except Exception as exc:
            log.warning("Failed to persist refresh token: %s", exc)

    @staticmethod
    def _load_persisted() -> Optional[str]:
        """Load a previously-persisted refresh token, if any."""
        try:
            p = AnthropicTokenManager._token_path()
            if p.exists():
                data = json_mod.loads(p.read_text())
                return data.get("refresh_token")
        except Exception as exc:
            log.warning("Failed to load persisted token: %s", exc)
        return None


# Initialized at startup if ANTHROPIC_REFRESH_TOKEN is set
_anthropic_token_mgr: Optional[AnthropicTokenManager] = None


# ── Credential injection rules ───────────────────────────────────────

INTERCEPT_RULES: dict[str, dict] = {
    "api.anthropic.com": {
        "strip_headers": ["authorization", "x-api-key"],
        "inject_headers": {
            "Authorization": "Bearer {ANTHROPIC_OAUTH_TOKEN}",
            "X-Api-Key": "{ANTHROPIC_API_KEY}",
        },
    },
    "api.search.brave.com": {
        "strip_headers": ["x-subscription-token"],
        "inject_headers": {
            "X-Subscription-Token": "{BRAVE_API_KEY}",
        },
    },
    "api.github.com": {
        "strip_headers": ["authorization"],
        "inject_headers": {
            "Authorization": "token {GH_TOKEN}",
        },
    },
    "github.com": {
        "strip_headers": ["authorization"],
        "inject_headers": {
            "Authorization": "basic {GH_TOKEN}",
        },
    },
}

# Resolved at startup
RESOLVED_RULES: dict[str, dict] = {}

# ── Resolve rules from env ───────────────────────────────────────────


def resolve_rules() -> dict[str, dict]:
    """Resolve env-var placeholders in inject_headers at startup.

    For ANTHROPIC_OAUTH_TOKEN: if ANTHROPIC_REFRESH_TOKEN is set, the
    header value is resolved dynamically at request time (marked with a
    sentinel).  Otherwise falls back to a static token from env.
    """
    resolved: dict[str, dict] = {}
    for host, rule in INTERCEPT_RULES.items():
        inject: dict[str, str] = {}
        for header, template in rule["inject_headers"].items():
            # Extract var name from e.g. "Bearer {ANTHROPIC_OAUTH_TOKEN}"
            match = re.search(r"\{(\w+)\}", template)
            if not match:
                continue
            var = match.group(1)

            # Dynamic OAuth: if we have a refresh token manager for this var,
            # mark the header for dynamic resolution at request time
            if var == "ANTHROPIC_OAUTH_TOKEN" and _anthropic_token_mgr is not None:
                inject[header] = "__DYNAMIC_ANTHROPIC_OAUTH__"
                log.info("  %s: %s → will inject %s (dynamic refresh)", host, var, header)
                continue

            value = os.environ.get(var)
            if value:
                # "basic {VAR}" → HTTP Basic auth (x-access-token:<token>)
                # Used for github.com git smart-HTTP which requires Basic auth
                if template.startswith("basic "):
                    creds = base64.b64encode(f"x-access-token:{value}".encode()).decode()
                    inject[header] = f"Basic {creds}"
                else:
                    inject[header] = template.replace(f"{{{var}}}", value)
                log.info("  %s: %s → will inject %s", host, var, header)
            else:
                log.info("  %s: %s not set, skipping %s", host, var, header)
        resolved[host] = {
            "strip_headers": set(h.lower() for h in rule["strip_headers"]),
            "inject_headers": inject,
        }
    return resolved


async def resolve_dynamic_headers(
    headers: dict[str, str],
    http_session: aiohttp.ClientSession,
) -> dict[str, str]:
    """Replace dynamic sentinel values with live tokens."""
    if not _anthropic_token_mgr:
        return headers
    result = dict(headers)
    for key, value in result.items():
        if value == "__DYNAMIC_ANTHROPIC_OAUTH__":
            token = await _anthropic_token_mgr.get_access_token(http_session)
            result[key] = f"Bearer {token}"
    return result


# ── CA Manager ───────────────────────────────────────────────────────

CA_DIR = Path(os.environ.get("CA_DIR", "/data"))
CA_KEY_PATH = CA_DIR / "ca.key"
CA_CERT_PATH = CA_DIR / "ca.pem"

_ca_key: Optional[rsa.RSAPrivateKey] = None
_ca_cert: Optional[x509.Certificate] = None
_cert_cache: dict[str, tuple[bytes, bytes]] = {}


def load_or_generate_ca() -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    """Load existing CA or generate a new one. Persists to CA_DIR."""
    CA_DIR.mkdir(parents=True, exist_ok=True)

    if CA_KEY_PATH.exists() and CA_CERT_PATH.exists():
        log.info("Loading existing CA from %s", CA_DIR)
        ca_key = serialization.load_pem_private_key(
            CA_KEY_PATH.read_bytes(), password=None
        )
        ca_cert = x509.load_pem_x509_certificate(CA_CERT_PATH.read_bytes())
        return ca_key, ca_cert  # type: ignore[return-value]

    log.info("Generating new CA key pair in %s", CA_DIR)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Gateway MITM CA"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "devcontainer-gateway"),
    ])
    now = datetime.datetime.now(datetime.UTC)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True,
                content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    CA_KEY_PATH.write_bytes(ca_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    CA_CERT_PATH.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    log.info("CA generated and saved")
    return ca_key, ca_cert


def generate_host_cert(hostname: str) -> tuple[bytes, bytes]:
    """Generate a TLS cert for *hostname* signed by our CA. Cached in-memory.

    Returns (cert_pem, key_pem).
    """
    if hostname in _cert_cache:
        return _cert_cache[hostname]

    assert _ca_key is not None and _ca_cert is not None

    host_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.UTC)
    host_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
        .issuer_name(_ca_cert.subject)
        .public_key(host_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(hostname)]),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(_ca_key, hashes.SHA256())
    )

    cert_pem = host_cert.public_bytes(serialization.Encoding.PEM)
    key_pem = host_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    _cert_cache[hostname] = (cert_pem, key_pem)
    log.info("Generated cert for %s", hostname)
    return cert_pem, key_pem


def make_server_ssl_context(cert_pem: bytes, key_pem: bytes) -> ssl.SSLContext:
    """Build an SSLContext for the server side of a MITM connection."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    # Only offer HTTP/1.1 — we don't implement HTTP/2 framing
    ctx.set_alpn_protocols(["http/1.1"])
    with tempfile.NamedTemporaryFile(delete=False, suffix=".crt") as cf, \
         tempfile.NamedTemporaryFile(delete=False, suffix=".key") as kf:
        cf.write(cert_pem)
        kf.write(key_pem)
        cf.flush()
        kf.flush()
    try:
        ctx.load_cert_chain(cf.name, kf.name)
    finally:
        os.unlink(cf.name)
        os.unlink(kf.name)
    return ctx


# ── Blind tunnel ─────────────────────────────────────────────────────

RELAY_BUF = 64 * 1024


async def blind_tunnel(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    target_reader: asyncio.StreamReader,
    target_writer: asyncio.StreamWriter,
) -> None:
    """Bidirectional TCP pipe."""

    async def relay(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await src.read(RELAY_BUF)
                if not data:
                    break
                dst.write(data)
                await dst.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError, OSError):
            pass
        finally:
            try:
                if not dst.is_closing():
                    dst.close()
            except Exception:
                pass

    await asyncio.gather(
        relay(client_reader, target_writer),
        relay(target_reader, client_writer),
    )


# ── MITM proxy ───────────────────────────────────────────────────────

async def mitm_proxy(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    hostname: str,
    port: int,
    rule: dict,
    http_session: aiohttp.ClientSession,
) -> None:
    """Terminate TLS as the MITM, read HTTP, inject creds, forward upstream."""

    # Generate host cert, upgrade connection to TLS (server-side)
    cert_pem, key_pem = generate_host_cert(hostname)
    ssl_ctx = make_server_ssl_context(cert_pem, key_pem)

    # Get the raw socket, dup it, and detach from asyncio
    transport = client_writer.transport
    raw_sock = transport.get_extra_info("socket")
    if raw_sock is None:
        log.error("MITM: cannot get raw socket for %s", hostname)
        return

    fd = os.dup(raw_sock.fileno())
    transport.abort()  # close transport without closing socket (we duped fd)

    duped_sock = socket_mod.socket(fileno=fd)

    # Do the TLS handshake in a thread (blocking)
    duped_sock.setblocking(True)
    loop = asyncio.get_event_loop()
    ssl_sock = await loop.run_in_executor(
        None, lambda: ssl_ctx.wrap_socket(duped_sock, server_side=True),
    )

    # Use blocking I/O in a thread for the entire MITM session
    await loop.run_in_executor(
        None, _mitm_sync, ssl_sock, hostname, port, rule, loop, http_session,
    )
    return


def _mitm_sync(
    ssl_sock,
    hostname: str,
    port: int,
    rule: dict,
    loop: asyncio.AbstractEventLoop,
    http_session: aiohttp.ClientSession,
) -> None:
    """Synchronous MITM handler running in a thread.

    Uses blocking socket I/O to avoid asyncio TLS upgrade issues.
    Upstream requests use aiohttp via run_coroutine_threadsafe.
    Streams response chunks back to the client for SSE support.
    """
    ssl_sock.settimeout(300)
    rfile = ssl_sock.makefile("rb")  # default buffering — ensures read(n) returns exactly n bytes

    try:
        # Read HTTP request line
        request_line = rfile.readline()
        if not request_line:
            return
        request_str = request_line.decode("utf-8", errors="replace").strip()
        parts = request_str.split(" ", 2)
        if len(parts) < 3:
            return
        method, path, _version = parts

        # Read headers
        raw_headers: list[tuple[str, str]] = []
        content_length: Optional[int] = None
        while True:
            line = rfile.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            decoded = line.decode("utf-8", errors="replace").strip()
            if ":" in decoded:
                key, value = decoded.split(":", 1)
                raw_headers.append((key.strip(), value.strip()))
                if key.strip().lower() == "content-length":
                    content_length = int(value.strip())

        # Read body
        body: Optional[bytes] = None
        if content_length and content_length > 0:
            body = rfile.read(content_length)

        # Build outgoing headers: strip dummies, inject real creds
        out_headers: dict[str, str] = {}
        strip_set = rule["strip_headers"]
        for key, value in raw_headers:
            if key.lower() not in strip_set:
                out_headers[key] = value

        for key, value in rule["inject_headers"].items():
            out_headers[key] = value

        # Resolve dynamic tokens (e.g. OAuth access tokens from refresh flow)
        future = asyncio.run_coroutine_threadsafe(
            resolve_dynamic_headers(out_headers, http_session), loop,
        )
        out_headers = future.result(timeout=30)

        # Remove hop-by-hop + Accept-Encoding (so upstream sends uncompressed;
        # avoids gzip mismatch since aiohttp auto-decompresses)
        for hdr in list(out_headers.keys()):
            if hdr.lower() in ("proxy-connection", "proxy-authorization", "accept-encoding"):
                del out_headers[hdr]

        url = f"https://{hostname}{path}" if port == 443 else f"https://{hostname}:{port}{path}"
        log.info("MITM %s %s (body: %s bytes)", method, url, len(body) if body else 0)

        # Use a thread-safe queue to stream response from async to sync
        q: queue_mod.Queue = queue_mod.Queue()

        async def do_upstream(headers_to_send: dict[str, str], retry_on_401: bool = True):
            try:
                async with http_session.request(
                    method, url,
                    headers=headers_to_send,
                    data=body,
                    allow_redirects=False,
                ) as resp:
                    # On 401 with a refresh-capable token, force-refresh and retry once
                    if resp.status == 401 and retry_on_401 and _anthropic_token_mgr:
                        resp_body = await resp.text()
                        if "expired" in resp_body.lower() or "authentication" in resp_body.lower():
                            log.warning("Got 401 from %s, forcing token refresh...", hostname)
                            new_token = await _anthropic_token_mgr.force_refresh(http_session)
                            refreshed_headers = dict(headers_to_send)
                            for k in refreshed_headers:
                                if k.lower() == "authorization" and "Bearer" in refreshed_headers[k]:
                                    refreshed_headers[k] = f"Bearer {new_token}"
                            await do_upstream(refreshed_headers, retry_on_401=False)
                            return
                    q.put(("headers", resp.status, resp.reason or "OK",
                           list(resp.headers.items())))
                    async for chunk in resp.content.iter_any():
                        q.put(("data", chunk))
                    q.put(("end",))
            except Exception as exc:
                q.put(("error", exc))

        asyncio.run_coroutine_threadsafe(do_upstream(out_headers), loop)

        # Read header message from queue
        msg = q.get(timeout=120)
        if msg[0] == "error":
            raise msg[1]
        assert msg[0] == "headers"
        _, status, reason, resp_headers = msg

        # Send status line
        ssl_sock.sendall(f"HTTP/1.1 {status} {reason}\r\n".encode())

        # Forward response headers (skip hop-by-hop + encoding-related)
        for key, value in resp_headers:
            if key.lower() in (
                "transfer-encoding", "connection", "keep-alive",
                "content-length", "content-encoding",
            ):
                continue
            ssl_sock.sendall(f"{key}: {value}\r\n".encode())

        # Use chunked Transfer-Encoding for streaming
        ssl_sock.sendall(b"Transfer-Encoding: chunked\r\n")
        ssl_sock.sendall(b"Connection: close\r\n\r\n")

        # Stream body chunks
        total_bytes = 0
        while True:
            msg = q.get(timeout=120)
            if msg[0] == "end":
                break
            elif msg[0] == "error":
                raise msg[1]
            elif msg[0] == "data":
                chunk = msg[1]
                if chunk:
                    total_bytes += len(chunk)
                    ssl_sock.sendall(f"{len(chunk):x}\r\n".encode())
                    ssl_sock.sendall(chunk)
                    ssl_sock.sendall(b"\r\n")

        # Chunked terminator
        ssl_sock.sendall(b"0\r\n\r\n")

        log.info("MITM %s %s → %d (%d bytes streamed)", method, url, status, total_bytes)

    except Exception as exc:
        log.error("MITM error for %s: %s", hostname, exc)
        try:
            err = f"Gateway error: {exc}".encode()
            ssl_sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\n")
            ssl_sock.sendall(f"Content-Length: {len(err)}\r\n\r\n".encode())
            ssl_sock.sendall(err)
        except Exception:
            pass
    finally:
        try:
            rfile.close()
        except Exception:
            pass
        try:
            ssl_sock.shutdown(2)
        except Exception:
            pass
        ssl_sock.close()


# ── Main connection handler ──────────────────────────────────────────

_http_session: Optional[aiohttp.ClientSession] = None


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Handle one incoming proxy connection."""
    peer = writer.get_extra_info("peername")

    try:
        # Read first line to determine request type
        first_line = await asyncio.wait_for(reader.readline(), timeout=30)
        if not first_line:
            writer.close()
            return

        first_str = first_line.decode("utf-8", errors="replace").strip()
        parts = first_str.split(" ", 2)
        if len(parts) < 2:
            writer.close()
            return

        method = parts[0].upper()

        # ── CONNECT (tunneling) ──────────────────────────────────
        if method == "CONNECT":
            target = parts[1]
            if ":" in target:
                hostname, port_str = target.rsplit(":", 1)
                port = int(port_str)
            else:
                hostname = target
                port = 443

            # Consume remaining headers (CONNECT has headers but no body)
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break

            rule = RESOLVED_RULES.get(hostname)
            if rule and rule["inject_headers"]:
                log.info("CONNECT %s:%d → MITM", hostname, port)
                # Send 200 Connection Established
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                # MITM the connection
                assert _http_session is not None
                await mitm_proxy(reader, writer, hostname, port, rule, _http_session)
            else:
                log.info("CONNECT %s:%d → blind tunnel", hostname, port)
                try:
                    target_reader, target_writer = await asyncio.open_connection(hostname, port)
                except Exception as exc:
                    log.error("Blind tunnel connect failed %s:%d: %s", hostname, port, exc)
                    writer.write(f"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n".encode())
                    await writer.drain()
                    writer.close()
                    return

                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                await blind_tunnel(reader, writer, target_reader, target_writer)

        # ── GET /healthz or /ca.pem (direct to gateway) ─────────
        elif len(parts) >= 2 and parts[1] in ("/healthz", "/ca.pem"):
            path = parts[1]
            # Consume headers
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break

            if path == "/healthz":
                body = b"OK"
                writer.write(b"HTTP/1.1 200 OK\r\n")
                writer.write(f"Content-Length: {len(body)}\r\n".encode())
                writer.write(b"Content-Type: text/plain\r\n\r\n")
                writer.write(body)
                await writer.drain()

            elif path == "/ca.pem":
                ca_pem = CA_CERT_PATH.read_bytes()
                writer.write(b"HTTP/1.1 200 OK\r\n")
                writer.write(f"Content-Length: {len(ca_pem)}\r\n".encode())
                writer.write(b"Content-Type: application/x-pem-file\r\n\r\n")
                writer.write(ca_pem)
                await writer.drain()

        # ── HTTP forward proxy (absolute URL) ────────────────────
        elif method in ("GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            url = parts[1]

            # Read headers
            headers: list[tuple[str, str]] = []
            content_length: Optional[int] = None
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                decoded = line.decode("utf-8", errors="replace").strip()
                if ":" in decoded:
                    key, value = decoded.split(":", 1)
                    key = key.strip()
                    value = value.strip()
                    headers.append((key, value))
                    if key.lower() == "content-length":
                        content_length = int(value)

            # Read body
            body: Optional[bytes] = None
            if content_length and content_length > 0:
                body = await reader.readexactly(content_length)

            # Forward headers (strip hop-by-hop)
            out_headers: dict[str, str] = {}
            for key, value in headers:
                if key.lower() not in ("proxy-connection", "proxy-authorization", "connection"):
                    out_headers[key] = value

            log.info("HTTP %s %s", method, url)

            assert _http_session is not None
            try:
                async with _http_session.request(
                    method, url,
                    headers=out_headers,
                    data=body,
                    allow_redirects=False,
                ) as upstream_resp:
                    resp_line = f"HTTP/1.1 {upstream_resp.status} {upstream_resp.reason or 'OK'}\r\n"
                    writer.write(resp_line.encode())
                    for key, value in upstream_resp.headers.items():
                        if key.lower() not in ("transfer-encoding", "connection"):
                            writer.write(f"{key}: {value}\r\n".encode())
                    writer.write(b"Transfer-Encoding: chunked\r\n\r\n")
                    await writer.drain()

                    async for chunk in upstream_resp.content.iter_any():
                        if chunk:
                            writer.write(f"{len(chunk):x}\r\n".encode())
                            writer.write(chunk)
                            writer.write(b"\r\n")
                            await writer.drain()
                    writer.write(b"0\r\n\r\n")
                    await writer.drain()

            except Exception as exc:
                log.error("HTTP forward error: %s", exc)
                err = f"Gateway error: {exc}".encode()
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\n")
                writer.write(f"Content-Length: {len(err)}\r\n\r\n".encode())
                writer.write(err)
                await writer.drain()

        else:
            writer.write(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()

    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError, OSError) as exc:
        log.debug("Connection error from %s: %s", peer, exc)
    except Exception as exc:
        log.error("Unhandled error from %s: %s", peer, exc, exc_info=True)
    finally:
        try:
            if not writer.is_closing():
                writer.close()
                await writer.wait_closed()
        except Exception:
            pass


# ── Main ─────────────────────────────────────────────────────────────

async def main() -> None:
    global _ca_key, _ca_cert, _http_session, RESOLVED_RULES, _anthropic_token_mgr

    log.info("Starting secret gateway...")

    # ── Initialize Anthropic OAuth token manager (if refresh token set) ──
    refresh_token = os.environ.get("ANTHROPIC_REFRESH_TOKEN")
    if refresh_token:
        log.info("ANTHROPIC_REFRESH_TOKEN set → enabling OAuth auto-refresh")
        _anthropic_token_mgr = AnthropicTokenManager(refresh_token)
    else:
        log.info("ANTHROPIC_REFRESH_TOKEN not set → using static credentials")

    log.info("Resolving credential injection rules:")
    RESOLVED_RULES = resolve_rules()

    _ca_key, _ca_cert = load_or_generate_ca()

    _http_session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(
            ssl=True,
            limit=100,
            force_close=True,  # Don't reuse connections — avoids stale keepalive
        ),
    )

    # ── Pre-fetch initial access token so first request isn't slow ──
    if _anthropic_token_mgr:
        try:
            await _anthropic_token_mgr.get_access_token(_http_session)
            log.info("Initial Anthropic access token acquired")
        except Exception as exc:
            log.error("Failed to acquire initial Anthropic token: %s", exc)
            log.error("Requests to api.anthropic.com will fail until refresh succeeds")

    server = await asyncio.start_server(handle_client, "0.0.0.0", 8080)

    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    log.info("Gateway listening on %s", addrs)

    try:
        async with server:
            await server.serve_forever()
    finally:
        await _http_session.close()


if __name__ == "__main__":
    asyncio.run(main())
