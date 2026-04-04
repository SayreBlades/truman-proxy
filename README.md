# Truman

**Sandboxed [pi](https://github.com/badlogic/pi-mono) agent runtime with credential injection.**

Truman provides a set of containers that give any project a secure, sandboxed AI coding agent. It complies with the [devcontainer specification](https://containers.dev), so it works with VS Code, the `devcontainer` CLI, GitHub Codespaces, and any other devcontainer-compatible tool.

- 🔒 **Agent never sees real API keys** — gateway injects credentials transparently
- 🌐 **Network isolation** — agent cannot access internet directly, only through MITM proxy
- 🔄 **Auto-refreshing tokens** — OAuth tokens refresh automatically
- 📁 **Works on any project** — drop `.devcontainer/` into your repo and go

## Quick Start

### Prerequisites

- Docker Desktop
- [pi](https://github.com/badlogic/pi-mono) installed on the host (for OAuth login)
- For VS Code: [Dev Containers](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers) extension
- For CLI: `npm install -g @devcontainers/cli` (optional)

### Add truman to your project

```bash
# 1. Copy the template into your project
cp -r template/.devcontainer/ /path/to/your-project/.devcontainer/

# 2. Run the interactive setup wizard
cd /path/to/your-project
.devcontainer/truman.sh init

# 3. Start the devcontainer
.devcontainer/truman.sh start
```

Then start using it — see the [template README](template/README.md) for full usage instructions:

- **[VS Code](template/README.md#vs-code)** — "Reopen in Container" for a full IDE experience
- **[Devcontainer CLI](template/README.md#devcontainer-cli)** — `devcontainer up` + `devcontainer exec` from any terminal
- **[Docker Compose](template/README.md#docker-compose-direct)** — `docker compose run --rm agent` for quick interactive sessions

## Architecture

```mermaid
flowchart TB
    subgraph Docker["🐳 Docker Environment"]
        subgraph SandboxNet["🔒 sandbox network (internal only)"]
            Agent["🤖 Agent Container<br/>• pi + gh CLI + tools<br/>• HTTPS_PROXY=gateway:8080<br/>• Only dummy API keys<br/>• Trusts gateway CA cert"]
        end
        
        subgraph EgressNet["🌐 egress network"]
            Gateway["🛡️ Gateway Container<br/>• Python MITM proxy<br/>• Intercepts configured hosts<br/>• Injects real credentials from gateway.yaml"]
        end
        
        Agent -->|":8080<br/>All HTTPS traffic"| Gateway
    end
    
    subgraph APIs["🌍 Internet APIs"]
        Anthropic["🧠 Anthropic API"]
        Brave["🔍 Brave Search"]
        GitHub["🐙 GitHub API"]
        Other["🌐 Other APIs"]
    end
    
    Gateway -->|"Real OAuth tokens"| Anthropic
    Gateway -->|"Real API key"| Brave
    Gateway -->|"Real PAT"| GitHub
    Gateway -->|"Blind TCP tunnel"| Other
```

### How It Works

1. **Agent** sends all HTTPS requests with dummy API keys through `HTTPS_PROXY` to the gateway
2. **Gateway** intercepts HTTPS traffic for configured hosts (Anthropic, Brave, GitHub) via MITM
3. Gateway strips dummy credentials and injects real ones from `gateway.yaml` before forwarding
4. For non-configured hosts, gateway performs blind TCP tunneling (no credential injection)
5. Agent runs on internal-only network — all traffic must go through gateway
6. Gateway automatically refreshes OAuth tokens proactively and reactively on 401 responses

### Credential Flow

| Service        | Agent sees              | Gateway injects            |
|----------------|-------------------------|----------------------------|
| Anthropic API  | `sk-ant-oat01-DUMMY...` | Auto-refreshed OAuth token |
| Brave Search   | `BSAdummy...`           | Real `BRAVE_API_KEY`       |
| GitHub API/git | `ghp_DUMMY...`          | Real `GH_TOKEN`            |

## Project Structure

```
truman/
├── images/
│   ├── gateway/          # MITM credential-injection proxy
│   │   ├── Dockerfile
│   │   ├── gateway.py
│   │   ├── pyproject.toml
│   │   └── uv.lock
│   └── agent/            # Pi coding agent container
│       ├── Dockerfile
│       └── entrypoint.sh
├── template/             # Copy into your project
│   └── .devcontainer/
│       ├── devcontainer.json
│       ├── docker-compose.yml
│       └── truman.sh
├── examples/
│   └── temperature-converter/
└── docs/
```

## Container Images

Published to GitHub Container Registry:

| Image                                | Purpose                              |
|--------------------------------------|--------------------------------------|
| `ghcr.io/sayreblades/truman-gateway` | MITM proxy with credential injection |
| `ghcr.io/sayreblades/truman-agent`   | Pi coding agent with tools           |

## Gateway Configuration

The gateway is configured via a single YAML file (`gateway.yaml`) that declares per-host interception rules and credentials. No image rebuild needed to add services.

### Token sharing

OAuth hosts (like Anthropic) support a shared token file via `token_file` + `token_file_key`. This lets the gateway and host tools (like `pi`) share a single access token instead of competing with separate refreshes. The host's `~/.pi/agent/auth.json` is mounted into the gateway container at `/host-auth/auth.json`.

When the gateway starts, it reads the existing access token from the file — no refresh needed. If the token expires, whichever side (host or gateway) detects it first refreshes and writes back, and the other picks up the new token.

### Adding a new service (API key)

Edit `gateway.yaml`:

```yaml
api.openai.com:
  type: apikey
  strip_headers: [authorization]
  inject_headers:
    Authorization: "Bearer $API_KEY"
  api_key: "sk-proj-YOUR-REAL-KEY"
  agent_env:
    OPENAI_API_KEY: "sk-proj-DUMMY0000000000000000000000"
```

Run `setup.sh` to regenerate `.env.agent`. Done — no image rebuild required.

### Adding a new service (OAuth)

For well-known providers in the gateway's built-in registry:

```yaml
api.newprovider.com:
  type: oauth
  provider: newprovider
  strip_headers: [authorization]
  inject_headers:
    Authorization: "Bearer $ACCESS_TOKEN"
  refresh_token: "rt-YOUR-REFRESH-TOKEN"
```

For custom OAuth providers, specify protocol details directly:

```yaml
auth.custom-saas.com:
  type: oauth
  token_url: "https://auth.custom-saas.com/oauth/token"
  client_id: "your-app-client-id"
  content_type: form
  strip_headers: [authorization]
  inject_headers:
    Authorization: "Bearer $ACCESS_TOKEN"
  refresh_token: "your-refresh-token"
```

## Skills & Prompts

Three ways to provide pi skills and prompts to the agent:

### (a) Baked into an extended image

```dockerfile
FROM ghcr.io/sayreblades/truman-agent:latest
COPY my-skills/ /opt/pi-staging/skills/my-skills/
COPY my-prompts/ /opt/pi-staging/prompts/
```

### (b) Mounted at runtime

In `docker-compose.yml`:

```yaml
agent:
  volumes:
    - ~/.pi/agent/skills:/opt/pi-custom/skills:ro
    - ~/.pi/agent/prompts:/opt/pi-custom/prompts:ro
```

The template includes this by default — if pi is installed on the host, its skills are automatically available.

### (c) Both

Baked skills load first, then mounted skills overlay on top. Same-name skills from the mount take priority.

## Devcontainer Compliance

Truman uses the [docker-compose variant](https://containers.dev/implementors/json_reference/) of the devcontainer spec:

- `devcontainer.json` → `"dockerComposeFile"` + `"service": "agent"`
- Works with VS Code Dev Containers extension
- Works with `devcontainer` CLI (`devcontainer up`, `devcontainer exec`)
- Works with GitHub Codespaces
- Works with DevPod

## Usage

See the **[template README](template/README.md)** for detailed instructions on:

- [VS Code integration](template/README.md#vs-code) — open project, "Reopen in Container", full IDE experience
- [Devcontainer CLI](template/README.md#devcontainer-cli) — `devcontainer up` / `devcontainer exec` from any terminal
- [Docker Compose](template/README.md#docker-compose-direct) — direct `docker compose run` for scripting
- [Customization](template/README.md#customization) — extending images, baking in skills

## Development (building truman itself)

```bash
make build          # Build gateway + agent images locally
make publish        # Push images to ghcr.io
make clean          # Remove locally-built images
```

## Design Documents

- [Architecture plan](docs/plan.md)
- [Phase 1: Docker container](docs/plan-phase-1.md)
- [Phase 2: Secret gateway + network isolation](docs/plan-phase-2.md)
- [Generalized auth configuration](docs/plan-auth.md)
