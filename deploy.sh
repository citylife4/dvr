#!/bin/bash
#
# deploy.sh — Deploy DVR RTSP Bridge to Raspberry Pi
#
# Usage:
#   ./deploy.sh pi@<hostname> [dvr-ip]
#
# Examples:
#   ./deploy.sh pi@192.168.1.177                  # uses DVR_HOST from .env or prompts
#   ./deploy.sh pi@192.168.1.177 192.168.1.174    # explicit DVR IP
#
set -euo pipefail

PI_HOST="${1:?Usage: $0 pi@hostname [dvr-ip]}"
DVR_IP="${2:-}"
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

echo "=== DVR RTSP Bridge Deployment ==="
echo "Target:  $PI_HOST"
echo "DVR:     $DVR_IP"
echo "Deploy:  $DEPLOY_DIR"
echo ""

# Detect architecture
ARCH=$(ssh "$PI_HOST" "uname -m")
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
ssh "$PI_HOST" "sudo apt-get update -qq && sudo apt-get install -y -qq \
    python3 ffmpeg"

echo ""
echo "--- Step 2: Create deploy directory ---"
ssh "$PI_HOST" "sudo mkdir -p $DEPLOY_DIR/hieasy_dvr $DEPLOY_DIR/web && \
    sudo chown -R \$(whoami):\$(whoami) $DEPLOY_DIR"

echo ""
echo "--- Step 3: Download mediamtx ---"
MEDIAMTX_URL="https://github.com/bluenviron/mediamtx/releases/download/v${MEDIAMTX_VERSION}/mediamtx_v${MEDIAMTX_VERSION}_${MEDIAMTX_ARCH}.tar.gz"
echo "Downloading: $MEDIAMTX_URL"
ssh "$PI_HOST" "cd $DEPLOY_DIR && \
    curl -sL '$MEDIAMTX_URL' | tar xz mediamtx && \
    chmod +x mediamtx"

echo ""
echo "--- Step 4: Copy application files ---"
scp -r "$SCRIPT_DIR/hieasy_dvr/"*.py "$PI_HOST:$DEPLOY_DIR/hieasy_dvr/"
scp "$SCRIPT_DIR/dvr_feeder.py" "$SCRIPT_DIR/dvr_rtsp_bridge.py" \
    "$SCRIPT_DIR/dvr_web.py" "$SCRIPT_DIR/mediamtx.yml" \
    "$PI_HOST:$DEPLOY_DIR/"
scp "$SCRIPT_DIR/web/index.html" "$PI_HOST:$DEPLOY_DIR/web/"

echo ""
echo "--- Step 5: Write environment file ---"
ssh "$PI_HOST" "cat > $DEPLOY_DIR/dvr.env << 'ENVEOF'
DVR_HOST=$DVR_IP
DVR_CMD_PORT=5050
DVR_MEDIA_PORT=6050
DVR_USERNAME=admin
DVR_PASSWORD=123456
DVR_WEB_PORT=8080
ENVEOF"

echo ""
echo "--- Step 6: Install systemd services ---"
scp "$SCRIPT_DIR/dvr-rtsp.service" "$SCRIPT_DIR/dvr-web.service" \
    "$PI_HOST:/tmp/"
ssh "$PI_HOST" "
    sudo useradd -r -s /usr/sbin/nologin -d $DEPLOY_DIR dvr 2>/dev/null || true
    sudo chown -R dvr:dvr $DEPLOY_DIR

    sudo mv /tmp/dvr-rtsp.service /tmp/dvr-web.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable dvr-rtsp.service dvr-web.service
"

echo ""
echo "--- Step 7: Connectivity check ---"
ssh "$PI_HOST" "
    cd $DEPLOY_DIR
    echo 'mediamtx:'; ./mediamtx --help >/dev/null 2>&1 && echo '  OK' || echo '  FAILED'
    echo 'ffmpeg:';   ffmpeg -version 2>/dev/null | head -1 || echo '  NOT FOUND'
    echo 'python3:';  python3 -c 'import socket; print(\"  OK\")'
    echo 'DVR ($DVR_IP:5050):'
    python3 -c \"
import socket; s=socket.socket(); s.settimeout(3)
try: s.connect(('$DVR_IP',5050)); print('  REACHABLE'); s.close()
except: print('  NOT REACHABLE')
\"
"

echo ""
echo "=== Deployment Complete ==="
echo ""
echo "Start:   ssh $PI_HOST 'sudo systemctl start dvr-rtsp dvr-web'"
echo "Status:  ssh $PI_HOST 'sudo systemctl status dvr-rtsp'"
echo "Logs:    ssh $PI_HOST 'sudo journalctl -u dvr-rtsp -f'"
echo ""
echo "Streams: rtsp://<pi-ip>:8554/ch{0..3}"
echo "Web UI:  http://<pi-ip>:8080/"
echo ""
echo "To change DVR IP later: edit /opt/dvr/dvr.env and restart services"
