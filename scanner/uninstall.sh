#!/bin/bash
# Remove the t230web LaunchAgent installed by install.sh.
#
# Scan images in ~/Pictures/T230/ and /tmp/t230/ are left untouched.

set -euo pipefail

PLIST_ID="com.brother.t230scan"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_ID.plist"

log() { printf '[uninstall-scanner] %s\n' "$*"; }

# --- Stop and unload ----------------------------------------------------------
if launchctl list "$PLIST_ID" >/dev/null 2>&1; then
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    log "agent stopped and unloaded"
else
    log "agent not currently loaded"
fi

# --- Remove plist -------------------------------------------------------------
if [[ -f "$PLIST_PATH" ]]; then
    rm -f "$PLIST_PATH"
    log "removed $PLIST_PATH"
else
    log "plist not found at $PLIST_PATH (already removed?)"
fi

log "done. Scan images in ~/Pictures/T230/ and /tmp/t230/ were left in place."
