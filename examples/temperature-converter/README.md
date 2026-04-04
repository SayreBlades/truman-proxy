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

```bash
# Start with the dev container
devcontainer up --workspace-folder . --config .devcontainer/dev/devcontainer.json

# Or start with the sandboxed agent
devcontainer up --workspace-folder . --config .devcontainer/agent/devcontainer.json

# Run commands in dev container
devcontainer exec --workspace-folder . --config .devcontainer/dev/devcontainer.json bash

# Run pi in the sandboxed agent
devcontainer exec --workspace-folder . --config .devcontainer/agent/devcontainer.json pi
```

### Docker Compose (direct)

```bash
# Interactive pi session in the sandboxed agent
docker compose -f .devcontainer/docker-compose.yml run --rm agent

# Shell into the dev container
docker compose -f .devcontainer/docker-compose.yml run --rm dev bash

# Stop everything
docker compose -f .devcontainer/docker-compose.yml down -v
```

## Usage

```bash
uv run src/app.py 100 C F      # 100°C → 212.0°F
uv run src/app.py 72  F C      # 72°F  → 22.22°C
uv run src/app.py 300 K C      # 300K  → 26.85°C
```
