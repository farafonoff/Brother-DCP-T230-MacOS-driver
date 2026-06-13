#!/bin/bash
# Install t230web.py as a macOS LaunchAgent.
#
# After installation the scanner UI starts automatically on every login and
# is available at http://127.0.0.1:8080/  (override with T230_PORT env var).
#
# Does NOT require sudo — LaunchAgents live in ~/Library/LaunchAgents/.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB_SCRIPT="$SCRIPT_DIR/t230web.py"

PLIST_ID="com.brother.t230scan"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_ID.plist"
LOG_DIR="$HOME/Library/Logs/t230scan"
PORT="${T230_PORT:-8080}"

log() { printf '[install-scanner] %s\n' "$*"; }
die() { printf '[install-scanner] ERROR: %s\n' "$*" >&2; exit 1; }

[[ -f "$WEB_SCRIPT" ]] || die "t230web.py not found at $WEB_SCRIPT"

# --- Require uv ---------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
    die "uv not found — install it first:
    curl -LsSf https://astral.sh/uv/install.sh | sh
then re-run this script."
fi
UV="$(command -v uv)"
log "uv: $UV"

# --- Log directory ------------------------------------------------------------
mkdir -p "$LOG_DIR"
log "logs: $LOG_DIR/t230scan.log"

# --- Write plist --------------------------------------------------------------
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${PLIST_ID}</string>

  <key>ProgramArguments</key>
  <array>
    <string>${UV}</string>
    <string>run</string>
    <string>--quiet</string>
    <string>--script</string>
    <string>${WEB_SCRIPT}</string>
  </array>

  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>${HOME}</string>
    <key>PATH</key>
    <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:$(dirname "$UV")</string>
    <key>T230_PORT</key>
    <string>${PORT}</string>
  </dict>

  <key>WorkingDirectory</key>
  <string>${SCRIPT_DIR}</string>

  <!-- Start immediately and on every login. -->
  <key>RunAtLoad</key>
  <true/>

  <!-- Restart on crash/non-zero exit; stop if it exits cleanly (code 0). -->
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>

  <!-- Both stdout and stderr go to the same rolling log. -->
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/t230scan.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/t230scan.log</string>
</dict>
</plist>
PLIST

log "wrote: $PLIST_PATH"

# --- Load (or reload if already running) --------------------------------------
if launchctl list "$PLIST_ID" >/dev/null 2>&1; then
    log "agent already loaded — reloading..."
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
fi
launchctl load "$PLIST_PATH"
log "agent loaded"

log "done."
log ""
log "  Scanner UI → http://127.0.0.1:${PORT}/"
log "  Logs       → $LOG_DIR/t230scan.log"
log "  Stop now   → launchctl unload \"$PLIST_PATH\""
log "  Start now  → launchctl load  \"$PLIST_PATH\""
log "  Uninstall  → $SCRIPT_DIR/uninstall.sh"
