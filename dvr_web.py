#!/usr/bin/env python3
"""
DVR Web Dashboard — serves live view + config dashboard + REST API.

Endpoints:
  /                      → Live view (4-channel WebRTC grid)
  /settings              → Configuration dashboard
  /api/config            → JSON: all config types from DVR
  /api/config/<main_cmd> → JSON: specific config type
  /api/status            → JSON: DVR status summary
  /api/config-types      → JSON: available config type list (no DVR needed)
  /<static files>        → Files from web/ directory

Port: $DVR_WEB_PORT (default 8080)
"""

import os
import sys
import json
import time
import http.server
import threading
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hieasy_dvr.config import DVRConfigClient, CONFIG_TYPES

PORT = int(os.environ.get('DVR_WEB_PORT', 8080))
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')

# ── Shared DVR client with connection reuse ───────────

_dvr_client = None
_dvr_lock = threading.Lock()       # serializes all DVR access
_config_cache = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 30  # seconds


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
    """Get a single config from DVR with caching and shared connection."""
    global _dvr_client
    now = time.time()

    # Check cache (no DVR lock needed)
    with _cache_lock:
        if main_cmd in _config_cache:
            data, ts = _config_cache[main_cmd]
            if now - ts < _CACHE_TTL:
                return data

    # Query DVR (serialized)
    with _dvr_lock:
        # Double-check cache (another thread may have populated it)
        with _cache_lock:
            if main_cmd in _config_cache:
                data, ts = _config_cache[main_cmd]
                if now - ts < _CACHE_TTL:
                    return data

        for attempt in range(2):
            try:
                _ensure_client()
                data = _dvr_client.get_config(main_cmd)
                info = CONFIG_TYPES.get(main_cmd, {})
                data['type_name'] = info.get('name', f'Config {main_cmd}')
                data['type_icon'] = info.get('icon', '📋')
                data['type_description'] = info.get('description', '')

                with _cache_lock:
                    _config_cache[main_cmd] = (data, time.time())
                return data
            except Exception:
                if _dvr_client:
                    _dvr_client.close()
                    _dvr_client = None
                if attempt == 1:
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


def main():
    with http.server.ThreadingHTTPServer(('', PORT), DVRHandler) as httpd:
        print(f'DVR Dashboard: http://0.0.0.0:{PORT}/')
        print(f'  Live View:  http://0.0.0.0:{PORT}/')
        print(f'  Settings:   http://0.0.0.0:{PORT}/settings')
        print(f'  API:        http://0.0.0.0:{PORT}/api/config-types')
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print('\nStopped.')


if __name__ == '__main__':
    main()
