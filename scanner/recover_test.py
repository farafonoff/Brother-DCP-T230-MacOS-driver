#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["libusb1>=3.0"]
# ///
"""Try a few recovery sequences against a wedged DCP-T230."""

import sys, time
import usb1

VID, PID = 0x04F9, 0x0716
IFACE = 1

def open_device(ctx):
    h = ctx.openByVendorIDAndProductID(VID, PID, skip_on_error=True)
    if h is None:
        print("not found"); sys.exit(1)
    try: h.setAutoDetachKernelDriver(True)
    except: pass
    return h

def try_lock(h):
    try:
        resp = h.controlRead(0xC0, 1, 0x0002, 0, 0xFF, timeout=5000)
        ok = len(resp) >= 5 and resp[1] == 0x10 and resp[2] == 1 and resp[3] == 2 and not (resp[4] & 0x80)
        return ok, bytes(resp[:5]).hex()
    except Exception as e:
        return False, str(e)

def try_unlock(h):
    try:
        h.controlRead(0xC0, 2, 0x0002, 0, 0xFF, timeout=5000)
    except: pass

def try_ssp(h):
    h.bulkWrite(0x03, b"\x1bSSP\nOS=LNX\nPSRC=FB\nRESO=300,300\nCLR=GRAY256\nAREA=0,0,2481,3507\n\x80", timeout=5000)
    try:
        for _ in range(50):
            try:
                d = h.bulkRead(0x84, 8192, timeout=1000)
                if d:
                    return d[0], bytes(d[:8]).hex()
            except usb1.USBErrorTimeout:
                pass
            time.sleep(0.1)
    except Exception as e:
        return None, str(e)
    return None, "(timeout)"

def attempt(label, fn):
    print(f"\n=== {label} ===")
    ctx = usb1.USBContext(); ctx.open()
    h = open_device(ctx)
    try:
        fn(h)
        # Always finish with: claim, lock, ssp test
        try:
            h.claimInterface(IFACE)
            h.setInterfaceAltSetting(IFACE, 0)
        except Exception as e:
            print(f"  claim/setalt error (proceeding): {e}")
        ok, info = try_lock(h)
        print(f"  lock: {'OK' if ok else 'BAD'} ({info})")
        if ok:
            status, info = try_ssp(h)
            print(f"  ssp:  status=0x{status:02x} {info}" if status is not None else f"  ssp: {info}")
            try_unlock(h)
        try: h.releaseInterface(IFACE)
        except: pass
    finally:
        h.close(); ctx.close()

# 1. Just open + lock + ssp (baseline)
attempt("baseline: open + claim + lock + ssp", lambda h: None)

# 2. resetDevice
attempt("after resetDevice()", lambda h: h.resetDevice())

# 3. setConfiguration cycle
def cfg_cycle(h):
    try: h.setConfiguration(1)
    except Exception as e: print(f"  setConfig(1): {e}")
attempt("after setConfiguration(1)", cfg_cycle)

# 4. clearHalt on both bulk pipes after claiming iface 1
def clear_halts(h):
    try:
        h.claimInterface(IFACE)
        h.clearHalt(0x03)
        h.clearHalt(0x84)
        h.releaseInterface(IFACE)
        print("  cleared halt on 0x03 and 0x84")
    except Exception as e:
        print(f"  clearHalt: {e}")
attempt("after clearHalt(0x03,0x84)", clear_halts)

# 5. Try ESC+P (one of the v1 short commands we never tested)
def esc_p(h):
    try:
        h.claimInterface(IFACE); h.setInterfaceAltSetting(IFACE, 0)
        try_lock(h)
        h.bulkWrite(0x03, b"\x1bP\n\x80", timeout=5000)
        time.sleep(0.5)
        try:
            d = h.bulkRead(0x84, 4096, timeout=2000)
            print(f"  ESC+P reply: {bytes(d[:32]).hex() if d else '(empty)'}")
        except usb1.USBErrorTimeout:
            print("  ESC+P: timeout")
        try_unlock(h); h.releaseInterface(IFACE)
    except Exception as e:
        print(f"  ESC+P: {e}")
attempt("after ESC+P", esc_p)

# 6. ESC+K
def esc_k(h):
    try:
        h.claimInterface(IFACE); h.setInterfaceAltSetting(IFACE, 0)
        try_lock(h)
        h.bulkWrite(0x03, b"\x1bK\n\x80", timeout=5000)
        time.sleep(0.5)
        try:
            d = h.bulkRead(0x84, 4096, timeout=2000)
            print(f"  ESC+K reply: {bytes(d[:32]).hex() if d else '(empty)'}")
        except usb1.USBErrorTimeout:
            print("  ESC+K: timeout")
        try_unlock(h); h.releaseInterface(IFACE)
    except Exception as e:
        print(f"  ESC+K: {e}")
attempt("after ESC+K", esc_k)
