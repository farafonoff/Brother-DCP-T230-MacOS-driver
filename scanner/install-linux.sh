#!/bin/bash
# Install t230web.py as a systemd service on Debian / Armbian / Ubuntu.
#
# Run as root  → system service, port 80, accessible on the LAN
#   sudo ./install-linux.sh
#
# Run as user  → user service, port 8080, localhost only
#   ./install-linux.sh
#
# Override defaults:
#   sudo T230_PORT=8080 T230_HOST=0.0.0.0 ./install-linux.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB_SCRIPT="$SCRIPT_DIR/t230web.py"
SERVICE_NAME="t230scan"

log() { printf '[install-scanner] %s\n' "$*"; }
die() { printf '[install-scanner] ERROR: %s\n' "$*" >&2; exit 1; }

[[ -f "$WEB_SCRIPT" ]] || die "t230web.py not found at $WEB_SCRIPT"
command -v systemctl >/dev/null 2>&1 || die "systemctl not found — this script requires systemd."
command -v python3   >/dev/null 2>&1 || die "python3 not found — install it: sudo apt install python3"

# ── System vs user service ────────────────────────────────────────────────────

if [[ $EUID -eq 0 ]]; then
    SYSTEM_SERVICE=true
    SERVICE_DIR="/etc/systemd/system"
    DEFAULT_PORT=80
    DEFAULT_HOST="0.0.0.0"
    LOG_DIR="/var/log/t230scan"
    RUN_USER="root"
    SYSTEMCTL="systemctl"
else
    SYSTEM_SERVICE=false
    SERVICE_DIR="$HOME/.config/systemd/user"
    DEFAULT_PORT=8080
    DEFAULT_HOST="127.0.0.1"
    LOG_DIR="$HOME/.local/share/t230scan/logs"
    RUN_USER="$USER"
    SYSTEMCTL="systemctl --user"
fi

PORT="${T230_PORT:-$DEFAULT_PORT}"
HOST="${T230_HOST:-$DEFAULT_HOST}"
SERVICE_PATH="$SERVICE_DIR/$SERVICE_NAME.service"

log "mode: $( [[ $SYSTEM_SERVICE == true ]] && echo 'system service (root)' || echo 'user service' )"
log "bind: $HOST:$PORT"

# ── Python runner: prefer uv, fall back to plain python3 ─────────────────────

if command -v uv >/dev/null 2>&1; then
    UV="$(command -v uv)"
    EXEC_START="${UV} run --quiet --script ${WEB_SCRIPT}"
    log "runner: uv ($UV)"
else
    log "uv not found — using python3 directly"
    PYTHON3="$(command -v python3)"

    # Install Python dependencies if missing.
    missing_deps=()
    python3 -c "import usb1"     2>/dev/null || missing_deps+=(libusb1)
    python3 -c "from PIL import Image" 2>/dev/null || missing_deps+=(Pillow)

    if [[ ${#missing_deps[@]} -gt 0 ]]; then
        log "installing Python packages: ${missing_deps[*]}"
        # Try apt first for Pillow (avoids Debian's pip restrictions).
        if [[ " ${missing_deps[*]} " == *" Pillow "* ]]; then
            if apt-cache show python3-pil >/dev/null 2>&1; then
                $( [[ $SYSTEM_SERVICE == true ]] && echo '' || echo 'sudo' ) \
                    apt-get install -y -q python3-pil && missing_deps=(${missing_deps[@]/Pillow/}) || true
            fi
        fi
        # Remaining deps via pip.
        if [[ ${#missing_deps[@]} -gt 0 ]]; then
            pip3 install --quiet --break-system-packages "${missing_deps[@]}" 2>/dev/null \
                || pip3 install --quiet "${missing_deps[@]}"
        fi
    fi

    # Verify.
    python3 -c "import usb1" 2>/dev/null \
        || die "libusb1 Python package not found. Install: pip3 install libusb1"
    python3 -c "from PIL import Image" 2>/dev/null \
        || log "WARNING: Pillow not available — thumbnails will use full images"

    EXEC_START="${PYTHON3} ${WEB_SCRIPT}"
    log "runner: python3 ($PYTHON3)"
fi

# ── python3-usb check (button listener) ──────────────────────────────────────

if ! python3 -c "import usb.core" 2>/dev/null; then
    log "WARNING: python3-usb not found — hardware scan button will be disabled."
    log "  sudo apt install python3-usb"
fi

# ── libusb system library ─────────────────────────────────────────────────────

if ! ldconfig -p 2>/dev/null | grep -q 'libusb-1\.0'; then
    log "WARNING: libusb-1.0-0 system library not found."
    log "  sudo apt install libusb-1.0-0"
fi

# ── udev rule (lets non-root access the USB device) ──────────────────────────

UDEV_RULE_PATH="/etc/udev/rules.d/70-brother-t230.rules"
UDEV_CONTENT='# Brother DCP-T230 — allow plugdev members to open the USB device
SUBSYSTEM=="usb", ATTR{idVendor}=="04f9", ATTR{idProduct}=="0716", MODE="0664", GROUP="plugdev"
SUBSYSTEM=="usb", ATTR{idVendor}=="04f9", ATTR{idProduct}=="0439", MODE="0664", GROUP="plugdev"'

if [[ ! -f "$UDEV_RULE_PATH" ]]; then
    log "installing udev rule → $UDEV_RULE_PATH"
    if [[ $SYSTEM_SERVICE == true ]]; then
        echo "$UDEV_CONTENT" > "$UDEV_RULE_PATH"
    else
        echo "$UDEV_CONTENT" | sudo tee "$UDEV_RULE_PATH" >/dev/null
    fi
    udevadm control --reload-rules 2>/dev/null \
        || sudo udevadm control --reload-rules 2>/dev/null || true
    udevadm trigger --subsystem-match=usb 2>/dev/null \
        || sudo udevadm trigger --subsystem-match=usb 2>/dev/null || true
fi

# ── lingering (user service only) ────────────────────────────────────────────

if [[ $SYSTEM_SERVICE == false ]] && command -v loginctl >/dev/null 2>&1; then
    if ! loginctl show-user "$USER" 2>/dev/null | grep -q 'Linger=yes'; then
        log "enabling systemd user lingering for $USER..."
        sudo loginctl enable-linger "$USER" \
            || log "WARNING: could not enable lingering — service won't start without an active session."
    fi
fi

# ── Log directory ─────────────────────────────────────────────────────────────

mkdir -p "$LOG_DIR"

# ── Systemd unit file ─────────────────────────────────────────────────────────

mkdir -p "$SERVICE_DIR"

cat > "$SERVICE_PATH" <<UNIT
[Unit]
Description=Brother DCP-T230 scanner web UI
After=network.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${SCRIPT_DIR}
ExecStart=${EXEC_START}
Environment=T230_PORT=${PORT}
Environment=T230_HOST=${HOST}
Environment=HOME=$( [[ $SYSTEM_SERVICE == true ]] && echo "/root" || echo "$HOME" )
Environment=PATH=/usr/local/bin:/usr/bin:/bin

Restart=on-failure
RestartSec=5

StandardOutput=append:${LOG_DIR}/t230scan.log
StandardError=append:${LOG_DIR}/t230scan.log

[Install]
WantedBy=$( [[ $SYSTEM_SERVICE == true ]] && echo "multi-user.target" || echo "default.target" )
UNIT

log "wrote: $SERVICE_PATH"

# ── Enable and start ──────────────────────────────────────────────────────────

$SYSTEMCTL daemon-reload

if $SYSTEMCTL is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    log "service already running — restarting..."
    $SYSTEMCTL restart "$SERVICE_NAME"
else
    $SYSTEMCTL enable --now "$SERVICE_NAME"
fi

log "done."
log ""
log "  Scanner UI → http://${HOST}:${PORT}/"
log "  Logs       → $LOG_DIR/t230scan.log"
log "               $SYSTEMCTL status $SERVICE_NAME"
log "               journalctl $( [[ $SYSTEM_SERVICE == false ]] && echo '--user' ) -u $SERVICE_NAME -f"
log "  Stop       → $SYSTEMCTL stop $SERVICE_NAME"
log "  Uninstall  → $SCRIPT_DIR/uninstall-linux.sh"
