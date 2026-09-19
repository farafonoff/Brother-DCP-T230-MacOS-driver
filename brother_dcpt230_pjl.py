#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Brother DCP-T230 — native macOS CUPS filter.

Reads PWG Raster from stdin (produced by CUPS rastertopwg / cgpdftoraster),
rewrites the PWG page header to match Brother's quirks, wraps the stream
in Brother's exact PJL envelope, and writes it to stdout for delivery
over the USB backend.

Ground truth obtained by running Brother's own `brdcpt230filter` Linux
binary on A4 Plain/Normal/Color inside an amd64 Docker container and
diffing the output byte-for-byte.

Exact wire format (one page example):

  ESC%-12345X@PJL                                  <- UEL + bare @PJL init
  @PJL SET HOLD=OFF
  @PJL SET STRINGCODESET=UTF8
  @PJL SET PAPER=A4
  @PJL SET BORDERLESS=OFF
  @PJL SET JTTOPMARGIN=300
  @PJL SET JTBOTMARGIN=300
  @PJL SET JTLEFTMARGIN=300
  @PJL SET JTRIGHTMARGIN=300
  @PJL SET RENDERMODE=COLOR
  @PJL SET PRINTQUALITY=NORMAL
  @PJL SET DUPLEX=OFF
  @PJL SET MEDIATYPE=REGULAR
  @PJL SET MANUALFEED=OFF
  @PJL SET SOURCETRAY=AUTO
  @PJL JOBSETTINGLOG DRIVER=I0004036110000000100020009000200000007
  @PJL SET FIDELITY=TRUE
  @PJL ENTER LANGUAGE=PWGRASTER
  <PWG Raster v2 stream: RaS2 magic + 1796-byte page header(s) + pixel data>
  ESC%-12345X ESC%-12345X                          <- two bare UELs, no EOJ

Notable deviations from a "standard" CUPS PWG Raster pipeline:

* No JOB NAME, USERNAME, JOBNAME, LOGINUSER — Brother never emits them.
* JOBSETTINGLOG DRIVER value is an unquoted composite ID, not a free string.
* The PWG page header from `rastertopwg` has several fields that Brother
  zeroes out (margins, ImageBox, TraySwitch, TotalPageCount, AlternatePrimary)
  and one it sets (MediaType="auto"). We rewrite those in-place.

CUPS filter calling convention:
  argv[1] job-id, argv[2] user, argv[3] title,
  argv[4] copies, argv[5] options, [argv[6] filename]
"""

import os
import shlex
import struct
import subprocess
import sys
import threading
from typing import Dict, Tuple


ESC = b"\x1b"
UEL = ESC + b"%-12345X"


# ----------------------------------------------------------------------
# Option mapping tables (derived from Brother's brprintconf and brdcpt230filter)
# ----------------------------------------------------------------------

# PPD PageSize keyword -> (PJL PAPER value, borderless?)
PAPER_MAP: Dict[str, Tuple[str, bool]] = {
    "A4":                  ("A4",              False),
    "BrA4_B":              ("A4",              True),
    "A3":                  ("A3",              False),
    "BrA3_B_B":            ("A3",              True),
    "Letter":              ("LETTER",          False),
    "BrLetter_B":          ("LETTER",          True),
    "Legal":               ("LEGAL",           False),
    "Executive":           ("EXECUTIVE",       False),
    "A5":                  ("A5",              False),
    "A6":                  ("A6",              False),
    "BrA6_B":              ("A6",              True),
    "B5":                  ("JISB5",           False),
    "JISB6":               ("JISB6",           False),
    "BrPostC4x6_S":        ("P4X6",            False),
    "BrPostC4x6_B":        ("P4X6",            True),
    "BrIndexC5x8_S":       ("P5X8",            False),
    "BrIndexC5x8_B":       ("P5X8",            True),
    "BrPhotoL_S":          ("P3X5",            False),
    "BrPhotoL_B":          ("P3X5",            True),
    "BrPhoto2L_S":         ("2LBAN",           False),
    "BrPhoto2L_B":         ("2LBAN",           True),
    "EnvDL":               ("DL",              False),
    "EnvC5":               ("C5",              False),
    "Env10":               ("COM10",           False),
    "EnvMonarch":          ("MONARCH",         False),
    "FanFoldGermanLegal":  ("FOLIO",           False),
    "195x270mm":           ("SIXTEENK195X270", False),
    "MexicanLegal":        ("MEXICANLEGAL",    False),
    "IndianLegal":         ("INDIALEGAL",      False),
}

# Print quality (BRResolution) -> PJL PRINTQUALITY
QUALITY_MAP: Dict[str, str] = {
    "Draft":  "DRAFT",
    "Normal": "NORMAL",
    "Fine":   "HIGH",
}

# Media type (BRMediaType) -> PJL MEDIATYPE
MEDIA_MAP: Dict[str, str] = {
    "Plain":            "REGULAR",
    "Thin":             "REGULAR",
    "Thick":            "THICK2",
    "Thicker":          "THICK2",
    "BOND":             "BOND",
    "Bond":             "BOND",
    "Recycled":         "RECYCLED",
    "Env":              "ENVELOPES",
    "Envelopes":        "ENVELOPES",
    "EnvThick":         "ENVTHICK",
    "EnvThin":          "ENVTHIN",
    "Label":            "LABEL",
    "Glossy":           "GLOSSY",
    "Inkjet":           "INKJET",
    "IJHagakiCom":      "IJHAGAKICOM",
    "GlossyHagakiCom":  "GLOSSYHAGAKICOM",
    "PlainHagakiCom":   "PLAINHAGAKICOM",
    "IJHagakiAddr":     "IJHAGAKIADDR",
    "GlossyHagakiAddr": "GLOSSYHAGAKIADDR",
    "PlainHagakiAddr":  "PLAINHAGAKIADDR",
}

# Paper source (BRInputSlot) -> PJL SOURCETRAY
TRAY_MAP: Dict[str, str] = {
    "AutoSelect": "AUTO",
    "Auto":       "AUTO",
    "Tray1":      "TRAY1",
    "Tray2":      "TRAY2",
    "Tray3":      "TRAY3",
    "MPTray":     "MPTRAY",
}


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def log(level: str, msg: str) -> None:
    """Emit a CUPS-style log line on stderr."""
    sys.stderr.write(f"{level}: {msg}\n")
    sys.stderr.flush()


def parse_options(opt_str: str) -> Dict[str, str]:
    """Parse a CUPS option string ('a=b c=d ...') into a dict."""
    out: Dict[str, str] = {}
    if not opt_str:
        return out
    try:
        tokens = shlex.split(opt_str)
    except ValueError:
        # shlex can choke on unclosed quotes; fall back to naive split.
        tokens = opt_str.split()
    for tok in tokens:
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k.strip()] = v.strip()
        else:
            out[tok.strip()] = "true"
    return out


def pjl_escape(s: str) -> str:
    """Quote a PJL string value: replace stray quotes and drop control chars."""
    if s is None:
        return ""
    cleaned = "".join(ch for ch in s if 0x20 <= ord(ch) < 0x7F and ch != '"')
    return cleaned[:80]  # PJL values are typically capped; be conservative.


# ----------------------------------------------------------------------
# PJL envelope construction
# ----------------------------------------------------------------------

def build_preamble(job_id: str, user: str, title: str,
                   opts: Dict[str, str]) -> bytes:
    """Assemble the PJL header that precedes the PWG Raster payload.

    Order and contents match Brother's official brdcpt220filter output exactly:
    UEL + @PJL
    @PJL SET USERNAME="..."
    @PJL SET JOBNAME="..."
    @PJL SET LOGINUSER="..."
    @PJL JOB NAME="..."
    @PJL SET PAPER=...
    @PJL SET BORDERLESS=...
    @PJL SET JT*MARGIN=...
    @PJL SET RENDERMODE=...
    @PJL SET PRINTQUALITY=...
    @PJL SET DUPLEX=...
    @PJL SET MEDIATYPE=...
    @PJL SET SOURCETRAY=...
    @PJL SET FIDELITY=TRUE
    @PJL ENTER LANGUAGE=PWGRASTER
    """
    # Paper / borderless ---------------------------------------------------
    page_size = opts.get("PageSize") or opts.get("media") or "Letter"
    pjl_paper, borderless = PAPER_MAP.get(page_size, ("LETTER", False))
    margin = 0 if borderless else 300

    # Colour / quality / media / tray -------------------------------------
    mono = opts.get("BRMonoColor", "FullColor") == "Mono"
    quality = opts.get("BRResolution", "Normal")
    duplex = opts.get("Duplex", "None")
    media = opts.get("BRMediaType", "Plain")
    tray = opts.get("BRInputSlot", "AutoSelect")
    manual_feed = (tray == "Manual")

    safe_user = pjl_escape(user) or "guest"
    safe_title = pjl_escape(title) or "job"

    lines = [
        "@PJL ",                           # bare UEL-follower, Brother-style
        f'@PJL SET USERNAME="{safe_user}"',
        f'@PJL SET JOBNAME="{safe_title}"',
        f'@PJL SET LOGINUSER="{safe_user}"',
        f'@PJL JOB NAME="{safe_title}"',
        f"@PJL SET PAPER={pjl_paper}",
        f"@PJL SET BORDERLESS={'ON' if borderless else 'OFF'}",
        f"@PJL SET JTTOPMARGIN={margin}",
        f"@PJL SET JTBOTMARGIN={margin}",
        f"@PJL SET JTLEFTMARGIN={margin}",
        f"@PJL SET JTRIGHTMARGIN={margin}",
        f"@PJL SET RENDERMODE={'GRAYSCALE' if mono else 'COLOR'}",
        f"@PJL SET PRINTQUALITY={QUALITY_MAP.get(quality, 'NORMAL')}",
        f"@PJL SET DUPLEX={'OFF' if duplex == 'None' else 'ON'}",
        f"@PJL SET MEDIATYPE={MEDIA_MAP.get(media, 'REGULAR')}",
        (f"@PJL SET SOURCETRAY={TRAY_MAP.get(tray, 'AUTO')}"
         if not manual_feed else "@PJL SET SOURCETRAY=AUTO"),
        "@PJL SET FIDELITY=TRUE",
        "@PJL ENTER LANGUAGE=PWGRASTER",
    ]

    body = ("\n".join(lines) + "\n").encode("ascii")
    return UEL + body


def build_postamble(title: str) -> bytes:
    # Matches Brother's brdcpt220filter MEndJob:
    # UEL + @PJL EOJ NAME="..." + UEL
    clean_title = pjl_escape(title) or "job"
    return UEL + f'@PJL EOJ NAME="{clean_title}"\n'.encode("ascii") + UEL


# ----------------------------------------------------------------------
# PWG Raster v2 header rewriter
# ----------------------------------------------------------------------

PWG_HEADER_SIZE = 1796
PWG_SYNC = b"RaS2"

# All eight sync words defined by cups/raster.h: the plain (big-endian)
# form of CUPS Raster v1/v1/v2/v3 and their REVSYNC (byte-swapped) twins.
# macOS's cgpdftoraster emits big-endian "RaS2" when FINAL_CONTENT_TYPE is
# honoured, but silently falls back to little-endian "Apple Raster" v3
# ("3SaR") when it isn't -- same 1796-byte header layout, just written
# with the opposite byte order. We accept any of them and normalize.
_PWG_SYNC_BIG_ENDIAN = {
    b"RaSt", b"RaS1", b"RaS2", b"RaS3",  # no swap needed
}
_PWG_SYNC_LITTLE_ENDIAN = {
    b"tSaR", b"1SaR", b"2SaR", b"3SaR",  # swap before patching
}

# Byte ranges within the 1796-byte header that are ASCII strings, not
# 4-byte numeric fields -- must be left alone when byte-swapping.
_PWG_STRING_RANGES = (
    (0, 64), (64, 64), (128, 64), (192, 64),   # MediaClass/Color/Type/OutputType
    (580, 1024),                                # cupsString[16][64]
    (1604, 64), (1668, 64), (1732, 64),         # cupsMarkerType/RenderingIntent/PageSizeName
)


def _is_pwg_string_offset(off: int) -> bool:
    return any(start <= off < start + length for start, length in _PWG_STRING_RANGES)


def _swap_pwg_header_endianness(hdr: bytearray) -> None:
    """Byte-swap every 4-byte numeric field in place (skips string fields).

    Converts a little-endian ("Apple Raster") header to the canonical
    big-endian layout Brother's firmware expects, in place. Swapping is
    its own inverse, so this also works the other direction if ever needed.
    """
    for off in range(0, PWG_HEADER_SIZE, 4):
        if _is_pwg_string_offset(off):
            continue
        hdr[off:off + 4] = hdr[off:off + 4][::-1]


def patch_pwg_header(hdr: bytearray) -> bytearray:
    """Rewrite a 1796-byte PWG v2 page header to match Brother's output.

    rastertopwg derives several fields from the PPD/PageSize that Brother's
    own driver either zeroes out or overrides. These are the fields we
    observed differing in the reference capture; everything else is left
    untouched (pixel data, resolution, color depth, dimensions).
    """
    assert len(hdr) == PWG_HEADER_SIZE

    # MediaType (offset 128, 64-byte string) -> "auto"
    hdr[128:128 + 64] = b"auto".ljust(64, b"\x00")

    # ImagingBoundingBox[L,B,R,T] (offsets 284-299) -> all 0
    struct.pack_into(">IIII", hdr, 284, 0, 0, 0, 0)

    # NumCopies (340) -> 0
    struct.pack_into(">I", hdr, 340, 0)

    # PageSize[0], PageSize[1] (352, 356). rastertopwg can truncate
    # fractional PPD PaperDimension values (e.g. 841.68 -> 841) while
    # Brother rounds to nearest. Re-compute from cupsWidth/cupsHeight
    # and HWResolution so we always match Brother's representation.
    hw_x = struct.unpack_from(">I", hdr, 276)[0]
    hw_y = struct.unpack_from(">I", hdr, 280)[0]
    w_px = struct.unpack_from(">I", hdr, 372)[0]
    h_px = struct.unpack_from(">I", hdr, 376)[0]
    if hw_x and hw_y:
        struct.pack_into(">I", hdr, 352, round(w_px * 72 / hw_x))
        struct.pack_into(">I", hdr, 356, round(h_px * 72 / hw_y))

    # TraySwitch (364) -> 0
    struct.pack_into(">I", hdr, 364, 0)

    # cupsInteger[0..7] — Brother leaves these at 0 but rastertopwg
    # sometimes sets TotalPageCount / ImageBox / AlternatePrimary here.
    struct.pack_into(">IIIIIIII", hdr, 452, 0, 0, 0, 0, 0, 0, 0, 0)

    # cupsPageSizeName (1732, 64-byte string) -> empty
    hdr[1732:1732 + 64] = b"\x00" * 64

    return hdr


def sniff_pwg_header(hdr: bytes) -> None:
    """Log the page header so we can see what we're about to send."""
    def u(off: int) -> int:
        return struct.unpack_from(">I", hdr, off)[0]
    log("DEBUG",
        f"PWG page: {u(372)}x{u(376)}px @ {u(276)}x{u(280)}dpi, "
        f"{u(384)}bpc/{u(388)}bpp, bpl={u(392)}, "
        f"colorspace={u(400)}, numColors={u(420)}, "
        f"compression={u(404)}, "
        f"mediaType={bytes(hdr[128:128+16]).rstrip(bytes([0]))!r}, "
        f"margins=[{u(284)},{u(288)},{u(292)},{u(296)}]")


class PwgRewriter:
    """Stream-oriented rewriter for concatenated PWG v2 pages.

    PWG v2 is framed as:
        [4 bytes: 'RaS2'] ([1796-byte page header] [compressed pixel data])+
    We buffer until we have the sync + a full header, patch the header,
    emit it, then stream the pixel bytes raw until we see another header
    — which is detected by the next 'RaS2' appearing OR, in practice,
    by header count being one per RaS2 block. Brother/rastertopwg emit
    exactly one 'RaS2' marker at the start of the whole stream; each
    subsequent page header follows immediately after the previous page's
    pixel data. So we track page boundaries via cupsBytesPerLine * cupsHeight
    read from the header we just patched.
    """
    def __init__(self, out):
        self.out = out
        self.buf = bytearray()
        self.state = "SYNC"       # SYNC -> HEADER -> PIXELS -> HEADER -> ...
        self.pixels_remaining = 0
        self.first_header_logged = False
        self.needs_swap = False

    def feed(self, data: bytes) -> None:
        self.buf.extend(data)
        while self._drain():
            pass

    def _drain(self) -> bool:
        if self.state == "SYNC":
            if len(self.buf) < 4:
                return False
            sync = bytes(self.buf[:4])
            if sync in _PWG_SYNC_LITTLE_ENDIAN:
                self.needs_swap = True
            elif sync in _PWG_SYNC_BIG_ENDIAN:
                self.needs_swap = False
            else:
                log("WARNING", f"expected a PWG/CUPS raster sync, got {sync!r} — passing through")
                # Degrade gracefully: flush buffer unchanged, don't touch stream.
                self.out.write(bytes(self.buf))
                self.buf.clear()
                self.state = "PASSTHRU"
                return False
            if sync != PWG_SYNC:
                log("WARNING",
                    f"non-canonical raster sync {sync!r} "
                    f"(cupsRasterVersion/FINAL_CONTENT_TYPE not honoured upstream) "
                    f"— normalizing to {PWG_SYNC!r}")
            # Always emit the canonical big-endian PWG v2 sync: whatever
            # variant we accepted, the header gets byte-swapped to match.
            self.out.write(PWG_SYNC)
            del self.buf[:4]
            self.state = "HEADER"
            return True

        if self.state == "HEADER":
            if len(self.buf) < PWG_HEADER_SIZE:
                return False
            hdr = bytearray(self.buf[:PWG_HEADER_SIZE])
            del self.buf[:PWG_HEADER_SIZE]
            if self.needs_swap:
                _swap_pwg_header_endianness(hdr)
            if not self.first_header_logged:
                sniff_pwg_header(bytes(hdr))
                self.first_header_logged = True
            patch_pwg_header(hdr)
            self.out.write(bytes(hdr))
            # Switch to raw passthrough. Any bytes still buffered from
            # the chunk that contained the header (the first pixel bytes
            # of the page) must be flushed now — the main loop only
            # passes *new* bytes through once state == PASSTHRU.
            self.state = "PASSTHRU"
            if self.buf:
                self.out.write(bytes(self.buf))
                self.buf.clear()
            return True

        return False  # PASSTHRU: handled by the main loop

    def passthrough(self, data: bytes) -> None:
        self.out.write(data)


class PrefixedStream:
    """Wraps an input stream with pre-read bytes."""
    def __init__(self, prefix: bytes, stream):
        self._prefix = prefix
        self._stream = stream

    def read(self, n: int = -1) -> bytes:
        if self._prefix:
            if n < 0 or n >= len(self._prefix):
                out = self._prefix
                self._prefix = b""
                if n > len(out):
                    out += self._stream.read(n - len(out))
                return out
            out = self._prefix[:n]
            self._prefix = self._prefix[n:]
            return out
        return self._stream.read(n)

    def close(self):
        if hasattr(self._stream, "close"):
            self._stream.close()


def find_rastertopwg() -> str:
    for p in (
        "/usr/libexec/cups/filter/rastertopwg",
        "/usr/lib/cups/filter/rastertopwg",
        "/usr/local/libexec/cups/filter/rastertopwg",
    ):
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return ""


def convert_cups_to_pwg(src_stream, argv, lead_bytes: bytes):
    """Pipes CUPS raster (e.g. 3SaR from macOS cgpdftoraster) into rastertopwg
    to generate standard PWG Raster with PackBits compression and correct canvas.
    """
    r_path = find_rastertopwg()
    if not r_path:
        log("WARNING", "rastertopwg not found; streaming raster as-is")
        return PrefixedStream(lead_bytes, src_stream), None

    cmd = [r_path] + list(argv[1:6])
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    def writer():
        try:
            proc.stdin.write(lead_bytes)
            while True:
                chunk = src_stream.read(65536)
                if not chunk:
                    break
                proc.stdin.write(chunk)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    return proc.stdout, proc


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main(argv) -> int:
    if len(argv) < 6:
        sys.stderr.write(
            "ERROR: Brother DCP-T230 filter invoked with bad arguments.\n"
            "       Usage: brother_dcpt230_pjl job-id user title copies options [file]\n"
        )
        return 1

    job_id, user, title, _copies, options = argv[1:6]
    filename = argv[6] if len(argv) >= 7 else ""

    opts = parse_options(options)

    log("INFO", f"Brother DCP-T230 macOS filter: job={job_id} user={user!r} title={title!r}")
    log("DEBUG", f"Options: {opts}")

    # Input ----------------------------------------------------------------
    if filename and filename != "-":
        try:
            src = open(filename, "rb")
        except OSError as e:
            log("ERROR", f"Cannot open input file {filename!r}: {e}")
            return 1
        close_src = True
    else:
        src = sys.stdin.buffer
        close_src = False

    # Check raster format: CUPS raster vs PWG raster -----------------------
    proc_to_wait = None
    lead = src.read(16)
    if lead.startswith(b"RaS2") and lead[4:14] == b"PwgRaster\x00":
        # Native PWG raster from upstream
        in_stream = PrefixedStream(lead, src)
    elif len(lead) >= 4 and (lead[:4] in _PWG_SYNC_BIG_ENDIAN or lead[:4] in _PWG_SYNC_LITTLE_ENDIAN):
        log("INFO", f"CUPS raster ({lead[:4]!r}) detected; converting to PWG raster via rastertopwg")
        in_stream, proc_to_wait = convert_cups_to_pwg(src, argv, lead)
    else:
        in_stream = PrefixedStream(lead, src)

    out = sys.stdout.buffer

    # Ignore SIGPIPE so a broken USB link produces a clean error, not a crash.
    try:
        import signal
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (ImportError, ValueError, OSError):
        pass

    try:
        # 1) PJL preamble -------------------------------------------------
        out.write(build_preamble(job_id, user, title, opts))
        out.flush()

        # 2) Stream + rewrite PWG Raster -----------------------------------
        rewriter = PwgRewriter(out)
        total = 0
        chunk = 256 * 1024
        while True:
            buf = in_stream.read(chunk)
            if not buf:
                break
            total += len(buf)
            if rewriter.state == "PASSTHRU":
                rewriter.passthrough(buf)
            else:
                rewriter.feed(buf)
        # If we never got past SYNC/HEADER (truncated input), flush whatever
        # is buffered to avoid losing bytes.
        if rewriter.state != "PASSTHRU" and rewriter.buf:
            out.write(bytes(rewriter.buf))

        out.flush()
        log("INFO", f"Forwarded {total} bytes of PWG raster")

        # 3) PJL postamble ------------------------------------------------
        out.write(build_postamble(title))
        out.flush()

        if proc_to_wait:
            proc_to_wait.wait()

        log("INFO", "Job complete")
        return 0

    except BrokenPipeError:
        log("ERROR", "Pipe to backend closed unexpectedly (USB disconnect?)")
        return 1
    except Exception as e:  # noqa: BLE001 — top-level error handler
        log("ERROR", f"{type(e).__name__}: {e}")
        return 1
    finally:
        if in_stream:
            in_stream.close()
        if close_src:
            try:
                src.close()
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main(sys.argv))
