#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["libusb1>=3.0", "Pillow>=9.0"]
# ///
"""
Tiny web UI for the Brother DCP-T230 native Python driver.

Serves a single HTML page on http://0.0.0.0:8080/ (default; set T230_HOST
and T230_PORT to override).  Features:
  - Scan button: streams a live progressive JPEG into the centre preview.
  - Cancel button: aborts an in-progress scan.
  - Download link for the current scan session.
  - Auto-save every scan to ~/Pictures/T230/.
  - Scan History panel (right column): thumbnails of past scans, click to
    preview, metadata, and download link.
  - Hardware scan-button support: pressing the physical button on the printer
    triggers a 300-DPI colour scan, which appears in the history panel.

Run:
    uv run t230web.py
or:
    chmod +x t230web.py && ./t230web.py
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import time
import uuid
import shutil
import threading
import datetime
import pathlib
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Driver and button listener live next to this file.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from t230scan import T230, ScanError, DeviceNotFound, Cancelled, ProtocolError
from button import ButtonListener

# ── Configuration ────────────────────────────────────────────────────────────

PORT      = int(os.environ.get("T230_PORT", "8080"))
HOST      = os.environ.get("T230_HOST", "0.0.0.0")
TMP_DIR   = pathlib.Path("/tmp/t230")
TMP_DIR.mkdir(exist_ok=True)
PICTURES_DIR = pathlib.Path.home() / "Pictures" / "T230"
PICTURES_DIR.mkdir(parents=True, exist_ok=True)
THUMB_DIR = PICTURES_DIR / ".thumbs"
THUMB_DIR.mkdir(parents=True, exist_ok=True)
TMP_TTL_SECONDS = 3600

# ── Global scan state ────────────────────────────────────────────────────────

# Only one scan at a time (USB claim is exclusive).
scan_lock = threading.Lock()
# scan_id -> threading.Event for in-flight cancellation.
cancel_events: dict[str, threading.Event] = {}
cancel_events_lock = threading.Lock()

# Button-triggered scan status (no scan_id since it's not streamed to a client).
_scanning = False
_scanning_lock = threading.Lock()

# ── Thumbnail helper ─────────────────────────────────────────────────────────

def _make_thumbnail(src: pathlib.Path, dst: pathlib.Path) -> None:
    """Resize *src* to a max-280px JPEG thumbnail and write to *dst*.
    Falls back to a plain copy if Pillow is unavailable."""
    try:
        from PIL import Image
        with Image.open(src) as im:
            im.thumbnail((280, 280))
            im.save(dst, format="JPEG", quality=75)
    except ImportError:
        shutil.copy2(src, dst)
    except Exception as e:
        sys.stderr.write(f"[thumb] {src.name}: {e}\n")
        try:
            shutil.copy2(src, dst)
        except Exception:
            pass

# ── Filename parsing ──────────────────────────────────────────────────────────

_SCAN_RE = re.compile(
    r"^scan-(\d{4}-\d{2}-\d{2}-\d{6})-(\d+)dpi-([a-z0-9]+)\.jpg$",
    re.IGNORECASE,
)

def _parse_scan_filename(name: str) -> dict | None:
    """Parse ``scan-YYYY-MM-DD-HHMMSS-{dpi}dpi-{mode}.jpg``.

    Returns a dict with keys ``dt``, ``dpi``, ``mode``, ``filename``, or
    *None* if the name doesn't match."""
    m = _SCAN_RE.match(name)
    if not m:
        return None
    try:
        dt = datetime.datetime.strptime(m.group(1), "%Y-%m-%d-%H%M%S")
    except ValueError:
        return None
    return {
        "filename": name,
        "dt":       dt.isoformat(sep=" "),
        "dpi":      int(m.group(2)),
        "mode":     m.group(3).upper(),
    }

# ── History ───────────────────────────────────────────────────────────────────

def _list_history() -> list[dict]:
    """Return the 100 most-recent scans from PICTURES_DIR as a list of dicts.

    Each dict has: filename, thumb (URL), url, dt, dpi, mode, size (bytes).
    Sorted newest-first."""
    items = []
    for p in PICTURES_DIR.glob("*.jpg"):
        parsed = _parse_scan_filename(p.name)
        if parsed is None:
            continue
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        parsed["size"] = size
        parsed["url"]  = f"/history/file/{urllib.parse.quote(p.name)}"
        parsed["thumb"] = f"/history/thumb/{urllib.parse.quote(p.name)}"
        items.append(parsed)
    items.sort(key=lambda x: x["dt"], reverse=True)
    return items[:100]

# ── Background scan (button-triggered) ───────────────────────────────────────

def scan_background(dpi: int = 300, mode: str = "C24BIT") -> None:
    """Run a full scan in the background (used by the hardware button).

    Non-blocking on scan_lock — logs and returns immediately if another scan
    is in progress.  Sets the global ``_scanning`` flag for the /scan/status
    endpoint while active."""
    global _scanning

    if not scan_lock.acquire(blocking=False):
        sys.stderr.write("[button-scan] another scan in progress — skipping\n")
        return

    with _scanning_lock:
        _scanning = True

    pic_path = pictures_path(dpi, mode)
    sys.stderr.write(f"[button-scan] starting {dpi} DPI {mode} → {pic_path}\n")
    try:
        with T230() as scanner, open(pic_path, "wb") as f:
            for band in scanner.scan(dpi, mode):
                f.write(band)
        sys.stderr.write(f"[button-scan] saved {pic_path}\n")
        # Generate thumbnail.
        thumb_path = THUMB_DIR / pic_path.name
        _make_thumbnail(pic_path, thumb_path)
    except DeviceNotFound as e:
        sys.stderr.write(f"[button-scan] device not found: {e}\n")
    except (ScanError, ProtocolError, Exception) as e:
        sys.stderr.write(f"[button-scan] {type(e).__name__}: {e}\n")
    finally:
        with _scanning_lock:
            _scanning = False
        scan_lock.release()

# ── Misc helpers ──────────────────────────────────────────────────────────────

def cleanup_tmp() -> None:
    """Drop /tmp/t230/*.jpg older than TMP_TTL_SECONDS."""
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


def _safe_filename(name: str) -> bool:
    """Return True iff *name* is a plain filename with no path traversal."""
    return bool(name) and name == pathlib.Path(name).name and ".." not in name


# ── HTML page ─────────────────────────────────────────────────────────────────

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
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
  * { box-sizing: border-box; }
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

  /* Main three-column layout */
  main {
    display: grid;
    grid-template-columns: 240px 1fr 240px;
    min-height: 0;
  }
  aside {
    background: var(--panel);
    border-right: 1px solid var(--border);
    padding: 12px; overflow-y: auto;
  }
  aside.history-panel {
    border-right: none;
    border-left: 1px solid var(--border);
  }
  section.preview {
    background: #1a1a1a;
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
    max-width: 100%;
    max-height: 100%;
    object-fit: contain;
    box-shadow: 0 4px 16px rgba(0,0,0,0.6);
    background: #fff;
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
  button:hover:not(:disabled) { background: linear-gradient(#454545, #383838); }
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

  /* ── History panel ── */
  .history-list {
    display: flex; flex-direction: column; gap: 6px;
  }
  .history-empty {
    color: var(--text-dim); font-size: 12px;
    padding: 8px 0; text-align: center;
  }
  .history-card {
    display: flex; align-items: center; gap: 8px;
    background: var(--panel-2);
    border: 1px solid var(--border);
    border-radius: 3px;
    padding: 6px;
    cursor: pointer;
    transition: border-color 0.15s;
  }
  .history-card:hover { border-color: var(--border-2); }
  .history-card.active { border-color: var(--accent); }
  .history-card img {
    width: 56px; height: 56px;
    object-fit: cover;
    border-radius: 2px;
    background: #1a1a1a;
    flex-shrink: 0;
  }
  .history-card-meta {
    flex: 1; min-width: 0;
    font-size: 11px; line-height: 1.55;
    color: var(--text-dim);
    overflow: hidden;
  }
  .history-card-meta .hdate {
    color: var(--text); font-weight: 500;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .history-card-meta .hinfo {
    color: var(--text-dim);
  }
  .history-card-dl {
    flex-shrink: 0;
    font-size: 11px; color: #b0e0b3; text-decoration: none;
    padding: 2px 6px;
    background: linear-gradient(#384a38, #2c3a2c);
    border: 1px solid var(--border); border-radius: 2px;
  }
  .history-card-dl:hover { background: linear-gradient(#445544, #384a38); }

  /* Lock all interactive controls while a scan is in progress */
  body.scanning select,
  body.scanning .history-card,
  body.scanning .history-card-dl,
  body.scanning a.dl {
    pointer-events: none;
    opacity: 0.35;
  }

  /* ── Settings collapse (desktop: always open, hide toggle) ── */
  @media (min-width: 769px) {
    #settingsDetails > :not(summary) { display: block !important; }
    #settingsDetails > summary { display: none; }
  }

  /* ── Mobile layout ── */
  @media (max-width: 768px) {
    main { grid-template-columns: 1fr; }

    /* Order: settings/action first, preview second, history last */
    aside:not(.history-panel) {
      order: 1;
      border-right: none;
      border-bottom: 1px solid var(--border);
      overflow-y: visible;
    }
    section.preview {
      order: 2;
      min-height: 56vw;
      padding: 10px;
    }
    aside.history-panel {
      order: 3;
      border-left: none;
      border-top: 1px solid var(--border);
    }
    #preview { max-height: 75vw; }

    /* Settings collapse toggle */
    #settingsDetails > summary {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 6px 0;
      cursor: pointer;
      color: var(--text-dim);
      font-size: 11px; font-weight: 600;
      text-transform: uppercase; letter-spacing: 0.5px;
      list-style: none; -webkit-appearance: none;
    }
    #settingsDetails > summary::after { content: '▾'; }
    #settingsDetails[open] > summary::after { content: '▴'; }

    /* Touch targets */
    button { min-height: 44px; padding: 10px 16px; font-size: 14px; }
    select { min-height: 36px; font-size: 14px; }

    /* History: horizontal scroll strip */
    .history-list {
      flex-direction: row;
      overflow-x: auto; overflow-y: hidden;
      padding-bottom: 6px;
      -webkit-overflow-scrolling: touch;
      scrollbar-width: thin;
    }
    .history-card {
      flex-direction: column;
      flex-shrink: 0;
      width: 96px;
      padding: 6px;
    }
    .history-card img { width: 100%; height: 72px; }
    .history-card-meta { font-size: 10px; }
    .history-card-dl { display: block; text-align: center; margin-top: 4px; }

    footer { flex-direction: column; gap: 2px; font-size: 11px; }
  }
</style>
</head>
<body>
<header>
  <span class="pulse"></span>
  <span>Brother DCP-T230 Scanner</span>
</header>

<main>
  <!-- ── Left column: controls ── -->
  <aside>
    <details id="settingsDetails">
      <summary>Settings</summary>
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
    </details>

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

  <!-- ── Centre column: preview ── -->
  <section class="preview">
    <img id="preview" alt="scanned image">
    <div class="empty-hint">No image. Press <strong>Scan</strong> to start.</div>
  </section>

  <!-- ── Right column: history ── -->
  <aside class="history-panel">
    <div class="group">
      <h2>Scan History</h2>
      <div class="history-list" id="historyList">
        <div class="history-empty">No scans yet</div>
      </div>
    </div>
  </aside>
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
const histList  = document.getElementById('historyList');

let currentScanId    = null;   // active HTTP-streaming scan
let timer            = null;
let selectedFilename = null;   // highlighted history card
let lastHistoryDt    = null;   // newest dt seen, for refresh detection
let wasButtonScanning = false; // tracks /scan/status transitions

// ── Utility ──────────────────────────────────────────────────────────────────

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

function fmtSize(bytes) {
  if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + ' MB';
  if (bytes >= 1024)    return (bytes / 1024).toFixed(0) + ' KB';
  return bytes + ' B';
}

// ── Manual scan (Scan button) ─────────────────────────────────────────────

scanBtn.addEventListener('click', () => {
  const dpi      = document.getElementById('dpi').value;
  const mode     = document.getElementById('mode').value;
  const modeLabel = mode === 'C24BIT' ? 'Color' : 'Grayscale';
  currentScanId  = crypto.randomUUID();
  scanBtn.disabled  = true;
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
  scanBtn.disabled   = false;
  setBodyState('done');
  if (currentScanId) {
    dl.href = `/scan/file/${currentScanId}`;
    dlBox.style.display = '';
    setStatus(`Done · ${img.naturalWidth}×${img.naturalHeight} px · saved to ~/Pictures/T230/`);
  }
  // Refresh history so the new scan appears immediately.
  setTimeout(loadHistory, 800);
});

img.addEventListener('error', () => {
  stopTimer();
  cancelBtn.disabled = true;
  scanBtn.disabled   = false;
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

// ── History ──────────────────────────────────────────────────────────────────

async function loadHistory() {
  try {
    const r = await fetch('/history');
    if (!r.ok) return;
    const items = await r.json();
    renderHistory(items);
  } catch (e) {
    // Silently ignore network errors — history is best-effort.
  }
}

function renderHistory(items) {
  if (!items || items.length === 0) {
    histList.innerHTML = '<div class="history-empty">No scans yet</div>';
    return;
  }
  const frag = document.createDocumentFragment();
  for (const item of items) {
    const card = document.createElement('div');
    card.className = 'history-card' + (item.filename === selectedFilename ? ' active' : '');
    card.dataset.filename = item.filename;

    // Thumbnail
    const thumb = document.createElement('img');
    thumb.src = item.thumb;
    thumb.alt = '';
    thumb.loading = 'lazy';
    card.appendChild(thumb);

    // Meta text
    const metaDiv = document.createElement('div');
    metaDiv.className = 'history-card-meta';
    // Date — strip seconds for brevity
    const shortDt = item.dt.replace(/:\d\d$/, '');
    const modeLabel = item.mode === 'C24BIT' ? 'Color' : item.mode === 'GRAY256' ? 'Gray' : item.mode;
    metaDiv.innerHTML =
      `<div class="hdate">${shortDt}</div>` +
      `<div class="hinfo">${item.dpi} DPI · ${modeLabel} · ${fmtSize(item.size)}</div>`;
    card.appendChild(metaDiv);

    // Download link
    const a = document.createElement('a');
    a.className = 'history-card-dl';
    a.href = item.url;
    a.download = item.filename;
    a.textContent = 'DL';
    a.addEventListener('click', e => e.stopPropagation());
    card.appendChild(a);

    card.addEventListener('click', () => showHistoryItem(item));
    frag.appendChild(card);
  }
  histList.innerHTML = '';
  histList.appendChild(frag);
}

function showHistoryItem(item) {
  selectedFilename = item.filename;
  // Update active styling.
  document.querySelectorAll('.history-card').forEach(c => {
    c.classList.toggle('active', c.dataset.filename === item.filename);
  });
  // Show in preview.
  currentScanId = null;
  img.src = item.url;
  const modeLabel = item.mode === 'C24BIT' ? 'Color' : item.mode === 'GRAY256' ? 'Grayscale' : item.mode;
  setMeta(`${item.dpi} DPI · ${modeLabel} · ${fmtSize(item.size)}`);
  setStatus(item.dt);
  setBodyState('done');
  // Show a download link in the left panel too.
  dl.href = item.url;
  dl.download = item.filename;
  dlBox.style.display = '';
}

// ── Button-scan status polling ────────────────────────────────────────────────

async function pollScanStatus() {
  try {
    const r = await fetch('/scan/status');
    if (!r.ok) return;
    const data = await r.json();
    const isScanning = data.scanning;

    if (isScanning && !currentScanId) {
      // Button-triggered scan in progress.
      if (!wasButtonScanning) {
        setBodyState('scanning');
        setStatus('Scanning from button…');
        setMeta('300 DPI · Color');
      }
      wasButtonScanning = true;
    } else if (!isScanning && wasButtonScanning) {
      // Just finished.
      wasButtonScanning = false;
      setBodyState('done');
      setStatus('Scan complete — saved to ~/Pictures/T230/');
      loadHistory();
    }
  } catch (e) {
    // Network error — ignore.
  }
}

// ── Startup & polling intervals ───────────────────────────────────────────────

loadHistory();
setInterval(loadHistory,      3000);
setInterval(pollScanStatus,   1000);
</script>
</body>
</html>
"""


# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write(
            f"{self.log_date_time_string()} {self.address_string()} "
            f"{fmt % args}\n")

    # ── send helpers ──

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

    def _send_json(self, obj) -> None:
        body = json.dumps(obj).encode("utf-8")
        self._send_simple(200, body, "application/json")

    def _send_file(self, path: pathlib.Path, inline: bool = True) -> None:
        """Stream *path* to the client as image/jpeg."""
        if not path.is_file():
            self._send_simple(404, b"not found", "text/plain")
            return
        try:
            data = path.read_bytes()
        except OSError as e:
            self._send_simple(500, f"read error: {e}".encode(), "text/plain")
            return
        extra: dict[str, str] = {}
        if not inline:
            ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            extra["Content-Disposition"] = f'attachment; filename="scan-{ts}.jpg"'
        self._send_simple(200, data, "image/jpeg", extra)

    # ── routing ──

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path

        if p in ("/", ""):
            self._send_simple(200, PAGE.encode("utf-8"),
                              "text/html; charset=utf-8")
            return

        if p == "/scan/start":
            self._serve_scan(u)
            return

        if p.startswith("/scan/file/"):
            self._serve_saved(p[len("/scan/file/"):])
            return

        if p == "/scan/status":
            with _scanning_lock:
                scanning = _scanning
            self._send_json({"scanning": scanning})
            return

        if p == "/history":
            self._send_json(_list_history())
            return

        if p.startswith("/history/thumb/"):
            self._serve_thumb(urllib.parse.unquote(p[len("/history/thumb/"):]))
            return

        if p.startswith("/history/file/"):
            self._serve_history_file(urllib.parse.unquote(p[len("/history/file/"):]))
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

    # ── /scan/start — chunked streaming + tee to disk ──

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
            # Generate thumbnail in background so we don't hold up the response.
            thumb_path = THUMB_DIR / pic_path.name
            threading.Thread(
                target=_make_thumbnail, args=(pic_path, thumb_path),
                daemon=True).start()
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

    # ── /scan/file/<id> — download from /tmp ──

    def _serve_saved(self, scan_id: str):
        if not all(c.isalnum() or c == "-" for c in scan_id) or len(scan_id) > 64:
            self._send_simple(400, b"bad id", "text/plain"); return
        path = TMP_DIR / f"{scan_id}.jpg"
        if not path.is_file():
            self._send_simple(404, b"not found (or expired)", "text/plain")
            return
        try:
            data = path.read_bytes()
        except OSError as e:
            self._send_simple(500, f"read error: {e}".encode(), "text/plain")
            return
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self._send_simple(
            200, data, "image/jpeg",
            {"Content-Disposition": f'attachment; filename="scan-{ts}.jpg"'})

    # ── /history/thumb/<filename> ──

    def _serve_thumb(self, filename: str):
        if not _safe_filename(filename):
            self._send_simple(400, b"bad filename", "text/plain"); return
        src = PICTURES_DIR / filename
        if not src.is_file():
            self._send_simple(404, b"not found", "text/plain"); return
        dst = THUMB_DIR / filename
        if not dst.is_file():
            _make_thumbnail(src, dst)
        self._send_file(dst, inline=True)

    # ── /history/file/<filename> — serve original inline ──

    def _serve_history_file(self, filename: str):
        if not _safe_filename(filename):
            self._send_simple(400, b"bad filename", "text/plain"); return
        self._send_file(PICTURES_DIR / filename, inline=True)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    # Start the hardware button listener.
    listener = ButtonListener(
        on_press=lambda: threading.Thread(
            target=scan_background, daemon=True).start()
    )
    listener.start()

    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"t230web listening on http://{HOST}:{PORT}/  (Ctrl-C to quit)",
          file=sys.stderr)
    print(f"  bind host:            {HOST}", file=sys.stderr)
    print(f"  port:                 {PORT}", file=sys.stderr)
    print(f"  /tmp/t230 buffer dir: {TMP_DIR}", file=sys.stderr)
    print(f"  auto-save dir:        {PICTURES_DIR}", file=sys.stderr)
    print(f"  thumbnails dir:       {THUMB_DIR}", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
