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

# ── Skills sync ──────────────────────────────────────────────────
# Sync baked-in skills from the image staging area into the persistent volume.
# This ensures rebuilds with updated skills take effect without wiping sessions.
# rsync --delete ensures removed skills are cleaned up too.
if [ -d /opt/pi-staging/skills ]; then
    rsync -a --delete /opt/pi-staging/skills/ /home/pi/.pi/agent/skills/
fi

# Seed default settings only if none exist yet (preserve user changes)
if [ ! -f /home/pi/.pi/agent/settings.json ]; then
    cp /opt/pi-staging/settings.json /home/pi/.pi/agent/settings.json
fi

# ── Drop to non-root user ────────────────────────────────────────
exec gosu pi "$@"
