#!/usr/bin/env bash
# Installer for celestron-skyportal-bridge on Raspberry Pi OS (Bookworm+).
#
# Installs the INDI server with the Celestron driver, the Python
# dependencies in a dedicated virtualenv, the systemd services, and
# configures the WiFi hotspot that SkyPortal connects to (1.2.3.4:2000).
#
# Usage:  sudo ./install.sh [options]
#   --user NAME         run the services as this user (default: the user
#                       invoking sudo)
#   --ssid NAME         hotspot SSID            (default: Celestron)
#   --password PASS     hotspot WPA2 password, min 8 chars
#                       (default: celestron127)
#   --wifi-interface IF hotspot interface       (default: wlan0)

set -euo pipefail

INSTALL_DIR=/opt/celestron
VENV="$INSTALL_DIR/venv"
CONF=/etc/celestron-bridge.conf
INDI_DRIVER=indi_celestron_gps

RUN_USER="${SUDO_USER:-$(id -un)}"
SSID="Celestron"
WIFI_PASS="celestron127"
WIFI_IF="wlan0"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user)           RUN_USER="$2"; shift 2 ;;
        --ssid)           SSID="$2"; shift 2 ;;
        --password)       WIFI_PASS="$2"; shift 2 ;;
        --wifi-interface) WIFI_IF="$2"; shift 2 ;;
        -h|--help)        grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1 (see --help)" >&2; exit 1 ;;
    esac
done

[[ $EUID -eq 0 ]] || { echo "please run with sudo" >&2; exit 1; }
id "$RUN_USER" &>/dev/null || { echo "user '$RUN_USER' does not exist" >&2; exit 1; }
[[ ${#WIFI_PASS} -ge 8 ]] || { echo "--password must be at least 8 characters" >&2; exit 1; }

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "==> Installing packages (INDI server, Python venv, NetworkManager)"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    indi-bin python3-venv network-manager

echo "==> Granting serial port access to '$RUN_USER'"
usermod -aG dialout "$RUN_USER"

echo "==> Installing files into $INSTALL_DIR"
install -d "$INSTALL_DIR"
install -m 644 "$SRC_DIR/skyportal_bridge.py" "$SRC_DIR/celestron_indi.py" \
    "$INSTALL_DIR/"

echo "==> Creating Python virtualenv"
[[ -d $VENV ]] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet -r "$SRC_DIR/requirements.txt"

echo "==> Installing configuration"
if [[ ! -f $CONF ]]; then
    install -m 644 "$SRC_DIR/celestron-bridge.conf.example" "$CONF"
    echo "    created $CONF (edit it to change serial port, INDI host, ...)"
else
    echo "    $CONF already exists, leaving it untouched"
fi

echo "==> Installing 'celestron' command"
cat > /usr/local/bin/celestron <<EOF
#!/bin/sh
exec $VENV/bin/python $INSTALL_DIR/celestron_indi.py "\$@"
EOF
chmod 755 /usr/local/bin/celestron

echo "==> Installing systemd services"
cat > /etc/systemd/system/indiserver.service <<EOF
[Unit]
Description=INDI server (Celestron NexStar)
After=multi-user.target

[Service]
User=$RUN_USER
ExecStart=/usr/bin/indiserver $INDI_DRIVER
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/skyportal-bridge.service <<EOF
[Unit]
Description=SkyPortal bridge (TCP 2000 -> NexStar hand controller)
After=network.target indiserver.service

[Service]
User=$RUN_USER
ExecStart=$VENV/bin/python $INSTALL_DIR/skyportal_bridge.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now indiserver.service skyportal-bridge.service

echo "==> Configuring WiFi hotspot '$SSID' on $WIFI_IF (IP 1.2.3.4)"
# Note: this turns $WIFI_IF into an access point; if the Pi was using it
# as a WiFi client, that connection will drop. Use Ethernet or a second
# WiFi adapter for internet access.
nmcli radio wifi on
nmcli connection delete celestron-hotspot &>/dev/null || true
nmcli connection add type wifi ifname "$WIFI_IF" con-name celestron-hotspot \
    autoconnect yes ssid "$SSID" \
    802-11-wireless.mode ap 802-11-wireless.band bg \
    wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$WIFI_PASS" \
    ipv4.method shared ipv4.addresses 1.2.3.4/24 ipv6.method disabled
nmcli connection up celestron-hotspot

echo
echo "Done. Summary:"
echo "  - services:      indiserver, skyportal-bridge (enabled at boot)"
echo "  - configuration: $CONF"
echo "  - CLI:           celestron            (status of the telescope)"
echo "  - hotspot:       SSID '$SSID', password '$WIFI_PASS'"
echo
echo "Connect the phone to the '$SSID' WiFi network, then open SkyPortal:"
echo "Connection -> WiFi (the app reaches the scope at 1.2.3.4:2000)."
echo "Note: '$RUN_USER' may need to log out/in for the dialout group"
echo "to take effect (services are unaffected)."
