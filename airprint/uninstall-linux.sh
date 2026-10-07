#!/bin/bash
set -u
[[ $EUID -eq 0 ]] || { echo "must be run as root"; exit 1; }
systemctl disable --now t230-airprint.service 2>/dev/null
rm -f /etc/systemd/system/t230-airprint.service /etc/avahi/services/t230-airprint.service
rm -rf /usr/local/lib/t230ipp
systemctl daemon-reload
echo "[airprint] removed"
