#!/bin/bash
# Install t230web.py as a systemd user service on Debian / Armbian / Ubuntu.
#
# After installation the scanner UI starts automatically on every login and
# is available at http://127.0.0.1:8080/  (override with T230_PORT env var).
#
# Does NOT require sudo — systemd user services live in
# ~/.config/systemd/user/.
#
# Requirements:
#   - systemd (standard on Debian 8+, Armbian, Ubuntu 15.04+)
#   - Python 3.10+
#   - uv  (https://docs.astral.sh/uv/)
#   - libusb-1.0  (sudo apt install libusb-1.0-0)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB_SCRIPT="$SCRIPT_DIR/t230web.py"

SERVICE_NAME="t230scan"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_PATH="$SERVICE_DIR/$SERVICE_NAME.service"
LOG_DIR="$HOME/.local/share/t230scan/logs"
PORT="${T230_PORT:-8080}"

log() { printf '[install-scanner] %s\n' "$*"; }
die() { printf '[install-scanner] ERROR: %s\n' "$*" >&2; exit 1; }

[[ -f "$WEB_SCRIPT" ]] || die "t230web.py not found at $WEB_SCRIPT"

# --- Require systemd ----------------------------------------------------------
if ! command -v systemctl >/dev/null 2>&1; then
    die "systemctl not found — this script requires systemd."
fi

# --- Require uv ---------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
    die "uv not found — install it first:
    curl -LsSf https://astral.sh/uv/install.sh | sh
then re-run this script."
fi
UV="$(command -v uv)"
log "uv: $UV"

# --- Require libusb -----------------------------------------------------------
if ! ldconfig -p 2>/dev/null | grep -q 'libusb-1\.0' && \
   ! ls /usr/lib/*/libusb-1.0.so* /usr/lib/libusb-1.0.so* 2>/dev/null | grep -q .; then
    log "WARNING: libusb-1.0 not found in ldconfig — you may need to run:"
    log "  sudo apt install libusb-1.0-0"
fi

# --- Allow USB access without sudo --------------------------------------------
# On Linux the scanner USB device is owned by root:plugdev by default.
# Add the current user to the plugdev group so uv/python can open it.
if ! groups | grep -qw plugdev; then
    log "Adding $USER to plugdev group (required for USB access)..."
    sudo usermod -aG plugdev "$USER" || log "WARNING: could not add to plugdev — you may need: sudo usermod -aG plugdev $USER"
    log "NOTE: You must log out and back in for group membership to take effect."
fi

# Install a udev rule so the T230 device node is group-readable by plugdev.
UDEV_RULE_PATH="/etc/udev/rules.d/70-brother-t230.rules"
if [[ ! -f "$UDEV_RULE_PATH" ]]; then
    log "Installing udev rule for Brother DCP-T230 (requires sudo)..."
    sudo tee "$UDEV_RULE_PATH" >/dev/null <<'UDEV'
# Brother DCP-T230 — allow plugdev members to open the USB device
SUBSYSTEM=="usb", ATTR{idVendor}=="04f9", ATTR{idProduct}=="0439", MODE="0664", GROUP="plugdev"
UDEV
    sudo udevadm control --reload-rules
    sudo udevadm trigger --subsystem-match=usb || true
    log "udev rule installed: $UDEV_RULE_PATH"
else
    log "udev rule already present: $UDEV_RULE_PATH"
fi

# --- Enable lingering so service runs without an active login session ---------
# (Optional but useful on headless / Armbian boards.)
if command -v loginctl >/dev/null 2>&1; then
    if ! loginctl show-user "$USER" 2>/dev/null | grep -q 'Linger=yes'; then
        log "Enabling systemd user lingering for $USER (requires sudo)..."
        sudo loginctl enable-linger "$USER" || log "WARNING: could not enable lingering — service will only run while you are logged in."
    fi
fi

# --- Log directory ------------------------------------------------------------
mkdir -p "$LOG_DIR"
log "logs: $LOG_DIR/t230scan.log"

# --- Write service unit -------------------------------------------------------
mkdir -p "$SERVICE_DIR"
cat > "$SERVICE_PATH" <<UNIT
[Unit]
Description=Brother DCP-T230 scanner web UI
After=network.target

[Service]
Type=simple
WorkingDirectory=${SCRIPT_DIR}
ExecStart=${UV} run --quiet --script ${WEB_SCRIPT}
Environment=T230_PORT=${PORT}
Environment=HOME=${HOME}
Environment=PATH=/usr/local/bin:/usr/bin:/bin:$(dirname "$UV")

# Restart on crash; don't restart on clean exit (code 0).
Restart=on-failure
RestartSec=5

# Log to the systemd journal; also redirect to a plain file via
# StandardOutput/StandardError so the same log path works on boards
# that lack persistent journald storage (common on Armbian).
StandardOutput=append:${LOG_DIR}/t230scan.log
StandardError=append:${LOG_DIR}/t230scan.log

[Install]
WantedBy=default.target
UNIT

log "wrote: $SERVICE_PATH"

# --- Reload systemd and enable/start service ----------------------------------
systemctl --user daemon-reload

if systemctl --user is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    log "service already running — restarting..."
    systemctl --user restart "$SERVICE_NAME"
else
    systemctl --user enable --now "$SERVICE_NAME"
fi

log "done."
log ""
log "  Scanner UI → http://127.0.0.1:${PORT}/"
log "  Logs       → $LOG_DIR/t230scan.log"
log "               journalctl --user -u $SERVICE_NAME -f"
log "  Stop now   → systemctl --user stop $SERVICE_NAME"
log "  Start now  → systemctl --user start $SERVICE_NAME"
log "  Uninstall  → $SCRIPT_DIR/uninstall-linux.sh"
