"""Minimal RFC 6455 WebSocket support (server side, stdlib only).

Just enough to carry termchat's existing JSON message protocol over a
browser WebSocket connection: the opening handshake and text-frame
framing.  No compression extensions, no fragmentation of outgoing
frames (every termchat message is tiny JSON, so a single frame per
message is plenty).
"""

import base64
import hashlib
import struct

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME = 16 * 1024 * 1024

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


def accept_key(client_key: str) -> str:
    """Compute the Sec-WebSocket-Accept value for a given client key."""
    digest = hashlib.sha1((client_key + _GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def recv_frame(sock):
    """Read one WebSocket frame.

    Returns ``(opcode, payload)`` or ``None`` if the peer closed the
    connection.  Client frames are always masked per RFC 6455; this
    unmasks them transparently.
    """
    header = _recv_exact(sock, 2)
    if header is None:
        return None
    b0, b1 = header
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    if length == 126:
        ext = _recv_exact(sock, 2)
        if ext is None:
            return None
        (length,) = struct.unpack("!H", ext)
    elif length == 127:
        ext = _recv_exact(sock, 8)
        if ext is None:
            return None
        (length,) = struct.unpack("!Q", ext)
    if length > MAX_FRAME:
        return None
    mask_key = None
    if masked:
        mask_key = _recv_exact(sock, 4)
        if mask_key is None:
            return None
    payload = _recv_exact(sock, length) if length else b""
    if payload is None:
        return None
    if masked:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def send_frame(sock, payload: bytes, opcode: int = OP_TEXT):
    """Send one unmasked (server -> client) WebSocket frame."""
    length = len(payload)
    header = bytes([0x80 | opcode])  # FIN=1, no extensions
    if length < 126:
        header += bytes([length])
    elif length < 65536:
        header += bytes([126]) + struct.pack("!H", length)
    else:
        header += bytes([127]) + struct.pack("!Q", length)
    sock.sendall(header + payload)
