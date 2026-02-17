"""
DVR Configuration Client — reads all config types from the DVR.

Uses GetCfg (Command ID 14) to retrieve config. SetCfg is not supported
by this DVR firmware (returns error 16001 for all config types).
"""

import socket
import re
import xml.etree.ElementTree as ET
from .protocol import CMD_MAGIC, VERSION, HEADER_SIZE, pack_cmd_header, make_xml, recv_msg, parse_body
from .auth import compute_hash

# ── Config type registry ──────────────────────────────

CONFIG_TYPES = {
    101: {
        'name': 'Network',
        'icon': '🌐',
        'description': 'IP address, ports, DHCP, DDNS, PPPoE, WiFi',
    },
    103: {
        'name': 'Network Services',
        'icon': '📡',
        'description': 'NMS, AMS, NTP, Email settings',
    },
    105: {
        'name': 'Display / OSD',
        'icon': '🖥️',
        'description': 'On-screen display, channel names, fonts',
    },
    107: {
        'name': 'Encoding',
        'icon': '🎬',
        'description': 'Compression, resolution, bitrate, framerate',
    },
    109: {
        'name': 'Record Schedule',
        'icon': '⏺️',
        'description': 'Recording schedules per channel',
    },
    111: {
        'name': 'System Time',
        'icon': '🕐',
        'description': 'Current DVR date and time',
    },
    115: {
        'name': 'Decoder / Serial',
        'icon': '🔌',
        'description': 'Serial port and decoder (PTZ) settings',
    },
    117: {
        'name': 'Alarm',
        'icon': '🚨',
        'description': 'Alarm inputs, outputs, motion detection',
    },
    121: {
        'name': 'Users',
        'icon': '👤',
        'description': 'User accounts and permissions',
    },
    123: {
        'name': 'Device Info',
        'icon': 'ℹ️',
        'description': 'Model, firmware, channel count (read-only)',
    },
    125: {
        'name': 'Device Config',
        'icon': '⚙️',
        'description': 'DVR ID, timezone, DST, language, device name',
    },
    127: {
        'name': 'Storage',
        'icon': '💾',
        'description': 'Hard disk info, disk groups',
    },
    129: {
        'name': 'Device Status',
        'icon': '📊',
        'description': 'Live channel status, motion, bitrates',
    },
    131: {
        'name': 'Maintenance',
        'icon': '🔧',
        'description': 'Auto-maintenance schedule',
    },
    133: {
        'name': 'Custom Settings',
        'icon': '🎛️',
        'description': 'Work mode, feature toggles (email, CMS, NTP)',
    },
    139: {
        'name': 'Source Device',
        'icon': '📹',
        'description': 'Connected camera/source info',
    },
    221: {
        'name': 'Storage (Extended)',
        'icon': '💿',
        'description': 'Extended disk and partition info',
    },
}


def _xml_element_to_dict(elem):
    """Convert an XML element (and children) to a nested dict."""
    result = {}
    # Add attributes
    if elem.attrib:
        result.update(elem.attrib)
    # Add children
    children_by_tag = {}
    for child in elem:
        tag = child.tag
        child_dict = _xml_element_to_dict(child)
        if tag in children_by_tag:
            # Multiple children with same tag → make a list
            if not isinstance(children_by_tag[tag], list):
                children_by_tag[tag] = [children_by_tag[tag]]
            children_by_tag[tag].append(child_dict)
        else:
            children_by_tag[tag] = child_dict
    if children_by_tag:
        result['_children'] = children_by_tag
    # Add text content if present
    if elem.text and elem.text.strip():
        result['_text'] = elem.text.strip()
    return result


def parse_config_xml(xml_str):
    """
    Parse a GetCfgReply XML string into a structured dict.

    Returns:
        {
            'config_len': int,
            'version': str,
            'cmd_reply': str,
            'main_cmd': int,
            'assist_cmd': int,
            'data': {tag: {attrs...}, ...}  # the actual config elements
        }
    """
    # Parse XML
    # Strip the XML declaration if present (ET doesn't need it)
    xml_clean = re.sub(r'<\?xml[^?]*\?>\s*', '', xml_str)
    try:
        root = ET.fromstring(xml_clean)
    except ET.ParseError:
        return {'error': 'XML parse error', 'raw': xml_str}

    # Find GetCfgReply element
    reply = root.find('.//GetCfgReply')
    if reply is None:
        return {'error': 'No GetCfgReply found', 'raw': xml_str}

    result = {
        'config_len': int(reply.get('ConfigLen', 0)),
        'version': reply.get('Version', ''),
        'cmd_reply': reply.get('CmdReply', ''),
        'main_cmd': None,
        'assist_cmd': None,
        'data': {},
    }

    if result['cmd_reply'] != '0':
        result['error'] = f'DVR returned error {result["cmd_reply"]}'
        return result

    for child in reply:
        if child.tag == 'CfgInfo':
            result['main_cmd'] = int(child.get('MainCommand', 0))
            result['assist_cmd'] = int(child.get('AssistCommand', -1))
        else:
            result['data'][child.tag] = _xml_element_to_dict(child)

    return result


class DVRConfigClient:
    """Reads configuration from the DVR via GetCfg commands."""

    def __init__(self, host=None, port=5050, username='admin', password='123456'):
        import os
        self.host = host or os.environ.get('DVR_HOST', '192.168.1.174')
        self.port = port
        self.username = username
        self.password = password
        self._sock = None

    def connect(self):
        """Establish TCP connection and log in."""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(15)
        self._sock.connect((self.host, self.port))
        self._login()

    def close(self):
        """Close the connection."""
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _login(self):
        """Perform challenge-response login."""
        # Get nonce
        body = make_xml(26, '<LoginGetFlag />')
        self._sock.sendall(pack_cmd_header(len(body)) + body)
        hdr, body = recv_msg(self._sock)
        xml = parse_body(body)
        m = re.search(r'LoginFlag="([^"]*)"', xml)
        if not m:
            raise ConnectionError(f'No LoginFlag in response: {xml[:200]}')
        nonce = m.group(1)

        # Compute and send hash
        hash_val = compute_hash(nonce, self.username, self.password)
        login_xml = (f'<UserLogin LoginFlag="{hash_val}" '
                     f'UserName="{self.username}" PassWord="{self.password}" />')
        body = make_xml(24, login_xml)
        self._sock.sendall(pack_cmd_header(len(body)) + body)
        hdr, body = recv_msg(self._sock)
        xml = parse_body(body)
        if 'CmdReply="0"' not in xml:
            raise ConnectionError(f'Login failed: {xml[:200]}')

    def get_config(self, main_cmd, assist_cmd=-1):
        """
        Get a single config type.

        Args:
            main_cmd: Config MainCommand value (e.g. 111 for SySTime)
            assist_cmd: Config AssistCommand (default -1 for all)

        Returns:
            Parsed config dict (see parse_config_xml).
        """
        if not self._sock:
            self.connect()

        inner = f'<GetCfg MainCmd="{main_cmd}" AssistCmd="{assist_cmd}" />'
        body = make_xml(14, inner)
        self._sock.sendall(pack_cmd_header(len(body)) + body)

        # Read response, handling possible heartbeat messages
        for _ in range(5):
            hdr, resp_body = recv_msg(self._sock)
            if hdr is None:
                raise ConnectionError('No response from DVR')
            xml_str = parse_body(resp_body)
            # Skip heartbeat messages
            if 'HeartBeat' in xml_str:
                # Reply to heartbeat
                hb_reply = make_xml(79, '<HeartBeatNoticeReply />')
                self._sock.sendall(pack_cmd_header(len(hb_reply)) + hb_reply)
                continue
            return parse_config_xml(xml_str)

        raise ConnectionError('Too many non-config responses from DVR')

    def get_all_configs(self):
        """
        Get all known config types.

        Returns:
            Dict mapping main_cmd → parsed config dict.
            Each entry also includes 'type_name', 'type_icon', 'type_description'.
        """
        results = {}
        for mc, info in CONFIG_TYPES.items():
            try:
                cfg = self.get_config(mc)
                cfg['type_name'] = info['name']
                cfg['type_icon'] = info['icon']
                cfg['type_description'] = info['description']
                results[mc] = cfg
            except Exception as e:
                # Reconnect on failure and continue
                results[mc] = {
                    'error': str(e),
                    'type_name': info['name'],
                    'type_icon': info['icon'],
                    'type_description': info['description'],
                }
                try:
                    self.close()
                    self.connect()
                except Exception:
                    pass
        return results

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()
