#!/bin/bash
# One-shot rebuild: drop the existing CUPS queue, remove old driver files,
# install the current sources, and re-register the queue using whichever
# USB URI CUPS currently sees for the printer. Safe to run repeatedly.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRINTER_NAME="DCP_T230"
PPD_PATH="/Library/Printers/PPDs/Contents/Resources/Brother-DCP-T230.ppd.gz"

log() { printf '[reinstall] %s\n' "$*"; }
die() { printf '[reinstall] ERROR: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "must be run as root (try: sudo $0)"

# 1. Drop the queue if it exists — the next lpadmin below will recreate it
#    with the freshly installed PPD so stale options never carry over.
if lpstat -p "$PRINTER_NAME" >/dev/null 2>&1; then
    log "removing existing queue '$PRINTER_NAME'"
    lpadmin -x "$PRINTER_NAME" || true
fi

# 2. Clean old filter + PPD files.
log "running uninstall.sh"
bash "$SCRIPT_DIR/uninstall.sh" >/dev/null

# 3. Install fresh files. Suppress install.sh's interactive lpadmin prompt;
#    we do the re-register ourselves below.
log "running install.sh"
INSTALL_OUT=$(printf 'n\n' | bash "$SCRIPT_DIR/install.sh")
printf '%s\n' "$INSTALL_OUT" | sed 's/^/[install] /'

# 4. Re-register the queue, picking up the current USB URI.
log "scanning for printer via lpinfo"
URI="$(lpinfo -v 2>/dev/null | awk '/usb:.*DCP[_-]?T230/ {print $2; exit}')"
if [[ -z "$URI" ]]; then
    log "no USB URI detected (printer powered off? cable unplugged?)"
    log "you can add the queue later with:"
    log "    sudo lpadmin -p $PRINTER_NAME -E -v <URI> -P $PPD_PATH"
    exit 0
fi

log "registering $PRINTER_NAME -> $URI"
lpadmin -p "$PRINTER_NAME" -E -v "$URI" -P "$PPD_PATH"
lpadmin -p "$PRINTER_NAME" -o printer-is-shared=false || true
cupsenable "$PRINTER_NAME" || true
cupsaccept "$PRINTER_NAME" || true

log "done. Test with:"
log "    lp -d $PRINTER_NAME <some.pdf>"
