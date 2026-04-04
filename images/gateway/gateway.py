"""
Secret-injection MITM forward proxy.

Runs as a sidecar container alongside the pi agent. Intercepts HTTPS for
configured hosts (injecting real API credentials), blind-tunnels everything
else, and forwards plain HTTP as-is.

Uses raw asyncio for the proxy server (needed for CONNECT / TLS upgrade)
and aiohttp ClientSession for upstream HTTPS requests.

Configuration is loaded from a YAML file (gateway.yaml) that declares
per-host interception rules and credentials.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import json as json_mod
import logging
import os
import queue as queue_mod
import socket as socket_mod
import ssl
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import aiohttp
import yaml
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

# ── Configuration ────────────────────────────────────────────────────

CONFIG_PATH = Path(os.environ.get("GATEWAY_CONFIG", "/etc/gateway/gateway.yaml"))
CA_DIR = Path(os.environ.get("CA_DIR", "/data"))

# Refresh 5 minutes before actual expiry
TOKEN_REFRESH_MARGIN_S = 5 * 60

# ── Well-known OAuth Provider Registry ───────────────────────────────
# Protocol details only — no user credentials.

OAUTH_PROVIDERS: dict[str, dict] = {
    "anthropic": {
        "token_url": "https://platform.claude.com/v1/oauth/token",
        "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
        "content_type": "json",
    },
}


# ── Generic OAuth Token Manager ──────────────────────────────────────

class OAuthTokenManager:
    """Generic OAuth2 refresh_token grant manager.

    Works with any provider that supports the refresh_token grant type.
    Protocol details (token_url, client_id, content_type) come from config
    or the built-in provider registry.

    Supports an optional shared token file (e.g. pi's auth.json) so that
    the gateway and host tools share a single access token instead of
    stomping each other with competing refreshes.  When token_file is set:

      - On startup: read access token + expiry from the file (no refresh)
      - On request: use cached token if valid, else re-read file, else refresh
      - On refresh: write new tokens back so the host picks them up
    """

    def __init__(
        self,
        hostname: str,
        *,
        token_url: str,
        client_id: str,
        refresh_token: str,
        content_type: str = "json",
        client_secret: str | None = None,
        scope: str | None = None,
        token_file: str | None = None,
        token_file_key: str | None = None,
    ) -> None:
        self.hostname = hostname
        self.token_url = token_url
        self.client_id = client_id
        self.content_type = content_type
        self.client_secret = client_secret
        self.scope = scope

        # Shared token file support
        self._token_file = Path(token_file) if token_file else None
        self._token_file_key = token_file_key

        # Initialize tokens — prefer shared file, then persisted, then config
        self._refresh_token = refresh_token
        self._access_token: str | None = None
        self._expires_at: float = 0
        self._lock = asyncio.Lock()

        # Try loading from shared token file first
        if self._token_file:
            loaded = self._read_token_file()
            if loaded:
                self._access_token = loaded.get("access")
                self._refresh_token = loaded.get("refresh", self._refresh_token)
                expires_ms = loaded.get("expires", 0)
                if expires_ms:
                    self._expires_at = (expires_ms / 1000) - TOKEN_REFRESH_MARGIN_S
        else:
            # Fall back to gateway's own persisted refresh token
            persisted_rt = self._load_persisted_refresh()
            if persisted_rt:
                self._refresh_token = persisted_rt

    def _read_token_file(self) -> dict | None:
        """Read token data from the shared token file."""
        if not self._token_file or not self._token_file_key:
            return None
        try:
            if not self._token_file.exists():
                return None
            data = json_mod.loads(self._token_file.read_text())
            entry = data.get(self._token_file_key)
            if entry and isinstance(entry, dict):
                return entry
        except Exception as exc:
            log.warning(
                "Failed to read token file %s[%s]: %s",
                self._token_file, self._token_file_key, exc,
            )
        return None

    def _write_token_file(self) -> None:
        """Write current tokens back to the shared token file."""
        if not self._token_file or not self._token_file_key:
            return
        try:
            # Read existing file, update our key, write back
            if self._token_file.exists():
                data = json_mod.loads(self._token_file.read_text())
            else:
                data = {}
            data[self._token_file_key] = {
                "type": "oauth",
                "access": self._access_token,
                "refresh": self._refresh_token,
                "expires": int(
                    (self._expires_at + TOKEN_REFRESH_MARGIN_S) * 1000
                ),
            }
            self._token_file.write_text(json_mod.dumps(data, indent=4))
            log.info("Wrote tokens back to %s[%s]",
                     self._token_file, self._token_file_key)
        except Exception as exc:
            log.warning(
                "Failed to write token file %s: %s", self._token_file, exc,
            )

    async def get_access_token(self, http_session: aiohttp.ClientSession) -> str:
        """Return a valid access token, refreshing if necessary."""
        if self._access_token and time.time() < self._expires_at:
            return self._access_token
        return await self._refresh(http_session)

    async def force_refresh(self, http_session: aiohttp.ClientSession) -> str:
        """Force a token refresh (e.g. after a 401).

        If using a shared token file, re-read it first — the host may have
        already refreshed.  Only do a real refresh if the file token is also
        stale.
        """
        async with self._lock:
            # Re-read shared file — host may have refreshed
            if self._token_file:
                loaded = self._read_token_file()
                if loaded:
                    new_access = loaded.get("access")
                    new_expires_ms = loaded.get("expires", 0)
                    new_expires_at = (new_expires_ms / 1000) - TOKEN_REFRESH_MARGIN_S
                    # If the file has a different, still-valid token, use it
                    if (new_access
                            and new_access != self._access_token
                            and time.time() < new_expires_at):
                        log.info(
                            "Picked up refreshed token from file for %s",
                            self.hostname,
                        )
                        self._access_token = new_access
                        self._expires_at = new_expires_at
                        if loaded.get("refresh"):
                            self._refresh_token = loaded["refresh"]
                        return self._access_token

        # File didn't help — do a real refresh
        return await self._refresh(http_session)

    async def _refresh(self, http_session: aiohttp.ClientSession) -> str:
        async with self._lock:
            # Double-check: re-read shared file under lock
            if self._token_file:
                loaded = self._read_token_file()
                if loaded:
                    new_access = loaded.get("access")
                    new_expires_ms = loaded.get("expires", 0)
                    new_expires_at = (new_expires_ms / 1000) - TOKEN_REFRESH_MARGIN_S
                    if new_access and time.time() < new_expires_at:
                        self._access_token = new_access
                        self._expires_at = new_expires_at
                        if loaded.get("refresh"):
                            self._refresh_token = loaded["refresh"]
                        log.info(
                            "Using token from shared file for %s (expires in %ds)",
                            self.hostname,
                            int(new_expires_at + TOKEN_REFRESH_MARGIN_S - time.time()),
                        )
                        return self._access_token
            elif self._access_token and time.time() < self._expires_at:
                # Without token file: simple double-check after lock
                return self._access_token

            log.info("Refreshing OAuth token for %s ...", self.hostname)

            body = {
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "refresh_token": self._refresh_token,
            }
            if self.client_secret:
                body["client_secret"] = self.client_secret
            if self.scope:
                body["scope"] = self.scope

            if self.content_type == "json":
                kwargs = {
                    "json": body,
                    "headers": {
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                }
            else:  # form
                kwargs = {
                    "data": body,
                    "headers": {
                        "Accept": "application/json",
                    },
                }

            async with http_session.post(self.token_url, **kwargs) as resp:
                resp_body = await resp.text()
                if not resp.ok:
                    log.error(
                        "OAuth refresh failed for %s: %d %s",
                        self.hostname, resp.status, resp_body,
                    )
                    raise RuntimeError(
                        f"OAuth refresh failed for {self.hostname}: "
                        f"{resp.status} {resp_body}"
                    )
                data = json_mod.loads(resp_body)

            self._access_token = data["access_token"]
            expires_in = data.get("expires_in", 3600)
            self._expires_at = time.time() + expires_in - TOKEN_REFRESH_MARGIN_S

            if data.get("refresh_token"):
                self._refresh_token = data["refresh_token"]

            # Write back to shared file or gateway's own persistence
            if self._token_file:
                self._write_token_file()
            else:
                self._persist_refresh()

            log.info(
                "OAuth token refreshed for %s (expires in %ds)",
                self.hostname, expires_in,
            )
            return self._access_token

    # ── Gateway-local persistence (used when no token_file) ──────

    def _persist_path(self) -> Path:
        return CA_DIR / "oauth" / f"{self.hostname}.json"

    def _persist_refresh(self) -> None:
        """Save the current refresh token to disk for restart survival."""
        try:
            p = self._persist_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json_mod.dumps({"refresh_token": self._refresh_token}))
            log.info("Persisted rotated refresh token for %s", self.hostname)
        except Exception as exc:
            log.warning("Failed to persist refresh token for %s: %s",
                        self.hostname, exc)

    def _load_persisted_refresh(self) -> str | None:
        """Load a previously-persisted refresh token, if any."""
        try:
            p = self._persist_path()
            if p.exists():
                data = json_mod.loads(p.read_text())
                rt = data.get("refresh_token")
                if rt:
                    log.info("Loaded persisted refresh token for %s",
                             self.hostname)
                    return rt
        except Exception as exc:
            log.warning("Failed to load persisted token for %s: %s",
                        self.hostname, exc)
        return None

    def validate(self) -> list[str]:
        """Validate configuration at startup. Returns list of issues."""
        issues: list[str] = []

        if self._token_file:
            if not self._token_file.exists():
                issues.append(
                    f"token_file '{self._token_file}' does not exist"
                )
            elif not self._token_file_key:
                issues.append("token_file set but token_file_key is missing")
            else:
                loaded = self._read_token_file()
                if loaded is None:
                    issues.append(
                        f"token_file_key '{self._token_file_key}' not found "
                        f"in {self._token_file}"
                    )
                else:
                    if not loaded.get("access"):
                        issues.append(
                            f"no access token in "
                            f"{self._token_file}[{self._token_file_key}]"
                        )
                    if not loaded.get("refresh"):
                        issues.append(
                            f"no refresh token in "
                            f"{self._token_file}[{self._token_file_key}]"
                        )
                    expires_ms = loaded.get("expires", 0)
                    if expires_ms:
                        remaining_s = (expires_ms / 1000) - time.time()
                        if remaining_s <= 0:
                            issues.append(
                                f"access token in "
                                f"{self._token_file}[{self._token_file_key}] "
                                f"is expired (will refresh on first request)"
                            )
                        elif remaining_s < TOKEN_REFRESH_MARGIN_S:
                            issues.append(
                                f"access token in "
                                f"{self._token_file}[{self._token_file_key}] "
                                f"expires in {int(remaining_s)}s "
                                f"(will refresh on first request)"
                            )
        return issues


# ── Host Rule & Config Parsing ───────────────────────────────────────

@dataclass
class HostRule:
    strip_headers: set[str]             # lowercased header names to remove
    inject_templates: dict[str, str]    # header templates with $VARIABLE placeholders
    rule_type: str                      # "apikey" or "oauth"
    api_key: str | None = None          # for type: apikey
    oauth_manager: OAuthTokenManager | None = None  # for type: oauth


def parse_host_rule(hostname: str, cfg: dict) -> HostRule:
    """Parse a single host rule from config."""
    rule_type = cfg["type"]
    strip = set(h.lower() for h in cfg.get("strip_headers", []))
    inject = cfg.get("inject_headers", {})

    if rule_type == "apikey":
        api_key = cfg["api_key"]
        return HostRule(strip, inject, rule_type, api_key=api_key)

    elif rule_type == "oauth":
        # Resolve provider defaults, then apply explicit overrides
        provider_name = cfg.get("provider")
        provider_defaults = (
            OAUTH_PROVIDERS.get(provider_name, {}) if provider_name else {}
        )

        token_url = cfg.get("token_url", provider_defaults.get("token_url"))
        client_id = cfg.get("client_id", provider_defaults.get("client_id"))
        content_type = cfg.get(
            "content_type", provider_defaults.get("content_type", "json"),
        )

        if not token_url or not client_id:
            raise ValueError(
                f"OAuth rule for {hostname}: must specify 'provider' or "
                f"'token_url' + 'client_id'"
            )

        mgr = OAuthTokenManager(
            hostname,
            token_url=token_url,
            client_id=client_id,
            refresh_token=cfg.get("refresh_token", ""),
            content_type=content_type,
            client_secret=cfg.get("client_secret"),
            scope=cfg.get("scope"),
            token_file=cfg.get("token_file"),
            token_file_key=cfg.get("token_file_key"),
        )
        return HostRule(strip, inject, rule_type, oauth_manager=mgr)

    else:
        raise ValueError(f"Unknown rule type '{rule_type}' for {hostname}")


def load_config() -> dict[str, HostRule]:
    """Load and validate gateway configuration from YAML."""
    raw = yaml.safe_load(CONFIG_PATH.read_text())
    if not raw:
        log.warning("Empty gateway config — no hosts will be intercepted")
        return {}
    rules = {}
    for hostname, cfg in raw.items():
        rules[hostname] = parse_host_rule(hostname, cfg)
    return rules


# ── Header Resolution ────────────────────────────────────────────────

async def resolve_headers(
    rule: HostRule,
    http_session: aiohttp.ClientSession,
) -> dict[str, str]:
    """Resolve $VARIABLE placeholders in inject_headers to real values."""

    if rule.rule_type == "apikey":
        credential = rule.api_key
    elif rule.rule_type == "oauth":
        credential = await rule.oauth_manager.get_access_token(http_session)
    else:
        return {}

    # Compute $BASIC_AUTH
    basic_auth = base64.b64encode(
        f"x-access-token:{credential}".encode()
    ).decode()

    replacements = {
        "$API_KEY": credential if rule.rule_type == "apikey" else "",
        "$ACCESS_TOKEN": credential if rule.rule_type == "oauth" else "",
        "$BASIC_AUTH": basic_auth,
    }

    resolved = {}
    for header, template in rule.inject_templates.items():
        value = template
        for var, replacement in replacements.items():
            value = value.replace(var, replacement)
        resolved[header] = value

    return resolved


# ── Global state ─────────────────────────────────────────────────────

_rules: dict[str, HostRule] = {}
_http_session: Optional[aiohttp.ClientSession] = None


# ── CA Manager ───────────────────────────────────────────────────────

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
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=0), critical=True,
        )
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
        .subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]),
        )
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

    async def relay(
        src: asyncio.StreamReader, dst: asyncio.StreamWriter,
    ) -> None:
        try:
            while True:
                data = await src.read(RELAY_BUF)
                if not data:
                    break
                dst.write(data)
                await dst.drain()
        except (
            ConnectionResetError, BrokenPipeError,
            asyncio.CancelledError, OSError,
        ):
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
    rule: HostRule,
    http_session: aiohttp.ClientSession,
) -> None:
    """Terminate TLS as the MITM, read HTTP, inject creds, forward upstream."""

    cert_pem, key_pem = generate_host_cert(hostname)
    ssl_ctx = make_server_ssl_context(cert_pem, key_pem)

    transport = client_writer.transport
    raw_sock = transport.get_extra_info("socket")
    if raw_sock is None:
        log.error("MITM: cannot get raw socket for %s", hostname)
        return

    fd = os.dup(raw_sock.fileno())
    transport.abort()

    duped_sock = socket_mod.socket(fileno=fd)
    duped_sock.setblocking(True)
    loop = asyncio.get_event_loop()
    ssl_sock = await loop.run_in_executor(
        None, lambda: ssl_ctx.wrap_socket(duped_sock, server_side=True),
    )

    await loop.run_in_executor(
        None, _mitm_sync, ssl_sock, hostname, port, rule, loop, http_session,
    )


def _mitm_sync(
    ssl_sock,
    hostname: str,
    port: int,
    rule: HostRule,
    loop: asyncio.AbstractEventLoop,
    http_session: aiohttp.ClientSession,
) -> None:
    """Synchronous MITM handler running in a thread.

    Uses blocking socket I/O to avoid asyncio TLS upgrade issues.
    Upstream requests use aiohttp via run_coroutine_threadsafe.
    Streams response chunks back to the client for SSE support.
    """
    ssl_sock.settimeout(300)
    rfile = ssl_sock.makefile("rb")

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
        for key, value in raw_headers:
            if key.lower() not in rule.strip_headers:
                out_headers[key] = value

        # Resolve $VARIABLE templates to real credential values
        future = asyncio.run_coroutine_threadsafe(
            resolve_headers(rule, http_session), loop,
        )
        resolved_inject = future.result(timeout=30)

        for key, value in resolved_inject.items():
            out_headers[key] = value

        # Remove hop-by-hop + Accept-Encoding
        for hdr in list(out_headers.keys()):
            if hdr.lower() in (
                "proxy-connection", "proxy-authorization", "accept-encoding",
            ):
                del out_headers[hdr]

        url = (
            f"https://{hostname}{path}" if port == 443
            else f"https://{hostname}:{port}{path}"
        )
        log.info(
            "MITM %s %s (body: %s bytes)",
            method, url, len(body) if body else 0,
        )

        q: queue_mod.Queue = queue_mod.Queue()

        async def do_upstream(
            headers_to_send: dict[str, str],
            retry_on_401: bool = True,
        ):
            try:
                async with http_session.request(
                    method, url,
                    headers=headers_to_send,
                    data=body,
                    allow_redirects=False,
                ) as resp:
                    # On 401 with OAuth, force-refresh and retry once
                    if (resp.status == 401 and retry_on_401
                            and rule.oauth_manager):
                        resp_body = await resp.text()
                        if ("expired" in resp_body.lower()
                                or "authentication" in resp_body.lower()):
                            log.warning(
                                "Got 401 from %s, forcing token refresh...",
                                hostname,
                            )
                            await rule.oauth_manager.force_refresh(
                                http_session,
                            )
                            refreshed_inject = await resolve_headers(
                                rule, http_session,
                            )
                            refreshed_headers = dict(headers_to_send)
                            for k, v in refreshed_inject.items():
                                refreshed_headers[k] = v
                            await do_upstream(
                                refreshed_headers, retry_on_401=False,
                            )
                            return
                    q.put((
                        "headers", resp.status, resp.reason or "OK",
                        list(resp.headers.items()),
                    ))
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

        ssl_sock.sendall(f"HTTP/1.1 {status} {reason}\r\n".encode())

        for key, value in resp_headers:
            if key.lower() in (
                "transfer-encoding", "connection", "keep-alive",
                "content-length", "content-encoding",
            ):
                continue
            ssl_sock.sendall(f"{key}: {value}\r\n".encode())

        ssl_sock.sendall(b"Transfer-Encoding: chunked\r\n")
        ssl_sock.sendall(b"Connection: close\r\n\r\n")

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

        ssl_sock.sendall(b"0\r\n\r\n")

        log.info(
            "MITM %s %s → %d (%d bytes streamed)",
            method, url, status, total_bytes,
        )

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

async def handle_client(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
) -> None:
    """Handle one incoming proxy connection."""
    peer = writer.get_extra_info("peername")

    try:
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

            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break

            rule = _rules.get(hostname)
            if rule and rule.inject_templates:
                log.info("CONNECT %s:%d → MITM", hostname, port)
                writer.write(
                    b"HTTP/1.1 200 Connection Established\r\n\r\n",
                )
                await writer.drain()
                assert _http_session is not None
                await mitm_proxy(
                    reader, writer, hostname, port, rule, _http_session,
                )
            else:
                log.info("CONNECT %s:%d → blind tunnel", hostname, port)
                try:
                    target_reader, target_writer = (
                        await asyncio.open_connection(hostname, port)
                    )
                except Exception as exc:
                    log.error(
                        "Blind tunnel connect failed %s:%d: %s",
                        hostname, port, exc,
                    )
                    writer.write(
                        b"HTTP/1.1 502 Bad Gateway\r\n"
                        b"Content-Length: 0\r\n\r\n"
                    )
                    await writer.drain()
                    writer.close()
                    return

                writer.write(
                    b"HTTP/1.1 200 Connection Established\r\n\r\n",
                )
                await writer.drain()
                await blind_tunnel(
                    reader, writer, target_reader, target_writer,
                )

        # ── GET /healthz or /ca.pem (direct to gateway) ─────────
        elif len(parts) >= 2 and parts[1] in ("/healthz", "/ca.pem"):
            path = parts[1]
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
        elif method in (
            "GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS",
        ):
            url = parts[1]

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

            body: Optional[bytes] = None
            if content_length and content_length > 0:
                body = await reader.readexactly(content_length)

            out_headers: dict[str, str] = {}
            for key, value in headers:
                if key.lower() not in (
                    "proxy-connection", "proxy-authorization", "connection",
                ):
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
                    resp_line = (
                        f"HTTP/1.1 {upstream_resp.status} "
                        f"{upstream_resp.reason or 'OK'}\r\n"
                    )
                    writer.write(resp_line.encode())
                    for key, value in upstream_resp.headers.items():
                        if key.lower() not in (
                            "transfer-encoding", "connection",
                        ):
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
                writer.write(
                    f"Content-Length: {len(err)}\r\n\r\n".encode(),
                )
                writer.write(err)
                await writer.drain()

        else:
            writer.write(
                b"HTTP/1.1 405 Method Not Allowed\r\n"
                b"Content-Length: 0\r\n\r\n"
            )
            await writer.drain()

    except (
        ConnectionResetError, BrokenPipeError,
        asyncio.CancelledError, OSError,
    ) as exc:
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
    global _ca_key, _ca_cert, _http_session, _rules

    log.info("Starting secret gateway...")
    log.info("Loading config from %s", CONFIG_PATH)

    _rules = load_config()

    log.info("Loaded rules for %d hosts:", len(_rules))
    for hostname, rule in _rules.items():
        log.info(
            "  %s: type=%s, strip=%s, inject=%s",
            hostname, rule.rule_type,
            rule.strip_headers,
            list(rule.inject_templates.keys()),
        )

    # ── Validate OAuth configurations ────────────────────────────
    startup_ok = True
    for hostname, rule in _rules.items():
        if rule.oauth_manager:
            issues = rule.oauth_manager.validate()
            if issues:
                for issue in issues:
                    # "expired" / "expires in" are warnings, rest are errors
                    if "expire" in issue.lower() or "will refresh" in issue.lower():
                        log.warning("  %s: %s", hostname, issue)
                    else:
                        log.error("  %s: %s", hostname, issue)
                        startup_ok = False
            else:
                mgr = rule.oauth_manager
                if mgr._token_file:
                    remaining = mgr._expires_at + TOKEN_REFRESH_MARGIN_S - time.time()
                    log.info(
                        "  %s: token_file OK (%s[%s], "
                        "token expires in %.0fh)",
                        hostname, mgr._token_file,
                        mgr._token_file_key,
                        remaining / 3600,
                    )
                else:
                    log.info("  %s: standalone OAuth (no shared token file)",
                             hostname)

    if not startup_ok:
        log.error("Startup validation failed — fix errors above")
        raise SystemExit(1)

    _ca_key, _ca_cert = load_or_generate_ca()

    _http_session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(
            ssl=True,
            limit=100,
            force_close=True,
        ),
    )

    # Pre-fetch OAuth tokens (only refreshes if needed — shared file
    # tokens that are still valid will be used as-is)
    for hostname, rule in _rules.items():
        if rule.oauth_manager:
            try:
                token = await rule.oauth_manager.get_access_token(
                    _http_session,
                )
                log.info(
                    "OAuth token ready for %s (prefix: %s...)",
                    hostname, token[:20],
                )
            except Exception as exc:
                log.error(
                    "Failed to acquire initial token for %s: %s",
                    hostname, exc,
                )
                log.error(
                    "Requests to %s will fail until refresh succeeds",
                    hostname,
                )

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
