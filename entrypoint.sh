#!/bin/bash
set -e

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

exec "$@"
