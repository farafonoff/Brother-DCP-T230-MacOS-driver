#!/bin/bash
# Install the native Brother DCP-T230 printer driver on Debian / Armbian / Ubuntu.
#
# Copies the CUPS filter to /usr/lib/cups/filter/ and the PPD to
# /usr/share/ppd/Brother/, then optionally registers a CUPS queue.
#
# Must be run as root: sudo ./install-linux.sh
#
# Requirements (auto-checked below):
#   sudo apt install cups python3 python3-usb cups-filters avahi-daemon

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILTER_SRC="$SCRIPT_DIR/brother_dcpt230_pjl"
PPD_SRC="$SCRIPT_DIR/brother-dcpt230.ppd"

FILTER_NAME="brother_dcpt230_pjl"
PPD_DIR="/usr/share/ppd/Brother"
PPD_DEST="$PPD_DIR/Brother-DCP-T230.ppd"
FILTER_DIR="/usr/lib/cups/filter"

PRINTER_NAME="DCP_T230"
PRINTER_URI=""

log() { printf '[install] %s\n' "$*"; }
die() { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }

[[ -f "$FILTER_SRC" ]] || die "filter not found at $FILTER_SRC"
[[ -f "$PPD_SRC"    ]] || die "PPD not found at $PPD_SRC"

[[ $EUID -eq 0 ]] || die "must be run as root (try: sudo $0)"

# --- Check dependencies -------------------------------------------------------
missing_pkgs=()
command -v cupsd   >/dev/null 2>&1 || missing_pkgs+=(cups)
command -v python3 >/dev/null 2>&1 || missing_pkgs+=(python3)
# CUPS filters live in /usr/lib/cups/filter/, not on $PATH
[[ -x /usr/lib/cups/filter/pdftoraster ]] || missing_pkgs+=(cups-filters)

if [[ ${#missing_pkgs[@]} -gt 0 ]]; then
    die "missing packages: ${missing_pkgs[*]}
Install with:  sudo apt install ${missing_pkgs[*]}"
fi

# avahi-daemon enables Bonjour/AirPrint discovery for phones and tablets.
if ! systemctl is-active --quiet avahi-daemon 2>/dev/null; then
    log "WARNING: avahi-daemon not running — phones and tablets won't discover the printer."
    log "  sudo apt install avahi-daemon && sudo systemctl enable --now avahi-daemon"
fi

# Warn if python3-usb is absent (filter needs it at print time).
if ! python3 -c "import usb.core" 2>/dev/null; then
    log "WARNING: python3-usb not found — install it so the filter can talk to the printer:"
    log "  sudo apt install python3-usb"
fi

# --- Filter path: Debian puts CUPS filters under /usr/lib/cups/filter --------
[[ -d "$FILTER_DIR" ]] || die "CUPS filter directory not found at $FILTER_DIR (is cups installed?)"

# --- Install the filter -------------------------------------------------------
install -m 0755 -o root -g root "$FILTER_SRC" "$FILTER_DIR/$FILTER_NAME"
log "installed filter: $FILTER_DIR/$FILTER_NAME"

# --- Install the PPD ----------------------------------------------------------
mkdir -p "$PPD_DIR"
install -m 0644 -o root -g root "$PPD_SRC" "$PPD_DEST"
log "installed PPD: $PPD_DEST"

# --- Validate the PPD ---------------------------------------------------------
if command -v cupstestppd >/dev/null 2>&1; then
    if cupstestppd -q "$PPD_DEST" 2>/dev/null; then
        log "cupstestppd: PPD conforms"
    else
        log "cupstestppd: WARNINGS (non-fatal). Run: cupstestppd -v $PPD_DEST"
    fi
fi

# --- Restart CUPS so it picks up the new filter and PPD ----------------------
if systemctl is-active --quiet cups 2>/dev/null; then
    systemctl restart cups
    log "CUPS restarted"
elif command -v service >/dev/null 2>&1; then
    service cups restart 2>/dev/null && log "CUPS restarted" || log "WARNING: could not restart CUPS — restart it manually"
fi

# --- Optionally register a printer via lpadmin --------------------------------
if command -v lpinfo >/dev/null 2>&1; then
    log "scanning USB backends for the DCP-T230..."
    USB_LINE="$(lpinfo -v 2>/dev/null | awk '/usb:.*DCP[_-]?T230/ {print; exit}')"
    if [[ -n "$USB_LINE" ]]; then
        PRINTER_URI="$(awk '{print $2}' <<<"$USB_LINE")"
        log "found printer at: $PRINTER_URI"
    else
        log "no DCP-T230 detected over USB (is it connected and powered on?)"
    fi
fi

if [[ -n "$PRINTER_URI" ]]; then
    read -r -p "[install] Register CUPS queue '$PRINTER_NAME' using $PRINTER_URI? [y/N] " ans
    if [[ "${ans:-n}" =~ ^[Yy]$ ]]; then
        lpadmin -p "$PRINTER_NAME" -E -v "$PRINTER_URI" -P "$PPD_DEST"
        lpadmin -p "$PRINTER_NAME" -o printer-is-shared=true || true
        cupsenable  "$PRINTER_NAME" || true
        cupsaccept  "$PRINTER_NAME" || true
        log "queue '$PRINTER_NAME' created, enabled, and shared on the network"
        log "test with:  lp -d $PRINTER_NAME /usr/share/doc/cups/examples/testprint.pdf"
    else
        log "skipped lpadmin registration"
    fi
else
    log "skipping lpadmin registration"
    log "Add the printer later with:"
    log "  sudo lpadmin -p $PRINTER_NAME -E -v <URI> -P $PPD_DEST"
    log "  lpinfo -v   # to find the URI once the printer is connected"
fi

log "done."
