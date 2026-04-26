# Brother DCP-T230 — native macOS scanner driver

A small open-source scanner driver for the Brother DCP-T230 that runs
natively on macOS (Intel **and** Apple Silicon) without any Linux
binaries, VMs, or Rosetta. Pairs with the printer driver one level up.

Brother's official scanner stack (`brscan5` + `brscan-skey`) is shipped
as x86_64 / i686 Linux ELFs, so it cannot be loaded by SANE on macOS.
This directory is the result of reverse-engineering Brother's
`libsane-brother5.so` and `brscan-skey` enough to drive the device's
USB scan channel directly. The Python implementation is the runtime
driver; the C tools document the protocol and were used during
reverse engineering.

## Files

| File | Role |
| --- | --- |
| `t230scan.py` | Python 3 USB scanner driver. CLI + library; yields JPEG bands. |
| `t230web.py` | Tiny `http.server` web UI on `127.0.0.1:8080` with progressive JPEG streaming. |
| `decode_models.py` | Decoder for brscan5's obfuscated `?#`-prefixed model files (`brscan5ext_*.ini`). Used during reverse engineering. |
| `recover_test.py` | Diagnostic script — tries `resetDevice`, `setConfiguration`, `clearHalt`, ESC+P / ESC+K against a wedged device. |
| `t230scan.c` | C reference implementation. Has both eSCL-over-IPP-USB and vendor-protocol paths plus a debug shell (`probe`, `caps`, `lock`, `ssp`, `xsc`, `vscan`, `proxy`, …). |
| `getdeviceid.c` | Issues IEEE-1284 GET_DEVICE_ID against every interface/alt to dump the device's command-set advertisement. |
| `dump_descriptors.c` | Dumps the full USB config + interface + endpoint descriptor tree. |
| `check_alt.c` | GET_INTERFACE probe — asks the device which alt setting it currently has active (used to debug SET_INTERFACE not reaching firmware). |
| `iface0_test.c` | Sends the brscan5 lock + CKD via iface 0 (printer interface) to verify the scan channel is *not* multiplexed there. |

The Python driver is what you actually run. The C files are kept as
reference for anyone wanting to re-derive or extend the protocol.

## Wire protocol (how the Python driver talks to the device)

```
USB device:  VID=0x04F9  PID=0x0716  (DCP-T230)
Interface 1, alt 0  — vendor-class scan channel
  Bulk OUT  0x03      — host → device commands and pixel-fetch requests
  Bulk IN   0x84      — device → host replies and image bands

Open a session   : controlRead(bmRequestType=0xC0, bRequest=1, wValue=0x0002, wLength=0xFF)
Close a session  : controlRead(bmRequestType=0xC0, bRequest=2, wValue=0x0002, wLength=0xFF)
                   (5-byte echo response: bytes 1..3 must be 0x10, mode, 0x02; bit 7 of byte 4 clear)

Brother v2 framing on the bulk pipes:
  ESC + <CMD> + LF + <body?> + 0x80
  CMD ∈ { CKD, SSP, XSC, Q, QDI, … }

End-to-end scan:
  1. lock          (open scan channel)
  2. CKD           (check device)
  3. SSP body=     OS=LNX\nPSRC=FB\nRESO=<dpi>,<dpi>\nCLR=<mode>\nAREA=0,0,<W>,<H>\n
  4. XSC           (start image transfer)
  5. read bulk-IN until trailer:
       65 536-byte bands, each prefixed with a 14-byte header
       starting `00 02 01 00 (kind) 00 …`
       (kind = 0x11 grayscale, 0x15 24-bit colour);
       trailer `00 21 01 00 00 20` follows the last band.
       ZLP keep-alives during long carriage moves — back off ~10 ms.
  6. unlock        (close scan channel)
```

This was reconstructed by disassembling `libsane-brother5.so` and
`brscan-skey` from `brscan5-1.5.1-0.amd64.deb`. The `ControlScannerDevice`
helper (lock/unlock), `CUsbDevAccsCore` (which selects iface 1 when
> 1 USB interfaces are present), and the SSP/CKD/XSC bodies were all
extracted directly from the binary. `decode_models.py` recovers the
clear text of the obfuscated `models/brscan5ext_*.ini` lines that
encode each model's capabilities (resolution caps, colour modes,
scan-area limits).

No part of Brother's binary is needed at runtime.

## Installation

The scanner driver is **not** wired into `../install.sh`; it runs
in user space. There is nothing to install system-wide — just the
two runtime dependencies:

```sh
brew install libusb
# Python 3.10+ is already on macOS 13+; otherwise: brew install python@3.12
```

The Python scripts use [PEP 723](https://peps.python.org/pep-0723/)
inline metadata and bootstrap the `libusb1` wheel automatically when
launched via [`uv`](https://github.com/astral-sh/uv):

```sh
brew install uv         # once
./t230scan.py page.jpg  # uv resolves deps on first run, then caches them
```

If you'd rather use a regular venv:

```sh
python3 -m venv .venv
.venv/bin/pip install 'libusb1>=3.0'
.venv/bin/python t230scan.py page.jpg
```

### USB permissions

On macOS no extra rules are needed — libusb opens the device through
IOKit. If the printer queue has the device claimed, pause the queue
(or stop `cupsd`) before scanning; the scan channel and the print
channel can't both be active at once.

## Usage

### Single-shot CLI

```sh
./t230scan.py OUTPUT.jpg [DPI] [MODE]
#   DPI  : 100 / 150 / 200 / 300 / 600 / 1200    (default 300)
#   MODE : GRAY256 | C24BIT                       (default C24BIT)
```

The output is a baseline JPEG. The driver concatenates the bands the
device emits and trims to the SOI/EOI markers, so the file is valid
even if the firmware appends a few stuffing bytes to the last band.

### Web UI

```sh
./t230web.py
# open http://127.0.0.1:8080/
```

A single HTML page with a *Scan* button. The browser renders the
baseline JPEG progressively while the carriage is still moving — no
WebSocket, no client-side decoder; bytes are streamed straight into
an `<img>`. A copy is saved to `~/Pictures/T230/scan-<timestamp>.jpg`
and offered as a Download link once the scan completes.

Override the port with `T230_PORT=9000 ./t230web.py`.

### As a library

```python
from t230scan import T230, ScanError

with T230() as scanner, open("page.jpg", "wb") as f:
    for chunk in scanner.scan(dpi=300, mode="C24BIT"):
        f.write(chunk)
```

The session is one-shot per `with` block: `__enter__` claims iface 1,
locks the scan channel, drains stale bytes; `__exit__` always
unlocks and releases.

## Troubleshooting

* **`DeviceNotFound: DCP-T230 (VID 04f9, PID 0716) not found on USB`.**
  The printer is off, asleep, or the cable is unplugged. Wake it
  by pressing any button.
* **`scan-channel ctl(mode=1): bad echo …`.**
  Another process owns the scan channel (a previous run that didn't
  unlock cleanly, or the print queue is mid-job). Wait a few seconds,
  or run `./recover_test.py` to try the standard recovery sequences
  (resetDevice → setConfiguration cycle → clearHalt on 0x03/0x84).
* **`SSP returned 0xb0`.**
  Stale bytes in the bulk-IN queue from an aborted previous scan.
  The driver drains on session open, but if it was killed mid-band
  the device may still need a power cycle. `recover_test.py` is the
  fastest way to see whether a software-only recovery is enough.
* **The web page loads but *Scan* hangs.**
  Check `/var/log/system.log` and the terminal where `t230web.py`
  is running. Most stalls are SSP failures; the page logs the error
  status from the driver.
* **The print queue and the scanner fight.**
  The device serialises the two — the scanner takes interface 1, the
  printer takes interface 0. They can't *both* hold the bus
  exclusively. Pause the print queue while scanning if you see USB
  errors mid-job.
* **Sanity-check the device's protocol claims.**
  ```sh
  cc -O2 -Wall getdeviceid.c -lusb-1.0 -o getdeviceid
  ./getdeviceid
  ```
  Expect a `MFG:Brother;CMD:…;MDL:DCP-T230;CLS:PRINTER;…` IEEE-1284 string.

## Caveats & limitations

* **One scan at a time.** The libusb claim is exclusive; `t230web.py`
  guards this with a process-level lock, so a second *Scan* click
  while one is in flight is rejected.
* **No SANE backend.** This driver speaks straight to libusb; it does
  not register itself as a SANE backend, so `Image Capture.app` and
  other macOS apps won't see the device. Use the web UI or call the
  driver from your own script. (A SANE shim would be a tractable next
  step — the surface area is small.)
* **No ADF / no flatbed-area selection.** `t230web.py` always scans
  the full A4 area; `t230scan.py` accepts only DPI and mode on the
  CLI. The underlying `T230.scan()` method takes an `area` argument
  if you need to crop in code.
* **Modes are limited to what the firmware accepts.** `GRAY256` and
  `C24BIT` are the only colour modes that don't return `SSP=0xb0`.
  `TEXT` and `ERRDIF` are advertised by some Brother models but are
  rejected by the T230.
* **The C reference tools assume Homebrew libusb.** Build with:
  ```sh
  cc -O2 -Wall -Wextra t230scan.c \
     -I/opt/homebrew/include/libusb-1.0 \
     -L/opt/homebrew/lib -lusb-1.0 -lpthread -o t230scan
  ```
  On Intel Macs replace `/opt/homebrew` with `/usr/local`.

## License

Derived from analysis of Brother's GPLv2-licensed `brscan5` Linux
package. Any materials here that qualify as derivative work are
offered under the **GPL v2 or later**. Original reverse-engineering
notes and this Python/C code are also offered under the same terms.

Brother and Brother DCP-T230 are trademarks of Brother Industries, Ltd.
This project is not affiliated with or endorsed by Brother Industries.
