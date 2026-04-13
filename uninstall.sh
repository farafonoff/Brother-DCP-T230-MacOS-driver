#!/bin/bash
# Remove the native Brother DCP-T230 driver.
# Does NOT delete any CUPS queues you may have created — use
# `lpadmin -x <queue>` for that.

set -euo pipefail

FILTER_NAME="brother_dcpt230_pjl"
PPD_NAME="Brother-DCP-T230.ppd.gz"

LOCATIONS=(
    "/usr/libexec/cups/filter/$FILTER_NAME"
    "/usr/local/libexec/cups/filter/$FILTER_NAME"
    "/Library/Printers/Brother/DCP-T230/$FILTER_NAME"
    "/Library/Printers/PPDs/Contents/Resources/$PPD_NAME"
)

log() { printf '[uninstall] %s\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
    log "must be run as root (try: sudo $0)"
    exit 1
fi

removed=0
for path in "${LOCATIONS[@]}"; do
    if [[ -e "$path" || -L "$path" ]]; then
        rm -f "$path"
        log "removed $path"
        removed=$((removed + 1))
    fi
done

# Try to clean up an empty vendor dir.
rmdir "/Library/Printers/Brother/DCP-T230" 2>/dev/null || true
rmdir "/Library/Printers/Brother" 2>/dev/null || true

log "removed $removed file(s). If a CUPS queue still exists, remove it with:"
log "    lpadmin -x <queue_name>"
