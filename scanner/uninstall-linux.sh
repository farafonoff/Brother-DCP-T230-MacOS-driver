#!/bin/bash
# Remove the t230web systemd service installed by install-linux.sh.
# Run with sudo to remove a system service, without sudo for a user service.
#
# Scan images in ~/Pictures/T230/ are left untouched.

set -euo pipefail

SERVICE_NAME="t230scan"

log() { printf '[uninstall-scanner] %s\n' "$*"; }

if [[ $EUID -eq 0 ]]; then
    SERVICE_PATH="/etc/systemd/system/$SERVICE_NAME.service"
    SYSTEMCTL="systemctl"
else
    SERVICE_PATH="$HOME/.config/systemd/user/$SERVICE_NAME.service"
    SYSTEMCTL="systemctl --user"
fi

if $SYSTEMCTL is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    $SYSTEMCTL stop "$SERVICE_NAME"
    log "service stopped"
fi

if $SYSTEMCTL is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
    $SYSTEMCTL disable "$SERVICE_NAME"
    log "service disabled"
fi

if [[ -f "$SERVICE_PATH" ]]; then
    rm -f "$SERVICE_PATH"
    $SYSTEMCTL daemon-reload
    log "removed $SERVICE_PATH"
else
    log "service file not found (already removed?)"
fi

log "done. Scan images in ~/Pictures/T230/ were left in place."
