"""
DVR client — manages command and media connections to the HiEasy DVR.
"""
import socket
import struct
import threading
import re
import sys
import time
import logging

from .protocol import (
    CMD_MAGIC, VERSION, HEADER_SIZE,
    pack_cmd_header, pack_media_header, make_xml,
    recv_msg, parse_body,
    ID_LOGIN_GET_FLAG, ID_USER_LOGIN,
    ID_STREAM_CREATE, ID_STREAM_START,
    ID_STREAM_STOP, ID_STREAM_DESTROY,
    ID_LOGOUT, ID_HEARTBEAT_REPLY,
)
from .auth import compute_hash
from .stream import iter_frames

log = logging.getLogger(__name__)

# Maximum age of unretrieved messages before pruning (seconds)
_MSG_MAX_AGE = 60
# Maximum queued messages before pruning oldest
_MSG_MAX_COUNT = 200
# Heartbeat miss threshold: if no heartbeat arrives within this many seconds
# the DVR has likely dropped us.
_HEARTBEAT_MISS_SEC = 45


class DVRClient:
    """
    Client for HiEasy DVR.

    Usage::

        dvr = DVRClient('192.168.1.x')
        dvr.connect(channel=0)

        for codec, h264_data in dvr.stream():
            sys.stdout.buffer.write(h264_data)

        dvr.disconnect()
    """

    def __init__(self, host, cmd_port=5050, media_port=6050,
                 username='admin', password='123456'):
        self.host = host
        self.cmd_port = cmd_port
        self.media_port = media_port
        self.username = username
        self.password = password

        self._cmd_sock = None
        self._media_sock = None
        self._session = None
        self._running = False
        self._dead = False              # set True when cmd connection is lost
        self._msgs = []                 # (timestamp, hdr, body_str)
        self._lock = threading.Lock()   # protects _msgs list
        self._send_lock = threading.Lock()   # serializes writes to _cmd_sock
        self._last_heartbeat = 0        # time.time() of last heartbeat seen
        self._disconnect_lock = threading.Lock()  # makes disconnect() reentrant

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self, channel=0, stream_type=1):
        """
        Full connection sequence: login → create stream → media connect → start.

        :param channel: Camera channel (0-3 for 4-channel DVR)
        :param stream_type: 1 = main stream, 2 = sub stream
        """
        log.info("Connecting to %s:%d ...", self.host, self.cmd_port)

        # Reset state from any previous connection
        self._dead = False
        self._msgs = []
        self._last_heartbeat = 0

        # Command TCP connection
        self._cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._cmd_sock.settimeout(10)
        _configure_keepalive(self._cmd_sock)
        self._cmd_sock.connect((self.host, self.cmd_port))

        # --- Login ---
        self._login()

        # Start background threads
        self._running = True
        self._last_heartbeat = time.time()  # DVR sends first heartbeat soon
        threading.Thread(target=self._reader_loop, daemon=True, name=f'dvr-reader').start()
        threading.Thread(target=self._heartbeat_loop, daemon=True, name=f'dvr-heartbeat').start()

        # --- Create stream ---
        xml = make_xml(
            ID_STREAM_CREATE,
            '<RealStreamCreateRequest Channel="{}" Mode="{}" Type="1" />'.format(
                channel, stream_type
            ),
        )
        self._send_cmd(pack_cmd_header(len(xml)) + xml)

        _, reply = self._wait_for('RealStreamCreateReply', timeout=5)
        if not reply:
            raise ConnectionError("No RealStreamCreateReply from DVR")

        m = re.search(r'MediaSession="(\d+)"', reply)
        if not m:
            raise ConnectionError("No MediaSession in reply: " + reply[:200])
        self._session = int(m.group(1))
        log.info("MediaSession: %d", self._session)

        # --- Media TCP connection ---
        self._media_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._media_sock.settimeout(10)
        _configure_keepalive(self._media_sock)
        self._media_sock.connect((self.host, self.media_port))
        self._media_sock.sendall(pack_media_header(self._session))
        self._media_sock.recv(HEADER_SIZE)  # Handshake reply

        # --- Start stream ---
        xml = make_xml(
            ID_STREAM_START,
            '<RealStreamStartRequest MediaSession="{}" />'.format(self._session),
        )
        self._send_cmd(pack_cmd_header(len(xml)) + xml)
        self._wait_for('RealStreamStartReply', timeout=3)
        log.info("Stream started on channel %d", channel)

    def stream(self):
        """
        Generator yielding (codec, h264_bytes) from the media connection.
        Stops when disconnect() is called, the socket closes, or the
        command channel dies (heartbeat miss / reader death).
        """
        if not self._media_sock:
            raise RuntimeError("Not connected — call connect() first")
        for codec, data in iter_frames(self._media_sock):
            if not self._running or self._dead:
                break
            yield codec, data

    def disconnect(self):
        """Gracefully disconnect from the DVR (reentrant / thread-safe)."""
        with self._disconnect_lock:
            if not self._running and self._cmd_sock is None:
                return   # already disconnected
            self._running = False

            try:
                if self._session and self._cmd_sock and not self._dead:
                    # Stop stream
                    xml = make_xml(
                        ID_STREAM_STOP,
                        '<RealStreamStopRequest MediaSession="{}" />'.format(
                            self._session
                        ),
                    )
                    self._send_cmd(pack_cmd_header(len(xml)) + xml)
                    time.sleep(0.2)

                    # Destroy stream
                    xml = make_xml(
                        ID_STREAM_DESTROY,
                        '<RealStreamDestroyRequest MediaSession="{}" />'.format(
                            self._session
                        ),
                    )
                    self._send_cmd(pack_cmd_header(len(xml)) + xml)
                    time.sleep(0.2)

                    # Logout
                    xml = make_xml(
                        ID_LOGOUT,
                        '<Logout UserName="{}" />'.format(self.username),
                    )
                    self._send_cmd(pack_cmd_header(len(xml)) + xml)
            except Exception:
                pass

            for sock in (self._media_sock, self._cmd_sock):
                try:
                    if sock:
                        sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    if sock:
                        sock.close()
                except Exception:
                    pass

            self._cmd_sock = None
            self._media_sock = None
            self._session = None
            self._dead = False
            self._msgs = []
            log.info("Disconnected")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _send_cmd(self, data):
        """Thread-safe send on the command socket."""
        with self._send_lock:
            if self._cmd_sock:
                self._cmd_sock.sendall(data)

    def _login(self):
        """Perform LoginGetFlag → hash oracle → UserLogin."""
        # Get nonce — during login we're single-threaded so direct send is fine
        xml = make_xml(ID_LOGIN_GET_FLAG, f'<LoginGetFlag UserName="{self.username}" />')
        self._cmd_sock.sendall(pack_cmd_header(len(xml)) + xml)

        _, body = recv_msg(self._cmd_sock)
        body_str = parse_body(body)
        m = re.search(r'LoginFlag="([^"]*)"', body_str)
        if not m:
            raise ConnectionError("No LoginFlag in response: " + body_str[:200])

        flag = m.group(1)
        log.info("Login flag (nonce): %s", flag)

        # Compute hash
        hash_val = compute_hash(flag, self.username, self.password)
        if not hash_val:
            raise ConnectionError("Hash oracle failed — cannot authenticate")

        # Send login
        xml = make_xml(
            ID_USER_LOGIN,
            '<UserLogin UserName="{}" UserIP="192.168.1.1" '
            'UserMAC="00:00:00:00:00:00" LoginFlag="{}" />'.format(
                self.username, hash_val
            ),
        )
        self._cmd_sock.sendall(pack_cmd_header(len(xml)) + xml)

        _, body = recv_msg(self._cmd_sock)
        body_str = parse_body(body)
        if 'CmdReply="0"' not in body_str:
            raise ConnectionError("Login failed: " + body_str[:200])

        log.info("Login successful")

    def _reader_loop(self):
        """Background thread: read messages from command socket."""
        while self._running:
            try:
                hdr, body = recv_msg(self._cmd_sock, timeout=2)
                if hdr is None:
                    # Clean socket close (recv returned b'')
                    log.warning("Command socket closed by DVR")
                    self._dead = True
                    return
                if body:
                    body_str = parse_body(body)
                    now = time.time()
                    with self._lock:
                        self._msgs.append((now, hdr, body_str))
                        # Prune old / excess messages to prevent unbounded growth
                        if len(self._msgs) > _MSG_MAX_COUNT:
                            self._msgs = self._msgs[-_MSG_MAX_COUNT // 2:]
            except socket.timeout:
                continue  # normal — no message within timeout
            except OSError as e:
                if not self._running:
                    return  # disconnect was called
                log.error("Command socket error: %s", e)
                self._dead = True
                return
            except Exception as e:
                if not self._running:
                    return
                log.error("Reader loop error: %s", e)
                self._dead = True
                return

    def _heartbeat_loop(self):
        """Background thread: respond to HeartBeatNotice + detect stalls."""
        while self._running:
            if self._dead:
                return

            replied = False
            with self._lock:
                for i, (ts, hdr, body_str) in enumerate(self._msgs):
                    if 'HeartBeatNotice' in body_str and 'Reply' not in body_str:
                        self._msgs.pop(i)
                        self._last_heartbeat = time.time()
                        try:
                            r = make_xml(
                                ID_HEARTBEAT_REPLY,
                                '<HeartBeatNoticeReply CmdReply="0" '
                                'NetDataFlow="0" NetHistoryDataFlow="0" />',
                            )
                            h = struct.pack(
                                '>IIIIIIIII',
                                CMD_MAGIC, VERSION, hdr[2], 0,
                                len(r), 3, 0, 0, 0,
                            )
                            self._send_cmd(h + r)
                            replied = True
                        except Exception as e:
                            log.error("Failed to send heartbeat reply: %s", e)
                            self._dead = True
                            return
                        break

            # Detect heartbeat miss (DVR stopped talking to us)
            if (self._last_heartbeat > 0 and not replied and
                    time.time() - self._last_heartbeat > _HEARTBEAT_MISS_SEC):
                log.error("No heartbeat from DVR for %ds — connection dead",
                          _HEARTBEAT_MISS_SEC)
                self._dead = True
                return

            time.sleep(1)

    def _wait_for(self, tag, timeout=5):
        """Wait for a message containing `tag` in the reader queue."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._dead:
                return None, None
            with self._lock:
                for i, (ts, hdr, body_str) in enumerate(self._msgs):
                    if tag in body_str:
                        self._msgs.pop(i)
                        return hdr, body_str
            time.sleep(0.1)
        return None, None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _configure_keepalive(sock):
    """
    Enable TCP keepalive with aggressive timers.

    Default OS keepalive is often 2 hours — way too slow.  We set:
      idle = 15s   (start probing after 15s of silence)
      interval = 5s  (probe every 5s)
      probes = 3     (give up after 3 missed probes → ~30s to detect dead)
    """
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    try:
        if sys.platform == 'linux':
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 15)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
        elif sys.platform == 'darwin':
            # macOS: TCP_KEEPALIVE sets the idle time (no interval/cnt tunables)
            TCP_KEEPALIVE = 0x10
            sock.setsockopt(socket.IPPROTO_TCP, TCP_KEEPALIVE, 15)
    except (AttributeError, OSError) as e:
        log.debug("Could not tune TCP keepalive: %s", e)
