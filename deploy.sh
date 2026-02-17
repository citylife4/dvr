#!/bin/bash
#
# deploy.sh — Deploy DVR RTSP Bridge (Local Installation)
#
# Usage:
#   ./deploy.sh [dvr-ip]
#
# Examples:
#   ./deploy.sh                    # uses DVR_HOST from .env or prompts
#   ./deploy.sh 192.168.1.174      # explicit DVR IP
#
set -euo pipefail

DVR_IP="${1:-}"
DEPLOY_DIR="/opt/dvr"
MEDIAMTX_VERSION="1.11.3"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Resolve DVR IP: CLI arg > .env file > prompt
if [[ -z "$DVR_IP" && -f "$SCRIPT_DIR/.env" ]]; then
    DVR_IP=$(grep -oP '(?<=^DVR_HOST=).+' "$SCRIPT_DIR/.env" 2>/dev/null || true)
fi
if [[ -z "$DVR_IP" ]]; then
    read -rp "DVR IP address: " DVR_IP
fi

echo "=== DVR Dashboard — Local Installation ==="
echo "DVR:     $DVR_IP"
echo "Deploy:  $DEPLOY_DIR"
echo ""

# Detect architecture
ARCH=$(uname -m)
echo "Architecture: $ARCH"

case "$ARCH" in
    aarch64) MEDIAMTX_ARCH="linux_arm64v8" ;;
    armv7l)  MEDIAMTX_ARCH="linux_armv7" ;;
    armv6l)  MEDIAMTX_ARCH="linux_armv6" ;;
    x86_64)  MEDIAMTX_ARCH="linux_amd64" ;;
    *)       echo "ERROR: Unsupported architecture: $ARCH"; exit 1 ;;
esac

echo ""
echo "--- Step 1: Install system dependencies ---"
sudo apt-get update -qq && sudo apt-get install -y -qq \
    python3 ffmpeg curl

echo ""
echo "--- Step 2: Create deploy directory ---"
sudo mkdir -p $DEPLOY_DIR/hieasy_dvr $DEPLOY_DIR/web $DEPLOY_DIR/cache $DEPLOY_DIR/recordings
sudo chown -R $(whoami):$(whoami) $DEPLOY_DIR

echo ""
echo "--- Step 3: Download mediamtx ---"
MEDIAMTX_URL="https://github.com/bluenviron/mediamtx/releases/download/v${MEDIAMTX_VERSION}/mediamtx_v${MEDIAMTX_VERSION}_${MEDIAMTX_ARCH}.tar.gz"
echo "Downloading: $MEDIAMTX_URL"
cd $DEPLOY_DIR
curl -sL "$MEDIAMTX_URL" | tar xz mediamtx
chmod +x mediamtx
cd "$SCRIPT_DIR"

echo ""
echo "--- Step 4: Copy application files ---"
sudo cp "$SCRIPT_DIR/hieasy_dvr/"*.py "$DEPLOY_DIR/hieasy_dvr/"
sudo cp "$SCRIPT_DIR/dvr_feeder.py" "$SCRIPT_DIR/dvr_rtsp_bridge.py" \
    "$SCRIPT_DIR/dvr_web.py" "$SCRIPT_DIR/mediamtx.yml" \
    "$DEPLOY_DIR/"
sudo cp "$SCRIPT_DIR/web/"*.html "$DEPLOY_DIR/web/"

echo ""
echo "--- Step 5: Write environment file ---"
sudo tee $DEPLOY_DIR/dvr.env > /dev/null << ENVEOF
DVR_HOST=$DVR_IP
DVR_CMD_PORT=5050
DVR_MEDIA_PORT=6050
DVR_USERNAME=admin
DVR_PASSWORD=123456
DVR_WEB_PORT=8080

# Recording (set DVR_RECORD_ENABLED=true to enable)
DVR_RECORD_ENABLED=false
DVR_RECORD_CHANNELS=0
DVR_RECORD_SEGMENT_MIN=15
DVR_RECORD_DIR=$DEPLOY_DIR/recordings
DVR_RECORD_RETENTION_HR=24
DVR_RECORD_SCHEDULE=0-23

# Google Drive upload (optional — see hieasy_dvr/gdrive.py for setup)
DVR_GDRIVE_ENABLED=false
DVR_GDRIVE_CREDENTIALS=$DEPLOY_DIR/gdrive-credentials.json
DVR_GDRIVE_FOLDER_ID=
DVR_GDRIVE_DELETE_LOCAL=false
ENVEOF

echo ""
echo "--- Step 6: Install systemd service ---"
sudo useradd -r -s /usr/sbin/nologin -d $DEPLOY_DIR dvr 2>/dev/null || true
sudo chown -R dvr:dvr $DEPLOY_DIR

# Remove old split services if present
sudo systemctl stop dvr-rtsp dvr-web 2>/dev/null || true
sudo systemctl disable dvr-rtsp dvr-web 2>/dev/null || true
sudo rm -f /etc/systemd/system/dvr-rtsp.service /etc/systemd/system/dvr-web.service

sudo cp "$SCRIPT_DIR/dvr.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable dvr.service

echo ""
echo "--- Step 7: Connectivity check ---"
cd $DEPLOY_DIR
echo 'mediamtx:'; ./mediamtx --help >/dev/null 2>&1 && echo '  OK' || echo '  FAILED'
echo 'ffmpeg:';   ffmpeg -version 2>/dev/null | head -1 || echo '  NOT FOUND'
echo 'python3:';  python3 -c 'import socket; print("  OK")'
echo "DVR ($DVR_IP:5050):"
python3 -c "
import socket; s=socket.socket(); s.settimeout(3)
try: s.connect(('$DVR_IP',5050)); print('  REACHABLE'); s.close()
except: print('  NOT REACHABLE — check DVR IP and network')
"
cd "$SCRIPT_DIR"

echo ""
echo "--- Step 8: Start service ---"
sudo systemctl restart dvr.service
sleep 3

# Health check
STATUS=$(systemctl is-active dvr.service 2>/dev/null || true)
if [[ "$STATUS" == "active" ]]; then
    echo "  ✓ dvr.service is running"
else
    echo "  ✗ dvr.service failed to start"
    echo "  Check logs: sudo journalctl -u dvr -n 20"
    exit 1
fi

# Check web dashboard responds
if curl -sf --max-time 5 http://localhost:8080/api/config-types > /dev/null 2>&1; then
    echo "  ✓ Web dashboard is responding"
else
    echo "  ⚠ Web dashboard not yet responding (may need a few seconds)"
fi

# Check mediamtx responds
if curl -sf --max-time 5 http://localhost:9997/v3/paths/list > /dev/null 2>&1; then
    echo "  ✓ mediamtx RTSP server is running"
else
    echo "  ⚠ mediamtx not yet responding (may need a few seconds)"
fi

echo ""
echo "=== Installation Complete — Service Running ==="
echo ""
echo "Dashboard: http://<this-ip>:8080/"
echo "Settings:  http://<this-ip>:8080/settings"
echo "RTSP:      rtsp://<this-ip>:8554/ch{0..3}"
echo ""
echo "Manage:    sudo systemctl {start|stop|restart|status} dvr"
echo "Logs:      sudo journalctl -u dvr -f"
echo ""
echo "To change DVR IP later: edit /opt/dvr/dvr.env and restart:"
echo "  sudo systemctl restart dvr"
