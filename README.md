# Pi Agent in Docker Compose

Run [pi](https://github.com/badlogic/pi-mono) inside a Docker container with the host project filesystem bind-mounted in. Sessions persist across restarts via a named Docker volume.

## Prerequisites

- Docker Desktop (or Docker Engine + Compose plugin)
- An Anthropic API key

## Quick Start

```bash
# 1. Add your API keys
cp .env.example .env
# Edit .env with your real keys

# 2. Build and run interactively
make run
```

## Make Targets

| Target       | Description                            |
|--------------|----------------------------------------|
| `make help`  | Show all available targets             |
| `make build` | Build the container image              |
| `make run`   | Interactive CLI/TUI session            |
| `make rpc`   | RPC mode (JSONL on stdin/stdout)       |
| `make shell` | Drop into a bash shell (for debugging) |
| `make clean` | Remove containers, volumes, and image  |

## Architecture

```
Host                                    Container
────                                    ─────────
Terminal ──make run──────────▶  pi (CLI/TUI)
Process  ──make rpc──────────▶  pi (RPC, JSONL stdin/stdout)
$PWD     ──bind mount────────▶  /workspace
         ──named volume──────▶  /home/pi/.pi/agent/  (sessions, settings, skills)
```

- **CLI mode** (`make run`): Interactive terminal — type prompts, see pi work.
- **RPC mode** (`make rpc`): Pipe JSONL in/out for programmatic access.

## API Keys

All keys go in `.env` (git-ignored). Required:

- `ANTHROPIC_API_KEY` — for the LLM

Optional (for skills):

- `BRAVE_API_KEY` — Brave Search skill

## Skills

Container-viable skills from `~/.pi/agent/skills/` are baked into the image at build time. Rebuild to pick up skill changes:

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

## Details

See [docs/plan-phase-1.md](docs/plan-phase-1.md) for the full design document.
