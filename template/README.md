# Truman Devcontainer Template

Copy the `.devcontainer/` directory into your project to get a sandboxed pi agent runtime.

## Quick Start

```bash
# 1. Copy .devcontainer/ into your project
cp -r .devcontainer/ /path/to/your-project/.devcontainer/

# 2. Sync your Anthropic credentials
cd /path/to/your-project
.devcontainer/sync-token.sh

# 3. (Optional) Add Brave/GitHub keys
#    Edit .devcontainer/.env

# 4. Add to .gitignore
echo '.devcontainer/.env' >> .gitignore
```

Then start using truman via [VS Code](#vs-code) or the [devcontainer CLI](#devcontainer-cli).

## What's Included

| File                 | Purpose                           | Git status    |
|----------------------|-----------------------------------|---------------|
| `devcontainer.json`  | VS Code / devcontainer CLI config | Commit        |
| `docker-compose.yml` | Gateway + agent container setup   | Commit        |
| `.env.agent`         | Dummy API keys (safe)             | Commit        |
| `.env.example`       | Template for real credentials     | Commit        |
| `.env`               | Your real API keys                | **Gitignore** |
| `setup.sh`           | Pre-flight credential check       | Commit        |
| `sync-token.sh`      | Extract token from host pi        | Commit        |

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
