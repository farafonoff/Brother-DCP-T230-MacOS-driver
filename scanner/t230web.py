#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["libusb1>=3.0"]
# ///
"""
Tiny web UI for the Brother DCP-T230 native Python driver.

Serves a single HTML page on http://127.0.0.1:8080/. The Scan button
kicks off a real scan that streams progressively into the page's <img>
element (browser renders the baseline JPEG as bytes arrive — no
WebSocket, no client-side decoder), tees a copy to disk, and offers a
Download link once complete.

Run:
    uv run t230web.py
or:
    chmod +x t230web.py && ./t230web.py
"""

from __future__ import annotations

import os
import sys
import time
import uuid
import threading
import urllib.parse
import datetime
import pathlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Driver lives next to this file.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from t230scan import T230, ScanError, DeviceNotFound, Cancelled, ProtocolError

PORT = int(os.environ.get("T230_PORT", "8080"))
TMP_DIR = pathlib.Path("/tmp/t230")
TMP_DIR.mkdir(exist_ok=True)
PICTURES_DIR = pathlib.Path.home() / "Pictures" / "T230"
PICTURES_DIR.mkdir(parents=True, exist_ok=True)
TMP_TTL_SECONDS = 3600

# Only one scan at a time (USB claim is exclusive).
scan_lock = threading.Lock()
# scan_id -> threading.Event for in-flight cancellation.
cancel_events: dict[str, threading.Event] = {}
cancel_events_lock = threading.Lock()


# ---------- HTML page ----------

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>DCP-T230 Scan</title>
<style>
  /* Photoshop-ish dark palette */
  :root {
    --bg:        #232323;
    --panel:     #2d2d2d;
    --panel-2:   #383838;
    --border:    #1a1a1a;
    --border-2:  #4a4a4a;
    --text:      #d4d4d4;
    --text-dim:  #888;
    --accent:    #5294e2;
    --accent-2:  #6ba9ee;
    --danger:    #c46060;
    --success:   #5fa763;
    --shadow:    0 1px 0 #4a4a4a inset, 0 -1px 0 #1a1a1a inset;
  }
  *  { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    font: 13px -apple-system, "SF Pro Text", system-ui, sans-serif;
    background: var(--bg); color: var(--text);
    display: grid; grid-template-rows: auto 1fr auto;
    min-height: 100vh;
  }

  /* Title bar */
  header {
    background: linear-gradient(#3a3a3a, #2a2a2a);
    border-bottom: 1px solid var(--border);
    padding: 8px 12px; font-weight: 600; font-size: 12px;
    color: #bbb; letter-spacing: 0.4px;
    display: flex; align-items: center; gap: 8px;
  }
  header .pulse {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--text-dim); transition: background 0.2s;
  }
  body.scanning header .pulse { background: var(--accent); animation: pulse 1s ease-in-out infinite; }
  body.error    header .pulse { background: var(--danger); }
  body.done     header .pulse { background: var(--success); }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }

  /* Main two-column layout */
  main {
    display: grid; grid-template-columns: 280px 1fr; min-height: 0;
  }
  aside {
    background: var(--panel);
    border-right: 1px solid var(--border);
    padding: 12px; overflow-y: auto;
  }
  section.preview {
    background: #1a1a1a;
    /* checkerboard for empty preview */
    background-image:
      linear-gradient(45deg, #232323 25%, transparent 25%),
      linear-gradient(-45deg, #232323 25%, transparent 25%),
      linear-gradient(45deg, transparent 75%, #232323 75%),
      linear-gradient(-45deg, transparent 75%, #232323 75%);
    background-size: 24px 24px;
    background-position: 0 0, 0 12px, 12px -12px, -12px 0px;
    overflow: hidden; min-height: 0;
    display: flex; align-items: center; justify-content: center;
    padding: 16px;
  }
  #preview {
    /* Always fit within the preview pane — both dimensions, preserving
     * aspect ratio. min-height: 0 on the flex container is what lets
     * max-height: 100% actually take effect. */
    max-width: 100%;
    max-height: 100%;
    object-fit: contain;
    box-shadow: 0 4px 16px rgba(0,0,0,0.6);
    background: #fff;
    /* tiny perceived border, helps separate light pages from background */
    outline: 1px solid #555;
  }
  #preview:not([src]) { display: none; }

  .empty-hint {
    color: var(--text-dim); font-size: 12px;
    align-self: center; text-align: center;
  }
  body.scanning .empty-hint, #preview[src] ~ .empty-hint { display: none; }

  /* Status bar */
  footer {
    background: linear-gradient(#2a2a2a, #1f1f1f);
    border-top: 1px solid var(--border);
    padding: 6px 12px; font-size: 11px; color: var(--text-dim);
    display: flex; justify-content: space-between; gap: 16px;
    min-height: 26px; align-items: center;
  }
  footer .right { color: var(--text-dim); }

  /* Panel groups */
  .group {
    background: var(--panel-2);
    border: 1px solid var(--border);
    border-radius: 3px;
    padding: 10px 12px 12px;
    margin-bottom: 10px;
    box-shadow: 0 1px 0 #404040 inset;
  }
  .group h2 {
    font-size: 11px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.5px; color: var(--text-dim);
    margin: 0 0 8px; padding-bottom: 6px;
    border-bottom: 1px solid var(--border);
  }

  .row { margin: 8px 0; display: flex; align-items: center; gap: 10px; }
  .row label { width: 80px; font-size: 12px; color: #bbb; }

  select, button {
    font: inherit; color: var(--text);
    background: linear-gradient(#3d3d3d, #303030);
    border: 1px solid var(--border);
    border-radius: 3px; padding: 4px 8px;
    box-shadow: var(--shadow);
  }
  select { flex: 1; min-width: 0; }
  button {
    cursor: pointer; padding: 6px 14px; font-size: 12px;
    transition: background 0.1s;
  }
  button:hover:not(:disabled) {
    background: linear-gradient(#454545, #383838);
  }
  button:active:not(:disabled) {
    background: linear-gradient(#2a2a2a, #353535);
    box-shadow: 0 1px 2px #000 inset;
  }
  button:disabled { opacity: 0.4; cursor: default; }
  button.primary {
    background: linear-gradient(#5294e2, #4078c8);
    border-color: #2c5fa0; color: white;
  }
  button.primary:hover:not(:disabled) {
    background: linear-gradient(#6ba9ee, #5294e2);
  }
  button.danger {
    background: linear-gradient(#3d3d3d, #303030);
    color: #d8a0a0;
  }

  .actions { display: flex; gap: 6px; margin-top: 8px; }
  .actions button { flex: 1; }

  a.dl {
    display: inline-block; text-decoration: none;
    color: #b0e0b3; padding: 6px 14px; font-size: 12px;
    background: linear-gradient(#384a38, #2c3a2c);
    border: 1px solid var(--border); border-radius: 3px;
    box-shadow: var(--shadow);
  }
  a.dl:hover { background: linear-gradient(#445544, #384a38); }

  .meta {
    font-size: 11px; color: var(--text-dim);
    margin-top: 6px; line-height: 1.5;
  }
  .meta code {
    background: #1a1a1a; padding: 1px 4px; border-radius: 2px;
    color: #b0c0d0; font-size: 10.5px;
  }
</style>
</head>
<body>
<header>
  <span class="pulse"></span>
  <span>Brother DCP-T230 Scanner</span>
</header>

<main>
  <aside>
    <div class="group">
      <h2>Resolution</h2>
      <select id="dpi">
        <option value="100">100 DPI (draft)</option>
        <option value="200">200 DPI</option>
        <option value="300" selected>300 DPI</option>
        <option value="400">400 DPI</option>
        <option value="600">600 DPI</option>
        <option value="1200">1200 DPI (slow)</option>
      </select>
    </div>

    <div class="group">
      <h2>Mode</h2>
      <select id="mode">
        <option value="C24BIT" selected>Color (24-bit RGB)</option>
        <option value="GRAY256">Grayscale (8-bit)</option>
      </select>
    </div>

    <div class="group">
      <h2>Action</h2>
      <div class="actions">
        <button id="scanBtn" class="primary">Scan</button>
        <button id="cancelBtn" class="danger" disabled>Cancel</button>
      </div>
      <div class="meta" id="dlBox" style="display:none">
        <a id="dl" class="dl" download>Download</a>
      </div>
      <div class="meta">
        Auto-saves to <code>~/Pictures/T230/</code>
      </div>
    </div>
  </aside>

  <section class="preview">
    <img id="preview" alt="scanned image">
    <div class="empty-hint">No image. Press <strong>Scan</strong> to start.</div>
  </section>
</main>

<footer>
  <span id="status">Ready</span>
  <span class="right" id="meta"></span>
</footer>

<script>
const scanBtn   = document.getElementById('scanBtn');
const cancelBtn = document.getElementById('cancelBtn');
const dl        = document.getElementById('dl');
const dlBox     = document.getElementById('dlBox');
const img       = document.getElementById('preview');
const status    = document.getElementById('status');
const meta      = document.getElementById('meta');

let currentScanId = null;
let timer = null;
let bytesAtLast = 0;

function setBodyState(s) {
  document.body.classList.remove('scanning', 'error', 'done');
  if (s) document.body.classList.add(s);
}
function setStatus(msg) { status.textContent = msg; }
function setMeta(msg)   { meta.textContent   = msg; }

function startTimer(label) {
  const t0 = performance.now();
  timer = setInterval(() => {
    const s = ((performance.now() - t0) / 1000).toFixed(1);
    setStatus(`${label} ${s} s`);
  }, 100);
}
function stopTimer() { if (timer) { clearInterval(timer); timer = null; } }

scanBtn.addEventListener('click', () => {
  const dpi = document.getElementById('dpi').value;
  const mode = document.getElementById('mode').value;
  const modeLabel = mode === 'C24BIT' ? 'Color' : 'Grayscale';
  currentScanId = crypto.randomUUID();
  scanBtn.disabled = true;
  cancelBtn.disabled = false;
  dlBox.style.display = 'none';
  img.removeAttribute('src');
  setBodyState('scanning');
  setMeta(`${dpi} DPI · ${modeLabel}`);
  startTimer('Scanning…');
  img.src = `/scan/start?dpi=${dpi}&mode=${mode}&id=${currentScanId}`;
});

img.addEventListener('load', () => {
  stopTimer();
  cancelBtn.disabled = true;
  scanBtn.disabled = false;
  setBodyState('done');
  if (currentScanId) {
    dl.href = `/scan/file/${currentScanId}`;
    dlBox.style.display = '';
    setStatus(`Done · ${img.naturalWidth}×${img.naturalHeight} px · saved to ~/Pictures/T230/`);
  }
});

img.addEventListener('error', () => {
  stopTimer();
  cancelBtn.disabled = true;
  scanBtn.disabled = false;
  setBodyState('error');
  if (img.getAttribute('src')) setStatus('Scan failed');
  else setStatus('Cancelled');
});

cancelBtn.addEventListener('click', async () => {
  if (!currentScanId) return;
  cancelBtn.disabled = true;
  setStatus('Cancelling…');
  try { await fetch(`/scan/cancel/${currentScanId}`, {method: 'POST'}); } catch (e) {}
  img.removeAttribute('src');
  stopTimer();
  scanBtn.disabled = false;
  setBodyState('error');
  setStatus('Cancelled — device cleaning up (next scan may take a few s longer)');
});
</script>
</body>
</html>
"""


# ---------- helpers ----------

def cleanup_tmp() -> None:
    """Drop /tmp/t230/*.jpg older than TMP_TTL_SECONDS. Called on each
    new scan; no scheduler needed."""
    now = time.time()
    for p in TMP_DIR.iterdir():
        try:
            if now - p.stat().st_mtime > TMP_TTL_SECONDS:
                p.unlink()
        except OSError:
            pass


def pictures_path(dpi: int, mode: str) -> pathlib.Path:
    ts = datetime.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    return PICTURES_DIR / f"scan-{ts}-{dpi}dpi-{mode.lower()}.jpg"


def parse_int_list(q: dict[str, list[str]], key: str, allowed: set[int],
                   default: int) -> int | None:
    raw = q.get(key, [str(default)])[0]
    try:
        v = int(raw)
    except ValueError:
        return None
    return v if v in allowed else None


# ---------- HTTP handler ----------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # quieten the default access log a bit
    def log_message(self, fmt, *args):
        sys.stderr.write(
            f"{self.log_date_time_string()} {self.address_string()} "
            f"{fmt % args}\n")

    # -- send helpers --

    def _send_simple(self, code: int, body: bytes, ctype: str,
                     extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # -- routing --

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/" or u.path == "":
            self._send_simple(200, PAGE.encode("utf-8"),
                              "text/html; charset=utf-8")
            return
        if u.path == "/scan/start":
            self._serve_scan(u)
            return
        if u.path.startswith("/scan/file/"):
            self._serve_saved(u.path[len("/scan/file/"):])
            return
        self._send_simple(404, b"not found", "text/plain")

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path.startswith("/scan/cancel/"):
            scan_id = u.path[len("/scan/cancel/"):]
            with cancel_events_lock:
                ev = cancel_events.get(scan_id)
            if ev is not None:
                ev.set()
                self._send_simple(200, b"cancelled", "text/plain")
            else:
                self._send_simple(404, b"no such scan", "text/plain")
            return
        self._send_simple(404, b"not found", "text/plain")

    # -- /scan/start (chunked streaming + tee to disk) --

    def _serve_scan(self, u):
        q = urllib.parse.parse_qs(u.query)
        dpi = parse_int_list(q, "dpi",
                             {100, 200, 300, 400, 600, 1200}, 300)
        if dpi is None:
            self._send_simple(400, b"bad dpi (use 100/200/300/400/600/1200)",
                              "text/plain"); return
        mode = q.get("mode", ["C24BIT"])[0]
        if mode not in ("C24BIT", "GRAY256"):
            self._send_simple(400, b"bad mode (use C24BIT or GRAY256)",
                              "text/plain"); return
        scan_id = q.get("id", [str(uuid.uuid4())])[0]
        # Defensive: only accept hex/uuid-shaped ids so we can use them as
        # filenames safely.
        if not all(c.isalnum() or c == "-" for c in scan_id) or len(scan_id) > 64:
            self._send_simple(400, b"bad id", "text/plain"); return

        if not scan_lock.acquire(blocking=False):
            self._send_simple(409, b"another scan is already in progress",
                              "text/plain"); return

        cancel = threading.Event()
        with cancel_events_lock:
            cancel_events[scan_id] = cancel

        cleanup_tmp()
        tmp_path = TMP_DIR / f"{scan_id}.jpg"
        pic_path = pictures_path(dpi, mode)

        # Headers — chunked transfer encoding so we can flush each band.
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        client_alive = True
        try:
            with T230() as scanner, \
                 open(tmp_path, "wb") as tmp_f, \
                 open(pic_path, "wb") as pic_f:
                for band in scanner.scan(dpi, mode, cancel=cancel):
                    tmp_f.write(band)
                    pic_f.write(band)
                    if client_alive:
                        try:
                            self.wfile.write(b"%X\r\n" % len(band))
                            self.wfile.write(band)
                            self.wfile.write(b"\r\n")
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            client_alive = False
                            cancel.set()
                if client_alive:
                    try:
                        self.wfile.write(b"0\r\n\r\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        client_alive = False
            sys.stderr.write(f"[scan] id={scan_id} dpi={dpi} mode={mode} "
                             f"tmp={tmp_path} pic={pic_path}\n")
        except Cancelled:
            sys.stderr.write(f"[scan] id={scan_id} cancelled\n")
        except DeviceNotFound as e:
            sys.stderr.write(f"[scan] device not found: {e}\n")
        except (ScanError, ProtocolError, Exception) as e:
            sys.stderr.write(f"[scan] {type(e).__name__}: {e}\n")
        finally:
            with cancel_events_lock:
                cancel_events.pop(scan_id, None)
            scan_lock.release()

    # -- /scan/file/<id> --

    def _serve_saved(self, scan_id: str):
        if not all(c.isalnum() or c == "-" for c in scan_id) or len(scan_id) > 64:
            self._send_simple(400, b"bad id", "text/plain"); return
        path = TMP_DIR / f"{scan_id}.jpg"
        if not path.is_file():
            self._send_simple(404, b"not found (or expired)", "text/plain")
            return
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError as e:
            self._send_simple(500, f"read error: {e}".encode(), "text/plain")
            return
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self._send_simple(
            200, body, "image/jpeg",
            {"Content-Disposition": f'attachment; filename="scan-{ts}.jpg"'})


# ---------- main ----------

def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"t230web listening on http://127.0.0.1:{PORT}/  (Ctrl-C to quit)",
          file=sys.stderr)
    print(f"  /tmp/t230 buffer dir: {TMP_DIR}", file=sys.stderr)
    print(f"  auto-save dir:        {PICTURES_DIR}", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
