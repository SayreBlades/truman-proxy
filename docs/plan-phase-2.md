# Phase 2: Secret Gateway + Network Isolation

**Author:** Sayre Blades  
**Date:** 2026-04-01  
**Status:** Draft — ready for review

Phase 2 of the [full plan](plan.md). Add a secret-injection gateway and Docker network
isolation so that real API credentials never enter the agent container.

Builds on [Phase 1](plan-phase-1.md) (pi running in Docker with bind-mounted workspace).

---

## Goals

1. Real API secrets (Anthropic token, Brave key, GitHub PAT, future credentials) **never enter the agent container**
2. The agent container sits on an **internal-only Docker network** — it can only reach the gateway, not the internet directly
3. A Python **secret gateway** runs as a sidecar container, acting as a **forward proxy** that:
   - **MITM-intercepts HTTPS** for configured hosts, injecting real credentials (replacing dummy placeholders)
   - **Blindly tunnels HTTPS** for all other hosts (no decryption, no credential injection)
   - **Forwards plain HTTP** requests as-is
4. The agent container receives **dummy API keys** that pass client-library validation but are swapped out by the gateway before reaching real APIs
5. Gateway is **greenfield Python** (aiohttp + cryptography) — simple, extensible

## Non-Goals (deferred to later phases)

- Rate limiting / request policy engine
- Web UI / WebSocket bridge
- VS Code devcontainer integration
- Image size optimization

---

## Prior Art

This approach follows the same pattern used by both major open-source projects in this space:

| Project                                                | Mechanism                                                                       | Complexity                                 |
|--------------------------------------------------------|---------------------------------------------------------------------------------|--------------------------------------------|
| [OneCLI](https://github.com/onecli/onecli)             | Rust MITM forward proxy + custom CA + encrypted vault                           | High (Rust, PostgreSQL, Next.js dashboard) |
| [Gondolin](https://github.com/earendil-works/gondolin) | Full userspace network stack (TypeScript) + TLS MITM + placeholder substitution | Very high (custom TCP/IP stack, micro-VMs) |
| **This (Phase 2)**                                     | Python MITM forward proxy + custom CA                                           | Low (~400 lines of Python)                 |

All three use the same core mechanism: **generate a custom CA, install it in the agent's trust store, MITM HTTPS for configured hosts, inject credentials in the decrypted HTTP headers, forward to the real upstream.**

The key advantage of MITM over application-level redirects: **fully transparent to any tool**. `gh`, `git`, `curl`, `pip`, the Anthropic SDK, Brave search — everything that respects `HTTPS_PROXY` gets credential injection for free, with zero per-tool configuration.

---

## Architecture
```
┌──────────────────────────────────────────────────────────────────────────┐
│ Docker                                                                   │
│                                                                          │
│  ┌─────────────────────────┐  sandbox     ┌────────────────────────────┐ │
│  │  Agent Container        │──(internal)─▶│  Gateway Container         │ │
│  │                         │   :8080      │  (Python / aiohttp)        │ │
│  │  pi agent + gh cli      │              │                            │ │
│  │                         │              │  CONNECT api.anthropic.com │ │
│  │  HTTPS_PROXY=           │              │   → MITM: inject real key  │──▶ Internet
│  │   http://gateway:8080   │              │                            │ │
│  │                         │              │  CONNECT api.github.com    │ │
│  │  Dummy keys only:       │              │   → MITM: inject real PAT  │ │
│  │   ANTHROPIC_OAUTH_TOKEN │              │                            │ │
│  │   BRAVE_API_KEY         │              │  CONNECT pypi.org          │ │
│  │   GH_TOKEN              │              │   → Blind tunnel (no MITM) │ │
│  │                         │              │                            │ │
│  │  Trusts gateway CA cert │              │  CA cert + key (volume)    │ │
│  │  (fetched on startup)   │              │  .env has real keys        │ │
│  └─────────────────────────┘              └────────────────────────────┘ │
│                                                                          │
│  Networks:                                                               │
│  ┌──────────────────────────────────────┐                                │
│  │ sandbox (internal: true)             │                                │
│  │   agent ↔ gateway                    │                                │
│  └──────────────────────────────────────┘                                │
│  ┌──────────────────────────────────────┐                                │
│  │ egress                               │                                │
│  │   gateway → internet                 │                                │
│  └──────────────────────────────────────┘                                │
└──────────────────────────────────────────────────────────────────────────┘
```

### Traffic flow

| Example                              | Proxy behavior   | What happens                                                                                                                      |
|--------------------------------------|------------------|-----------------------------------------------------------------------------------------------------------------------------------|
| Anthropic SDK → `api.anthropic.com`  | **MITM**         | Gateway terminates TLS, strips dummy `Authorization: Bearer sk-ant-oat01-DUMMY...`, injects real OAuth token, forwards over HTTPS |
| Brave skill → `api.search.brave.com` | **MITM**         | Gateway terminates TLS, strips dummy `X-Subscription-Token`, injects real Brave key, forwards                                     |
| `gh api` → `api.github.com`          | **MITM**         | Gateway terminates TLS, strips dummy `Authorization: token ghp_DUMMY...`, injects real PAT, forwards                              |
| `git clone` → `github.com`           | **MITM**         | Same as above — git sends auth through the proxy, gateway swaps credentials                                                       |
| `pip install` → `pypi.org`           | **Blind tunnel** | Gateway opens TCP tunnel, bidirectional pipe, no decryption                                                                       |
| `curl http://example.com`            | **HTTP forward** | Gateway forwards plain HTTP request as-is                                                                                         |
| Direct internet from agent           | **Blocked**      | `internal: true` network — no route exists                                                                                        |

---

## Key Design Decisions

### D1 — MITM forward proxy with custom CA

**Decision:** The gateway acts as an HTTP forward proxy (set via `HTTPS_PROXY`/`HTTP_PROXY`). For a configured set of hostnames, it performs TLS MITM to intercept and modify HTTPS requests. For all other hosts, it does blind CONNECT tunneling.

**How MITM works:**

1. Agent's HTTP client sends `CONNECT api.anthropic.com:443` to gateway
2. Gateway checks: is `api.anthropic.com` in the intercept list?
3. **Yes (intercepted):**
   - Gateway sends `200 Connection Established`
   - Gateway generates a TLS certificate for `api.anthropic.com`, signed by its custom CA
   - Gateway does TLS handshake with the agent using this cert (agent trusts the CA)
   - Gateway reads the now-plaintext HTTP request (headers, body)
   - Gateway strips dummy credential headers, injects real ones
   - Gateway opens a new HTTPS connection to the real `api.anthropic.com`
   - Gateway streams the response back, re-encrypting for the agent
4. **No (passthrough):**
   - Gateway sends `200 Connection Established`
   - Gateway opens TCP connection to target
   - Bidirectional pipe — gateway sees only encrypted bytes

**Rationale:** This is the same approach used by OneCLI (Rust) and Gondolin (TypeScript). It's fully transparent to all applications — any tool that respects `HTTPS_PROXY` gets credential injection with zero per-tool configuration. No `models.json` overrides, no skill modifications, no `http_unix_socket` hacks, no `git insteadOf` rewrites.

### D2 — CA certificate distribution

**Decision:** The gateway generates a CA key pair on first startup (persisted in a Docker volume). It serves the CA public certificate via an HTTP endpoint: `GET http://gateway:8080/ca.pem`.

The agent container's entrypoint fetches the CA cert and installs it:

```bash
# Fetch gateway CA cert (gateway is healthy by this point via depends_on)
curl -sf http://gateway:8080/ca.pem -o /usr/local/share/ca-certificates/gateway-ca.crt
update-ca-certificates

# Also set for Node.js (which uses its own cert bundle)
export NODE_EXTRA_CA_CERTS=/usr/local/share/ca-certificates/gateway-ca.crt
```

This approach:
- Avoids shared volumes for the CA cert (no risk of agent reading the private key)
- Works automatically — no manual cert copying
- The CA private key stays in the gateway's volume, never accessible to the agent

### D3 — Making Node.js `fetch()` respect `HTTPS_PROXY`

**Decision:** Node.js 22's native `fetch()` (undici-based) does **not** respect `HTTPS_PROXY` by default. Node 22 provides a built-in flag to enable this:

```
NODE_OPTIONS="--use-env-proxy"
```

This activates undici's `EnvHttpProxyAgent` internally, making all `fetch()` calls (Anthropic SDK, Brave search skill, any Node.js HTTP) route through `HTTPS_PROXY`. No bootstrap scripts, no npm packages, no code changes — just one environment variable.

The flag produces an experimental warning (`[UNDICI-EHPA] Warning: EnvHttpProxyAgent is experimental`), suppressed with `NODE_NO_WARNINGS=1`.

**Verified behavior:**
- Without `--use-env-proxy`: `fetch()` ignores `HTTPS_PROXY`, connects directly
- With `--use-env-proxy`: `fetch()` routes through `HTTPS_PROXY`

**How other projects handle this:**
- **Gondolin** doesn't need proxy env vars at all — its userspace network stack intercepts traffic at the Ethernet/IP level before Node.js ever makes a "real" connection
- **OneCLI** sets `HTTPS_PROXY`/`HTTP_PROXY` and relies on the same mechanism

### D4 — Credential injection rules

**Decision:** Rules are a Python data structure in the gateway source, matching by hostname. For each intercepted host, the gateway strips dummy credential headers and injects real ones:

```python
INTERCEPT_RULES = {
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
            "Authorization": "token {GH_TOKEN}",
        },
    },
}
```

Values like `{ANTHROPIC_OAUTH_TOKEN}` are resolved from the gateway's environment at startup. If a variable is unset, that header injection is skipped (supports both `ANTHROPIC_OAUTH_TOKEN` and `ANTHROPIC_API_KEY` — whichever is present).

### D5 — Dummy API keys

**Decision:** Static dummy keys in `.env.agent` (committed to repo). They must pass client-side validation:

| Key                     | Client-side validation                                                              | Dummy value                                                                                       |
|-------------------------|-------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------|
| `ANTHROPIC_OAUTH_TOKEN` | `apiKey.includes("sk-ant-oat")` → treated as OAuth, sent as `Authorization: Bearer` | `sk-ant-oat01-DUMMY000000000000000000000000000000000000000000000000000000000000000000-0000000000` |
| `BRAVE_API_KEY`         | None (sent as header verbatim)                                                      | `BSAdummy0000000000000000000000`                                                                  |
| `GH_TOKEN`              | None (gh sends as `Authorization: token <value>`)                                   | `ghp_DUMMY0000000000000000000000000000000000`                                                     |

  These values are obviously fake on inspection but structurally valid enough to pass library initialization.

### D6 — `.env` file split

**Decision:** Two env files:

| File         | Contains                      | Read by                        | Git status                          |
|--------------|-------------------------------|--------------------------------|-------------------------------------|
| `.env`       | Real API keys                 | Gateway container (`env_file`) | **gitignored**                      |
| `.env.agent` | Dummy API keys + proxy config | Agent container (`env_file`)   | **committed** (safe — only dummies) |

`.env.agent`:
```bash
# Dummy keys (structurally valid, never sent to real APIs)
ANTHROPIC_OAUTH_TOKEN=sk-ant-oat01-DUMMY000000000000000000000000000000000000000000000000000000000000000000-0000000000
BRAVE_API_KEY=BSAdummy0000000000000000000000
GH_TOKEN=ghp_DUMMY0000000000000000000000000000000000

# Proxy configuration (set by docker-compose, but listed here for documentation)
HTTPS_PROXY=http://gateway:8080
HTTP_PROXY=http://gateway:8080
NO_PROXY=gateway
```

`.env` (real keys, gitignored):
```bash
ANTHROPIC_OAUTH_TOKEN=sk-ant-oat01-REAL...
BRAVE_API_KEY=BSAp-REAL...
GH_TOKEN=ghp_REAL...
```

### D7 — Docker networking

**Decision:** Two Docker networks:

```yaml
networks:
  sandbox:
    internal: true   # No external connectivity
  egress:
    # Default — routable to the internet
```

| Container | Networks             | Can reach                                   |
|-----------|----------------------|---------------------------------------------|
| Agent     | `sandbox` only       | Gateway (via `gateway:8080`). Nothing else. |
| Gateway   | `sandbox` + `egress` | Agent + internet                            |

### D8 — gh CLI installation

**Decision:** Install `gh` CLI in the agent container Dockerfile. It respects `HTTPS_PROXY` natively (Go's `net/http` honors proxy env vars). With the MITM approach, `gh` works transparently — no `http_unix_socket` or other workarounds needed.

The dummy `GH_TOKEN` is set via `.env.agent`. When gh makes API calls through the proxy, the gateway MITM-intercepts `api.github.com`, strips the dummy token, and injects the real PAT.

Git operations (`git clone`, `git push` to github.com) also go through the proxy and get the real PAT injected.

---

## Gateway Implementation

### Overview

Single-file Python gateway (`gateway.py`) using `aiohttp` (server + HTTP client) and `cryptography` (CA + cert generation).

### Components

**1. CA Manager**
- On startup: load or generate CA key pair from `/data/ca.key` + `/data/ca.pem`
- Endpoint: `GET /ca.pem` serves the public CA certificate
- Cert cache: in-memory dict mapping hostname → generated cert (avoid regenerating per-request)

**2. CONNECT Handler (MITM or passthrough)**
```
Client → CONNECT api.anthropic.com:443 → Gateway
  If host in INTERCEPT_RULES:
    → 200 Connection Established
    → TLS handshake (gateway cert for api.anthropic.com)
    → Read plaintext HTTP request
    → Strip/inject credential headers
    → Forward to real api.anthropic.com over HTTPS
    → Stream response back (re-encrypted to client)
  Else:
    → 200 Connection Established
    → Open TCP to target
    → Bidirectional pipe (blind tunnel)
```

**3. HTTP Forward Proxy**
- Handle requests with absolute URLs: `GET http://example.com/path`
- Forward as-is, stream response back

**4. Health Check**
- `GET /healthz` → 200 OK

**5. Logging**
- Log: CONNECT target, MITM vs passthrough decision, upstream status codes
- Never log: credential values, request/response bodies

### Dependencies

```
aiohttp
cryptography
```

### Streaming

Critical for the Anthropic API (SSE). The MITM handler must:
1. Forward request body to upstream without full buffering
2. Stream upstream response chunks back to the client as they arrive
3. Not buffer the full response before sending

`aiohttp.ClientSession` with `chunked` reading handles this naturally.

---

## docker-compose.yml

```yaml
services:
  gateway:
    build:
      context: ./gateway
    env_file: .env
    volumes:
      - gateway-data:/data
    networks:
      - sandbox
      - egress
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')"]
      interval: 5s
      timeout: 3s
      retries: 3

  agent:
    build:
      context: .
      additional_contexts:
        skills: ~/.pi/agent/skills
    stdin_open: true
    tty: true
    volumes:
      - .:/workspace
      - pi-data:/home/pi/.pi/agent
    env_file: .env.agent
    environment:
      - HTTPS_PROXY=http://gateway:8080
      - HTTP_PROXY=http://gateway:8080
      - NO_PROXY=gateway
      - NODE_EXTRA_CA_CERTS=/usr/local/share/ca-certificates/gateway-ca.crt
      - NODE_OPTIONS=--use-env-proxy
      - NODE_NO_WARNINGS=1
    networks:
      - sandbox
    depends_on:
      gateway:
        condition: service_healthy
    working_dir: /workspace

networks:
  sandbox:
    internal: true
  egress:

volumes:
  pi-data:
  gateway-data:
```

---

## Agent Container Changes

### Dockerfile additions

```dockerfile
# Add gh CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*
```

No proxy bootstrap script needed — Node 22's built-in `--use-env-proxy` flag (set via `NODE_OPTIONS` in docker-compose.yml) makes `fetch()` respect `HTTPS_PROXY` automatically.

### Entrypoint additions

```bash
# Fetch and install gateway CA certificate
echo "Fetching gateway CA certificate..."
curl -sf --retry 3 http://gateway:8080/ca.pem \
    -o /usr/local/share/ca-certificates/gateway-ca.crt
update-ca-certificates 2>/dev/null
```

---

## Gateway: Repo Layout

```
gateway/
├── Dockerfile          # Python 3.12-slim + aiohttp + cryptography
├── gateway.py          # Single-file gateway (~400 lines)
└── requirements.txt    # aiohttp, cryptography
```

### gateway/Dockerfile

```dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY gateway.py .

VOLUME /data
EXPOSE 8080

CMD ["python", "gateway.py"]
```

---

## Updated Repo Layout

```
.
├── Dockerfile                  # Agent container image
├── docker-compose.yml          # Agent + gateway + networks
├── Makefile                    # Ergonomic targets
├── entrypoint.sh               # Agent entrypoint
├── .env                        # Real API keys — gateway only (gitignored)
├── .env.agent                  # Dummy keys + proxy config (committed)
├── .env.example                # Template showing all expected vars
├── gateway/
│   ├── Dockerfile              # Gateway image
│   ├── gateway.py              # MITM forward proxy
│   └── requirements.txt        # aiohttp, cryptography
├── docs/
│   ├── plan.md                 # Full architecture plan
│   ├── plan-phase-1.md         # Phase 1 design doc
│   └── plan-phase-2.md         # This file
└── README.md                   # Usage instructions
```

---

## Implementation Order

1. **Gateway skeleton** — `gateway/` directory, Dockerfile, aiohttp hello-world on :8080 with `/healthz`
2. **CA manager** — Generate/load CA cert+key, serve via `GET /ca.pem`
3. **CONNECT handler (blind tunnel)** — Handle `CONNECT` with bidirectional TCP pipe
4. **CONNECT handler (MITM)** — For intercepted hosts: TLS termination, cert generation, request forwarding
5. **Credential injection** — Strip dummy headers, inject real credentials from env vars
6. **HTTP forward proxy** — Handle absolute-URL requests (`GET http://...`)
7. **Streaming verification** — Verify SSE streaming works for Anthropic API responses
8. **Agent Dockerfile updates** — Install gh CLI, CA cert fetch in entrypoint
9. **Docker networking** — Two networks, `.env` split, `HTTPS_PROXY` config
10. **Dummy keys** — Create `.env.agent` with structurally-valid dummy tokens
11. **Makefile update** — Update targets for two-container setup
12. **Smoke tests** — Full verification suite
13. **README update** — Document Phase 2 setup

---

## Smoke Tests

| Test                    | Command (from agent container)                                 | Expected                                               |
|-------------------------|----------------------------------------------------------------|--------------------------------------------------------|
| LLM works               | `pi -p "say hello"`                                            | Response from Claude via gateway                       |
| Brave search works      | Run brave skill search                                         | Results via gateway                                    |
| gh CLI works            | `gh api /user`                                                 | Authenticated response via gateway                     |
| git clone (private)     | `git clone https://github.com/<private-repo>`                  | Succeeds via gateway with real PAT                     |
| pip install works       | `pip install requests`                                         | Installs via blind CONNECT tunnel                      |
| curl HTTPS works        | `curl https://example.com`                                     | HTML response via blind tunnel                         |
| Direct internet blocked | Unset HTTPS_PROXY, `curl https://example.com`                  | Connection refused / timeout                           |
| Gateway health          | `curl http://gateway:8080/healthz`                             | 200 OK                                                 |
| CA cert served          | `curl http://gateway:8080/ca.pem`                              | PEM certificate                                        |
| Dummy keys in agent     | `echo $ANTHROPIC_OAUTH_TOKEN`                                  | Shows dummy value                                      |
| Real keys NOT in agent  | `env \| grep -v DUMMY \| grep -i 'anthropic\|brave\|gh_token'` | No real keys visible                                   |
| MITM verification       | `GH_DEBUG=api gh api /user 2>&1 \| grep "Request to"`          | Shows `https://api.github.com` (gh thinks it's direct) |

---

## Adding a New Credential-Injected Service

To add a new service (e.g., Jira, Confluence, npm registry):

1. **Gateway:** Add hostname + header rules to `INTERCEPT_RULES` in `gateway.py`:
   ```python
   "jira.company.com": {
       "strip_headers": ["authorization"],
       "inject_headers": {
           "Authorization": "Bearer {JIRA_TOKEN}",
       },
   },
   ```
2. **`.env`:** Add the real credential: `JIRA_TOKEN=real-token-here`
3. **`.env.agent`:** Add a dummy: `JIRA_TOKEN=DUMMY_JIRA_TOKEN_0000`
4. **Done.** No application changes needed — any tool that talks to `jira.company.com` through the proxy gets credentials injected.

---

## Future Work

### OAuth credential flows (partially implemented)

The gateway now supports **Anthropic OAuth token refresh** natively. When `ANTHROPIC_REFRESH_TOKEN` is set in `.env`, the gateway:

1. Obtains a fresh access token on startup
2. Proactively refreshes before expiry (5-minute margin)
3. Reactively refreshes on 401 responses (expired token detected)
4. Updates the refresh token if rotated by the provider

The refresh token is obtained from the host pi installation (`~/.pi/agent/auth.json`) via `make sync-token`. It is long-lived and only needs to be synced once (unless revoked).

**Still deferred:**

1. **Authorization Code / Device Code flow:** Gateway initiating OAuth flows on behalf of the agent (currently the user runs `/login` in host pi and syncs the refresh token).
2. **Multi-provider OAuth:** GitHub OAuth Apps, Atlassian, Google, etc. Currently only Anthropic has refresh support; others use static tokens/keys.
3. **Token revocation:** Revoking tokens when the agent session ends.
4. **`credentials.yaml` config:** A declarative config file replacing the current `INTERCEPT_RULES` dict, supporting both static and OAuth credential types.

### Rate limiting / policy engine

Per-host or per-agent request rate limits, method/path allowlists (similar to OneCLI's rules engine).

### Audit logging

Structured log of every proxied request: timestamp, target host, method, path, status code, agent identity. Useful for compliance and debugging.

### Allowlist mode

Currently the gateway allows traffic to any host (blind tunnel for unknown hosts). A future mode could restrict egress to only explicitly allowed hosts.

---

## Relationship to Full Plan

| Full Plan Component          | Phase 2 Status                                      |
|------------------------------|-----------------------------------------------------|
| Devcontainer (pi + tools)    | ✅ Phase 1                                          |
| Bind-mounted workspace       | ✅ Phase 1                                          |
| CLI / TUI access             | ✅ Phase 1                                          |
| RPC access                   | ✅ Phase 1                                          |
| Secret gateway               | ✅ **This phase** (MITM forward proxy)              |
| Internal-only network        | ✅ **This phase**                                   |
| Placeholder env vars         | ✅ **This phase** (dummy keys in `.env.agent`)      |
| gh CLI + git integration     | ✅ **This phase** (transparent via MITM)            |
| VS Code devcontainer         | ❌ Deferred                                         |
| Web access (browser chat UI) | ❌ Deferred                                         |
| Policy engine / rate limits  | ❌ Deferred                                         |
