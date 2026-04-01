# Phase 1: Pi Agent in Docker Compose

**Author:** Sayre Blades  
**Date:** 2026-04-01  
**Status:** Final — ready to implement

Phase 1 of the [full plan](plan.md). Get pi running inside a Docker container with the host project filesystem mounted in. **No secret gateway, no network isolation, no web UI** — just a working containerized agent with CLI and RPC access.

---

## Goals

1. Pi agent runs inside a Docker container, operating on bind-mounted project files
2. Two access modes work:
   - **CLI/TUI** — interactive terminal (container as command)
   - **RPC** — JSONL over stdin/stdout (container as command)
3. Sessions persist across container restarts
4. User-level skills are baked into the image
5. API key passed via environment variable (`.env` file)
6. Usage wrapped in a Makefile for ergonomics

## Non-Goals (deferred to later phases)

- Secret gateway / credential injection proxy
- Network isolation (internal-only Docker network)
- Web UI (WebSocket bridge + browser client — see [research notes](#d1--web-ui-research-notes))
- VS Code devcontainer integration
- Multi-agent orchestration
- Image size optimization / multi-stage builds

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│ Host                                                    │
│                                                         │
│  Terminal ──make run────────▶ Container (pi CLI/TUI)    │
│                                                         │
│  Process  ──make rpc────────▶ Container (pi RPC)        │
│             stdin/stdout JSONL                           │
│                                                         │
│  $PWD ────bind mount────────▶ /workspace                │
│                                                         │
│  sessions (named volume) ───▶ /home/pi/.pi/agent/       │
└─────────────────────────────────────────────────────────┘
```

### Single image, two entry points

The same Docker image supports both modes. The Makefile provides convenient
targets that translate to the appropriate `docker compose run` invocations.

| Mode | Make target | Docker command                                   | What happens                  |
|------|-------------|--------------------------------------------------|-------------------------------|
| CLI  | `make run`  | `docker compose run --rm agent`                  | TTY attached, interactive pi  |
| RPC  | `make rpc`  | `docker compose run --rm -T agent pi --mode rpc` | JSONL on stdin/stdout, no TTY |

---

## Key Design Decisions

### D1 — Web UI research notes

> **Deferred to a future phase.** Captured here for context.

[`@mariozechner/pi-web-ui`](https://github.com/badlogic/pi-mono/tree/main/packages/web-ui) is a browser-side component library. Its `Agent` (from `pi-agent-core`) runs the LLM loop and tools **in the browser**. The built-in tools are browser-native (JS REPL, document extraction, artifacts). There is no built-in mechanism to proxy tool execution (bash, read, write, edit) to a remote container.

`pi-agent-core` does export a [`streamProxy`](https://www.npmjs.com/package/@mariozechner/pi-agent-core) function for routing LLM calls through a server, but that only proxies the **model streaming** — not tool execution. Tools still run client-side.

When we add web support, the approach will be:

1. Pi runs inside the container in `--mode rpc` (JSONL on stdin/stdout)
2. A thin **WebSocket bridge** (small Node.js script) runs alongside pi in the same container, forwarding JSON messages bidirectionally between WebSocket clients and pi's stdin/stdout
3. A **minimal static HTML/JS client** connects to the WebSocket and renders the chat, speaking the [pi RPC protocol](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/rpc.md)

**Longer-term option:** Build a richer web UI using `pi-web-ui` components with a custom `Agent` subclass that proxies both LLM calls and tool execution over WebSocket to the container.

### D2 — Session persistence

Pi stores sessions in `~/.pi/agent/sessions/`, organized by working directory. Inside the container, the agent's home is `/home/pi` and the working directory is `/workspace`.

**Decision:** Use a **named Docker volume** mounted at `/home/pi/.pi/agent/` inside the container. This persists:
- Sessions (`sessions/`)
- Settings (`settings.json`)
- Auth state (if any beyond env vars)

The volume survives `docker compose down` and container recreation. Sessions are keyed by the container-side working directory (`/workspace`), which is stable.

**Trade-off:** Sessions are not visible from the host filesystem (they live inside a Docker volume). This is acceptable for Phase 1. If host-visible sessions are needed later, we can bind-mount a host directory instead.

### D3 — Skills

Pi auto-discovers skills from `~/.pi/agent/skills/` (user-level) and `.pi/skills/` / `.agents/skills/` (project-level, walking up from cwd).

**Decision:** Use BuildKit **additional build contexts** to COPY skills directly from the host `~/.pi/agent/skills/` into the image at build time. No need to snapshot skills into the repo. Project-level skills (if any) come in via the bind-mounted workspace.

`docker-compose.yml` declares the additional context:

```yaml
build:
  context: .
  additional_contexts:
    skills: ~/.pi/agent/skills
```

The Dockerfile then selectively copies container-viable skills:

```dockerfile
COPY --from=skills pi-skills/brave-search   /home/pi/.pi/agent/skills/pi-skills/brave-search
COPY --from=skills pi-skills/gccli          /home/pi/.pi/agent/skills/pi-skills/gccli
COPY --from=skills pi-skills/gdcli          /home/pi/.pi/agent/skills/pi-skills/gdcli
COPY --from=skills pi-skills/gmcli          /home/pi/.pi/agent/skills/pi-skills/gmcli
COPY --from=skills pi-skills/transcribe     /home/pi/.pi/agent/skills/pi-skills/transcribe
COPY --from=skills pi-skills/youtube-transcript /home/pi/.pi/agent/skills/pi-skills/youtube-transcript
COPY --from=skills polymarket               /home/pi/.pi/agent/skills/polymarket
```

Skills audit from `~/.pi/agent/skills/`:

| Skill                | Has deps?                      | Container-viable?             | Include? |
|----------------------|--------------------------------|-------------------------------|----------|
| `brave-search`       | node_modules                   | ✅ Yes (HTTP only)            | ✅       |
| `browser-tools`      | node_modules                   | ❌ No (needs Chrome on :9222) | ❌       |
| `gccli`              | none (SKILL.md only)           | ✅ (instructions only)        | ✅       |
| `gdcli`              | none                           | ✅                            | ✅       |
| `gmcli`              | none                           | ✅                            | ✅       |
| `transcribe`         | shell script                   | ✅                            | ✅       |
| `vscode`             | none                           | ❌ No (needs VS Code on host) | ❌       |
| `youtube-transcript` | package.json (no node_modules) | ✅ (needs `npm install`)      | ✅       |
| `polymarket`         | ?                              | ✅                            | ✅       |

**Build step:** After copying, the Dockerfile runs `npm install` in skills that need it (brave-search already has node_modules from the host copy; youtube-transcript needs install). Browser-tools and vscode are excluded by not being listed in the selective COPY commands.

### D4 — API key injection

**Decision:** Pass API keys via a `.env` file read by Docker Compose. Pi picks up `ANTHROPIC_API_KEY` from the environment automatically. Skill-specific keys (e.g. `BRAVE_API_KEY`) are passed the same way.

```
# .env (git-ignored)
ANTHROPIC_API_KEY=sk-ant-...
BRAVE_API_KEY=...
```

No auth.json needed inside the container. Env vars take precedence.

### D5 — Container user

**Decision:** Run as a non-root user `pi` (UID 1000) inside the container. This is standard practice and avoids files created in `/workspace` being owned by root on the host.

On macOS with Docker Desktop, bind-mount permissions are handled transparently (the Linux VM maps UIDs). On Linux hosts, UID 1000 typically matches the host user.

### D6 — Settings

Pi inside the container needs a `settings.json` that makes sense for the containerized environment.

**Decision:** Bake a default `settings.json` into the image:

```json
{
  "defaultProvider": "anthropic",
  "defaultModel": "claude-sonnet-4-20250514",
  "defaultThinkingLevel": "high"
}
```

The user can override by placing `.pi/settings.json` in their project (it's picked up from the bind mount). The named volume also persists any changes made via `/settings` inside pi.

---

## Repo Layout (host)

```
.
├── Dockerfile                  # Container image definition
├── docker-compose.yml          # Agent service + build contexts
├── Makefile                    # Ergonomic targets: run, rpc, build, clean
├── .env.example                # Template for API keys
├── .env                        # Actual API keys (git-ignored)
├── docs/
│   ├── plan.md                 # Full architecture plan
│   └── plan-phase-1.md          # This file
├── README.md                   # Usage instructions
└── ...                         # Existing project files (src/, pyproject.toml, etc.)
```

Skills are **not** in the repo. They are pulled directly from `~/.pi/agent/skills/`
at build time via BuildKit additional build contexts (see [D3](#d3--skills)).

## Container Layout (inside the image)

```
/workspace/                     # Bind-mounted from host $PWD (read-write)
/home/pi/.pi/agent/             # Named volume (persisted across restarts)
├── sessions/                   # Pi session files
├── settings.json               # Baked-in defaults (overridable)
└── skills/                     # COPY'd from host ~/.pi/agent/skills/ at build time
    ├── pi-skills/
    │   ├── brave-search/
    │   ├── gccli/
    │   ├── gdcli/
    │   ├── gmcli/
    │   ├── transcribe/
    │   └── youtube-transcript/
    └── polymarket/
```

---

## Dockerfile

Base: `node:22-bookworm-slim`

Install layers (ordered for cache efficiency):

1. **System packages:** git, curl, python3, pip, common CLI tools
2. **uv** (Python package manager): install via official script
3. **pi:** `npm install -g @mariozechner/pi-coding-agent`
4. **Non-root user:** create `pi` user (UID 1000), set up home directory
5. **Skills:** Selective `COPY --from=skills` for container-viable skills, then `npm install` where needed
6. **Settings:** COPY default `settings.json` into `/home/pi/.pi/agent/`

Working directory: `/workspace`  
Default command: `pi`

---

## docker-compose.yml

```yaml
services:
  agent:
    build:
      context: .
      additional_contexts:
        skills: ~/.pi/agent/skills
    stdin_open: true       # -i
    tty: true              # -t
    volumes:
      - .:/workspace
      - pi-data:/home/pi/.pi/agent
    env_file: .env
    working_dir: /workspace

volumes:
  pi-data:
```

---

## Makefile

```makefile
.PHONY: help build run rpc shell clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

build: ## Build the container image
	docker compose build

run: build ## Interactive CLI/TUI
	docker compose run --rm agent

rpc: build ## RPC mode (JSONL on stdin/stdout)
	docker compose run --rm -T agent pi --mode rpc

shell: build ## Shell into the container (for debugging)
	docker compose run --rm agent bash

clean: ## Remove containers, volumes, and image
	docker compose down -v --rmi local
```

`make` with no target prints help by default (first target).

---

## Skills Build Details

Pi resolves `{baseDir}` in SKILL.md to the skill's directory at runtime, so paths work automatically once skills are in place at `/home/pi/.pi/agent/skills/`.

The selective COPY approach (see [D3](#d3--skills)) means only container-viable skills are included. Skills are always fresh from the host — rebuild the image (`make build`) to pick up changes.

---

## Resolved Questions

1. **Skill env vars:** Pass `BRAVE_API_KEY` (and any other skill-specific keys) through `.env` alongside `ANTHROPIC_API_KEY`. List all expected vars in `.env.example`.

2. **Container image size:** ~800MB–1.2GB is acceptable for Phase 1. Multi-stage build can slim this later.

3. **Hot reload:** Project file changes on the host are immediately visible inside the container via the bind mount. Skills baked into the image require a rebuild (`make build`).

---

## Implementation Order

1. **Dockerfile** — build the image, verify `pi --version` runs
2. **docker-compose.yml + Makefile** — get `make run` working (interactive CLI)
3. **RPC mode** — verify `make rpc` works with a test prompt piped in
4. **README** — document usage
5. **Smoke test** — send a prompt via CLI, see pi execute a bash command against `/workspace`, verify file changes appear on host

---

## Relationship to Full Plan

Phase 1 implements the **devcontainer** box from the full architecture, without the secret gateway, network isolation, or web UI:

| Full Plan Component          | Phase 1 Status                                     |
|------------------------------|----------------------------------------------------|
| Devcontainer (pi + tools)    | ✅ Implemented                                     |
| Bind-mounted workspace       | ✅ Implemented                                     |
| CLI / TUI access             | ✅ Implemented                                     |
| RPC access                   | ✅ Implemented                                     |
| Secret gateway               | ❌ Deferred                                        |
| Internal-only network        | ❌ Deferred                                        |
| Placeholder env vars         | ❌ Not needed without gateway                      |
| VS Code devcontainer         | ❌ Deferred                                        |
| Web access (browser chat UI) | ❌ Deferred (see [D1](#d1--web-ui-research-notes)) |

The gateway can be added as a second container in a future phase. The web UI can be added as a new service (`web`) in `docker-compose.yml` using the WebSocket bridge approach documented in D1, without changing the agent image.
