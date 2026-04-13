#!/bin/bash
# Install the native Brother DCP-T230 driver on macOS.
#
# Lays the Python filter into /usr/libexec/cups/filter/ (with a fallback
# to /usr/local/libexec/cups/filter/ when SIP blocks the system path),
# installs the PPD under /Library/Printers/PPDs/, and optionally
# registers the printer via lpadmin.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILTER_SRC="$SCRIPT_DIR/brother_dcpt230_pjl"
PPD_SRC="$SCRIPT_DIR/brother-dcpt230.ppd"

FILTER_NAME="brother_dcpt230_pjl"
PPD_NAME="Brother-DCP-T230.ppd.gz"

SYSTEM_FILTER_DIR="/usr/libexec/cups/filter"
LOCAL_FILTER_DIR="/usr/local/libexec/cups/filter"
PPD_DIR="/Library/Printers/PPDs/Contents/Resources"
VENDOR_DIR="/Library/Printers/Brother/DCP-T230"

PRINTER_NAME="DCP_T230"
PRINTER_URI=""   # filled in below

log() { printf '[install] %s\n' "$*"; }
die() { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }

[[ -f "$FILTER_SRC" ]] || die "filter not found at $FILTER_SRC"
[[ -f "$PPD_SRC"    ]] || die "PPD not found at $PPD_SRC"

if [[ $EUID -ne 0 ]]; then
    die "must be run as root (try: sudo $0)"
fi

# --- Choose a writable filter directory ------------------------------------
FILTER_DIR="$SYSTEM_FILTER_DIR"
if ! ( mkdir -p "$FILTER_DIR" && touch "$FILTER_DIR/.wtest" ) 2>/dev/null; then
    log "system filter dir not writable (SIP?); using $LOCAL_FILTER_DIR"
    FILTER_DIR="$LOCAL_FILTER_DIR"
    mkdir -p "$FILTER_DIR"
fi
rm -f "$FILTER_DIR/.wtest"

# --- Install the filter ----------------------------------------------------
mkdir -p "$VENDOR_DIR"
install -m 0755 "$FILTER_SRC" "$VENDOR_DIR/$FILTER_NAME"
log "installed filter: $VENDOR_DIR/$FILTER_NAME"

# Symlink the filter into CUPS's filter search path.
ln -sf "$VENDOR_DIR/$FILTER_NAME" "$FILTER_DIR/$FILTER_NAME"
chmod 0755 "$VENDOR_DIR/$FILTER_NAME"
log "linked: $FILTER_DIR/$FILTER_NAME -> $VENDOR_DIR/$FILTER_NAME"

# --- Install the PPD (gzipped, where macOS expects it) ---------------------
mkdir -p "$PPD_DIR"
gzip -9 -c "$PPD_SRC" > "$PPD_DIR/$PPD_NAME"
chmod 0644 "$PPD_DIR/$PPD_NAME"
log "installed PPD: $PPD_DIR/$PPD_NAME"

# --- Verify the PPD ---------------------------------------------------------
if command -v cupstestppd >/dev/null 2>&1; then
    if cupstestppd -q "$PPD_DIR/$PPD_NAME" >/dev/null 2>&1; then
        log "cupstestppd: PPD conforms"
    else
        log "cupstestppd: WARNINGS (non-fatal). Run: cupstestppd -v $PPD_DIR/$PPD_NAME"
    fi
fi

# --- Optionally register a printer via lpadmin -----------------------------
if command -v lpinfo >/dev/null 2>&1; then
    log "scanning USB backends for the DCP-T230..."
    # Output lines look like: direct usb://Brother/DCP-T230?serial=XXX "Brother DCP-T230"
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
        lpadmin -p "$PRINTER_NAME" -E -v "$PRINTER_URI" -P "$PPD_DIR/$PPD_NAME"
        lpadmin -p "$PRINTER_NAME" -o printer-is-shared=false || true
        cupsenable "$PRINTER_NAME" || true
        cupsaccept "$PRINTER_NAME" || true
        log "queue '$PRINTER_NAME' created and enabled"
        log "test with:  lp -d $PRINTER_NAME /System/Library/Fonts/Supplemental/Times\\ New\\ Roman.ttf"
    else
        log "skipped lpadmin registration"
    fi
else
    log "skipping lpadmin registration"
    log "you can add the printer later via System Settings > Printers & Scanners,"
    log "choosing 'Brother DCP-T230 (PWG/PJL native)' as the driver."
fi

log "done."
