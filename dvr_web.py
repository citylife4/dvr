#!/usr/bin/env python3
"""
Minimal web server for the DVR live-view page.

Serves web/index.html on port 8080 (or $DVR_WEB_PORT).
The page connects directly to mediamtx's WebRTC/HLS endpoints.
"""

import os
import sys
import http.server
import functools

PORT = int(os.environ.get('DVR_WEB_PORT', 8080))
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')


class CORSHandler(http.server.SimpleHTTPRequestHandler):
    """Adds CORS headers so the page can reach mediamtx on other ports."""

    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-cache')
        super().end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write(f'[dvr-web] {args[0]}\n')


def main():
    os.chdir(WEB_DIR)
    handler = CORSHandler
    with http.server.HTTPServer(('', PORT), handler) as httpd:
        print(f'DVR web viewer: http://0.0.0.0:{PORT}/')
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print('\nStopped.')


if __name__ == '__main__':
    main()
