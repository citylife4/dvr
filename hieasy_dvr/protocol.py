"""
HiEasy DVR wire protocol: 36-byte big-endian headers + XML bodies.
"""
import struct

# Header constants
HEADER_SIZE = 36
CMD_MAGIC = 0x05011154
MEDIA_MAGIC = 0x05011150
VERSION = 0x00001001

# XML Command IDs (body <Command ID="N">)
ID_USER_LOGIN = 24
ID_USER_LOGIN_REPLY = 25
ID_LOGIN_GET_FLAG = 26
ID_LOGIN_GET_FLAG_REPLY = 27
ID_LOGOUT = 28
ID_LOGOUT_REPLY = 29
ID_HEARTBEAT = 78
ID_HEARTBEAT_REPLY = 79
ID_STREAM_CREATE = 136
ID_STREAM_CREATE_REPLY = 137
ID_STREAM_START = 138
ID_STREAM_START_REPLY = 139
ID_STREAM_STOP = 140
ID_STREAM_STOP_REPLY = 141
ID_STREAM_DESTROY = 142
ID_STREAM_DESTROY_REPLY = 143

# Transaction ID counter
_txn = [0x10000]


def next_txn():
    _txn[0] += 1
    return _txn[0]


def pack_cmd_header(body_len, txn=None):
    """Build a 36-byte command header."""
    if txn is None:
        txn = next_txn()
    return struct.pack(
        '>IIIIIIIII',
        CMD_MAGIC, VERSION, txn, 0, body_len, 3, 0, 0, 0
    )


def pack_media_header(session_id):
    """Build a 36-byte media handshake header."""
    return struct.pack(
        '>IIIIIIIII',
        MEDIA_MAGIC, VERSION, 4, 0, 3, 0, 0, 0, session_id
    )


def make_xml(cmd_id, inner):
    """Build a null-terminated XML command body."""
    xml = (
        '<?xml version="1.0" encoding="GB2312" standalone="yes" ?>\n'
        '<Command ID="{}">\n'
        '    {}\n'
        '</Command>\n'
    ).format(cmd_id, inner)
    return xml.encode('utf-8') + b'\x00'


def recv_msg(sock, timeout=10):
    """
    Receive one complete message from a command socket.
    Returns (header_tuple, body_bytes) or (None, None) on clean close.
    Raises socket.timeout if no data within `timeout`.
    Raises OSError on connection errors.
    Header tuple is 9 big-endian uint32 fields.
    """
    sock.settimeout(timeout)
    buf = b''
    while len(buf) < HEADER_SIZE:
        chunk = sock.recv(HEADER_SIZE - len(buf))
        if not chunk:
            return None, None
        buf += chunk
    hdr = struct.unpack('>IIIIIIIII', buf)
    body_len = hdr[4]
    body = b''
    while len(body) < body_len:
        chunk = sock.recv(body_len - len(body))
        if not chunk:
            break
        body += chunk
    return hdr, body


def parse_body(body):
    """Decode XML body to string, stripping null terminator."""
    return body.decode('utf-8', errors='replace').rstrip('\x00')
