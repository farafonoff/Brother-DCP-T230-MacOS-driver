#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["libusb1>=3.0"]
# ///
"""
Native macOS scanner driver for the Brother DCP-T230, in pure Python.

This is a port of the t230scan.c reference implementation; same wire
protocol, same constants — see the comments in t230scan.c for the
disassembly trail. The only feature added here is band-granular
streaming via `T230.scan()` yielding chunks: that lets a caller (the web
server) push bytes into an HTTP response as they arrive, so the browser
can render a baseline JPEG progressively while the carriage is still
moving.

Dependencies (managed automatically by `uv` via the PEP 723 block above):
    libusb1                 — Python wrapper for libusb-1.0
    libusb-1.0 dylib        — `brew install libusb`

Run as a script:
    uv run t230scan.py page.jpg 300 C24BIT
or:
    chmod +x t230scan.py && ./t230scan.py page.jpg 300 GRAY256

Use as a library:
    with T230() as scanner:
        with open("page.jpg", "wb") as f:
            for chunk in scanner.scan(dpi=300, mode="C24BIT"):
                f.write(chunk)
"""

from __future__ import annotations

import time
import threading
from typing import Iterator

import usb1


# ---------- Device / protocol constants ----------

VID = 0x04F9
PID = 0x0716

# brscan5's CUsbDevAccsCore picks interface 1 (its [+0x39] field) when the
# device has > 1 USB interfaces. The T230 has 3 interfaces; we follow the
# same logic. Alt 0 is the vendor-class scan channel; alt 1 of the same
# interface would be IPP-USB (printing).
IFACE_NO = 1
ALT_NO = 0
EP_OUT = 0x03
EP_IN = 0x84

# Lock control transfer: bmRequestType=0xC0 (vendor IN to device),
# wValue=0x0002 constant, wIndex=0, wLength=0xff.
LOCK_REQTYPE = 0xC0
LOCK_WVALUE = 0x0002
LOCK_WLEN = 0xFF

# Brother v2 wire framing on the bulk pipe:
ESC = 0x1B
LF = 0x0A
END = 0x80

# Brother sends the page in fixed 65 536-byte bands, each prefixed with a
# 14-byte header that starts with `00 02 01 00 (kind) 00 ...`. The last
# band is shorter. The end-of-scan trailer `00 21 01 00 00 20` follows
# the last band. (Stable header prefix is 4 bytes — bytes 4..5 vary by
# colour mode: `11 00` for grayscale, `15 00` for 24-bit colour.)
BAND_TOTAL = 65536
BAND_HDR_LEN = 14
BAND_HDR_PREFIX = b"\x00\x02\x01\x00"
TRAILER_MAGIC = b"\x00\x21\x01\x00"

# ZLP keep-alive: while the firmware is processing, it sends zero-length
# bulk packets. brscan5 backs off 10 ms between empty reads up to a
# generous deadline. Same here.
ZLP_BACKOFF_MS = 10
DEFAULT_CMD_DEADLINE_MS = 35000

# Idle thresholds for the XSC streaming loop. The first one is what we
# wait when no trailer has been seen yet (so we don't bail mid-scan
# during a between-band carriage move). Once the trailer arrives we drop
# to a small value just so the device's bulk-IN queue drains. Stopping
# the read loop at the trailer (without continuing until idle) leaves
# residue that makes the next SSP return 0xb0 — verified the hard way.
IDLE_BEFORE_TRAILER_MS = 8000
IDLE_AFTER_TRAILER_MS = 1500
HARD_DEADLINE_MS = 900000   # 15 min — covers 1200 DPI color


# ---------- Exceptions ----------

class ScanError(Exception):
    """Generic scanner error."""


class DeviceNotFound(ScanError):
    pass


class ProtocolError(ScanError):
    pass


class Cancelled(ScanError):
    pass


# ---------- Driver ----------

class T230:
    """Context-managed handle on a Brother DCP-T230 over USB.

    Use as a context manager:
        with T230() as t:
            for band in t.scan(300, "GRAY256"):
                ...

    Concurrency note: the device only supports one active session at a
    time and the libusb claim is exclusive. Wrap T230() with a Lock if
    multiple threads might race.
    """

    def __init__(self) -> None:
        self._ctx: usb1.USBContext | None = None
        self._h: usb1.USBDeviceHandle | None = None
        self._locked = False

    # -- Context manager --

    def __enter__(self) -> "T230":
        self._ctx = usb1.USBContext()
        self._ctx.open()
        try:
            h = self._ctx.openByVendorIDAndProductID(VID, PID, skip_on_error=True)
        except usb1.USBError as e:
            self._ctx.close(); self._ctx = None
            raise ScanError(f"usb open failed: {e}") from e
        if h is None:
            self._ctx.close(); self._ctx = None
            raise DeviceNotFound(
                f"DCP-T230 (VID {VID:04x}, PID {PID:04x}) not found on USB")
        self._h = h
        try:
            try:
                self._h.setAutoDetachKernelDriver(True)
            except usb1.USBError:
                pass  # not all platforms support this; macOS does
            self._h.claimInterface(IFACE_NO)
            try:
                self._h.setInterfaceAltSetting(IFACE_NO, ALT_NO)
            except usb1.USBError:
                pass  # device sometimes already at alt 0; non-fatal
            self._control_scan_channel(1)
            self._locked = True
            # Drain any stale bulk-IN bytes left over from a previous session
            # (e.g. an aborted scan, or a probe that didn't read its reply).
            # Without this, our first CKD/SSP reads back somebody else's
            # leftover response. Quick drain (3 short reads, 200 ms each).
            self._drain_pipe(0.6)
        except Exception:
            self._teardown()
            raise
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._teardown()

    def _teardown(self) -> None:
        if self._h is not None:
            if self._locked:
                try:
                    self._control_scan_channel(2)
                except Exception:
                    pass
                self._locked = False
            try:
                self._h.releaseInterface(IFACE_NO)
            except usb1.USBError:
                pass
            self._h.close()
            self._h = None
        if self._ctx is not None:
            self._ctx.close()
            self._ctx = None

    # -- Low-level USB helpers --

    def _control_scan_channel(self, mode: int) -> None:
        """Open (mode=1) or close (mode=2) the scan channel via vendor
        control transfer. The 5-byte response must echo specific bytes
        or the device is in an unexpected state."""
        assert self._h is not None
        resp = self._h.controlRead(
            LOCK_REQTYPE, mode, LOCK_WVALUE, 0, LOCK_WLEN, timeout=5000)
        if len(resp) < 5:
            raise ProtocolError(f"scan-channel ctl(mode={mode}): "
                                f"short response ({len(resp)} bytes)")
        if (resp[1] != 0x10 or resp[2] != mode or resp[3] != 0x02 or
                (resp[4] & 0x80)):
            raise ProtocolError(f"scan-channel ctl(mode={mode}): "
                                f"bad echo {bytes(resp[:5]).hex()}")

    def _bulk_out(self, data: bytes, timeout_ms: int = 30000) -> None:
        assert self._h is not None
        n = self._h.bulkWrite(EP_OUT, bytes(data), timeout=timeout_ms)
        if n != len(data):
            raise ProtocolError(f"bulk OUT short: {n}/{len(data)}")

    def _drain_pipe(self, total_seconds: float = 0.6) -> None:
        """Read and discard any bytes the device has queued. Bounded by
        wall-clock so we don't block forever if the device is genuinely
        spewing data; per-read timeout 200 ms."""
        deadline = time.monotonic() + total_seconds
        while time.monotonic() < deadline:
            data = self._bulk_in(8192, 200)
            if data is None:        # timeout
                return
            if not data:            # ZLP
                continue
            # discard

    def _bulk_in(self, max_len: int, timeout_ms: int) -> bytes | None:
        """Returns:
            bytes — possibly empty (a USB ZLP);
            None  — libusb timeout fired.
        """
        assert self._h is not None
        try:
            data = self._h.bulkRead(EP_IN, max_len, timeout=timeout_ms)
        except usb1.USBErrorTimeout:
            return None
        return bytes(data)

    # -- Brother v2 framed round-trip --

    def round_trip(self, cmd: str, body: bytes | None = None,
                   deadline_ms: int = DEFAULT_CMD_DEADLINE_MS) -> bytes:
        """Send `\\x1B + cmd + \\n + body? + \\x80` and read the reply,
        riding through the device's ZLP keep-alives until real bytes
        arrive."""
        pkt = bytes([ESC]) + cmd.encode("ascii") + bytes([LF])
        if body:
            pkt += body
        pkt += bytes([END])
        self._bulk_out(pkt)
        return self._read_response(deadline_ms)

    def _read_response(self, deadline_ms: int) -> bytes:
        """Pump the bulk IN pipe, treating empty packets as keep-alives.
        Stops on first short non-empty packet (= end of message) or on
        deadline."""
        out = bytearray()
        elapsed = 0
        # Phase 1: wait for first non-empty packet
        while elapsed < deadline_ms:
            data = self._bulk_in(8192, 1000)
            if data is None:
                elapsed += 1000
                continue
            if not data:  # ZLP
                time.sleep(ZLP_BACKOFF_MS / 1000)
                elapsed += ZLP_BACKOFF_MS
                continue
            out.extend(data)
            if len(data) < 8192:
                return bytes(out)
            break
        else:
            return bytes(out)
        # Phase 2: drain any follow-up packets (short timeout)
        while True:
            data = self._bulk_in(8192, 200)
            if not data:
                break
            out.extend(data)
            if len(data) < 8192:
                break
        return bytes(out)

    # -- Scan flow --

    def scan(self, dpi: int, mode: str = "C24BIT",
             cancel: threading.Event | None = None,
             ) -> Iterator[bytes]:
        """Run CKD → SSP → XSC and yield JPEG payload bytes
        band-by-band. The first chunk includes the JPEG header (SOI,
        DQT, SOF0, DHT, SOS); subsequent chunks are entropy-data
        continuation; the last chunk ends in EOI (FF D9). Concatenating
        all yielded chunks gives a valid baseline JPEG.

        ``mode`` is one of "GRAY256", "C24BIT" (those are the values the
        T230 firmware accepts; TEXT and ERRDIF are rejected).

        ``cancel`` is an optional Event; if set, the scan aborts cleanly
        (sending an XSC abort and draining) and raises Cancelled.
        """
        if mode not in ("GRAY256", "C24BIT"):
            raise ValueError(f"unsupported mode {mode!r}")

        # CKD: confirm flatbed has document/lid down.
        ckd = self.round_trip("CKD", b"PSRC=FB\n")
        if len(ckd) < 1 or ckd[0] != 0x00:
            head = ckd[:8].hex() if ckd else "(no reply)"
            raise ProtocolError(f"CKD failed (status=0x{ckd[0] if ckd else 0:02x}): {head}")

        # SSP: set scan parameters. AREA is in pixel-units at the requested DPI.
        max_x = int(8.27 * dpi)
        max_y = int(11.69 * dpi)
        ssp_body = (
            f"OS=LNX\n"
            f"PSRC=FB\n"
            f"RESO={dpi},{dpi}\n"
            f"CLR={mode}\n"
            f"AREA=0,0,{max_x},{max_y}\n"
        ).encode("ascii")
        ssp = self.round_trip("SSP", ssp_body)
        # Status 0xb0 means the firmware still thinks a previous scan is in
        # progress (we cancelled too eagerly, or a prior session crashed).
        # Run the abort+drain dance and retry once.
        if len(ssp) >= 1 and ssp[0] == 0xb0:
            self._abort_scan()
            ssp = self.round_trip("SSP", ssp_body)
        if len(ssp) < 4 or ssp[0] != 0x00 or ssp[1:4] != b"SSP":
            head = ssp[:16].hex() if ssp else "(no reply)"
            raise ProtocolError(f"SSP rejected: {head}")

        # XSC: start the scan. We don't go through round_trip because the
        # response is a long stream, not a single message.
        xsc_body = (
            f"RESO={dpi},{dpi}\n"
            f"AREA=0,0,{max_x},{max_y}\n"
        ).encode("ascii")
        self._bulk_out(bytes([ESC]) + b"XSC" + bytes([LF]) + xsc_body + bytes([END]))

        # Stream the bands.
        raw = bytearray()
        bands_yielded = 0
        seen_trailer_at: int | None = None
        idle_threshold_ms = IDLE_BEFORE_TRAILER_MS
        since_last_ms = 0
        elapsed_ms = 0

        try:
            while elapsed_ms < HARD_DEADLINE_MS:
                if cancel is not None and cancel.is_set():
                    raise Cancelled("scan cancelled by caller")

                data = self._bulk_in(16384, 1000)
                if data is None:
                    elapsed_ms += 1000
                    since_last_ms += 1000
                    if raw and since_last_ms >= idle_threshold_ms:
                        break
                    continue
                if not data:
                    time.sleep(ZLP_BACKOFF_MS / 1000)
                    elapsed_ms += ZLP_BACKOFF_MS
                    since_last_ms += ZLP_BACKOFF_MS
                    if raw and since_last_ms >= idle_threshold_ms:
                        break
                    continue

                raw.extend(data)
                since_last_ms = 0

                # Yield any newly-complete bands' payloads.
                while (bands_yielded + 1) * BAND_TOTAL <= len(raw):
                    start = bands_yielded * BAND_TOTAL
                    if not _is_band_header(raw, start):
                        # Out of sync. This shouldn't happen if the
                        # device is well-behaved, but bail visibly.
                        raise ProtocolError(
                            f"expected band header at offset {start}, "
                            f"got {bytes(raw[start:start+8]).hex()}")
                    payload = bytes(raw[start + BAND_HDR_LEN : start + BAND_TOTAL])
                    bands_yielded += 1
                    yield payload

                # Look for end-of-scan trailer in the last 4 KB of raw.
                if seen_trailer_at is None and len(raw) >= 4:
                    tail_start = max(0, len(raw) - 4096)
                    pos = bytes(raw).find(TRAILER_MAGIC, tail_start)
                    if pos >= 0:
                        seen_trailer_at = pos
                        idle_threshold_ms = IDLE_AFTER_TRAILER_MS

            # Scan loop ended (either trailer-then-idle, or hard deadline).
            # Flush any remaining last-band payload (the partial band
            # following the last fully-yielded one).
            partial_start = bands_yielded * BAND_TOTAL
            if partial_start < len(raw):
                if not _is_band_header(raw, partial_start):
                    raise ProtocolError(
                        f"expected band header at offset {partial_start}, "
                        f"got {bytes(raw[partial_start:partial_start+8]).hex()}")
                end = seen_trailer_at if seen_trailer_at is not None else len(raw)
                if end > partial_start + BAND_HDR_LEN:
                    yield bytes(raw[partial_start + BAND_HDR_LEN : end])

        except Cancelled:
            self._abort_scan()
            raise
        except GeneratorExit:
            # Caller stopped iterating early — treat as a cancel.
            self._abort_scan()
            return

    def _abort_scan(self) -> None:
        """Send the parameter-less XSC ('\\x1bXSC\\n\\x80') = abort form
        per MakeXSCcmdString in the disassembly, then drain the bulk-IN
        queue with the SAME idle-wait policy as natural scan completion
        — anything shorter leaves the firmware in a half-done state and
        the next SSP returns 0xb0. Empirically: 8 s of consecutive silence
        is what natural completion uses; we match it."""
        try:
            self._bulk_out(bytes([ESC]) + b"XSC" + bytes([LF]) + bytes([END]))
        except Exception:
            pass
        idle_target_ms = 8000
        since_last = 0
        elapsed = 0
        deadline = 60000  # 60 s hard cap
        while elapsed < deadline and since_last < idle_target_ms:
            data = self._bulk_in(8192, 500)
            if data is None:
                elapsed += 500
                since_last += 500
                continue
            if not data:
                time.sleep(ZLP_BACKOFF_MS / 1000)
                elapsed += ZLP_BACKOFF_MS
                since_last += ZLP_BACKOFF_MS
                continue
            since_last = 0
            # discard the bytes — we're cancelling, the data isn't useful


# ---------- Helpers ----------

def _is_band_header(buf: bytes | bytearray, pos: int) -> bool:
    """The 4-byte stable prefix of a band header is `00 02 01 00`. Bytes
    4..5 differ by colour mode (11 00 = grayscale, 15 00 = 24-bit colour)
    so we only check the first 4."""
    return (pos + 4 <= len(buf)
            and buf[pos:pos + 4] == BAND_HDR_PREFIX)


def trim_to_jpeg(data: bytes) -> bytes:
    """Find the first FF D8 and last FF D9 — the band-strip already
    produces a contiguous JPEG, but the stream sometimes has a few
    leading/trailing bytes; this is a safety trim for callers that want
    a strictly-formed JPEG."""
    soi = data.find(b"\xff\xd8")
    eoi = data.rfind(b"\xff\xd9")
    if soi < 0 or eoi < 0 or eoi <= soi:
        return data
    return data[soi:eoi + 2]


# ---------- CLI for parity testing ----------

def _main() -> int:
    import argparse, sys
    p = argparse.ArgumentParser(description="Brother DCP-T230 USB scanner")
    p.add_argument("output", help="output JPEG path")
    p.add_argument("dpi", type=int, nargs="?", default=300,
                   help="resolution (default 300)")
    p.add_argument("mode", nargs="?", default="C24BIT",
                   choices=["GRAY256", "C24BIT"],
                   help="colour mode (default C24BIT)")
    args = p.parse_args()

    try:
        with T230() as scanner, open(args.output, "wb") as out:
            for chunk in scanner.scan(args.dpi, args.mode):
                out.write(chunk)
    except ScanError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
