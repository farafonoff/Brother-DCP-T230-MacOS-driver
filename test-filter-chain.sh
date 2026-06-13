#!/bin/bash
# Test the CUPS filter chain without touching the printer or installed driver.
# All output goes to /tmp/t230-test/. Safe to run at any time.

set -euo pipefail

DIR=/tmp/t230-test
PPD=/usr/share/ppd/Brother/Brother-DCP-T230.ppd
FILTER_DIR=/usr/lib/cups/filter

mkdir -p "$DIR"

log() { printf '[test] %s\n' "$*"; }
ok()  { printf '[test] OK: %s\n' "$*"; }
fail(){ printf '[test] FAIL: %s\n' "$*" >&2; }

# --- Minimal PostScript test page --------------------------------------------
cat > "$DIR/test.ps" << 'EOF'
%!PS
/Helvetica findfont 24 scalefont setfont
72 720 moveto (DCP-T230 filter test) show
showpage
EOF
log "test input: $DIR/test.ps"

# --- Step 1: gstoraster (PostScript -> raster) --------------------------------
log "running gstoraster..."
PPD="$PPD" \
  "$FILTER_DIR/gstoraster" 1 root "test" 1 "media=A4" "$DIR/test.ps" \
  > "$DIR/test.ras" 2>"$DIR/gs.log"
GS_EXIT=$?

cat "$DIR/gs.log"
log "gstoraster exit: $GS_EXIT"

SYNC=$(xxd "$DIR/test.ras" | head -1 | awk '{print $2$3}' | cut -c1-8)
log "raster sync word: $SYNC"
case "$SYNC" in
    52615332) ok "RaS2 — correct, filter will accept this" ;;
    52615333) fail "RaS3 — mismatch! filter expects RaS2. cupsRasterVersion not respected by gstoraster." ;;
    *)        fail "unknown sync word: $SYNC" ;;
esac

# --- Step 2: brother_dcpt230_pjl (raster -> PJL) -----------------------------
log "running brother_dcpt230_pjl..."
PPD="$PPD" \
  "$FILTER_DIR/brother_dcpt230_pjl" 1 root "test" 1 "media=A4" \
  < "$DIR/test.ras" > "$DIR/test.pjl" 2>"$DIR/filter.log"
FILTER_EXIT=$?

cat "$DIR/filter.log"
log "brother_dcpt230_pjl exit: $FILTER_EXIT"

PJL_HEAD=$(head -c 10 "$DIR/test.pjl" 2>/dev/null || true)
if [[ "$PJL_HEAD" == $'\x1b%-12345X'* ]] || [[ "$PJL_HEAD" == *"@PJL"* ]]; then
    ok "output starts with PJL header — filter chain works"
else
    fail "output does not look like PJL: $(xxd "$DIR/test.pjl" | head -2)"
fi

log "output files in $DIR/"
ls -lh "$DIR/"
