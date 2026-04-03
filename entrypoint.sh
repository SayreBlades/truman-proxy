#!/bin/bash
set -e

# ── Gateway CA certificate (runs as root) ────────────────────────
# If running behind the secret gateway, fetch and install its CA cert
# so that MITM-intercepted HTTPS connections are trusted by all tools.
if [ -n "$HTTPS_PROXY" ]; then
    echo "Fetching gateway CA certificate..."
    curl -sf --retry 5 --retry-delay 1 http://gateway:8080/ca.pem \
        -o /usr/local/share/ca-certificates/gateway-ca.crt
    update-ca-certificates 2>/dev/null
    echo "Gateway CA certificate installed."
fi

# ── Security: verify real secrets aren't leaked into workspace ────
if [ -f /workspace/.env ] && grep -qE 'sk-ant-ort01-|BSAp-|gho_' /workspace/.env 2>/dev/null; then
    echo "⚠️  WARNING: Real secrets detected in /workspace/.env!" >&2
    echo "   Container may be misconfigured. Check volume mounts." >&2
fi

# ── Skills sync ──────────────────────────────────────────────────
# Sync baked-in skills from the image staging area into the persistent volume.
# This ensures rebuilds with updated skills take effect without wiping sessions.
# rsync --delete ensures removed skills are cleaned up too.
if [ -d /opt/pi-staging/skills ]; then
    rsync -a --delete /opt/pi-staging/skills/ /home/pi/.pi/agent/skills/
fi

# Sync baked-in prompts into the persistent volume.
if [ -d /opt/pi-staging/prompts ]; then
    rsync -a --delete /opt/pi-staging/prompts/ /home/pi/.pi/agent/prompts/
fi

# Seed default settings only if none exist yet (preserve user changes)
if [ ! -f /home/pi/.pi/agent/settings.json ]; then
    cp /opt/pi-staging/settings.json /home/pi/.pi/agent/settings.json
fi

# ── Git / GitHub setup ────────────────────────────────────────────
# Configure gh as git's credential helper so that git push/pull through
# the gateway proxy works without interactive prompts.  The agent's
# dummy GH_TOKEN is sent through the proxy; the gateway replaces it
# with the real token before it reaches GitHub.
gosu pi gh auth setup-git 2>/dev/null || true
gosu pi git config --global push.autoSetupRemote true

# Auto-configure git identity from GitHub profile (via gateway proxy).
# Only if not already set — preserves manual overrides.
if ! gosu pi git config --global user.name &>/dev/null; then
    GH_USER_JSON=$(gosu pi curl -sf https://api.github.com/user 2>/dev/null || true)
    if [ -n "$GH_USER_JSON" ]; then
        GH_NAME=$(echo "$GH_USER_JSON" | jq -r '.name // .login')
        GH_ID=$(echo "$GH_USER_JSON" | jq -r '.id')
        GH_LOGIN=$(echo "$GH_USER_JSON" | jq -r '.login')
        gosu pi git config --global user.name "$GH_NAME"
        gosu pi git config --global user.email "${GH_ID}+${GH_LOGIN}@users.noreply.github.com"
        echo "Git identity: $GH_NAME <${GH_ID}+${GH_LOGIN}@users.noreply.github.com>"
    fi
fi

# ── Drop to non-root user ────────────────────────────────────────
exec gosu pi "$@"
