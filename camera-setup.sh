#!/usr/bin/env bash
# Camera add-on installer for celestron-skyportal-bridge.
#
# Adds a Raspberry Pi CSI camera (any libcamera-supported module) to the
# setup: the INDI driver `indi_pylibcamera` is added to the indiserver so
# Ekos/KStars can capture FITS frames, and MediaMTX provides a low-latency
# WebRTC live view in the browser with switchable planetary / deep-sky
# profiles (see the `camera-mode` command).
#
# Usage:  sudo ./camera-setup.sh [options]
#   --user NAME   run the services as this user (default: the user
#                 invoking sudo)

set -euo pipefail

INSTALL_DIR=/opt/celestron
VENV="$INSTALL_DIR/camera-venv"
DRIVER="$VENV/bin/indi_pylibcamera"
SVC=/etc/systemd/system/indiserver.service

RUN_USER="${SUDO_USER:-$(id -un)}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user)    RUN_USER="$2"; shift 2 ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1 (see --help)" >&2; exit 1 ;;
    esac
done

[[ $EUID -eq 0 ]] || { echo "please run with sudo" >&2; exit 1; }
id "$RUN_USER" &>/dev/null || { echo "user '$RUN_USER' does not exist" >&2; exit 1; }

echo "==> Installing packages (INDI server, picamera2, Python deps)"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    indi-bin python3-venv python3-picamera2 python3-lxml python3-astropy curl

echo "==> Granting camera access to '$RUN_USER'"
usermod -aG video "$RUN_USER"

echo "==> Checking that a camera is detected"
# rpicam-hello prints the camera list on stderr; no `grep -q`, which
# would trigger a SIGPIPE failure under pipefail
if ! rpicam-hello --list-cameras 2>&1 | grep '^[0-9]' >/dev/null; then
    echo "    WARNING: no CSI camera detected (rpicam-hello --list-cameras)."
    echo "    Check the ribbon cable and 'camera_auto_detect=1' in"
    echo "    /boot/firmware/config.txt, then reboot. Continuing anyway."
fi

echo "==> Installing the indi_pylibcamera driver in $VENV"
install -d "$INSTALL_DIR"
# --system-site-packages: the driver needs the apt-provided picamera2
[[ -d $VENV ]] || python3 -m venv --system-site-packages "$VENV"
"$VENV/bin/pip" install --quiet indi_pylibcamera

echo "==> Adding the camera driver to the indiserver service"
if [[ -f $SVC ]] && grep -q '^ExecStart=.*indiserver' "$SVC"; then
    # keep the existing drivers (e.g. indi_celestron_gps), drop any stale
    # pylibcamera entry, then append the driver from this install
    line=$(grep '^ExecStart=' "$SVC" | sed 's/^ExecStart=//')
    new=""
    for tok in $line; do
        [[ $tok == *indi_pylibcamera ]] || new+=" $tok"
    done
    sed -i "s#^ExecStart=.*#ExecStart=${new# } $DRIVER#" "$SVC"
else
    cat > "$SVC" <<EOF
[Unit]
Description=INDI server (camera)
After=multi-user.target

[Service]
User=$RUN_USER
ExecStart=/usr/bin/indiserver $DRIVER
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
fi
systemctl daemon-reload
systemctl enable indiserver.service
systemctl restart indiserver.service

echo "==> Installing MediaMTX (WebRTC live view)"
if [[ ! -x /usr/local/bin/mediamtx ]]; then
    case "$(dpkg --print-architecture)" in
        arm64) MTX_ARCH=arm64 ;;
        armhf) MTX_ARCH=armv7 ;;
        *) echo "unsupported architecture for MediaMTX" >&2; exit 1 ;;
    esac
    MTX_VER=$(curl -s https://api.github.com/repos/bluenviron/mediamtx/releases/latest \
              | grep -oP '"tag_name": "\K[^"]+')
    [[ -n $MTX_VER ]] || { echo "cannot resolve latest MediaMTX release" >&2; exit 1; }
    curl -sL -o /tmp/mediamtx.tar.gz \
        "https://github.com/bluenviron/mediamtx/releases/download/$MTX_VER/mediamtx_${MTX_VER}_linux_${MTX_ARCH}.tar.gz"
    tar -xzf /tmp/mediamtx.tar.gz -C /tmp mediamtx
    install -m 755 /tmp/mediamtx /usr/local/bin/mediamtx
    rm -f /tmp/mediamtx.tar.gz /tmp/mediamtx
fi

echo "==> Installing streaming profiles"
install -d /etc/mediamtx
cat > /etc/mediamtx/planetary.yml <<EOF
# Planetary / alignment profile: fast, low latency, auto exposure
paths:
  cam:
    source: rpiCamera
    rpiCameraWidth: 1296
    rpiCameraHeight: 972
    rpiCameraFPS: 40
EOF
cat > /etc/mediamtx/deepsky.yml <<EOF
# Deep-sky preview profile: long exposure, high gain
paths:
  cam:
    source: rpiCamera
    rpiCameraWidth: 1296
    rpiCameraHeight: 972
    rpiCameraFPS: 1
    rpiCameraShutter: 900000
    rpiCameraGain: 8
EOF
[[ -e /etc/mediamtx/current.yml ]] || \
    ln -sf /etc/mediamtx/planetary.yml /etc/mediamtx/current.yml

cat > /etc/systemd/system/mediamtx.service <<EOF
[Unit]
Description=MediaMTX WebRTC camera streaming
After=network.target

[Service]
ExecStart=/usr/local/bin/mediamtx /etc/mediamtx/current.yml
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > /usr/local/bin/camera-mode <<'EOF'
#!/bin/bash
# Switch the camera between streaming profiles and INDI capture mode.
# Usage: sudo camera-mode {planetary|deepsky|off|status}
case "$1" in
  planetary|deepsky)
    ln -sf "/etc/mediamtx/$1.yml" /etc/mediamtx/current.yml
    systemctl restart mediamtx
    echo "Streaming '$1' on: http://$(hostname -I | awk '{print $1}'):8889/cam"
    echo "Note: while streaming, the camera is NOT available to INDI/Ekos." ;;
  off)
    systemctl stop mediamtx
    echo "Streaming stopped: camera available to INDI/Ekos." ;;
  status)
    systemctl is-active mediamtx && readlink /etc/mediamtx/current.yml \
        || echo "streaming off" ;;
  *)
    echo "Usage: sudo camera-mode {planetary|deepsky|off|status}"; exit 1 ;;
esac
EOF
chmod 755 /usr/local/bin/camera-mode
systemctl daemon-reload

echo
echo "Done. Summary:"
echo "  - INDI:      camera 'pylibcamera' on the indiserver (port 7624);"
echo "               connect from Ekos/KStars to capture FITS frames"
echo "  - live view: sudo camera-mode planetary   (or deepsky)"
echo "               then open http://<pi-address>:8889/cam"
echo "  - stop it:   sudo camera-mode off         (frees the camera"
echo "               for INDI/Ekos - only one can use it at a time)"
