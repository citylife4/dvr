"""
DVR Recording Scheduler — records H.264 streams to MP4 segments and
optionally uploads them to Google Drive.

Uses dvr_feeder.py piped to ffmpeg's segment muxer for seamless,
continuous recording with automatic file splitting.

Configuration (environment variables):
  DVR_RECORD_ENABLED       true/false           (default: false)
  DVR_RECORD_CHANNELS      comma-separated      (default: 0)
  DVR_RECORD_SEGMENT_MIN   segment minutes       (default: 15)
  DVR_RECORD_DIR           local recordings dir  (default: ./recordings)
  DVR_RECORD_RETENTION_HR  hours to keep local   (default: 24, 0=forever)
  DVR_RECORD_SCHEDULE      hour ranges           (default: 0-23 = always)
  DVR_RECORD_STREAM_TYPE   0=main 1=sub          (default: 0)

  DVR_GDRIVE_ENABLED       true/false            (default: false)
  DVR_GDRIVE_CREDENTIALS   path to JSON key      (required if gdrive on)
  DVR_GDRIVE_FOLDER_ID     Drive folder ID       (required if gdrive on)
  DVR_GDRIVE_DELETE_LOCAL   delete after upload   (default: false)

  DVR_UPLOAD_COMMAND        custom upload command (alternative to gdrive)
                            placeholders: {file} {channel} {filename}
                            example: rclone copy {file} gdrive:DVR/{channel}/
"""

import os
import sys
import json
import time
import logging
import threading
import subprocess
from datetime import datetime

log = logging.getLogger('dvr.recorder')


class RecordingScheduler:
    """Manages per-channel recording processes + upload queue."""

    def __init__(self):
        # ── Recording config ──
        self.enabled = _env_bool('DVR_RECORD_ENABLED', False)
        self.channels = _env_intlist('DVR_RECORD_CHANNELS', [0])
        self.segment_minutes = int(os.environ.get('DVR_RECORD_SEGMENT_MIN', '15'))
        self.stream_type = int(os.environ.get('DVR_RECORD_STREAM_TYPE', '0'))
        self.retention_hours = int(os.environ.get('DVR_RECORD_RETENTION_HR', '24'))
        self.schedule_hours = _parse_schedule(os.environ.get('DVR_RECORD_SCHEDULE', '0-23'))

        _base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.record_dir = os.environ.get('DVR_RECORD_DIR', os.path.join(_base, 'recordings'))
        self._feeder_script = os.path.join(_base, 'dvr_feeder.py')

        # ── Upload config ──
        self.gdrive_enabled = _env_bool('DVR_GDRIVE_ENABLED', False)
        self.gdrive_credentials = os.environ.get('DVR_GDRIVE_CREDENTIALS', '')
        self.gdrive_folder_id = os.environ.get('DVR_GDRIVE_FOLDER_ID', '')
        self.gdrive_delete_local = _env_bool('DVR_GDRIVE_DELETE_LOCAL', False)
        self.upload_command = os.environ.get('DVR_UPLOAD_COMMAND', '')

        # ── Runtime state ──
        self._running = False
        self._threads = {}          # channel → Thread
        self._processes = {}        # channel → (feeder, ffmpeg)
        self._lock = threading.Lock()
        self._uploader = None       # GDriveUploader instance
        self._uploaded = set()      # filepaths already uploaded
        self._upload_failures = {}  # filepath → retry count
        self._status = {}           # channel → dict

    # ── Public API ──────────────────────────────────────

    def start(self):
        """Start recording on all configured channels."""
        if self._running:
            return
        if not self.enabled:
            log.info('Recording disabled (set DVR_RECORD_ENABLED=true to enable)')
            return

        self._running = True
        os.makedirs(self.record_dir, exist_ok=True)
        self._load_upload_state()

        # Init Google Drive uploader
        if self.gdrive_enabled:
            try:
                from .gdrive import GDriveUploader
                self._uploader = GDriveUploader(
                    self.gdrive_credentials, self.gdrive_folder_id)
            except Exception as e:
                log.error('Google Drive init failed: %s', e)
                self._uploader = None

        # Per-channel recording threads
        for ch in self.channels:
            ch_dir = os.path.join(self.record_dir, f'ch{ch}')
            os.makedirs(ch_dir, exist_ok=True)
            self._status[ch] = {'state': 'starting', 'file': None,
                                'started': None, 'segments': 0}
            t = threading.Thread(target=self._record_loop, args=(ch,),
                                 daemon=True, name=f'rec-ch{ch}')
            self._threads[ch] = t
            t.start()

        # Upload worker thread
        if self._uploader or self.upload_command:
            t = threading.Thread(target=self._upload_loop, daemon=True,
                                 name='rec-upload')
            t.start()

        # Retention cleanup thread
        if self.retention_hours > 0:
            t = threading.Thread(target=self._cleanup_loop, daemon=True,
                                 name='rec-cleanup')
            t.start()

        log.info('Recording started: ch=%s, segment=%dm, schedule=%s',
                 self.channels, self.segment_minutes, sorted(self.schedule_hours))

    def stop(self):
        """Stop all recording processes gracefully."""
        if not self._running:
            return
        self._running = False
        with self._lock:
            for ch, (feeder, ffmpeg) in list(self._processes.items()):
                try:
                    feeder.terminate()
                except OSError:
                    pass
                try:
                    ffmpeg.wait(timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    ffmpeg.kill()
            self._processes.clear()
        for t in self._threads.values():
            t.join(timeout=10)
        self._threads.clear()
        log.info('Recording stopped')

    def get_status(self):
        """Return recording status summary (JSON-safe)."""
        return {
            'enabled': self.enabled,
            'running': self._running,
            'channels': {str(ch): dict(s) for ch, s in self._status.items()},
            'gdrive_enabled': self.gdrive_enabled,
            'gdrive_connected': self._uploader is not None,
            'upload_command': bool(self.upload_command),
            'upload_pending': self._count_pending_uploads(),
            'schedule': sorted(self.schedule_hours),
            'segment_minutes': self.segment_minutes,
            'stream_type': self.stream_type,
            'retention_hours': self.retention_hours,
            'record_dir': self.record_dir,
        }

    def get_recordings(self, channel=None, limit=50):
        """List local recording files (newest first)."""
        recordings = []
        if channel is not None:
            dirs = [os.path.join(self.record_dir, f'ch{channel}')]
        else:
            try:
                dirs = sorted(
                    os.path.join(self.record_dir, d)
                    for d in os.listdir(self.record_dir)
                    if os.path.isdir(os.path.join(self.record_dir, d))
                    and d.startswith('ch'))
            except FileNotFoundError:
                dirs = []
        for d in dirs:
            ch_name = os.path.basename(d)
            try:
                files = os.listdir(d)
            except FileNotFoundError:
                continue
            for f in files:
                if not f.endswith('.mp4'):
                    continue
                fp = os.path.join(d, f)
                try:
                    st = os.stat(fp)
                except FileNotFoundError:
                    continue
                recordings.append({
                    'channel': ch_name,
                    'filename': f,
                    'size': st.st_size,
                    'modified': st.st_mtime,
                    'uploaded': fp in self._uploaded,
                })
        recordings.sort(key=lambda r: r['modified'], reverse=True)
        return recordings[:limit]

    # ── Recording loop ──────────────────────────────────

    def _record_loop(self, channel):
        """Continuous recording for one channel using ffmpeg segment muxer."""
        ch_dir = os.path.join(self.record_dir, f'ch{channel}')

        while self._running:
            # Check schedule
            if not self._is_scheduled_now():
                self._status[channel]['state'] = 'waiting (schedule)'
                time.sleep(30)
                continue

            seg_sec = self.segment_minutes * 60
            pattern = os.path.join(ch_dir, '%Y-%m-%d_%H-%M-%S.mp4')

            self._status[channel]['state'] = 'recording'
            self._status[channel]['started'] = datetime.now().isoformat()

            try:
                feeder = subprocess.Popen(
                    [sys.executable, self._feeder_script,
                     '--channel', str(channel),
                     '--stream-type', str(self.stream_type)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                ffmpeg = subprocess.Popen(
                    ['ffmpeg', '-y', '-f', 'h264', '-i', 'pipe:0',
                     '-c', 'copy',
                     '-f', 'segment',
                     '-segment_time', str(seg_sec),
                     '-segment_format', 'mp4',
                     '-strftime', '1',
                     '-reset_timestamps', '1',
                     pattern],
                    stdin=feeder.stdout,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                feeder.stdout.close()  # allow SIGPIPE

                with self._lock:
                    self._processes[channel] = (feeder, ffmpeg)

                log.info('Recording ch%d → %s (segment=%ds)', channel, ch_dir, seg_sec)

                # Monitor until shutdown, schedule changes, or process dies
                while self._running and self._is_scheduled_now():
                    if ffmpeg.poll() is not None:
                        break
                    # Count completed segments
                    self._status[channel]['segments'] = sum(
                        1 for f in os.listdir(ch_dir) if f.endswith('.mp4'))
                    time.sleep(10)

                # Graceful stop: terminate feeder → pipe closes → ffmpeg finalizes
                feeder.terminate()
                try:
                    feeder.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    feeder.kill()
                try:
                    ffmpeg.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    ffmpeg.kill()

                with self._lock:
                    self._processes.pop(channel, None)

            except Exception as e:
                log.error('Recording error ch%d: %s', channel, e)
                self._status[channel]['state'] = 'error'
                time.sleep(10)
                continue

            if not self._running:
                break
            time.sleep(2)  # brief pause before restart

        self._status[channel]['state'] = 'stopped'

    # ── Upload loop ─────────────────────────────────────

    def _upload_loop(self):
        """Background worker: uploads completed segments."""
        while self._running:
            time.sleep(15)
            try:
                pending = self._find_completed_segments()
                for filepath, ch_name in pending:
                    if not self._running:
                        break
                    retries = self._upload_failures.get(filepath, 0)
                    if retries >= 3:
                        continue  # skip after 3 failures
                    try:
                        self._upload_one(filepath, ch_name)
                        self._uploaded.add(filepath)
                        self._upload_failures.pop(filepath, None)
                        self._save_upload_state()
                        if self.gdrive_delete_local:
                            os.remove(filepath)
                            log.info('Deleted local (after upload): %s', filepath)
                    except Exception as e:
                        self._upload_failures[filepath] = retries + 1
                        log.error('Upload failed (%d/3) %s: %s',
                                  retries + 1, os.path.basename(filepath), e)
            except Exception as e:
                log.error('Upload worker error: %s', e)

    def _upload_one(self, filepath, ch_name):
        """Upload a single file via Google Drive API or custom command."""
        filename = os.path.basename(filepath)
        if self._uploader:
            folder = self._uploader.ensure_subfolder(ch_name)
            self._uploader.upload(filepath, filename=filename, folder_id=folder)
        if self.upload_command:
            cmd = self.upload_command.replace('{file}', filepath) \
                                     .replace('{channel}', ch_name) \
                                     .replace('{filename}', filename)
            log.info('Running upload command: %s', cmd)
            subprocess.run(cmd, shell=True, check=True, timeout=300)

    def _find_completed_segments(self):
        """Find MP4 files old enough to be complete and not yet uploaded."""
        now = time.time()
        min_age = 60  # seconds since last write
        completed = []
        try:
            entries = os.listdir(self.record_dir)
        except FileNotFoundError:
            return completed
        for ch_dir_name in entries:
            ch_dir = os.path.join(self.record_dir, ch_dir_name)
            if not os.path.isdir(ch_dir) or not ch_dir_name.startswith('ch'):
                continue
            try:
                files = os.listdir(ch_dir)
            except FileNotFoundError:
                continue
            for f in files:
                if not f.endswith('.mp4'):
                    continue
                fp = os.path.join(ch_dir, f)
                try:
                    st = os.stat(fp)
                except FileNotFoundError:
                    continue
                if (now - st.st_mtime > min_age
                        and st.st_size > 0
                        and fp not in self._uploaded):
                    completed.append((fp, ch_dir_name))
        return completed

    def _count_pending_uploads(self):
        """Count files awaiting upload."""
        try:
            return len(self._find_completed_segments())
        except Exception:
            return 0

    # ── Retention cleanup ───────────────────────────────

    def _cleanup_loop(self):
        """Periodically delete old local recordings."""
        while self._running:
            time.sleep(300)
            try:
                cutoff = time.time() - (self.retention_hours * 3600)
                for ch_dir_name in os.listdir(self.record_dir):
                    ch_dir = os.path.join(self.record_dir, ch_dir_name)
                    if not os.path.isdir(ch_dir):
                        continue
                    for f in os.listdir(ch_dir):
                        if not f.endswith('.mp4'):
                            continue
                        fp = os.path.join(ch_dir, f)
                        try:
                            if os.path.getmtime(fp) < cutoff:
                                os.remove(fp)
                                self._uploaded.discard(fp)
                                log.info('Cleanup: removed %s/%s', ch_dir_name, f)
                        except FileNotFoundError:
                            pass
            except Exception as e:
                log.error('Cleanup error: %s', e)

    # ── Upload state persistence ────────────────────────

    def _load_upload_state(self):
        path = os.path.join(self.record_dir, '.upload_state.json')
        try:
            with open(path) as f:
                self._uploaded = set(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            self._uploaded = set()

    def _save_upload_state(self):
        path = os.path.join(self.record_dir, '.upload_state.json')
        try:
            with open(path, 'w') as f:
                json.dump(sorted(self._uploaded), f)
        except OSError:
            pass

    # ── Helpers ─────────────────────────────────────────

    def _is_scheduled_now(self):
        return datetime.now().hour in self.schedule_hours


# ── Module-level helpers ────────────────────────────────

def _env_bool(key, default=False):
    return os.environ.get(key, str(default)).lower() in ('true', '1', 'yes')


def _env_intlist(key, default):
    val = os.environ.get(key, '')
    if not val:
        return default
    return [int(x.strip()) for x in val.split(',') if x.strip()]


def _parse_schedule(s):
    """Parse hour-range string like '0-23' or '8-17,22-6' into set of hours."""
    hours = set()
    for part in s.split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            a, b = part.split('-', 1)
            a, b = int(a), int(b)
            if a <= b:
                hours.update(range(a, b + 1))
            else:  # wraps midnight, e.g. 22-6
                hours.update(range(a, 24))
                hours.update(range(0, b + 1))
        else:
            hours.add(int(part))
    return hours
