# HiEasy DVR RTSP Bridge

Connects to HiEasy Technology DVRs (SVL-AHDSET04 and similar) via their
proprietary TCP protocol and re-publishes camera streams as **standard RTSP**.
Includes a **web viewer** for all 4 channels in a browser.

No Windows DLLs, no Wine — pure Python authentication.

## Architecture

```
DVR                      Raspberry Pi (or any Linux)              Clients
┌────────────┐     ┌─────────────────────────────────┐     ┌──────────────┐
│ Port 5050  │◄───►│  dvr_feeder.py → ffmpeg          │     │ VLC / ffplay │
│ (command)  │     │                  ↓               │     │ Web browser  │
│ Port 6050  │◄───►│              mediamtx            │◄───►│ Home Assist. │
│ (media)    │     │   RTSP :8554  HLS :8888  WR :8889│     │ etc.         │
└────────────┘     │   Web viewer :8080               │     └──────────────┘
                   └─────────────────────────────────┘
```

## Quick Start

```bash
# 1. Clone
git clone <this-repo> && cd dvr

# 2. Configure — set your DVR's IP address
cp .env.example .env
nano .env           # change DVR_HOST to your DVR's IP

# 3. Test a single channel
DVR_HOST=192.168.1.x python3 dvr_feeder.py -c 0 -v 2>/dev/null | \
  ffmpeg -fflags +genpts -r 25 -f h264 -i pipe:0 -c copy -t 5 test.mp4

# 4. Deploy as a service (on Pi or any Linux)
./deploy.sh user@hostname [dvr-ip]
```

## Deploy to Raspberry Pi

```bash
./deploy.sh pi@192.168.1.177 192.168.1.174
```

This will install Python 3, ffmpeg, mediamtx, copy all files, write the
environment config, and enable two systemd services:

| Service | Port | Purpose |
|---|---|---|
| `dvr-rtsp` | 8554 (RTSP), 8888 (HLS), 8889 (WebRTC) | mediamtx + DVR bridge |
| `dvr-web` | 8080 | 4-channel web viewer |

Streams start **on-demand** — the DVR connection is only made when a client connects.

```bash
# Start / stop
sudo systemctl start dvr-rtsp dvr-web
sudo systemctl stop dvr-rtsp dvr-web

# Logs
sudo journalctl -u dvr-rtsp -f
```

## Accessing Streams

**RTSP** (VLC, ffplay, Home Assistant, Blue Iris, etc.):
```
rtsp://<host>:8554/ch0
rtsp://<host>:8554/ch1
rtsp://<host>:8554/ch2
rtsp://<host>:8554/ch3
```

**Web viewer** (all 4 channels in a 2×2 grid):
```
http://<host>:8080/
```

**HLS** (for embedding in web pages):
```
http://<host>:8888/ch0/
```

## Configuration

All settings are in `/opt/dvr/dvr.env` (or `.env` locally):

| Variable | Default | Description |
|---|---|---|
| `DVR_HOST` | *(required)* | DVR IP address |
| `DVR_CMD_PORT` | `5050` | Command port |
| `DVR_MEDIA_PORT` | `6050` | Media port |
| `DVR_USERNAME` | `admin` | Username |
| `DVR_PASSWORD` | `123456` | Password |
| `DVR_WEB_PORT` | `8080` | Web viewer port |

To change the DVR IP after deployment:
```bash
sudo nano /opt/dvr/dvr.env
sudo systemctl restart dvr-rtsp dvr-web
```

## Project Structure

```
hieasy_dvr/             Python package — DVR protocol + auth
├── __init__.py
├── protocol.py         Wire protocol (36-byte headers, XML commands)
├── auth.py             Pure Python DES authentication
├── client.py           DVRClient class
├── stream.py           H.264 frame extraction
└── _wine_oracle.py     Legacy Wine/DLL fallback (unused)

dvr_feeder.py           Single-channel H.264 feeder (stdout pipe)
dvr_rtsp_bridge.py      Multi-channel always-on bridge
dvr_web.py              Web viewer HTTP server
web/index.html          4-channel grid viewer (WebRTC)
mediamtx.yml            mediamtx RTSP server config
dvr-rtsp.service        systemd service (mediamtx)
dvr-web.service         systemd service (web viewer)
deploy.sh               One-command deployment
.env.example            Configuration template
```

## Requirements

- Python 3.8+ (stdlib only — no pip packages needed)
- ffmpeg
- mediamtx (downloaded automatically by `deploy.sh`)

## Video Specs

| Property | Value |
|---|---|
| Codec | H.264 Baseline |
| Resolution | 1920×1080 |
| Frame rate | 25 fps |
| Color | YUV420P, progressive |

## How Authentication Works

The DVR uses challenge-response with a **modified DES** cipher. The three
non-standard modifications vs. standard DES are:

1. **LSB-first bit ordering** (standard DES is MSB-first)
2. **LSB-first S-box output** extraction
3. **No L/R swap before Final Permutation** (FP applied to L‖R, not R‖L)

All permutation tables, S-boxes, and the key schedule are standard DES.
See `AGENTS.md` for the full reverse-engineering story.
