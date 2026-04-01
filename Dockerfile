# ── Pi Agent Container ─────────────────────────────────────────────
# Base: Node 22 on Debian Bookworm (slim)
FROM node:22-bookworm-slim

# ── 1. System packages ────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates python3 python3-pip python3-venv \
        jq ripgrep procps less rsync gosu \
    && rm -rf /var/lib/apt/lists/*

# ── 1b. gh CLI ────────────────────────────────────────────────────
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# ── 2. uv (Python package manager) ───────────────────────────────
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# ── 3. Pi coding agent ───────────────────────────────────────────
RUN npm install -g @mariozechner/pi-coding-agent

# ── 4. Non-root user ─────────────────────────────────────────────
# node:22-bookworm-slim ships with user "node" (UID 1000). Rename to "pi".
RUN usermod -l pi -d /home/pi -m node \
    && groupmod -n pi node \
    && mkdir -p /home/pi/.pi/agent/skills /workspace \
    && chown -R pi:pi /home/pi /workspace

# ── 5. Skills — staged at /opt/pi-staging (NOT under the volume) ─
#    The entrypoint syncs these into the volume on every start,
#    so rebuilds with updated skills take effect without wiping sessions.
COPY --from=skills pi-skills/brave-search       /opt/pi-staging/skills/pi-skills/brave-search
COPY --from=skills pi-skills/gccli              /opt/pi-staging/skills/pi-skills/gccli
COPY --from=skills pi-skills/gdcli              /opt/pi-staging/skills/pi-skills/gdcli
COPY --from=skills pi-skills/gmcli              /opt/pi-staging/skills/pi-skills/gmcli
COPY --from=skills pi-skills/transcribe         /opt/pi-staging/skills/pi-skills/transcribe
COPY --from=skills pi-skills/youtube-transcript /opt/pi-staging/skills/pi-skills/youtube-transcript
COPY --from=skills polymarket                   /opt/pi-staging/skills/polymarket

# Install npm deps for skills that need them
RUN cd /opt/pi-staging/skills/pi-skills/youtube-transcript && npm install --omit=dev

# ── 6. Default settings (staged) ─────────────────────────────────
RUN echo '{\n  "defaultProvider": "anthropic",\n  "defaultModel": "claude-sonnet-4-20250514",\n  "defaultThinkingLevel": "high"\n}' \
    > /opt/pi-staging/settings.json

# ── 7. Entrypoint — syncs staged content into the volume ─────────
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# ── 8. Fix ownership ─────────────────────────────────────────────
RUN chown -R pi:pi /home/pi /opt/pi-staging

# ── Switch to non-root user ──────────────────────────────────────
# NOTE: We do NOT set USER here. The entrypoint runs as root to install
# the gateway CA certificate, then drops to user "pi" via gosu.
WORKDIR /workspace

ENTRYPOINT ["entrypoint.sh"]
CMD ["pi"]
