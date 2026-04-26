# Brother DCP-T230 — native macOS driver

A tiny open-source CUPS driver for the Brother DCP-T230 inkjet that runs
natively on macOS (Intel **and** Apple Silicon) without any Linux
binaries, VMs, or Rosetta. Scanning is covered too — see
[`scanner/`](scanner/README.md).

Brother's official driver ships only as x86_64 / i686 Linux ELF
executables, which will not run on macOS. Fortunately the printer
itself speaks a standard protocol — **PJL wrapping a PWG Raster
stream** — so macOS's built-in CUPS filter chain already produces 95 %
of what the printer needs. The only thing missing was the thin PJL
envelope that tells the printer "here comes PWG raster data".
That envelope is what this driver provides.

## Files

| File | Role |
| --- | --- |
| `brother_dcpt230_pjl` | Python 3 CUPS filter. Wraps PWG Raster in PJL. |
| `brother-dcpt230.ppd` | PPD with media sizes, quality, color, media type. |
| `install.sh` | Copies filter + PPD into place, optionally calls `lpadmin`. |
| `uninstall.sh` | Removes the installed files (leaves your queues alone). |
| [`scanner/`](scanner/README.md) | Native USB scanner driver (Python + C reference). |

## Scanner

A separate, self-contained native scanner driver lives in
[`scanner/`](scanner/README.md): a Python 3 USB driver
(`t230scan.py`), a tiny progressive-JPEG web UI (`t230web.py`), and
the C reference implementation that documents the reverse-engineered
protocol. It runs in user space and is not touched by `install.sh`
in this directory. See [`scanner/README.md`](scanner/README.md) for
usage and the wire-protocol summary.

## Filter chain

CUPS on macOS does the hard work; our filter just prepends/appends PJL:

```
PDF ──► pdftopdf ──► cgpdftoraster ──► rastertopwg ──► brother_dcpt230_pjl ──► USB
                     (Apple)           (CUPS)         (this driver)
```

Output of `brother_dcpt230_pjl`:

```
ESC%-12345X                             ← UEL start
@PJL JOB NAME="..."
@PJL SET STRINGCODESET=UTF8
@PJL SET USERNAME="..."
@PJL SET JOBNAME="..."
@PJL SET LOGINUSER="..."
@PJL JOBSETTINGLOG DRIVER="macos-native-1.0"
@PJL SET PAPER=<LETTER|A4|…>
@PJL SET BORDERLESS=<ON|OFF>
@PJL SET JT{TOP|LEFT|RIGHT|BOT}MARGIN=<0|300>  ← 1/1200" units
@PJL SET RENDERMODE=<COLOR|GRAYSCALE>
@PJL SET PRINTQUALITY=<DRAFT|NORMAL|HIGH>
@PJL SET DUPLEX=OFF
@PJL SET MEDIATYPE=<REGULAR|GLOSSY|INKJET|…>
@PJL SET SOURCETRAY=AUTO
@PJL SET MANUALFEED=OFF
@PJL SET FIDELITY=TRUE
@PJL ENTER LANGUAGE=PWGRASTER
<PWG Raster stream: 4-byte "RaS2" + 1796-byte page header + pixels…>
@PJL EOJ NAME="..."
ESC%-12345X                             ← UEL end
```

This was reconstructed by reverse engineering the closed-source
`brdcpt230filter` Linux binary shipped by Brother:
* The literal `@PJL …` strings, the `RaS2` sync word, and the
  `@PJL ENTER LANGUAGE=PWGRASTER` terminator all live verbatim in the
  binary's `.rodata`.
* The PWG Raster page-header builder at address `0x4050c4` was
  disassembled and every field write was mapped to a field in the
  standard PWG Raster v2 header (`HWResolution`, `cupsWidth`,
  `cupsColorSpace=19 (sRGB) / 18 (sGray)`, `cupsNumColors`, …).
  The buffer size passed to the writer was exactly `0x704 = 1796`
  bytes — the canonical PWG v2 page header size.

No part of Brother's binary is needed at runtime.

## Installation

```sh
cd macos-driver
sudo ./install.sh
```

The installer will:

1. Copy `brother_dcpt230_pjl` into `/Library/Printers/Brother/DCP-T230/`
   and symlink it into the CUPS filter directory (`/usr/libexec/cups/filter/`
   if writable, otherwise `/usr/local/libexec/cups/filter/`).
2. Install `Brother-DCP-T230.ppd.gz` under
   `/Library/Printers/PPDs/Contents/Resources/`.
3. Try to auto-detect the printer over USB and offer to create a CUPS
   queue named `DCP_T230` for you.

If the auto-detect step skips your printer, add it manually from
**System Settings → Printers & Scanners → Add Printer** and pick
*“Brother DCP-T230 (PWG/PJL native)”* from the driver list.

### Manual queue creation (no GUI)

```sh
# Find the device URI
lpinfo -v | grep -i dcp-?t230

# Create the queue
sudo lpadmin -p DCP_T230 -E \
    -v "usb://Brother/DCP-T230?serial=XXXXXXXX" \
    -P /Library/Printers/PPDs/Contents/Resources/Brother-DCP-T230.ppd.gz
sudo cupsenable DCP_T230
sudo cupsaccept DCP_T230
```

## Testing

```sh
# Simple test print
lp -d DCP_T230 /System/Library/Fonts/Supplemental/Times\ New\ Roman.ttf

# Or a PDF with options:
lp -d DCP_T230 -o media=A4 -o BRResolution=Fine -o BRMediaType=Inkjet mydoc.pdf
```

### Inspecting what's really going to the printer

If you want to verify the PJL envelope without burning paper, redirect
the queue to a file:

```sh
sudo lpadmin -p DCP_T230 -v file:///tmp/out.prn \
    -P /Library/Printers/PPDs/Contents/Resources/Brother-DCP-T230.ppd.gz
sudo chmod 0666 /tmp/out.prn   # or adjust /etc/cups/cups-files.conf: FileDevice yes
lp -d DCP_T230 mydoc.pdf
head -c 4096 /tmp/out.prn | xxd | head -80
```

You should see `ESC%-12345X@PJL JOB NAME=…@PJL ENTER LANGUAGE=PWGRASTER\nRaS2…`.

### Enable `file://` URIs

By default CUPS refuses `file://` device URIs. To enable for testing,
add to `/etc/cups/cups-files.conf`:

```
FileDevice yes
```

then `sudo launchctl kickstart -k system/org.cups.cupsd`.

## Supported options

| CUPS option                | Values                                              |
| -------------------------- | --------------------------------------------------- |
| `PageSize` / `media`       | `Letter`, `A4`, `Legal`, `A5`, `A6`, `B5`, `BrPostC4x6_S`, `BrPhoto2L_S`, `Env10`, `EnvDL`, `EnvC5`, `EnvMonarch`, `FanFoldGermanLegal`, `MexicanLegal`, `IndianLegal`, plus their `*_B` borderless variants (where applicable). See the PPD for the full list. |
| `BRMediaType`              | `Plain`, `Inkjet`, `Glossy`, `Recycled`, `Env`, `EnvThin`, `EnvThick` |
| `BRResolution`             | `Draft`, `Normal`, `Fine`                           |
| `BRMonoColor`              | `FullColor`, `Mono`                                 |
| `BRInputSlot`              | `AutoSelect`, `Manual`                              |

## Troubleshooting

* **Job sits in queue forever / paused with “filter failed”.**
  Check `/var/log/cups/error_log` for lines tagged `brother_dcpt230_pjl`.
  The filter logs options, PWG page geometry, and byte counts at
  `DEBUG`/`INFO` level.
* **Page comes out blank or garbled.**
  Redirect to `file://` (see above), then run
  `xxd /tmp/out.prn | head -40` and verify the first line starts with
  `1b25 2d31 3233 3435 5840 504a 4c` (`ESC%-12345X@PJL`).
* **macOS doesn't show the driver in the picker.**
  `sudo launchctl kickstart -k system/org.cups.cupsd`, then reopen the
  Printers & Scanners sheet.
* **SIP blocks `/usr/libexec/cups/filter/`.**
  The installer falls back to `/usr/local/libexec/cups/filter/` which
  CUPS also searches. Create it first with
  `sudo mkdir -p /usr/local/libexec/cups/filter` if needed.

## Uninstall

```sh
sudo ./uninstall.sh
# then remove the queue
sudo lpadmin -x DCP_T230
```

## Caveats & limitations

* **Compression.** `rastertopwg` emits PWG Raster with PackBits
  compression by default. The reverse-engineered PWG header writer
  in Brother's binary doesn't explicitly set `cupsCompression`, and
  Brother's own printers generally accept both compressed and
  uncompressed PWG Raster, so this should "just work". If you see
  garbled pages, try forcing uncompressed output by adding
  `*cupsRasterVersion: 3` and `*cupsBitsPerColor: 8` hints to the PPD,
  or feed uncompressed raster via a `foomatic-rip`-style wrapper.
* **Resolution.** Brother's binary hard-codes 300 DPI in the PWG
  header (`HWResolution = 300, 300`). This driver relies on
  `rastertopwg` to populate the real resolution from the PPD's
  `DefaultResolution: 300dpi`. Setting *Fine* in the PPD raises the
  PJL quality but not the raster DPI; this matches Brother's own
  driver behavior.
* **No duplex, no Hagaki, no color-matching knobs.** Exposed set is
  deliberately minimal. Extending it is straightforward — add the
  keyword to the PPD and a mapping row in one of the dict tables at
  the top of `brother_dcpt230_pjl`.
* **Untested on every page size.** Letter, A4, and Photo 4x6 are the
  most exercised. Exotic sizes may need their PJL `PAPER=` name
  tweaked; all the ones I could extract from the binary's string
  table are mapped, but a few are guesses.

## License

The Brother-supplied Linux driver this work was derived from is
released under the GNU GPL v2. Any materials here that qualify as
derivative work are offered under the **GPL v2 or later**.
Original reverse-engineering notes and this Python/PPD code are
also offered under the same terms.

Brother, Brother DCP-T230 are trademarks of Brother Industries, Ltd.
This project is not affiliated with or endorsed by Brother Industries.
