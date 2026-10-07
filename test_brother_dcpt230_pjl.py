#!/usr/bin/env python3
"""Regression tests for the PWG/CUPS raster sync + endianness handling in
`brother_dcpt230_pjl`.

Covers the macOS Tahoe 26.x bug reported in GitHub issue #1: cgpdftoraster
falls back to little-endian "Apple Raster" v3 (sync b"3SaR") whenever
FINAL_CONTENT_TYPE isn't honoured, instead of big-endian PWG v2 (b"RaS2").
The filter must normalize any of the eight CUPS raster sync words to the
same canonical big-endian output, byte-for-byte, regardless of which
variant it was fed.

Run with: python3 -m unittest test_brother_dcpt230_pjl -v
"""

import importlib.util
import io
import struct
import sys
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

_MODULE_PATH = Path(__file__).with_name("brother_dcpt230_pjl")
_loader = SourceFileLoader("brother_dcpt230_pjl", str(_MODULE_PATH))
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
filt = importlib.util.module_from_spec(_spec)
sys.modules["brother_dcpt230_pjl"] = filt
_loader.exec_module(filt)


def build_header(endian: str, *, hw_res=(300, 300), width=2480, height=3507,
                  bpc=8, bpp=24, bpl=7440, colorspace=19, compression=0,
                  num_colors=3, page_size=(595, 841), media_type=b"") -> bytearray:
    """Build a synthetic 1796-byte PWG/CUPS raster page header.

    `endian` is '>' (big) or '<' (little) — only the numeric fields are
    affected; string fields are byte-order independent.
    """
    hdr = bytearray(filt.PWG_HEADER_SIZE)
    hdr[128:128 + 64] = media_type.ljust(64, b"\x00")
    struct.pack_into(endian + "II", hdr, 276, *hw_res)
    struct.pack_into(endian + "IIII", hdr, 284, 1, 2, 3, 4)  # non-zero: patch must zero it
    struct.pack_into(endian + "I", hdr, 340, 99)             # non-zero: patch must zero it
    struct.pack_into(endian + "II", hdr, 352, *page_size)    # wrong: patch must recompute
    struct.pack_into(endian + "I", hdr, 364, 1)              # non-zero: patch must zero it
    struct.pack_into(endian + "II", hdr, 372, width, height)
    struct.pack_into(endian + "III", hdr, 384, bpc, bpp, bpl)
    struct.pack_into(endian + "I", hdr, 400, colorspace)
    struct.pack_into(endian + "I", hdr, 404, compression)
    struct.pack_into(endian + "I", hdr, 420, num_colors)
    struct.pack_into(endian + "IIIIIIII", hdr, 452, 9, 9, 9, 9, 9, 9, 9, 9)  # patch must zero
    hdr[1732:1732 + 64] = b"custom-page-size".ljust(64, b"\x00")  # patch must clear
    return hdr


def run_rewriter(sync: bytes, hdr: bytearray, pixels: bytes = b"\x01\x02\x03\x04") -> bytes:
    out = io.BytesIO()
    rewriter = filt.PwgRewriter(out)
    rewriter.feed(sync + bytes(hdr) + pixels)
    return out.getvalue()


class NormalizationTests(unittest.TestCase):
    def test_canonical_big_endian_sync_unchanged_in_shape(self):
        hdr = build_header(">")
        out = run_rewriter(b"RaS2", hdr)
        self.assertEqual(out[:4], b"RaS2")
        self.assertEqual(len(out), 4 + filt.PWG_HEADER_SIZE + 4)

    def test_all_eight_sync_words_normalize_to_identical_output(self):
        pixels = b"\xaa\xbb\xcc\xdd"
        reference = None
        for sync, endian in [
            (b"RaSt", ">"), (b"tSaR", "<"),
            (b"RaS1", ">"), (b"1SaR", "<"),
            (b"RaS2", ">"), (b"2SaR", "<"),
            (b"RaS3", ">"), (b"3SaR", "<"),
        ]:
            hdr = build_header(endian)
            out = run_rewriter(sync, hdr, pixels)
            with self.subTest(sync=sync):
                self.assertEqual(out[:4], b"RaS2", f"sync not normalized for input {sync!r}")
                if reference is None:
                    reference = out
                else:
                    self.assertEqual(
                        out, reference,
                        f"output for {sync!r} differs from the RaS2/big-endian reference",
                    )

    def test_patched_fields_match_brothers_expectations(self):
        hdr = build_header("<", hw_res=(300, 300), width=2480, height=3507, page_size=(999, 999))
        out = run_rewriter(b"3SaR", hdr)
        patched = out[4:4 + filt.PWG_HEADER_SIZE]

        def u(off):
            return struct.unpack_from(">I", patched, off)[0]

        self.assertEqual(patched[128:128 + 4], b"auto")
        self.assertEqual((u(284), u(288), u(292), u(296)), (0, 0, 0, 0))
        self.assertEqual(u(340), 0)
        # PageSize recomputed from cupsWidth/Height + HWResolution, not the
        # bogus (999, 999) the synthetic header started with.
        self.assertEqual(u(352), round(2480 * 72 / 300))
        self.assertEqual(u(356), round(3507 * 72 / 300))
        self.assertEqual(u(364), 0)
        self.assertEqual(tuple(u(452 + i * 4) for i in range(8)), (0,) * 8)
        self.assertEqual(patched[1732:1732 + 64], b"\x00" * 64)

    def test_pixel_bytes_pass_through_unmodified(self):
        hdr = build_header("<")
        pixels = bytes(range(256)) * 4
        out = run_rewriter(b"3SaR", hdr, pixels)
        self.assertEqual(out[4 + filt.PWG_HEADER_SIZE:], pixels)

    def test_string_fields_survive_little_endian_swap_intact(self):
        hdr = build_header("<", media_type=b"photo")
        out = run_rewriter(b"3SaR", hdr)
        patched = out[4:4 + filt.PWG_HEADER_SIZE]
        # patch_pwg_header always overwrites MediaType with "auto", but the
        # swap step must not have corrupted it into non-ASCII garbage first.
        self.assertEqual(patched[128:128 + 4], b"auto")

    def test_unrecognized_sync_falls_back_to_raw_passthrough(self):
        garbage = b"NOPE" + b"\x00" * filt.PWG_HEADER_SIZE + b"trailing"
        out = io.BytesIO()
        rewriter = filt.PwgRewriter(out)
        rewriter.feed(garbage)
        self.assertEqual(out.getvalue(), garbage)
        self.assertEqual(rewriter.state, "PASSTHRU")

    def test_chunked_feed_across_byte_boundaries(self):
        """The state machine must reassemble sync+header correctly no matter
        how the input stream happens to be chunked (e.g. by a pipe), following
        the same feed()/passthrough() dispatch main() uses once PASSTHRU."""
        hdr = build_header("<")
        pixels = b"\x11\x22\x33\x44\x55"
        whole = b"3SaR" + bytes(hdr) + pixels
        out = io.BytesIO()
        rewriter = filt.PwgRewriter(out)
        for i in range(len(whole)):
            byte = whole[i:i + 1]
            if rewriter.state == "PASSTHRU":
                rewriter.passthrough(byte)
            else:
                rewriter.feed(byte)
        expected = run_rewriter(b"3SaR", hdr, pixels)
        self.assertEqual(out.getvalue(), expected)


# ---------------------------------------------------------------------------
# Apple Raster (image/urf) -> PWG
# ---------------------------------------------------------------------------

def urf_page(bpp, width, height, lines, dpi=300):
    hdr = bytes([bpp, 1 if bpp == 24 else 0, 1, 4]) + b"\0" * 8
    hdr += struct.pack(">III", width, height, dpi) + b"\0" * 8
    return hdr + b"".join(lines)


def urf_file(pages):
    return b"UNIRAST\x00" + struct.pack(">I", len(pages)) + b"".join(pages)


def decode_lines(body, width, px, height):
    """Independent decoder: expand coded lines to raw rows (handles code 128)."""
    rows, pos = [], 0
    while len(rows) < height:
        rep = body[pos] + 1
        pos += 1
        row = bytearray()
        while len(row) < width * px:
            n = body[pos]
            pos += 1
            if n < 128:
                row += body[pos:pos + px] * (n + 1)
                pos += px
            elif n == 128:
                row += b"\xff" * (width * px - len(row))
            else:
                c = 257 - n
                row += body[pos:pos + c * px]
                pos += c * px
        assert len(row) == width * px
        rows += [bytes(row)] * rep
    return rows[:height], pos


def convert_urf(data, chunk=None):
    out = io.BytesIO()
    conv = filt.UrfConverter(out)
    if chunk is None:
        conv.feed(data)
    else:
        for i in range(0, len(data), chunk):
            conv.feed(data[i:i + chunk])
    return out.getvalue()


class UrfTests(unittest.TestCase):
    W, H = 300, 4
    # rgb: one literal run of 3 px, one repeat of 297 px (2 groups: 128+128+41)
    LINE_A = bytes([0]) + bytes([256 - 2]) + b"\x01\x02\x03" * 3 + \
        bytes([127]) + b"\x09\x09\x09" + bytes([127]) + b"\x09\x09\x09" + \
        bytes([40]) + b"\x09\x09\x09"
    # 2 identical lines: 10 px literal-ish then 'fill rest with white'
    LINE_B = bytes([1]) + bytes([9]) + b"\x05\x06\x07" + bytes([128])

    def make(self):
        lines = [self.LINE_A, self.LINE_B, self.LINE_A]   # 1 + 2 + 1 = 4 lines
        return urf_file([urf_page(24, self.W, self.H, lines)])

    def test_header_and_sync(self):
        out = convert_urf(self.make())
        self.assertEqual(out[:4], b"RaS2")
        h = out[4:4 + filt.PWG_HEADER_SIZE]
        u = lambda o: struct.unpack_from(">I", h, o)[0]
        self.assertEqual((u(372), u(376), u(388), u(392)), (300, 4, 24, 900))
        self.assertEqual((u(276), u(280), u(400), u(420)), (300, 300, 19, 3))
        self.assertEqual(h[128:132], b"auto")
        self.assertEqual(h[0:9], b"PwgRaster")

    def test_body_decodes_to_same_pixels_and_has_no_fill_code(self):
        out = convert_urf(self.make())
        body = out[4 + filt.PWG_HEADER_SIZE:]
        rows, used = decode_lines(body, self.W, 3, self.H)
        self.assertEqual(used, len(body))
        a, _ = decode_lines(self.LINE_A, self.W, 3, 1)
        b, _ = decode_lines(self.LINE_B, self.W, 3, 1)
        self.assertEqual(rows, [a[0], b[0], b[0], a[0]])
        self.assertTrue(rows[1].endswith(b"\xff" * 3 * 200))
        # re-decode while rejecting code 128 outright
        pos = 0
        while pos < len(body):
            pos += 1
            x = 0
            while x < self.W:
                n = body[pos]
                self.assertNotEqual(n, 128)
                if n < 128:
                    pos += 4; x += n + 1
                else:
                    c = 257 - n; pos += 1 + 3 * c; x += c

    def test_every_chunking_gives_identical_output(self):
        data = self.make()
        ref = convert_urf(data)
        for chunk in (1, 2, 7, 31, 100):
            with self.subTest(chunk=chunk):
                self.assertEqual(convert_urf(data, chunk), ref)

    def test_multiple_pages_and_gray(self):
        gray_line = bytes([0]) + bytes([127]) + b"\x80" + bytes([127]) + b"\x80" + bytes([43]) + b"\x80"
        p1 = urf_page(8, 300, 2, [gray_line, gray_line])
        p2 = urf_page(8, 300, 1, [gray_line])
        out = convert_urf(urf_file([p1, p2]), chunk=13)
        self.assertEqual(out[:4], b"RaS2")
        pos = 4
        for h_px in (2, 1):
            h = out[pos:pos + filt.PWG_HEADER_SIZE]
            self.assertEqual(struct.unpack_from(">I", h, 376)[0], h_px)
            self.assertEqual(struct.unpack_from(">I", h, 400)[0], 18)
            pos += filt.PWG_HEADER_SIZE
            rows, used = decode_lines(out[pos:], 300, 1, h_px)
            pos += used
        self.assertEqual(pos, len(out))

    def test_bad_magic_raises(self):
        with self.assertRaises(ValueError):
            convert_urf(b"NOTURF!!" + b"\0" * 64)


if __name__ == "__main__":
    unittest.main()
