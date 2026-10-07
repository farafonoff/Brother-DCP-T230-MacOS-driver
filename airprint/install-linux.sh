#!/bin/bash
# Install the AirPrint (URF-only) front-end for the DCP-T230 queue.
# Additive: the existing CUPS queue and its Bonjour entry are left untouched.
# Usage: sudo ./install-linux.sh [QUEUE]    (default queue: DCP_T230)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QUEUE="${1:-DCP_T230}"
PORT=8631
NAME="AirPrint DCP-T230"
BIN=/usr/local/lib/t230ipp/t230ipp.py

log() { printf '[airprint] %s\n' "$*"; }
[[ $EUID -eq 0 ]] || { log "must be run as root"; exit 1; }
command -v python3 >/dev/null || { log "python3 missing"; exit 1; }
lpstat -p "$QUEUE" >/dev/null 2>&1 || { log "CUPS queue '$QUEUE' not found"; exit 1; }

install -d /usr/local/lib/t230ipp
install -m 0755 "$SCRIPT_DIR/t230ipp.py" "$BIN"
BUILD="$(git -c safe.directory="*" -C "$SCRIPT_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
sed -i "s/^BUILD = \"dev\".*/BUILD = \"$BUILD\"/" "$BIN"

cat > /etc/systemd/system/t230-airprint.service <<UNIT
[Unit]
Description=DCP-T230 AirPrint (URF-only) IPP front-end
After=cups.service network-online.target
Wants=cups.service

[Service]
ExecStart=/usr/bin/python3 $BIN --queue $QUEUE --listen 0.0.0.0:$PORT
Restart=on-failure
DynamicUser=yes

[Install]
WantedBy=multi-user.target
UNIT

UUID_FILE=/etc/t230-airprint.uuid
[[ -f $UUID_FILE ]] || cat /proc/sys/kernel/random/uuid > "$UUID_FILE"
UUID="$(cat "$UUID_FILE")"

# Bonjour announcement (avahi picks up files in this dir automatically).
# Deliberately no Brother/DCP-T230 identity (usb_MFG/MDL, product): macOS would
# match it to a locally installed native driver instead of driverless AirPrint.
cat > /etc/avahi/services/t230-airprint.service <<AVAHI
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name>$NAME</name>
  <service>
    <type>_ipp._tcp</type>
    <subtype>_universal._sub._ipp._tcp</subtype>
    <port>$PORT</port>
    <txt-record>txtvers=1</txt-record>
    <txt-record>qtotal=1</txt-record>
    <txt-record>rp=ipp/print</txt-record>
    <txt-record>ty=$NAME</txt-record>
    <txt-record>pdl=image/urf,image/pwg-raster</txt-record>
    <txt-record>URF=V1.4,CP1,W8,PQ4,SRGB24,RS300,FN3</txt-record>
    <txt-record>Color=T</txt-record>
    <txt-record>Duplex=F</txt-record>
    <txt-record>Copies=T</txt-record>
    <txt-record>kind=document,envelope,photo</txt-record>
    <txt-record>priority=0</txt-record>
    <txt-record>UUID=$UUID</txt-record>
  </service>
</service-group>
AVAHI

systemctl daemon-reload
systemctl enable --now t230-airprint.service
log "listening on :$PORT, advertised as '$NAME' (queue $QUEUE)"
log "check: ipptool -tv ipp://localhost:$PORT/ipp/print get-printer-attributes.test | grep document-format"
