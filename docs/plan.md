# Agent Containerization — Sandboxed AI Agents in Docker

**Author:** Sayre Blades
**Date:** 2026-03-30

Architecture and prototype for running AI coding agents (pi) entirely inside Docker containers with secret protection, network isolation, and project bind-mounts.

---

## Overview

Run the *entire* agent stack — LLM client, tool execution, filesystem access — inside a Docker devcontainer. The host provides only an IDE (VS Code, TUI, or web) and the project files. A secret-injection gateway runs as a sidecar container, intercepting all egress HTTP and injecting real credentials. The devcontainer sits on an internal-only Docker network and can only reach the outside world through the gateway. The project directory is bind-mounted from the host into the devcontainer; secrets never enter it in plaintext.

This is a middle-ground between two existing open-source approaches:

| Project                                                | What's sandboxed               | Isolation          | Secret model                      |
|--------------------------------------------------------|--------------------------------|--------------------|-----------------------------------|
| [Gondolin](https://github.com/earendil-works/gondolin) | Tools only (agent on host)     | Micro-VM (QEMU)    | Placeholder env → host proxy      |
| [OneCLI](https://github.com/onecli/onecli)             | Nothing (proxy only)           | None               | Encrypted vault → HTTP proxy      |
| **This**                                               | Devcontainer + tools + gateway | Docker (multi-ctr) | Placeholder env → sidecar gateway |

### Why Docker over micro-VMs?

- **Simpler** — Dockerfile, not a custom kernel + initramfs + QEMU flags.
- **Faster** — Containers start in seconds.
- **Standard tooling** — `apt install` whatever the agent needs.
- **Portable** — Docker Desktop (Mac), Linux, CI/CD.
- **Good-enough isolation** — Prevents the agent from reading host secrets, ~/.ssh, ~/.aws, etc. Network isolation ensures the devcontainer can only reach the gateway container, never the host or internet directly.

---

## Architecture

![architecture.png](./.assets/plan.png)

Components:

- **Host (trusted)**
  - **IDE** (VS Code / TUI / Web) — Thin client, sends prompts, renders responses
  - **/workspace** (project files) — Mounted into devcontainer
- **Docker**
  - **Secret Gateway** (OneCLI / custom proxy) — Intercepts egress HTTP; injects real credentials for allowed hosts. Only container with external network access.
  - **Devcontainer** (Node.js container) — LLM client + tools (bash, read, write, edit). Sees only placeholder secrets. Internal network only.
- **Web (external services)**
  - GitHub Copilot (LLM API)
  - LevelUp MCP (Internal MCP server)
  - Artifactory (Package registry)
  - GitHub (Repos, PRs, issues)
  - Public Web (DuckDuckGo, docs, etc.)
  - Internal Web (Corporate intranet sites)

---

## Design Decisions

### D1 — Where does the LLM API call happen?

**Decision:** Inside Docker. The devcontainer calls the GitHub Copilot API, but its traffic is forced through the secret gateway container (via `HTTP_PROXY` / `HTTPS_PROXY`). The gateway injects the real credentials; the devcontainer only holds a placeholder token.

**Rationale:** Keeps the architecture simple — the agent is one process in the devcontainer. The gateway is a second container on the same Docker network. The alternative (LLM call on host, tool dispatch into container) splits the agent across two runtimes and complicates streaming, context, and error handling.

### D2 — How does the user interact?

The host runs an IDE that connects to the devcontainer. Three modes, progressively richer:

1. **VS Code:** Open the project in a devcontainer — VS Code connects to the containerized agent natively. The IDE reads project files from the host workspace and the devcontainer reads/writes them via bind mount.
2. **CLI / TUI:** `docker exec -it <container> pi` — attach directly.
3. **pi.el:** Emacs connects to the containerized agent over a socket.

Prototype starts with mode 2 (CLI).

### D3 — How are secrets protected?

Layered approach — everything enforced inside Docker, nothing on the host:

1. **No secret files mounted** — Devcontainer never sees ~/.ssh, ~/.aws, .env, etc.
2. **Placeholder environment variables** — Devcontainer gets `GITHUB_TOKEN=PLACEHOLDER_xxx`.
3. **Gateway container** — A sidecar container (OneCLI or custom) sits on both the internal and egress Docker networks. It swaps placeholder values for real credentials on matching outbound HTTP requests.
4. **Docker network isolation** — The devcontainer is on an *internal-only* network. It cannot reach the internet or the host — only the gateway container. The gateway is the sole egress point.

### D4 — How is the project mounted?

- Project files live on the *host* at `$PWD`.
- Bind mount: `-v $PWD:/workspace` into the devcontainer.
- Read-write so the agent can create/edit files.
- The IDE on the host can also read the workspace directly (e.g., VS Code file explorer, syntax highlighting, git status).
- Only the project directory is mounted — nothing else from the host.
- The gateway container has *no* filesystem mounts from the host project.

---

## Prototype

Source lives in `.src/` (git-ignored). The prototype is a Docker Compose stack:

- `.src/Dockerfile` — Devcontainer image (Node.js + pi + common tools)
- `.src/docker-compose.yml` — Two containers (devcontainer + gateway) with isolated networking:
  - **sandbox** network (internal only) — devcontainer ↔ gateway
  - **egress** network — gateway → internet
- `.src/proxy/` — Minimal secret-injection proxy (or OneCLI config)
- `.src/start.sh` — Launch script that wires up $PWD, starts the gateway, prompts for secret configuration, then launches the devcontainer

---

## Inspiration & References

- [Gondolin](https://github.com/earendil-works/gondolin) — Micro-VM sandbox with programmable network/filesystem mediation. Excellent secrets model (placeholder env vars, host-side substitution). Has a [Pi extension](https://github.com/earendil-works/gondolin/blob/main/host/examples/pi-gondolin.ts) that redirects tools into the VM.
- [OneCLI](https://github.com/onecli/onecli) — Credential vault + Rust HTTP proxy. Docker-native. AES-256-GCM encrypted secrets, MITM HTTPS injection, rules engine.
- [Gondolin Security Design](https://earendil-works.github.io/gondolin/security/) — Threat model and trust boundary analysis.
- [Gondolin Secrets Handling](https://earendil-works.github.io/gondolin/secrets/) — Placeholder substitution mechanics.
- [OneCLI Architecture](https://onecli.sh/docs/how-it-works) — Rust gateway, rules engine, secret store.

---

## Open Questions

- [ ] Can OneCLI be used as-is for the gateway container, or do we need a custom proxy?
- [ ] What's the right interaction protocol for pi.el to talk to the containerized agent? (WebSocket? gRPC? simple HTTP SSE?)
- [ ] How to handle tools that need host access (e.g., open browser, clipboard)? Probably out-of-scope for the sandbox — those stay on the host IDE.
- [ ] Image size budget — how lean can we make the devcontainer?
- [ ] Should the devcontainer image be generic (any agent) or pi-specific?
- [ ] How does the gateway container receive real secrets on first run? Options: host env vars passed via `docker compose`, volume-mounted config, or the OneCLI web dashboard (exposed to host on port 10254).
- [ ] Does the gateway need access to the AT&T corporate network / VPN for internal web and Confluence? If so, the egress network may need host network access or a VPN sidecar.
- [ ] VS Code devcontainer integration — can we generate a `.devcontainer/` config that wires up the gateway sidecar automatically?
