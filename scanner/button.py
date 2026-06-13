"""
button.py — Brother DCP-T230 scan-button listener.

Polls the device every 200 ms via a vendor control transfer and calls
`on_press` on the rising edge of the button signal.

Protocol (reverse-engineered from brscan-skey-exe):
  bmRequestType = 0xC0  (vendor IN, device-to-host)
  bRequest      = 0x03
  wValue/wIndex = 0
  wLength       = 255

Response byte layout:
  [0] packet length
  [1] 0x10  (constant)
  [2] 0x03  (echoes bRequest)
  [3] event code — 0x00 = idle, 0x20 = button pressed
  [4] function   — 0x01 = idle, 0x03 = scan

The device is kept open across polls and reopened on any USB error.

Requirements:
  PyUSB (python3-usb) — optional; if missing, the listener is a no-op.

No set_configuration() calls are made (the scanner's configuration is
already active and the call conflicts with the scan driver's claim).
"""

from __future__ import annotations

import sys
import time
import threading

VID = 0x04F9
PID = 0x0716
POLL_INTERVAL = 0.2       # seconds between polls
RECONNECT_DELAY = 2.0     # seconds to wait after a USB error
CTRL_TIMEOUT_MS = 2000
BUTTON_PRESSED = 0x20
BUTTON_IDLE = 0x00


class ButtonListener:
    """Listens for DCP-T230 scan-button presses in a background thread.

    Parameters
    ----------
    on_press:
        Callable called (in the listener thread) on each button press
        transition (idle → pressed). Not called while the button is
        held — only on the leading edge.

    Example
    -------
    >>> listener = ButtonListener(on_press=lambda: print("pressed!"))
    >>> listener.start()
    """

    def __init__(self, on_press) -> None:
        self._on_press = on_press
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the background polling thread (daemon, named "t230-button")."""
        t = threading.Thread(target=self._run, name="t230-button", daemon=True)
        t.start()
        self._thread = t

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run(self) -> None:
        try:
            import usb.core
            import usb.util
        except ImportError:
            sys.stderr.write(
                "[button] python3-usb (PyUSB) not installed — "
                "hardware scan button disabled\n"
            )
            return

        sys.stderr.write("[button] listener started\n")

        dev = None
        last_event: int | None = None   # tracks previous state for edge detection

        while True:
            try:
                # ---- ensure device handle ----
                if dev is None:
                    dev = usb.core.find(idVendor=VID, idProduct=PID)
                    if dev is None:
                        # Device not connected yet — wait and retry.
                        time.sleep(RECONNECT_DELAY)
                        continue

                # ---- poll ----
                raw = bytes(
                    dev.ctrl_transfer(0xC0, 0x03, 0, 0, 255, timeout=CTRL_TIMEOUT_MS)
                )

                if len(raw) < 4:
                    time.sleep(POLL_INTERVAL)
                    continue

                event = raw[3]

                # Rising-edge detection: call on_press only when transitioning
                # from non-pressed to pressed (not while held).
                if event == BUTTON_PRESSED and last_event != BUTTON_PRESSED:
                    try:
                        self._on_press()
                    except Exception as e:
                        sys.stderr.write(f"[button] on_press raised: {e}\n")

                last_event = event

            except Exception as e:
                sys.stderr.write(f"[button] USB error: {e} — reconnecting in {RECONNECT_DELAY}s\n")
                # Release the device handle and start fresh.
                if dev is not None:
                    try:
                        import usb.util as _util
                        _util.dispose_resources(dev)
                    except Exception:
                        pass
                    dev = None
                last_event = None
                time.sleep(RECONNECT_DELAY)
                continue

            time.sleep(POLL_INTERVAL)
