#!/usr/bin/env python3
"""
DVR Web Dashboard — serves live view + config dashboard + REST API.
Optionally manages mediamtx as a child process for a single-service deployment.

Endpoints:
  /                      → Live view (4-channel WebRTC grid)
  /settings              → Configuration dashboard
  /recordings            → Recording status & file list
  /api/config            → JSON: all config types from DVR
  /api/config/<main_cmd> → JSON: specific config type
  /api/status            → JSON: DVR status summary
  /api/config-types      → JSON: available config type list (no DVR needed)
  /api/recordings        → JSON: list of local recording files
  /api/recordings/status → JSON: recorder + upload status
  /api/recordings/start  → POST: start recording
  /api/recordings/stop   → POST: stop recording
  /api/recordings/download/<ch>/<file> → Download a recording
  /<static files>        → Files from web/ directory

Port: $DVR_WEB_PORT (default 8080)
"""

import os
import sys
import json
import time
import signal
import http.server
import threading
import subprocess
import logging
import mimetypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format='[%(name)s] %(message)s',
)

from hieasy_dvr.config import DVRConfigClient, CONFIG_TYPES
from hieasy_dvr.recorder import RecordingScheduler

PORT = int(os.environ.get('DVR_WEB_PORT', 8080))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, 'web')
CACHE_DIR = os.path.join(BASE_DIR, 'cache')

_recorder = RecordingScheduler()

# ── Disk-backed config cache ──────────────────────────

os.makedirs(CACHE_DIR, exist_ok=True)

_dvr_client = None
_dvr_lock = threading.Lock()       # serializes all DVR access
_config_cache = {}                 # mc → (data, timestamp)
_cache_lock = threading.Lock()
_CACHE_TTL = 30  # seconds (memory)


def _load_disk_cache(mc):
    """Load a config from disk cache (JSON file)."""
    path = os.path.join(CACHE_DIR, f'{mc}.json')
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _save_disk_cache(mc, data):
    """Save a config to disk cache."""
    path = os.path.join(CACHE_DIR, f'{mc}.json')
    try:
        with open(path, 'w') as f:
            json.dump(data, f, ensure_ascii=False)
    except OSError:
        pass


def _ensure_client():
    """Ensure a connected DVR client exists. Must hold _dvr_lock."""
    global _dvr_client
    if _dvr_client is not None and _dvr_client._sock is not None:
        return
    if _dvr_client:
        _dvr_client.close()
    _dvr_client = DVRConfigClient()
    _dvr_client.connect()


def _get_config(main_cmd):
    """Get a single config from DVR with memory + disk caching."""
    global _dvr_client
    now = time.time()
    info = CONFIG_TYPES.get(main_cmd, {})

    def _enrich(data):
        data['type_name'] = info.get('name', f'Config {main_cmd}')
        data['type_icon'] = info.get('icon', '📋')
        data['type_description'] = info.get('description', '')
        return data

    # 1. Check memory cache
    with _cache_lock:
        if main_cmd in _config_cache:
            data, ts = _config_cache[main_cmd]
            if now - ts < _CACHE_TTL:
                return data

    # 2. Query DVR (serialized)
    with _dvr_lock:
        # Double-check memory cache
        with _cache_lock:
            if main_cmd in _config_cache:
                data, ts = _config_cache[main_cmd]
                if now - ts < _CACHE_TTL:
                    return data

        for attempt in range(2):
            try:
                _ensure_client()
                data = _dvr_client.get_config(main_cmd)
                _enrich(data)
                with _cache_lock:
                    _config_cache[main_cmd] = (data, time.time())
                _save_disk_cache(main_cmd, data)
                return data
            except Exception:
                if _dvr_client:
                    _dvr_client.close()
                    _dvr_client = None
                if attempt == 1:
                    # 3. Fall back to disk cache
                    cached = _load_disk_cache(main_cmd)
                    if cached:
                        cached['_cached'] = True
                        return _enrich(cached)
                    raise


def _get_all_configs():
    """Get all configs, reusing the shared DVR connection."""
    results = {}
    for mc, info in CONFIG_TYPES.items():
        try:
            results[str(mc)] = _get_config(mc)
        except Exception as e:
            results[str(mc)] = {
                'error': str(e),
                'type_name': info['name'],
                'type_icon': info['icon'],
                'type_description': info['description'],
            }
    return results


def _get_status():
    """Get DVR status summary (4 key configs)."""
    result = {}
    try:
        for mc, key in [(123, 'device_info'), (129, 'device_status'),
                        (111, 'system_time'), (127, 'storage')]:
            cfg = _get_config(mc)
            result[key] = cfg.get('data', {})
        result['connected'] = True
    except Exception as e:
        result['connected'] = False
        result['error'] = str(e)
    return result


# ── HTTP Handler ──────────────────────────────────────

class DVRHandler(http.server.SimpleHTTPRequestHandler):
    """Handles static files + REST API."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB_DIR, **kwargs)

    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-cache')
        super().end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write(f'[dvr-web] {args[0]}\n')

    def do_GET(self):
        path = self.path.split('?')[0]

        if path == '/api/config':
            self._json_response(_get_all_configs())
        elif path.startswith('/api/config/'):
            mc_str = path.split('/')[-1]
            try:
                mc = int(mc_str)
            except ValueError:
                self._json_response({'error': f'Invalid config type: {mc_str}'}, 400)
                return
            if mc not in CONFIG_TYPES:
                self._json_response({'error': f'Unknown config type {mc}'}, 404)
                return
            try:
                self._json_response(_get_config(mc))
            except Exception as e:
                self._json_response({'error': str(e)}, 502)
        elif path == '/api/status':
            self._json_response(_get_status())
        elif path == '/api/config-types':
            self._json_response([
                {'main_cmd': mc, 'name': info['name'],
                 'icon': info['icon'], 'description': info['description']}
                for mc, info in sorted(CONFIG_TYPES.items())
            ])
        elif path == '/settings' or path == '/settings/':
            self._serve_file('settings.html')
        elif path == '/recordings' or path == '/recordings/':
            self._serve_file('recordings.html')
        elif path == '/api/recordings':
            self._json_response(_recorder.get_recordings())
        elif path == '/api/recordings/status':
            self._json_response(_recorder.get_status())
        elif path.startswith('/api/recordings/download/'):
            self._serve_recording(path)
        elif path == '/favicon.ico':
            # Return empty 204 to avoid 404 noise in logs
            self.send_response(204)
            self.end_headers()
        else:
            super().do_GET()

    def _json_response(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, filename):
        filepath = os.path.join(WEB_DIR, filename)
        if not os.path.isfile(filepath):
            self.send_error(404)
            return
        with open(filepath, 'rb') as f:
            body = f.read()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_recording(self, path):
        """Serve a recording file for download."""
        # path = /api/recordings/download/ch0/filename.mp4
        parts = path.split('/')
        if len(parts) < 6:
            self.send_error(400)
            return
        ch = parts[4]   # e.g. 'ch0'
        fname = parts[5]
        # Security: reject path traversal
        if '..' in ch or '..' in fname or '/' in fname:
            self.send_error(403)
            return
        filepath = os.path.join(_recorder.record_dir, ch, fname)
        if not os.path.isfile(filepath):
            self.send_error(404)
            return
        fsize = os.path.getsize(filepath)
        mime = mimetypes.guess_type(filepath)[0] or 'application/octet-stream'
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(fsize))
        self.send_header('Content-Disposition', f'attachment; filename="{fname}"')
        self.end_headers()
        with open(filepath, 'rb') as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def do_POST(self):
        path = self.path.split('?')[0]
        if path == '/api/recordings/start':
            _recorder.enabled = True
            _recorder.start()
            self._json_response({'ok': True, 'status': 'started'})
        elif path == '/api/recordings/stop':
            _recorder.stop()
            self._json_response({'ok': True, 'status': 'stopped'})
        else:
            self.send_error(404)


# ── mediamtx subprocess management ────────────────────

_mediamtx_proc = None


def _start_mediamtx():
    """Start mediamtx if binary and config exist."""
    global _mediamtx_proc
    mediamtx_bin = os.path.join(BASE_DIR, 'mediamtx')
    mediamtx_yml = os.path.join(BASE_DIR, 'mediamtx.yml')

    if not os.path.isfile(mediamtx_bin):
        print(f'[dvr] mediamtx not found at {mediamtx_bin}, skipping RTSP server')
        return
    if not os.path.isfile(mediamtx_yml):
        print(f'[dvr] mediamtx.yml not found, skipping RTSP server')
        return

    print(f'[dvr] Starting mediamtx...')
    _mediamtx_proc = subprocess.Popen(
        [mediamtx_bin, mediamtx_yml],
        cwd=BASE_DIR,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


def _stop_mediamtx():
    """Stop mediamtx subprocess."""
    global _mediamtx_proc
    if _mediamtx_proc and _mediamtx_proc.poll() is None:
        print('[dvr] Stopping mediamtx...')
        _mediamtx_proc.terminate()
        try:
            _mediamtx_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _mediamtx_proc.kill()
        _mediamtx_proc = None


def main():
    _start_mediamtx()
    _recorder.start()

    def _shutdown(signum, frame):
        _recorder.stop()
        _stop_mediamtx()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)

    with http.server.ThreadingHTTPServer(('', PORT), DVRHandler) as httpd:
        print(f'[dvr] Dashboard: http://0.0.0.0:{PORT}/')
        print(f'[dvr]   Live:     http://0.0.0.0:{PORT}/')
        print(f'[dvr]   Settings: http://0.0.0.0:{PORT}/settings')
        print(f'[dvr]   Record:   http://0.0.0.0:{PORT}/recordings')
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            _recorder.stop()
            _stop_mediamtx()
            print('\n[dvr] Stopped.')


if __name__ == '__main__':
    main()
