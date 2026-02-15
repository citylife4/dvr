# AGENTS.md — Complete Session Work Log

> This document captures all work performed during the reverse-engineering and
> implementation session for the HiEasy DVR RTSP bridge project. Written for
> continuity — any agent picking this up should be able to understand the full
> context, what was tried, what worked, what failed, and what remains.

---

## 1. Project Goal

The user has a **SVL-AHDSET04** DVR (HiEasy Technology) at `192.168.1.174` with
credentials `admin` / `123456`. The N_Eye mobile app requires payment to view
cameras. The goal evolved through several phases:

1. **Phase 1**: Connect to the DVR and view cameras locally
2. **Phase 2**: Reverse-engineer the proprietary protocol (no RTSP available)
3. **Phase 3**: Achieve working H.264 video streaming
4. **Phase 4**: Package everything for **Raspberry Pi** deployment with an **RTSP server**

---

## 2. DVR Hardware & Network

| Property | Value |
|---|---|
| Model | SVL-AHDSET04 |
| Manufacturer | HiEasy Technology (NOT Xiongmai) |
| IP | 192.168.1.174 |
| MAC | 00:24:b9:bf:11:49 |
| Port 80 | HTTP (web UI) |
| Port 5050 | Command (proprietary TCP) |
| Port 6050 | Media (proprietary TCP) |
| Port 8050 | Mobile client |
| Channels | 4 (ch0–ch3) |
| RTSP | **NOT available** — no RTSP server on the DVR |

---

## 3. Protocol Reverse Engineering

### 3.1 Wire Format

The DVR uses a **proprietary XML-over-TCP protocol** with fixed-size binary headers.

**Header structure** (36 bytes, big-endian):

```
Offset  Size  Field
0x00    4     Magic (CMD: 0x05011154, Media: 0x05011150)
0x04    4     Version (0x00001001)
0x08    4     Transaction ID / Command code
0x0C    4     Field 3 (varies — payload size for media)
0x10    4     Body length (for command channel)
0x14    4     Field 5 (usually 3)
0x18    4     Field 6
0x1C    4     Field 7
0x20    4     Field 8 (MediaSession for media handshake)
```

**Body**: Null-terminated XML with GB2312 encoding declaration, wrapped in
`<Command ID="N">` tags.

### 3.2 Command Flow (Login + Stream)

```
Client                              DVR (port 5050)
  │                                    │
  │─── LoginGetFlag (ID=26) ──────────►│
  │◄── LoginGetFlagReply (ID=27) ──────│  (returns LoginFlag="<nonce>")
  │                                    │
  │─── UserLogin (ID=24) ─────────────►│  (sends LoginFlag="<hash>")
  │◄── UserLoginReply (ID=25) ─────────│  (CmdReply="0" = success)
  │                                    │
  │─── RealStreamCreate (ID=136) ──────►│  (Channel, Mode, Type)
  │◄── RealStreamCreateReply (ID=137) ──│  (returns MediaSession="<id>")
  │                                    │
  │════ Connect to port 6050 ══════════│
  │─── Media handshake header ─────────►│  (magic=0x05011150, field8=MediaSession)
  │◄── Handshake reply ───────────────│
  │                                    │
  │─── RealStreamStart (ID=138) ───────►│  (on port 5050, with MediaSession)
  │◄── RealStreamStartReply (ID=139) ──│
  │                                    │
  │◄══ H.264 data flows on port 6050 ══│
  │                                    │
  │─── HeartBeatNotice (ID=78/79) ─────│  (must be answered periodically)
```

**Key discovery**: `RealStreamStartRequest` (ID 138) is **required** after creating the
stream and connecting to the media port. Without it, no data flows. This was found
through extensive protocol analysis using MITM proxy captures from the N_Eye app.

### 3.3 Media Frame Format

Each media frame on port 6050:

```
[36-byte media header][44-byte sub-header][F3 bytes payload]
```

- `media header[3]` (field index 3, i.e. offset 0x0C) = payload size in bytes
- The 44-byte sub-header contains timestamp, codec info (3 = H.264), frame counter
- The payload starts with a **vendor-specific NAL prefix**: `000001c7` (22 bytes)
  followed by standard H.264 NAL units with 4-byte start codes (`00000001`)

**To extract clean H.264**: Find the first `00 00 00 01` 4-byte start code in the
payload and take everything from there. Skip vendor NAL types `0xC6` and `0xC7`.

### 3.4 Video Specifications (confirmed via ffprobe)

| Property | Value |
|---|---|
| Codec | H.264 Baseline |
| Resolution | 1920×1080 |
| Frame rate | 25 fps |
| Color space | YUV420P |
| Scan | Progressive |
| Level | 4.2 |

---

## 4. Authentication — The Hash Problem

### 4.1 How It Works

The DVR uses challenge-response authentication:
1. Client sends `LoginGetFlag` → DVR returns a numeric nonce string
2. Client must compute `hash(nonce, password)` and send it back as `LoginFlag` in `UserLogin`
3. The hash is a **32-character hex string** (16 bytes)

### 4.2 Hash Algorithm Analysis

The hash is computed inside `HieClientUnit.dll` (PE32, x86, 121 exports). Despite
the class being named `CCodecMD5`, the function `CCodecMD5::Encode` is **NOT called**
during login (proven by hooking). The actual hash function is a **proprietary custom
block cipher**.

**What was tried** (all FAILED to crack the algorithm):
- Collected **300 hash pairs** across 50 nonces × 6 passwords using the DLL as oracle
  (saved to `hash_pairs.json` on Windows side)
- Tested **25+ algorithms** in `crack_hash.py`: MD5, SHA1, SHA256, HMAC-MD5, MD5(nonce+pwd),
  MD5(pwd+nonce), double MD5, XOR variants, Sofia hash, custom concatenations
- Tested block ciphers in `crack_hash2.py`: DES ECB, TEA, XTEA with various key derivations

**Key structural findings** (from analysis):
1. **Two independent 8-byte blocks**: XOR of paired hashes shows the last 8 bytes
   are identical for certain nonce pairs → the hash is split into two halves computed
   independently
2. **Uses `atoi()` on nonce**: Non-numeric nonces like "a", "abc", "test", "hello" don't
   all map to the same hash. "a" and "abc" share both halves. "test" and "hello" share
   both halves. But these two groups differ from each other and from nonce "0".
   → The nonce string is processed character-by-character, not just converted to int
3. **Password affects grouping**: Which numeric nonces share the second-half value
   changes depending on password. For password='123456': nonces (2,3), (4,5), (7,8),
   (10,11) share their last 16 hex chars. For password='': (1,2), (4,5), (7,8), (9,10).
4. **Some passwords produce identical patterns**: 'admin' and '123456789' have exactly
   the same pairing pattern: (0,1), (3,4), (5,6), (8,9) — despite being completely
   different strings. → Password is likely reduced to a small key space before use.

**Conclusion**: The algorithm is a custom block cipher (possibly a Feistel network or
modified DES). Cracking it would require deeper DLL disassembly to find the actual
function called during login and reverse the implementation.

### 4.3 Hash Oracle Solution

Since the algorithm couldn't be cracked, we use the DLL as a **hash oracle**:

1. Start a fake DVR server on `localhost:15050`
2. Feed the real DVR's nonce to the fake server's `LoginGetFlagReply`
3. Point the SDK DLL at the fake server → it computes the hash and sends `UserLogin`
4. Capture the hash from the `LoginFlag` attribute in the intercepted `UserLogin` XML
5. Use the captured hash to authenticate with the real DVR

**Three backends** (tried in order):

| Backend | Platform | How |
|---|---|---|
| DLL direct | Windows (x86) | `ctypes.CDLL("HieClientUnit.dll")` |
| WSL2 interop | WSL2 Linux | Windows `py32/python.exe` runs natively via binfmt_misc |
| Wine + QEMU | ARM Linux (Pi) | `wine py32/python.exe _wine_oracle.py` with QEMU-user-static |

**WSL2 interop gotcha**: When calling Windows Python from WSL, you must convert paths
from Linux (`/mnt/c/temp/...`) to Windows (`C:\temp\...`) format. This is done via
`wslpath -w` with a manual fallback. This bug was found and fixed during testing.

### 4.4 DEVICE_INFO Struct (for DLL calls)

```
Offset  Size    Field
0x000   256     IP address (char[256], null-terminated)
0x100   4       CmdPort (int32, little-endian)
0x104   32      Username (char[32])
0x124   32      Password (char[32])
Total: 0x200 (512 bytes)
```

---

## 5. Files Created (In Git)

### 5.1 `hieasy_dvr/` Python Package

**`hieasy_dvr/__init__.py`**
- Package init, exports `DVRClient`, version `1.0.0`

**`hieasy_dvr/protocol.py`**
- Constants: `CMD_MAGIC`, `MEDIA_MAGIC`, `VERSION`, `HEADER_SIZE`, all command IDs
- `pack_cmd_header(body_len)` — builds 36-byte command header with auto-incrementing txn ID
- `pack_media_header(session_id)` — builds 36-byte media handshake header
- `make_xml(cmd_id, inner)` — builds null-terminated XML command body
- `recv_msg(sock)` — receives one complete header+body message
- `parse_body(body)` — decodes XML body, strips null terminator

**`hieasy_dvr/auth.py`**
- `compute_hash(flag_nonce, username, password)` — public API, tries backends in order
- `_oracle_via_dll()` — Windows ctypes backend
- `_oracle_via_wsl_interop()` — WSL2 backend, with `_wsl_to_win_path()` helper
- `_oracle_via_wine()` — Wine subprocess backend (for Pi)
- `_handle_sdk_client()` — fake DVR server handler (used by DLL backend)
- Fake server runs on `localhost:15050`, responds to LoginGetFlag/UserLogin/Logout

**`hieasy_dvr/client.py`**
- `DVRClient` class with `connect(channel, stream_type)`, `stream()`, `disconnect()`
- `connect()` performs full sequence: TCP connect → login → stream create → media connect → stream start
- Background threads: `_reader_loop()` (reads command messages), `_heartbeat_loop()` (responds to heartbeats)
- `_wait_for(tag)` — waits for a specific XML tag in the message queue
- `stream()` — generator yielding `(codec, h264_bytes)` tuples

**`hieasy_dvr/stream.py`**
- `extract_h264(payload)` — strips vendor NAL prefix (0xC6/0xC7), returns clean H.264
- `iter_frames(sock)` — generator parsing media frames from socket buffer
- Handles partial reads, magic byte synchronization, consecutive timeout detection

**`hieasy_dvr/_wine_oracle.py`**
- Standalone script meant to run under Windows Python (via Wine on Pi)
- Called as: `python.exe _wine_oracle.py <nonce> <username> <password>`
- Outputs: `HASH=<32hex>` on stdout
- Contains its own fake server implementation (must be self-contained for Wine execution)

### 5.2 Application Scripts

**`dvr_feeder.py`**
- Single-channel H.264 feeder, outputs raw H.264 to stdout
- Designed to pipe into ffmpeg: `dvr_feeder.py --channel 0 | ffmpeg -f h264 -i pipe:0 ...`
- CLI args: `--channel`, `--stream-type`, `--host`, `--cmd-port`, `--media-port`, `--username`, `--password`, `-v`
- All settings overridable via environment variables (`DVR_HOST`, etc.)
- Handles SIGTERM/SIGINT for graceful shutdown

**`dvr_rtsp_bridge.py`**
- Multi-channel manager: spawns dvr_feeder + ffmpeg pipelines for each channel
- Auto-restarts crashed channels with 3-second backoff
- CLI args: `--channels 0 1 2 3`, `--rtsp-url`, `--stream-type`, `-v`
- Alternative to mediamtx's `runOnDemand` for always-on streaming

### 5.3 Deployment Files

**`mediamtx.yml`**
- mediamtx RTSP server configuration
- Enables: RTSP (:8554), RTMP (:1935), HLS (:8888), WebRTC (:8889), API (:9997)
- 4 paths: `ch0`–`ch3`, each using `runOnDemand` to start feeder+ffmpeg on-demand
- `runOnDemandCloseAfter: 10s` stops the pipeline 10s after last client disconnects
- `runOnDemandStartTimeout: 30s` allows time for hash oracle + DVR connection

**`dvr-rtsp.service`**
- systemd unit file for running mediamtx as a service
- Runs as `dvr` system user from `/opt/dvr`
- Sets all DVR environment variables (host, ports, credentials, SDK dir, Wine prefix)
- Security hardening: `NoNewPrivileges`, `ProtectSystem=strict`
- Logs to journal (`SyslogIdentifier=dvr-rtsp`)

**`deploy.sh`**
- One-command SSH deployment: `./deploy.sh pi@<ip>`
- Auto-detects Pi architecture (aarch64, armv7l, armv6l)
- Step 1: Installs python3, ffmpeg
- Step 2: Installs Wine + QEMU-user-static (ARM only)
- Step 3: Creates `/opt/dvr` directory structure
- Step 4: Downloads correct mediamtx binary (v1.11.3) from GitHub
- Step 5: SCPs Python package + scripts
- Step 6: SCPs SDK DLLs + 32-bit Python from `dvr_tools_windows/`
- Step 7: Creates systemd `dvr` user, installs service, enables it
- Step 8: Runs connectivity tests (mediamtx binary, ffmpeg, python3, DVR port 5050)

**`requirements.txt`**
- No external dependencies — stdlib only (socket, struct, threading, ctypes, subprocess, etc.)

**`.gitignore`**
- Excludes: `__pycache__/`, `*.pyc`, `venv/`, `*.h264`, `*.log`, `*.json`
- Excludes: `dvr_tools_windows` symlink, all analysis scripts (crack_hash*.py, disasm*.py, etc.)

**`README.md`**
- Architecture diagram, quick deploy instructions, manual usage examples
- Configuration reference (environment variables)
- RTSP stream URLs, video specs, project structure

---

## 6. Files NOT in Git (Analysis/RE Scripts)

These live in the workspace but are gitignored. They were used during reverse engineering:

| File | Purpose |
|---|---|
| `crack_hash.py` | Tests 25+ hash algorithms against 300 collected pairs |
| `crack_hash2.py` | Deep structural analysis: DES, TEA, XTEA block cipher tests |
| `disasm_deep.py` | Deep DLL disassembly using capstone |
| `disasm_funcs.py` | Function-level DLL disassembly |
| `dvr_connect.py` | Early connection test script |
| `hieasy_client.py` | Earlier standalone client prototype |
| `mitm_proxy.py` | MITM TCP proxy for capturing N_Eye app traffic |
| `parse_traffic.py` | Parses captured MITM traffic dumps |

On Windows (`/mnt/c/temp/dvr_tools/` = `dvr_tools_windows` symlink):

| File | Purpose |
|---|---|
| `dvr_live.py` | **Original working viewer** (hash oracle + streaming + ffplay display) |
| `stream_test.py` | First working end-to-end stream test |
| `collect_hashes.py` | Batch hash collection (produced 300 pairs) |
| `hash_pairs.json` | 300 (nonce, password, hash) triples |
| `hook_md5.py` | DLL hook proving CCodecMD5::Encode is NOT called during login |
| `test_md5_direct.py` | Direct DLL MD5 function calls |
| `test_md5_direct2.py` | Direct DLL function testing variant |
| `allinone_hash.py` | Batch hash capture (earlier version) |
| `batch_hash2.py` | Another batch hash variant |
| `fake_dvr.py` | Fake DVR server (early version) |
| `fake_dvr2.py` | Fake DVR server (improved) |
| `relay_login.py` | Login relay between real DVR and SDK |
| `relay_login2.py` | Login relay variant |
| `HieClientUnit.dll` | The SDK DLL (PE32, x86, 121 exports) |
| `py32/` | 32-bit Python 3.10.11 (for loading x86 DLL) |
| Various `.dll` files | SDK dependencies (avcodec, avformat, avutil, pthread, etc.) |

---

## 7. Test Results

### 7.1 Hash Collection (Windows)
- Ran `collect_hashes.py` with 32-bit Python
- Collected 300 hash pairs: 50 nonces × 6 passwords ("123456", "admin", "1", "", "000000", "123456789")
- All pairs saved to `hash_pairs.json`

### 7.2 Hash Cracking (Linux)
- `crack_hash.py`: 0/300 matches across 25+ algorithm variants
- `crack_hash2.py`: DES crashed on empty key, TEA/XTEA 0 matches
- Confirmed: **custom block cipher, not any standard algorithm**

### 7.3 Feeder Test (WSL2)
- `dvr_feeder.py --channel 0 -v` → **200KB of valid H.264** captured to file
- Hash oracle via WSL2 interop completed in ~1 second
- `ffprobe` confirmed: H.264 Baseline, 1920×1080, 25fps, yuv420p
- One harmless warning: `sps_id 32 out of range` (from vendor NAL prefix)
- BrokenPipeError on stdout (expected when `head` closes pipe) handled cleanly

### 7.4 Compilation Check
- All Python files pass `py_compile` with zero errors

---

## 8. Known Issues & Future Work

### 8.1 Hash Algorithm (Unsolved)
The proprietary hash algorithm was not cracked. Key leads for future attempts:
- The actual hash function in the DLL is NOT `CCodecMD5::Encode` — need to trace from
  `HieClient_UserLogin` to find the real function
- Hash is two independent 8-byte blocks (probably a 64-bit block cipher used twice)
- Passwords 'admin' and '123456789' produce identical pairing patterns → password is
  reduced to a small key space (maybe only a few bits matter)
- If the algorithm were cracked, `auth.py` could use a pure Python implementation
  instead of the Wine/DLL oracle, eliminating the ~500MB Wine+QEMU dependency

### 8.2 Wine + QEMU on Raspberry Pi (Untested)
- The Wine backend (`_oracle_via_wine`) has NOT been tested on actual ARM hardware
- Wine + QEMU-user-static installation on Raspberry Pi OS may require manual setup
- Hash oracle latency under QEMU emulation is unknown (expected ~3-5 seconds)
- Login only happens once per session, so oracle latency is acceptable

### 8.3 Multi-Channel Limitations
- The DVR may not support 4 simultaneous stream sessions — untested
- `dvr_rtsp_bridge.py` staggers connections by 2 seconds to avoid overloading
- On-demand mode (`mediamtx.yml` with `runOnDemand`) is preferred: only the requested
  channel connects

### 8.4 Stream Reliability
- No reconnection logic in `DVRClient` if the DVR drops the connection
- `dvr_rtsp_bridge.py` handles this at the process level (restart feeder+ffmpeg)
- `mediamtx.yml` has `runOnDemandRestart: yes` for the on-demand case
- Heartbeat handling is implemented but not stress-tested for long sessions

### 8.5 Vendor NAL Prefix
- Each video frame starts with a 22-byte vendor-specific NAL unit (`000001c7` or `000001c6`)
- `extract_h264()` strips these by finding the first `00000001` 4-byte start code
- ffprobe reports `sps_id 32 out of range` warning (harmless, from the vendor NAL
  leaking before start code detection kicks in on the very first data)

---

## 9. Environment Details

| Component | Details |
|---|---|
| Development OS | WSL2 (Ubuntu 22.04) on Windows |
| Kernel | 6.6.87.2-microsoft-standard-WSL2 |
| Python (Linux) | 3.10.12 in venv at `/home/valverde/dev/dvr/venv` |
| Python (Windows) | 3.10.11 32-bit at `/mnt/c/temp/dvr_tools/py32/` |
| Project root | `/home/valverde/dev/dvr` |
| Windows tools | `/mnt/c/temp/dvr_tools/` (symlinked as `dvr_tools_windows`) |
| Git repo | Initialized on `master` branch, 1 commit (`b5e8202`) |
| Target | Raspberry Pi (model TBD, 64-bit recommended) |
| Deploy path | `/opt/dvr` on Pi |
| mediamtx version | v1.11.3 (set in deploy.sh) |

---

## 10. Quick Reference — Deploying to Pi

```bash
# From the project directory on the development machine:
./deploy.sh pi@192.168.1.XXX

# On the Pi:
sudo systemctl start dvr-rtsp
sudo systemctl status dvr-rtsp
sudo journalctl -u dvr-rtsp -f

# From any client on the LAN:
ffplay rtsp://<pi-ip>:8554/ch0
vlc rtsp://<pi-ip>:8554/ch0
```

---

## 11. Architecture Decision Log

1. **Why not RTSP directly from DVR?** — DVR has no RTSP server. Only proprietary TCP protocol.
2. **Why mediamtx?** — Lightweight Go binary, single file, ARM builds, supports on-demand streaming, RTSP+RTMP+HLS+WebRTC.
3. **Why hash oracle instead of pure Python auth?** — Hash algorithm is an unknown custom block cipher. 300 pairs tested against 25+ algorithms with zero matches.
4. **Why Wine+QEMU on Pi?** — The SDK DLL is PE32 x86. Wine runs Windows executables. QEMU-user-static translates x86 instructions to ARM via binfmt_misc.
5. **Why feeder+ffmpeg pipe?** — `dvr_feeder.py` handles the proprietary protocol and outputs clean H.264. `ffmpeg` handles RTSP publishing. Clean separation of concerns.
6. **Why on-demand vs. always-on?** — On-demand (`runOnDemand`) saves resources: DVR connection only made when a viewer connects. Always-on (`dvr_rtsp_bridge.py`) provided as alternative.
7. **Why systemd service?** — Auto-start on boot, auto-restart on crash, journal logging. Standard Linux service management.
