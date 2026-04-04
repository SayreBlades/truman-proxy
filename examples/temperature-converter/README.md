# Temperature Converter

Example project demonstrating truman's multi-container devcontainer setup.

A simple temperature conversion tool used as a sandbox for the pi coding agent.

## Setup

```bash
# From the truman repo root, build images locally:
make build

# Set up credentials:
cd examples/temperature-converter
cp .devcontainer/.env.example .devcontainer/.env
# Edit .devcontainer/.env with your real API keys
# Or: ../../template/.devcontainer/sync-token.sh (copy to .devcontainer/ first)
```

## Devcontainer Configurations

This example has two devcontainer configurations:

| Config | Service | Network | Purpose |
|--------|---------|---------|---------|
| **Agent (Sandboxed)** | `agent` | `sandbox` only | AI agent — all traffic through gateway |
| **Development** | `dev` | `sandbox` + `egress` | Human developer — direct internet, port forwarding |

### VS Code

1. Open this folder in VS Code
2. **Cmd+Shift+P** → **"Dev Containers: Reopen in Container"**
3. Pick **"Agent (Sandboxed)"** or **"Development"**

You can open both simultaneously in separate VS Code windows.

### Devcontainer CLI

The devcontainer CLI can only manage **one config at a time** per compose project. You pick which container to attach to — `devcontainer exec` works for that one, and you use `docker exec` for the other.

```bash
# Option A: Work in the dev container (human development)
devcontainer up --workspace-folder . --config .devcontainer/dev/devcontainer.json
devcontainer exec --workspace-folder . --config .devcontainer/dev/devcontainer.json bash

# The agent container is also running — use docker exec to reach it:
docker exec -it -u pi $(docker ps -qf "name=temperature.*agent") pi

# Option B: Work in the sandboxed agent
devcontainer up --workspace-folder . --config .devcontainer/agent/devcontainer.json
devcontainer exec --workspace-folder . --config .devcontainer/agent/devcontainer.json pi

# The dev container is also running — use docker exec to reach it:
docker exec -it -u pi $(docker ps -qf "name=temperature.*dev") bash
```

> **Note:** `devcontainer up` starts **all** services in docker-compose.yml regardless of which `--config` you choose. The `--config` only controls which container `devcontainer exec` attaches to. VS Code does not have this limitation — it can open both configs simultaneously in separate windows.

### Teardown

```bash
# Stop (preserves volumes / pi sessions)
docker compose -f .devcontainer/docker-compose.yml down

# Stop and wipe everything (clean slate)
docker compose -f .devcontainer/docker-compose.yml down -v
```

### Docker Compose (direct)

Bypasses the devcontainer layer entirely — useful for scripting or quick interactive sessions:

```bash
# Interactive pi session in the sandboxed agent
docker compose -f .devcontainer/docker-compose.yml run --rm agent

# Shell into the dev container
docker compose -f .devcontainer/docker-compose.yml run --rm dev bash
```

## Usage

```bash
uv run src/app.py 100 C F      # 100°C → 212.0°F
uv run src/app.py 72  F C      # 72°F  → 22.22°C
uv run src/app.py 300 K C      # 300K  → 26.85°C
```
