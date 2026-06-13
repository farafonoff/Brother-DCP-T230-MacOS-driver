#!/bin/bash
# Remove the t230web systemd user service installed by install-linux.sh.
#
# Scan images in ~/Pictures/T230/ and /tmp/t230/ are left untouched.
# The udev rule and plugdev group membership are left in place (they are
# harmless and removing them would require additional sudo prompts).

set -euo pipefail

SERVICE_NAME="t230scan"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_PATH="$SERVICE_DIR/$SERVICE_NAME.service"

log() { printf '[uninstall-scanner] %s\n' "$*"; }

# --- Stop and disable ---------------------------------------------------------
if systemctl --user is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    systemctl --user stop "$SERVICE_NAME"
    log "service stopped"
else
    log "service not currently running"
fi

if systemctl --user is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
    systemctl --user disable "$SERVICE_NAME"
    log "service disabled"
fi

# --- Remove unit file ---------------------------------------------------------
if [[ -f "$SERVICE_PATH" ]]; then
    rm -f "$SERVICE_PATH"
    systemctl --user daemon-reload
    log "removed $SERVICE_PATH"
else
    log "service file not found at $SERVICE_PATH (already removed?)"
fi

log "done. Scan images in ~/Pictures/T230/ and /tmp/t230/ were left in place."
log "The udev rule /etc/udev/rules.d/70-brother-t230.rules was left in place."
