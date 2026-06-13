#!/usr/bin/env python3
"""Probe which USB endpoint sends button-press events on the DCP-T230.

Run as root (or with appropriate udev permissions), then press the
scan button combo on the printer. The endpoint and raw bytes are
printed so the result can be wired into t230scan.py.
"""
import sys
import usb.core
import usb.util

VENDOR  = 0x04f9
PRODUCT = 0x0716

dev = usb.core.find(idVendor=VENDOR, idProduct=PRODUCT)
if not dev:
    sys.exit(f"device {VENDOR:04x}:{PRODUCT:04x} not found — is the printer on?")

for iface in (1, 2):
    try:
        dev.detach_kernel_driver(iface)
    except Exception:
        pass
    usb.util.claim_interface(dev, iface)

print("waiting for button press (Ctrl-C to quit)...")
while True:
    for ep, iface in [(0x84, 1), (0x88, 2)]:
        try:
            data = dev.read(ep, 64, timeout=500)
            if data:
                print(f"EP {ep:#x} (iface {iface}): {bytes(data).hex()}  {bytes(data)!r}")
        except usb.core.USBTimeoutError:
            pass
        except Exception as e:
            print(f"EP {ep:#x} (iface {iface}): {e}")
