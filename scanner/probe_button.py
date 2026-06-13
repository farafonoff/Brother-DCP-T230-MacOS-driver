#!/usr/bin/env python3
"""Probe scan-button state on the DCP-T230 via USB control transfer.

Reverse-engineered from brscan-skey-exe (usb_scanner_check_status):
  bmRequestType=0xC0, bRequest=0x03, wValue=0, wIndex=0, wLength=255
Response is ASCII text; decode_key_data in the binary parses tokens
like IMAGE / EMAIL / FILE to identify which button was pressed.

Run as root (or with appropriate udev permissions) and press the
scan button combo on the printer.
"""
import sys
import time
import usb.core
import usb.util

VENDOR  = 0x04f9
PRODUCT = 0x0716

dev = usb.core.find(idVendor=VENDOR, idProduct=PRODUCT)
if not dev:
    sys.exit(f"device {VENDOR:04x}:{PRODUCT:04x} not found — is the printer on?")

# Control transfer needs no interface claim.
print("polling for button press every 200 ms (Ctrl-C to quit)...")
last = None
while True:
    try:
        data = bytes(dev.ctrl_transfer(0xC0, 0x03, 0, 0, 255, timeout=2000))
        if data and data != last:
            last = data
            print(f"response: {data.hex()}  {data!r}")
    except usb.core.USBTimeoutError:
        pass
    except Exception as e:
        print(f"error: {e}")
    time.sleep(0.2)
