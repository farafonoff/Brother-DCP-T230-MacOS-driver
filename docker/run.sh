#!/bin/bash
# Runs Brother's real brdcpt230filter on a PDF and dumps the resulting
# print stream to /work/out/brother.prn. Mirrors exactly what
# filter_dcpt230 does, just without LPD in the loop.
set -euo pipefail

BR_PRT_PATH=/opt/brother/Printers/dcpt230
RC=$BR_PRT_PATH/inf/brdcpt230rc
PI=$BR_PRT_PATH/inf/paperinfij2
BRCONV=$BR_PRT_PATH/lpd/brdcpt230filter

INPUT=${1:-/work/in/input.pdf}
OUT=${2:-/work/out/brother.prn}
PAPER=${PAPER:-A4}
RES=${RES:-300}

mkdir -p "$(dirname "$OUT")"

# Pull page geometry from paperinfij2 (px @ 300dpi).
read_px() {
    awk -v want="$1:" 'tolower($1) == tolower(want) { print $2, $3; exit }' "$PI"
}
read_pt() {
    awk -v want="$1:" 'tolower($1) == tolower(want) { print $4, $5; exit }' \
        "$BR_PRT_PATH/inf/ImagingArea"
}
PXSIZE="$(read_px "$PAPER")"
WIDTH_PX=${PXSIZE% *}
HEIGHT_PX=${PXSIZE#* }

echo "[run] paper=$PAPER  ${WIDTH_PX}x${HEIGHT_PX}px @${RES}dpi"
echo "[run] input=$INPUT"
echo "[run] out=$OUT"

# The rcfile controls options (media, quality, color). Patch PageSize in-place.
WORKRC=$(mktemp)
sed -e "s/^PageSize=.*/PageSize=$PAPER/" "$RC" > "$WORKRC"
cat "$WORKRC"

# Rasterise with ghostscript (ppmraw, exactly like the Brother Perl filter).
gs -q -dNOPROMPT -dNOPAUSE -dSAFER \
   -sDEVICE=ppmraw -sstdout=%stderr -sOutputFile=- \
   -r"$RES" -g"${WIDTH_PX}x${HEIGHT_PX}" \
   "$INPUT" -c quit \
  | "$BRCONV" -pi "$PI" -rc "$WORKRC" > "$OUT"

rm -f "$WORKRC"

SIZE=$(stat -c %s "$OUT")
echo "[run] wrote $SIZE bytes"

# Quick structural peek.
echo "---- head ----"
head -c 512 "$OUT" | xxd | head -40
echo "---- tail ----"
tail -c 128 "$OUT" | xxd
