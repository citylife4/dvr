# HiEasy DVR RTSP Bridge

Connects to HiEasy Technology DVRs (SVL-AHDSET04 and similar) via their
proprietary TCP protocol and re-publishes camera streams as **standard RTSP**.
Includes a **web dashboard** with live view, configuration browser, and recording management.

No Windows DLLs, no Wine — pure Python authentication.

## Architecture

```
DVR                      Raspberry Pi (or any Linux)              Clients
┌────────────┐     ┌─────────────────────────────────┐     ┌──────────────┐
│ Port 5050  │◄───►│  dvr_feeder.py → ffmpeg          │     │ VLC / ffplay │
│ (command)  │     │                  ↓               │     │ Web browser  │
│ Port 6050  │◄───►│              mediamtx            │◄───►│ Home Assist. │
│ (media)    │     │   RTSP :8554  HLS :8888  WR :8889│     │ etc.         │
└────────────┘     │   Web dashboard  :8080           │     └──────────────┘
                   │   Recording scheduler            │
                   └─────────────────────────────────┘
```

A single `dvr.service` runs `dvr_web.py`, which manages mediamtx as a child
process and runs the recording scheduler. Streams start **on-demand** — the DVR
connection is only made when a client connects.

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

# 4. Deploy as a service
./deploy.sh 192.168.1.174    # DVR IP (auto-installs everything)
```

## Deploy

```bash
./deploy.sh [dvr-ip]
```

The deploy script:
1. Installs Python 3, ffmpeg, curl
2. Downloads the correct mediamtx binary for your architecture
3. Copies all application files to `/opt/dvr`
4. Creates the systemd service and starts it
5. Runs health checks

Supported architectures: aarch64, armv7l, armv6l, x86_64.

## Accessing Streams

**RTSP** (VLC, ffplay, Home Assistant, Blue Iris, etc.):
```
rtsp://<host>:8554/ch0   # Channel 0
rtsp://<host>:8554/ch1   # Channel 1
rtsp://<host>:8554/ch2   # Channel 2
rtsp://<host>:8554/ch3   # Channel 3
```

**Web dashboard** (live view, settings, recordings):
```
http://<host>:8080/             # 4-channel live grid (WebRTC)
http://<host>:8080/settings     # DVR configuration browser
http://<host>:8080/recordings   # Recording management
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
| `DVR_WEB_PORT` | `8080` | Web dashboard port |

### Recording

| Variable | Default | Description |
|---|---|---|
| `DVR_RECORD_ENABLED` | `false` | Enable recording on launch |
| `DVR_RECORD_CHANNELS` | `0` | Channels to record (comma-separated) |
| `DVR_RECORD_SEGMENT_MIN` | `15` | Segment duration in minutes |
| `DVR_RECORD_STREAM_TYPE` | `1` | 1=main (HD), 2=sub (SD) |
| `DVR_RECORD_DIR` | `/opt/dvr/recordings` | Local storage path |
| `DVR_RECORD_RETENTION_HR` | `24` | Hours to keep files (0=forever) |
| `DVR_RECORD_SCHEDULE` | `0-23` | Hour ranges to record |

### Google Drive Upload (optional)

| Variable | Default | Description |
|---|---|---|
| `DVR_GDRIVE_ENABLED` | `false` | Enable Google Drive upload |
| `DVR_GDRIVE_CREDENTIALS` | *(path)* | Service account JSON key file |
| `DVR_GDRIVE_FOLDER_ID` | | Target folder ID from Drive URL |
| `DVR_GDRIVE_DELETE_LOCAL` | `false` | Delete local file after upload |

See `hieasy_dvr/gdrive.py` for Google Drive setup instructions.

To change settings after deployment:
```bash
sudo nano /opt/dvr/dvr.env
sudo systemctl restart dvr
```

## Service Management

```bash
sudo systemctl start dvr
sudo systemctl stop dvr
sudo systemctl restart dvr
sudo systemctl status dvr
sudo journalctl -u dvr -f       # follow logs
```

## REST API

| Endpoint | Method | Description |
|---|---|---|
| `/api/config` | GET | All DVR config types (17 categories) |
| `/api/config/<mc>` | GET | Specific config type by MainCmd |
| `/api/config-types` | GET | Available config type list |
| `/api/status` | GET | DVR status summary |
| `/api/recordings` | GET | List local recording files |
| `/api/recordings/status` | GET | Recorder + upload status |
| `/api/recordings/start` | POST | Start recording |
| `/api/recordings/stop` | POST | Stop recording |
| `/api/recordings/<ch>/<file>` | DELETE | Delete a single recording |
| `/api/recordings/delete-all` | POST | Delete all recordings |

## Project Structure

```
hieasy_dvr/             Python package — DVR protocol + auth
├── __init__.py
├── protocol.py         Wire protocol (36-byte headers, XML commands)
├── auth.py             Pure Python DES authentication
├── client.py           DVRClient — stream connections
├── config.py           DVRConfigClient — GetCfg for 17 config types
├── stream.py           H.264 frame extraction
├── recorder.py         Recording scheduler (ffmpeg segments + upload)
└── gdrive.py           Google Drive upload via service account

dvr_feeder.py           Single-channel H.264 feeder (stdout pipe)
dvr_web.py              Web dashboard + REST API + mediamtx manager
dvr.service             Systemd service (single unified service)
mediamtx.yml            mediamtx RTSP server config
deploy.sh               One-command deployment with health checks
.env.example            Configuration template

web/
├── index.html          4-channel live grid viewer (WebRTC)
├── settings.html       Read-only DVR configuration dashboard
└── recordings.html     Recording management dashboard
```

## Requirements

- Python 3.8+ (stdlib only — no pip packages needed)
- ffmpeg (for RTSP publishing and recording)
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
