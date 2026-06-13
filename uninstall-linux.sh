#!/bin/bash
# Remove the native Brother DCP-T230 printer driver installed by install-linux.sh.
#
# Does NOT delete any CUPS queues — use `sudo lpadmin -x <queue>` for that.
# Must be run as root: sudo ./uninstall-linux.sh

set -euo pipefail

FILTER_NAME="brother_dcpt230_pjl"
PPD_DEST="/usr/share/ppd/Brother/Brother-DCP-T230.ppd"

LOCATIONS=(
    "/usr/lib/cups/filter/$FILTER_NAME"
    "$PPD_DEST"
)

log() { printf '[uninstall] %s\n' "$*"; }

[[ $EUID -eq 0 ]] || { log "must be run as root (try: sudo $0)"; exit 1; }

removed=0
for path in "${LOCATIONS[@]}"; do
    if [[ -e "$path" || -L "$path" ]]; then
        rm -f "$path"
        log "removed $path"
        removed=$((removed + 1))
    fi
done

# Clean up empty vendor dir.
rmdir "/usr/share/ppd/Brother" 2>/dev/null && log "removed /usr/share/ppd/Brother" || true

# Reload CUPS.
if systemctl is-active --quiet cups 2>/dev/null; then
    systemctl restart cups
    log "CUPS restarted"
fi

log "removed $removed file(s)."
log "If a CUPS queue still references this driver, remove it with:"
log "    sudo lpadmin -x <queue_name>"
