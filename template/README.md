# Truman Devcontainer Template

Copy the `.devcontainer/` directory into your project to get a sandboxed pi agent runtime.

## Quick Start

```bash
# 1. Copy .devcontainer/ into your project
cp -r .devcontainer/ /path/to/your-project/.devcontainer/

# 2. Run the interactive setup wizard
cd /path/to/your-project
.devcontainer/truman.sh init

# 3. Start the devcontainer
.devcontainer/truman.sh start
```

Then start using truman via [VS Code](#vs-code) or the [devcontainer CLI](#devcontainer-cli).

## What's Included

| File                  | Purpose                           | Git status    |
|-----------------------|-----------------------------------|---------------|
| `devcontainer.json`   | VS Code / devcontainer CLI config | Commit        |
| `docker-compose.yml`  | Gateway + agent container setup   | Commit        |
| `truman.sh`           | Setup wizard + lifecycle commands | Commit        |
| `gateway.yaml`        | Your credentials + rules          | **Generated** |
| `.env.agent`          | Dummy API keys for agent          | **Generated** |
| `.env`                | Host paths for docker-compose     | **Generated** |

## VS Code

**One-time setup:** Install the [Dev Containers](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers) extension (`ms-vscode-remote.remote-containers`).

### Open your project in the devcontainer

1. Open your project folder in VS Code
2. **Cmd+Shift+P** → **"Dev Containers: Reopen in Container"**
3. Wait for containers to start (~30 seconds first time, faster on subsequent opens)
4. The status bar (bottom-left) shows a green remote indicator when connected

### Working inside the devcontainer

Once connected, everything runs inside the sandboxed agent container:

- **Terminal** (**Ctrl+`**) — you're user `pi` with `pi`, `gh`, `git`, `uv` all available
- **File editing** — changes sync to/from the host via bind mount
- **Source control** — VS Code's git UI works through the gateway proxy
- **Extensions** — Python, GitLens, etc. run inside the container

### Common tasks from the VS Code terminal

```bash
# Start an interactive pi session
pi

# Single pi prompt
pi -p "explain this codebase"

# Git operations (go through gateway, credentials injected automatically)
git push
gh pr create

# Python
uv run src/app.py
```

### Stopping

- **Close VS Code window** — containers stop automatically (`shutdownAction: stopCompose`)
- **Cmd+Shift+P** → **"Dev Containers: Reopen Folder Locally"** — returns to local mode, stops containers

## Devcontainer CLI

The [devcontainer CLI](https://github.com/devcontainers/cli) lets you use truman from any terminal, no IDE required.

**Install:**
```bash
npm install -g @devcontainers/cli
```

### Start the containers

```bash
devcontainer up --workspace-folder .
```

### Run commands inside the container

```bash
# Interactive bash shell
devcontainer exec --workspace-folder . bash

# Start pi interactively
devcontainer exec --workspace-folder . pi

# Single pi prompt
devcontainer exec --workspace-folder . pi -p "what files are in this project?"

# Any command
devcontainer exec --workspace-folder . git status
devcontainer exec --workspace-folder . gh repo view
```

### Stop the containers

```bash
# Stop (preserves volumes / pi sessions)
docker compose -f .devcontainer/docker-compose.yml down

# Stop and wipe everything (clean slate)
docker compose -f .devcontainer/docker-compose.yml down -v
```

## Docker Compose (direct)

You can also use docker compose directly, bypassing the devcontainer layer entirely:

```bash
# Interactive pi session (runs entrypoint, then pi)
docker compose -f .devcontainer/docker-compose.yml run --rm agent

# Single prompt
docker compose -f .devcontainer/docker-compose.yml run --rm agent pi -p "hello"

# Shell into agent
docker compose -f .devcontainer/docker-compose.yml run --rm agent bash
```

This is useful for scripting or when you don't need devcontainer features.

## VS Code vs Devcontainer CLI: Limitations

VS Code provides features that the devcontainer CLI does not:

| Feature                            | VS Code                                   | Devcontainer CLI                    |
|------------------------------------|-------------------------------------------|-------------------------------------|
| Automatic port forwarding          | ✅ Detects new listeners, tunnels to host | ❌ No port forwarding               |
| Port auto-detection                | ✅ Notification + "Open in Browser"       | ❌                                  |
| `forwardPorts` / `portsAttributes` | ✅ Full support                           | ❌ Ignored                          |
| Extension installation             | ✅ Installs in container                  | ❌ N/A                              |
| Interactive terminal               | ✅ Integrated                             | ✅ Via `devcontainer exec ... bash` |
| Run commands                       | ✅ Terminal                               | ✅ `devcontainer exec`              |
| Multiple configs simultaneously    | ✅ Separate windows                       | ⚠️ One at a time (see below)        |

The devcontainer CLI is a headless tool — it starts containers and runs commands, but there is no background process watching for new port listeners or managing tunnels. If you're running a web server inside the sandboxed agent container, you won't be able to reach it from your host browser via the CLI alone.

### Solution: Multi-Container Setup (Agent + Dev)

You can add a **second devcontainer** to the same project — a normal development environment that shares the source tree but has direct internet access and full port forwarding. This gives you:

- **Agent container** — sandboxed, all traffic through gateway (for the AI)
- **Dev container** — normal dev experience with port forwarding (for the human)

Both share the same workspace and run simultaneously.

#### Directory structure

```
.devcontainer/
├── docker-compose.yml        # Shared: gateway + agent + dev (3 services)
├── truman.sh                 # Setup wizard + lifecycle commands
├── gateway.yaml              # Real secrets (generated, gitignored)
├── .env.agent                # Dummy keys (generated, gitignored)
├── .env                      # Host paths (generated, gitignored)
├── agent/
│   └── devcontainer.json     # Attaches to "agent" — sandboxed
└── dev/
    └── devcontainer.json     # Attaches to "dev" — normal dev experience
```

#### docker-compose.yml (3 services)

```yaml
services:
  gateway:
    image: ghcr.io/sayreblades/truman-gateway:latest
    volumes:
      - ./gateway.yaml:/etc/gateway/gateway.yaml:ro
      - ${PI_AUTH_JSON:-/dev/null}:/host-auth/auth.json
      - gateway-data:/data
    networks: [sandbox, egress]
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')"]
      interval: 5s
      timeout: 3s
      retries: 3

  agent:
    # Sandboxed — AI agent runs here, all traffic through gateway
    image: ghcr.io/sayreblades/truman-agent:latest
    volumes:
      - ..:/workspace
      - /dev/null:/workspace/.devcontainer/gateway.yaml:ro
      - pi-data:/home/pi/.pi/agent
      - ~/.pi/agent/skills:/opt/pi-custom/skills:ro
      - ~/.pi/agent/prompts:/opt/pi-custom/prompts:ro
    env_file: [{ path: .env.agent, required: true }]
    environment:
      - HTTPS_PROXY=http://gateway:8080
      - HTTP_PROXY=http://gateway:8080
      - NO_PROXY=gateway
      - NODE_EXTRA_CA_CERTS=/usr/local/share/ca-certificates/gateway-ca.crt
      - NODE_OPTIONS=--use-env-proxy
      - NODE_NO_WARNINGS=1
    networks: [sandbox]
    depends_on: { gateway: { condition: service_healthy } }
    working_dir: /workspace
    command: sleep infinity

  dev:
    # Normal dev environment — human works here.
    # Direct internet, port forwarding, no proxy or pi tooling.
    image: ghcr.io/sayreblades/truman-agent:latest
    volumes:
      - ..:/workspace
    networks:
      - egress
    working_dir: /workspace
    command: sleep infinity

networks:
  sandbox: { internal: true }
  egress:

volumes:
  pi-data:
  gateway-data:
```

#### agent/devcontainer.json

```json
{
  "name": "Agent (Sandboxed)",
  "dockerComposeFile": "../docker-compose.yml",
  "service": "agent",
  "workspaceFolder": "/workspace",
  "overrideCommand": false,
  "remoteUser": "pi",
  "shutdownAction": "none"
}
```

#### dev/devcontainer.json

```json
{
  "name": "Development",
  "dockerComposeFile": "../docker-compose.yml",
  "service": "dev",
  "workspaceFolder": "/workspace",
  "overrideCommand": false,
  "remoteUser": "pi",
  "shutdownAction": "stopCompose",
  "customizations": {
    "vscode": {
      "extensions": [
        "ms-python.python",
        "redhat.vscode-yaml",
        "GitHub.vscode-pull-request-github",
        "eamodio.gitlens"
      ]
    }
  }
}
```

#### Using multiple devcontainers

**VS Code** — when you "Reopen in Container," VS Code shows a picker:

- **Agent (Sandboxed)** — for interacting with the AI agent
- **Development** — for normal coding with port forwarding

You can open both simultaneously in separate VS Code windows.

**Devcontainer CLI** — the CLI can only manage **one config at a time** per compose project. You pick which container to attach to with `--config`; `devcontainer exec` works for that one, and you use `docker exec` for the other.

```bash
# Option A: Work in the dev container (human development)
devcontainer up --workspace-folder . --config .devcontainer/dev/devcontainer.json
devcontainer exec --workspace-folder . --config .devcontainer/dev/devcontainer.json bash

# The agent container is also running — use docker exec to reach it:
docker exec -it -u pi $(docker ps -qf "name=.*agent") pi

# Option B: Work in the sandboxed agent
devcontainer up --workspace-folder . --config .devcontainer/agent/devcontainer.json
devcontainer exec --workspace-folder . --config .devcontainer/agent/devcontainer.json pi

# The dev container is also running — use docker exec to reach it:
docker exec -it -u pi $(docker ps -qf "name=.*dev") bash
```

> **Note:** `devcontainer up` starts **all** services in docker-compose.yml regardless of which `--config` you choose. The `--config` only controls which container `devcontainer exec` attaches to. VS Code does not have this limitation — it can open both configs simultaneously in separate windows.

|                        | Agent container           | Dev container                      |
|------------------------|---------------------------|------------------------------------|
| **Purpose**            | AI agent (pi)             | Human developer                    |
| **Network**            | `sandbox` only (isolated) | `sandbox` + `egress` (full access) |
| **Internet**           | Through gateway only      | Direct                             |
| **Port forwarding**    | No (by design)            | ✅ Works in VS Code                |
| **Credentials**        | Dummy (gateway injects)   | No credential injection            |

## Customization

### Adding project-specific tools

Create `.devcontainer/Dockerfile`:

```dockerfile
FROM ghcr.io/sayreblades/truman-agent:latest

# Example: add your project's dependencies
RUN apt-get update && apt-get install -y postgresql-client && rm -rf /var/lib/apt/lists/*
```

Then update `docker-compose.yml`:

```yaml
agent:
  # Replace this:
  # image: ghcr.io/sayreblades/truman-agent:latest
  # With this:
  build:
    context: .
    dockerfile: Dockerfile
```

### Baking in custom skills

```dockerfile
FROM ghcr.io/sayreblades/truman-agent:latest
COPY my-skills/ /opt/pi-staging/skills/my-skills/
```

### Without host pi installation

If pi is not installed on the host, remove the skill/prompt volume mounts from `docker-compose.yml`:

```yaml
# Remove these lines:
# - ~/.pi/agent/skills:/opt/pi-custom/skills:ro
# - ~/.pi/agent/prompts:/opt/pi-custom/prompts:ro
```
