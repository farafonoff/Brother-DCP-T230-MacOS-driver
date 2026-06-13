#!/usr/bin/env python3
"""Probe scan-button state on the DCP-T230 via USB control transfer.

Reverse-engineered from brscan-skey-exe (usb_scanner_check_status):
  bmRequestType=0xC0, bRequest=0x03, wValue=0, wIndex=0, wLength=255

Response structure (binary, not ASCII like older Brother models):
  byte 0: packet length
  byte 1: 0x10 (constant)
  byte 2: 0x03 (echoes bRequest)
  byte 3: event code  — 0x00=idle, 0x20=button pressed
  byte 4: function    — 0x01=idle, 0x03=scan (when pressed)
  bytes 5+: extra data when event active

brscan-skey opens and closes the USB device on every poll — we do
the same to avoid the device going silent after a few queries.
"""
import sys
import time
import usb.core
import usb.util

VENDOR  = 0x04f9
PRODUCT = 0x0716
POLL_S  = 0.2

EVENT_NAMES = {0x00: "idle", 0x20: "button pressed"}
FUNC_NAMES  = {0x01: "ready", 0x03: "scan"}


def find_device():
    dev = usb.core.find(idVendor=VENDOR, idProduct=PRODUCT)
    if dev is None:
        raise OSError(f"device {VENDOR:04x}:{PRODUCT:04x} not found")
    dev.set_configuration()
    return dev


def check_button(dev) -> dict | None:
    """Send status control transfer, return parsed result or None on error."""
    try:
        raw = bytes(dev.ctrl_transfer(0xC0, 0x03, 0, 0, 255, timeout=2000))
    except Exception as e:
        raise OSError(f"control transfer failed: {e}")
    if len(raw) < 2:
        return None
    event = raw[3] if len(raw) > 3 else 0
    func  = raw[4] if len(raw) > 4 else 0
    return {
        "raw":        raw.hex(),
        "event":      event,
        "func":       func,
        "label":      EVENT_NAMES.get(event, f"unknown(0x{event:02x})"),
        "func_label": FUNC_NAMES.get(func, f"fn(0x{func:02x})"),
    }


def main():
    print(f"polling for button press every {POLL_S*1000:.0f} ms  (Ctrl-C to quit)...")
    last_event = None
    dev = None

    while True:
        try:
            # Open fresh each poll — mirrors brscan-skey behaviour,
            # prevents device going silent after a few queries.
            dev = find_device()
            result = check_button(dev)

            if result and result["event"] != last_event:
                last_event = result["event"]
                if result["event"] == 0:
                    print(f"  -> idle  (raw: {result['raw']})")
                else:
                    print(f"  -> {result['label']}  fn={result['func_label']}"
                          f"  (raw: {result['raw']})")

        except OSError as e:
            if last_event != "error":
                print(f"  WARNING: {e} - retrying...")
                last_event = "error"
        except usb.core.USBError as e:
            if last_event != "error":
                print(f"  WARNING: USB error: {e} - retrying...")
                last_event = "error"
        finally:
            if dev is not None:
                try:
                    usb.util.dispose_resources(dev)
                except Exception:
                    pass
                dev = None

        time.sleep(POLL_S)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nbye.")
