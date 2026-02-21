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
  DVR_RECORD_STREAM_TYPE   1=main 2=sub          (default: 1)
  DVR_RECORD_MIN_DISK_MB   min free MB to record (default: 500)

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

# Default minimum free disk space in MB before recording pauses
_DEFAULT_MIN_DISK_MB = 500


class RecordingScheduler:
    """Manages per-channel recording processes + upload queue."""

    def __init__(self):
        # ── Recording config ──
        self.enabled = _env_bool('DVR_RECORD_ENABLED', False)
        self.channels = _env_intlist('DVR_RECORD_CHANNELS', [0])
        self.segment_minutes = int(os.environ.get('DVR_RECORD_SEGMENT_MIN', '15'))
        self.stream_type = int(os.environ.get('DVR_RECORD_STREAM_TYPE', '1'))
        self.retention_hours = int(os.environ.get('DVR_RECORD_RETENTION_HR', '24'))
        self.min_disk_mb = int(os.environ.get('DVR_RECORD_MIN_DISK_MB',
                                              str(_DEFAULT_MIN_DISK_MB)))
        _sched_str = os.environ.get('DVR_RECORD_SCHEDULE', '0-23')
        self.schedule_hours = _parse_schedule(_sched_str)
        self._schedule_str = _sched_str   # kept for serialization

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

        # Init Google Drive uploader (OAuth preferred; fall back to service account)
        if self.gdrive_enabled:
            try:
                from .gdrive import OAuthDriveUploader, GDriveUploader
                _base2 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                token_path = os.path.join(_base2, 'cache', 'gdrive_token.json')
                cfg_path   = os.path.join(_base2, 'cache', 'gdrive_oauth.json')
                # Load stored OAuth client creds if present
                client_id = client_secret = ''
                if os.path.isfile(cfg_path):
                    try:
                        with open(cfg_path) as _f:
                            _oc = json.load(_f)
                        client_id     = _oc.get('client_id', '')
                        client_secret = _oc.get('client_secret', '')
                    except Exception:
                        pass
                oauth = OAuthDriveUploader(
                    token_path, client_id, client_secret, self.gdrive_folder_id)
                if oauth.is_authenticated:
                    self._uploader = oauth
                    log.info('Google Drive: using OAuth token')
                elif self.gdrive_credentials and os.path.isfile(self.gdrive_credentials):
                    self._uploader = GDriveUploader(
                        self.gdrive_credentials, self.gdrive_folder_id)
                    log.info('Google Drive: using service account')
                else:
                    log.warning('Google Drive enabled but not authenticated '
                                '(complete OAuth flow in web UI)')
                    self._uploader = None
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
        disk = self._get_disk_info()
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
            'min_disk_mb': self.min_disk_mb,
            'disk': disk,
        }

    def get_config(self) -> dict:
        """Return current recorder configuration as a JSON-safe dict."""
        return {
            'enabled':           self.enabled,
            'channels':          self.channels,
            'segment_minutes':   self.segment_minutes,
            'stream_type':       self.stream_type,
            'retention_hours':   self.retention_hours,
            'schedule':          self._schedule_str,
            'record_dir':        self.record_dir,
            'min_disk_mb':       self.min_disk_mb,
            'gdrive_enabled':    self.gdrive_enabled,
            'gdrive_credentials': self.gdrive_credentials,
            'gdrive_folder_id':  self.gdrive_folder_id,
            'gdrive_delete_local': self.gdrive_delete_local,
            'upload_command':    self.upload_command,
        }

    def update_config(self, cfg: dict, persist_path: str | None = None) -> None:
        """
        Apply new settings from *cfg* dict.  Stops and restarts the recorder
        if it was running so the changes take effect immediately.
        Fields not present in *cfg* are left unchanged.
        """
        was_running = self._running
        if was_running:
            self.stop()

        if 'enabled'            in cfg: self.enabled           = bool(cfg['enabled'])
        if 'channels'           in cfg: self.channels          = [int(c) for c in cfg['channels']]
        if 'segment_minutes'    in cfg: self.segment_minutes   = int(cfg['segment_minutes'])
        if 'stream_type'        in cfg: self.stream_type       = int(cfg['stream_type'])
        if 'retention_hours'    in cfg: self.retention_hours   = int(cfg['retention_hours'])
        if 'record_dir'         in cfg:
            new_dir = str(cfg['record_dir'])
            # Validate: path must be an absolute path and parent must exist
            if not os.path.isabs(new_dir):
                log.warning('record_dir must be absolute path, got: %s', new_dir)
            else:
                parent = os.path.dirname(new_dir.rstrip('/'))
                if not os.path.isdir(parent):
                    log.warning('record_dir parent does not exist: %s', parent)
                else:
                    try:
                        os.makedirs(new_dir, exist_ok=True)
                        # Quick write test
                        test_file = os.path.join(new_dir, '.write_test')
                        with open(test_file, 'w') as _tf:
                            _tf.write('ok')
                        os.remove(test_file)
                        self.record_dir = new_dir
                    except OSError as e:
                        log.warning('record_dir not writable: %s (%s)', new_dir, e)
        if 'min_disk_mb'        in cfg: self.min_disk_mb       = int(cfg['min_disk_mb'])
        if 'gdrive_enabled'     in cfg: self.gdrive_enabled    = bool(cfg['gdrive_enabled'])
        if 'gdrive_credentials' in cfg: self.gdrive_credentials = str(cfg['gdrive_credentials'])
        if 'gdrive_folder_id'   in cfg: self.gdrive_folder_id  = str(cfg['gdrive_folder_id'])
        if 'gdrive_delete_local'in cfg: self.gdrive_delete_local = bool(cfg['gdrive_delete_local'])
        if 'upload_command'     in cfg: self.upload_command    = str(cfg['upload_command'])
        if 'schedule' in cfg:
            s = str(cfg['schedule'])
            self.schedule_hours = _parse_schedule(s)
            self._schedule_str  = s

        if persist_path:
            try:
                import json as _json
                with open(persist_path, 'w') as _f:
                    _json.dump(self.get_config(), _f, indent=2)
                log.info('Recorder config saved to %s', persist_path)
            except OSError as e:
                log.error('Could not save recorder config: %s', e)

        if self.enabled:
            self.start()   # (re)start whether it was running or newly enabled
        elif was_running:
            log.info('Recording disabled \u2014 stopped')

    def _active_file(self, ch_dir):
        """Return the path of the MP4 currently being written, or None."""
        # The file being written is the most recently modified .mp4 in the
        # channel directory, but only when ffmpeg is actively running there.
        try:
            files = [os.path.join(ch_dir, f)
                     for f in os.listdir(ch_dir) if f.endswith('.mp4')]
        except FileNotFoundError:
            return None
        if not files:
            return None
        
        # Only consider it active if the channel is actually recording
        ch_name = os.path.basename(ch_dir)
        if ch_name.startswith('ch'):
            try:
                ch_num = int(ch_name[2:])
                with self._lock:
                    if ch_num not in self._processes:
                        return None
            except ValueError:
                pass

        newest = max(files, key=lambda p: os.path.getmtime(p))
        return newest

    def get_recordings(self, channel=None, limit=50, offset=0, date_filter=None):
        """List local recording files (newest first).

        Files that are currently being written by ffmpeg are excluded — they
        have no moov atom yet and would be unplayable.
        """
        # Determine which files are currently being written
        with self._lock:
            active_channels = set(self._processes.keys())

        in_progress = set()
        for ch in active_channels:
            ch_dir = os.path.join(self.record_dir, f'ch{ch}')
            cur = self._active_file(ch_dir)
            if cur:
                in_progress.add(cur)

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
                if date_filter and not f.startswith(date_filter):
                    continue
                
                fp = os.path.join(d, f)
                if fp in in_progress:
                    continue  # skip: moov not written yet
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
        
        # Sort newest first
        recordings.sort(key=lambda r: r['modified'], reverse=True)
        
        # Apply pagination
        return recordings[offset : offset + limit]

    def get_recording_dates(self):
        """Return a sorted list of unique dates (YYYY-MM-DD) that have recordings."""
        dates = set()
        try:
            ch_dirs = [d for d in os.listdir(self.record_dir)
                       if os.path.isdir(os.path.join(self.record_dir, d))
                       and d.startswith('ch')]
        except FileNotFoundError:
            return []
            
        for d in ch_dirs:
            path = os.path.join(self.record_dir, d)
            try:
                for f in os.listdir(path):
                    if f.endswith('.mp4') and len(f) >= 10:
                        # expected format: YYYY-MM-DD_HH-MM-SS.mp4
                        # simplistic check: grab first 10 chars
                        dates.add(f[:10])
            except OSError:
                continue
        return sorted(list(dates), reverse=True)

    def delete_recording(self, channel, filename):
        """Delete a single recording file.  Returns True on success."""
        # Security: reject any path traversal
        if '..' in channel or '/' in channel or '..' in filename or '/' in filename:
            raise ValueError('Invalid channel or filename')
        if not filename.endswith('.mp4'):
            raise ValueError('Only .mp4 files may be deleted')
        filepath = os.path.join(self.record_dir, channel, filename)
        if not os.path.isfile(filepath):
            raise FileNotFoundError(f'{channel}/{filename} not found')
        os.remove(filepath)
        self._uploaded.discard(filepath)
        log.info('Deleted recording %s/%s', channel, filename)
        return True

    def delete_all_recordings(self, date_filter=None):
        """Delete all local recording files, optionally filtered by date. Returns count deleted."""
        count = 0
        recs = self.get_recordings(limit=99999, date_filter=date_filter)
        for r in recs:
            try:
                self.delete_recording(r['channel'], r['filename'])
                count += 1
            except Exception as e:
                log.warning('Could not delete %s/%s: %s', r['channel'], r['filename'], e)
        return count

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

            # Check disk space before starting
            if not self._check_disk_space():
                self._status[channel]['state'] = 'paused (disk low)'
                log.warning('ch%d: disk low (%d MB min), pausing recording',
                            channel, self.min_disk_mb)
                self._emergency_cleanup()
                if not self._check_disk_space():
                    # Still not enough after cleanup — wait and retry
                    time.sleep(60)
                    continue

            # Ensure recording dir exists (USB may have been re-mounted)
            try:
                os.makedirs(ch_dir, exist_ok=True)
            except OSError as e:
                self._status[channel]['state'] = f'error (dir: {e})'
                log.error('ch%d: cannot create dir %s: %s', channel, ch_dir, e)
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
                    ['ffmpeg', '-y',
                     # Input: raw H.264 with no embedded timestamps — declare
                     # framerate and generate PTS so moov timestamps are valid.
                     '-fflags', '+genpts',
                     '-r', '25',
                     '-f', 'h264', '-i', 'pipe:0',
                     '-c', 'copy',
                     # Write moov atom at the start so completed segments are
                     # immediately playable without re-muxing.
                     '-movflags', '+faststart',
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
                    # Check disk space during recording
                    if not self._check_disk_space():
                        log.warning('ch%d: disk low during recording, stopping',
                                    channel)
                        self._status[channel]['state'] = 'paused (disk low)'
                        break
                    # Count completed segments
                    try:
                        self._status[channel]['segments'] = sum(
                            1 for f in os.listdir(ch_dir) if f.endswith('.mp4'))
                    except OSError:
                        pass
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
        """Periodically delete old local recordings and monitor disk space."""
        while self._running:
            time.sleep(300)
            try:
                # Emergency cleanup if disk is critically low
                if not self._check_disk_space():
                    self._emergency_cleanup()

                # Normal retention cleanup
                if self.retention_hours <= 0:
                    continue
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

    def _get_disk_info(self) -> dict:
        """Return disk usage info for the recording directory."""
        try:
            st = os.statvfs(self.record_dir)
            total = st.f_frsize * st.f_blocks
            free = st.f_frsize * st.f_bavail
            used = total - free
            return {
                'total_mb': round(total / (1024 * 1024)),
                'free_mb': round(free / (1024 * 1024)),
                'used_mb': round(used / (1024 * 1024)),
                'used_pct': round(used / total * 100, 1) if total > 0 else 0,
                'path': self.record_dir,
                'ok': free >= self.min_disk_mb * 1024 * 1024,
            }
        except OSError as e:
            return {
                'total_mb': 0, 'free_mb': 0, 'used_mb': 0,
                'used_pct': 0, 'path': self.record_dir,
                'ok': False, 'error': str(e),
            }

    def _check_disk_space(self) -> bool:
        """Return True if enough disk space is available to record."""
        try:
            st = os.statvfs(self.record_dir)
            free_mb = (st.f_frsize * st.f_bavail) / (1024 * 1024)
            return free_mb >= self.min_disk_mb
        except OSError:
            return False

    def _emergency_cleanup(self):
        """Delete oldest recordings (already-uploaded first) to free space.
        Called when disk is critically low — ignores retention settings."""
        log.warning('Emergency cleanup: disk low on %s (min %d MB)',
                    self.record_dir, self.min_disk_mb)
        # Collect all recordings with mtime, prioritizing uploaded files
        all_files = []
        try:
            for ch_dir_name in os.listdir(self.record_dir):
                ch_dir = os.path.join(self.record_dir, ch_dir_name)
                if not os.path.isdir(ch_dir) or ch_dir_name.startswith('.'):
                    continue
                active = self._active_file(ch_dir)
                for f in os.listdir(ch_dir):
                    if not f.endswith('.mp4'):
                        continue
                    fp = os.path.join(ch_dir, f)
                    if fp == active:
                        continue  # never delete the file currently being written
                    try:
                        mt = os.path.getmtime(fp)
                        uploaded = fp in self._uploaded
                        # Sort key: uploaded files first (0), then by oldest
                        all_files.append((0 if uploaded else 1, mt, fp))
                    except FileNotFoundError:
                        pass
        except OSError:
            return 0

        all_files.sort()  # uploaded+oldest first
        deleted = 0
        for _, _, fp in all_files:
            try:
                os.remove(fp)
                self._uploaded.discard(fp)
                deleted += 1
                log.info('Emergency cleanup: removed %s', fp)
                # Check if we have enough space now
                if self._check_disk_space():
                    break
            except OSError:
                pass
        if deleted:
            self._save_upload_state()
            log.info('Emergency cleanup: removed %d files', deleted)
        return deleted


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
