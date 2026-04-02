# Pi Agent in Docker Compose

Run [pi](https://github.com/badlogic/pi-mono) inside a Docker container with the host project filesystem bind-mounted in. A secret gateway sidecar injects real API credentials so the agent never sees them.

## Prerequisites

- Docker Desktop (or Docker Engine + Compose plugin)
- A [pi](https://github.com/badlogic/pi-mono) installation on the host (for OAuth login)
- Optionally: Brave API key, GitHub PAT

## Quick Start

```bash
# 1. Log in to Anthropic via pi on the host (if not already)
pi    # then type /login → select Anthropic

# 2. Sync your OAuth refresh token into .env
make sync-token

# 3. Add other keys (Brave, GitHub) to .env
# Edit .env — see .env.example for all options

# 4. Build and run interactively
make run
```

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Docker                                                                   │
│                                                                          │
│  ┌──────────────────────────┐  sandbox     ┌───────────────────────────┐ │
│  │  Agent Container         │──(internal)─▶│  Gateway Container        │ │
│  │  pi + gh CLI + tools     │   :8080      │  Python MITM proxy        │ │
│  │                          │              │                           │ │
│  │  HTTPS_PROXY=gateway     │              │  Intercepts configured    │ │
│  │  Only dummy API keys     │              │  hosts, injects real      │──▶ Internet
│  │  Trusts gateway CA cert  │              │  credentials from .env    │ │
│  └──────────────────────────┘              └───────────────────────────┘ │
│                                                                          │
│  sandbox (internal) ← agent can only reach gateway                       │
│  egress             ← gateway can reach the internet                     │
└──────────────────────────────────────────────────────────────────────────┘
```

### How It Works

1. **Agent** sends requests with dummy API keys through `HTTPS_PROXY`
2. **Gateway** intercepts HTTPS for configured hosts (Anthropic, Brave, GitHub)
3. Gateway strips dummy credentials, injects real ones from `.env`
4. Gateway forwards to the real API, streams the response back
5. For non-configured hosts: gateway does blind TCP tunneling (no MITM)
6. Agent sits on an internal-only Docker network — no direct internet access

### Credential Flow

| Service        | Agent sees              | Gateway injects                              |
|----------------|-------------------------|----------------------------------------------|
| Anthropic API  | `sk-ant-oat01-DUMMY...` | Auto-refreshed OAuth token (from `.env`)     |
| Brave Search   | `BSAdummy...`           | Real `BRAVE_API_KEY`                         |
| GitHub API/git | `ghp_DUMMY...`          | Real `GH_TOKEN`                              |

The gateway holds a long-lived **refresh token** for Anthropic and automatically obtains short-lived access tokens. Tokens are refreshed proactively before expiry and reactively on 401 responses — no manual token rotation needed.

## Make Targets

| Target              | Description                            |
|---------------------|----------------------------------------|
| `make help`         | Show all available targets             |
| `make build`        | Build all container images             |
| `make run`          | Interactive CLI/TUI session            |
| `make prompt p="…"` | Single prompt                          |
| `make rpc`          | RPC mode (JSONL on stdin/stdout)       |
| `make shell`        | Bash shell in agent (for debugging)    |
| `make shell-gateway`| Bash shell in gateway (for debugging)  |
| `make logs`         | Stream gateway logs                    |
| `make sync-token`   | Sync Anthropic OAuth token from host pi |
| `make clean`        | Remove containers, volumes, and images |

## API Keys

Two env files:

| File         | Contains                  | Read by           | Git status           |
|--------------|---------------------------|-------------------|----------------------|
| `.env`       | Real API keys             | Gateway container | **gitignored**       |
| `.env.agent` | Dummy keys + proxy config | Agent container   | **committed** (safe) |

### Required in `.env`

```bash
# Recommended: OAuth refresh token (auto-refreshes, never expires)
ANTHROPIC_REFRESH_TOKEN=sk-ant-ort01-...

# Alternatives (pick one):
# ANTHROPIC_OAUTH_TOKEN=sk-ant-oat01-...   # Static OAuth token (expires in hours)
# ANTHROPIC_API_KEY=sk-ant-api03-...        # API key (requires paid API plan)
```

Use `make sync-token` to extract the refresh token from your host pi installation automatically.

### Optional in `.env`

```bash
BRAVE_API_KEY=BSAp-...
GH_TOKEN=ghp_...   # GitHub PAT for gh CLI and git operations
```

## Adding a New Credential-Injected Service

1. Add hostname + header rules to `INTERCEPT_RULES` in `gateway/gateway.py`
2. Add the real credential to `.env`
3. Add a dummy value to `.env.agent`
4. Done — any tool that talks to that host through the proxy gets credentials injected

## Skills

Container-viable skills from `~/.pi/agent/skills/` are baked into the image at build time. Rebuild to pick up changes:

```bash
make build
```

Included: brave-search, gccli, gdcli, gmcli, transcribe, youtube-transcript, polymarket.
Excluded (need host resources): browser-tools, vscode.

## Sessions

Sessions persist in a named Docker volume (`pi-data`). They survive `docker compose down` and container recreation. To wipe everything:

```bash
make clean
```

## Design Documents

- [Full plan](docs/plan.md)
- [Phase 1: Docker container](docs/plan-phase-1.md)
- [Phase 2: Secret gateway + network isolation](docs/plan-phase-2.md)
