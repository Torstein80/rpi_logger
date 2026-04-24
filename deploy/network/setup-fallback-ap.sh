#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${1:-$SCRIPT_DIR/fallback-ap.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Environment file not found: $ENV_FILE" >&2
  echo "Copy fallback-ap.env.example to fallback-ap.env and edit it first." >&2
  exit 1
fi

# shellcheck disable=SC1090
source "$ENV_FILE"

: "${AP_SSID:?AP_SSID is required}"
: "${AP_PASSWORD:?AP_PASSWORD is required}"
: "${AP_INTERFACE:=wlan0}"
: "${AP_ADDRESS_CIDR:=10.77.0.1/24}"
: "${WIRED_MODE:=auto}"
: "${WIRED_CONNECTION_NAME:=haga-eth0}"
: "${WIRED_INTERFACE:=eth0}"
: "${WIRED_IP_CIDR:=10.87.0.5/24}"
: "${PI_HOSTNAME:=hagasolutions-rpi-logger}"

AP_CONNECTION_NAME="haga-fallback-ap"

if [[ "$EUID" -ne 0 ]]; then
  echo "Run as root: sudo $0 $ENV_FILE" >&2
  exit 1
fi

apt-get update
apt-get install -y network-manager avahi-daemon
systemctl enable NetworkManager avahi-daemon
systemctl start NetworkManager avahi-daemon

hostnamectl set-hostname "$PI_HOSTNAME"

# Remove any previous AP profile so reruns are idempotent.
if nmcli -t -f NAME connection show | grep -Fxq "$AP_CONNECTION_NAME"; then
  nmcli connection delete "$AP_CONNECTION_NAME"
fi

nmcli connection add type wifi ifname "$AP_INTERFACE" con-name "$AP_CONNECTION_NAME" autoconnect yes ssid "$AP_SSID"
nmcli connection modify "$AP_CONNECTION_NAME" \
  802-11-wireless.mode ap \
  802-11-wireless.band bg \
  wifi-sec.key-mgmt wpa-psk \
  wifi-sec.psk "$AP_PASSWORD" \
  ipv4.method shared \
  ipv4.addresses "$AP_ADDRESS_CIDR" \
  ipv6.method disabled \
  connection.autoconnect yes \
  connection.autoconnect-priority 50

if nmcli -t -f NAME connection show | grep -Fxq "$WIRED_CONNECTION_NAME"; then
  nmcli connection delete "$WIRED_CONNECTION_NAME"
fi
nmcli connection add type ethernet ifname "$WIRED_INTERFACE" con-name "$WIRED_CONNECTION_NAME" autoconnect yes

if [[ "$WIRED_MODE" == "manual" ]]; then
  nmcli connection modify "$WIRED_CONNECTION_NAME" ipv4.method manual ipv4.addresses "$WIRED_IP_CIDR"
  if [[ -n "${WIRED_GATEWAY:-}" ]]; then
    nmcli connection modify "$WIRED_CONNECTION_NAME" ipv4.gateway "$WIRED_GATEWAY"
  else
    nmcli connection modify "$WIRED_CONNECTION_NAME" -ipv4.gateway
  fi
  if [[ -n "${WIRED_DNS:-}" ]]; then
    nmcli connection modify "$WIRED_CONNECTION_NAME" ipv4.dns "$WIRED_DNS"
  else
    nmcli connection modify "$WIRED_CONNECTION_NAME" -ipv4.dns
  fi
else
  nmcli connection modify "$WIRED_CONNECTION_NAME" ipv4.method auto -ipv4.addresses -ipv4.gateway -ipv4.dns
fi
nmcli connection modify "$WIRED_CONNECTION_NAME" ipv6.method ignore connection.autoconnect yes connection.autoconnect-priority 100

nmcli connection up "$WIRED_CONNECTION_NAME" || true
nmcli connection up "$AP_CONNECTION_NAME"

cat <<STATUS

Configured network profiles:
  AP SSID:         $AP_SSID
  AP address:      $AP_ADDRESS_CIDR
  AP URL:          http://${AP_ADDRESS_CIDR%%/*}:8080
  AP hostname URL: http://${PI_HOSTNAME}.local:8080
  Wired profile:   $WIRED_CONNECTION_NAME ($WIRED_MODE)
  Wired interface: $WIRED_INTERFACE

Active devices:
$(nmcli device status)

Active connections:
$(nmcli -f NAME,TYPE,DEVICE connection show --active)
STATUS
