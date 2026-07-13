"""Wire protocol shared by the termchat server and client.

Messages are JSON objects framed with a 4-byte big-endian length prefix::

    +-----------------+-------------------------+
    | length (uint32) | JSON payload (length B) |
    +-----------------+-------------------------+

Every message is a JSON object with a ``type`` field.  The helper
functions below take care of framing so callers only ever deal with
plain dictionaries.

Message types
-------------

Client -> server::

    {"type": "register",       "username": str, "password": str}
    {"type": "login",          "username": str, "password": str}
    {"type": "list_channels"}
    {"type": "create_channel", "name": str}
    {"type": "join_channel",   "channel_id": int}
    {"type": "send_message",   "content": str}
    {"type": "get_history",    "channel_id": int}
    {"type": "list_users",     "channel_id": int}
    {"type": "logout"}

Server -> client::

    {"type": "register_result", "ok": bool, "error": str|None}
    {"type": "login_result",    "ok": bool, "user_id": int, "username": str, "error": str|None}
    {"type": "channels",        "channels": [{"id", "name", "created_by", "member_count"}]}
    {"type": "channel_created", "channel": {...}}
    {"type": "join_result",     "ok": bool, "channel_id": int, "name": str, "error": str|None}
    {"type": "message",         "channel_id": int, "user_id": int, "username": str,
                                 "content": str, "timestamp": int}
    {"type": "history",         "channel_id": int, "messages": [ ... message dicts ... ]}
    {"type": "users",           "channel_id": int, "users": [str, ...]}
    {"type": "presence",        "channel_id": int, "username": str, "status": "online"|"offline"}
    {"type": "system",          "channel_id": int|None, "content": str}
    {"type": "error",           "content": str}
"""

import json
import socket
import struct

_HEADER = struct.Struct("!I")
# Guard against absurd frame sizes so a hostile / corrupt peer cannot make
# us allocate gigabytes.  16 MiB is far more than any chat message needs.
MAX_FRAME = 16 * 1024 * 1024


class ProtocolError(Exception):
    """Raised when a peer sends a malformed or oversized frame."""


def send_msg(sock: socket.socket, obj: dict) -> None:
    """Serialise ``obj`` to JSON and send it as one length-prefixed frame."""
    data = json.dumps(obj).encode("utf-8")
    if len(data) > MAX_FRAME:
        raise ProtocolError("outgoing message too large")
    sock.sendall(_HEADER.pack(len(data)) + data)


def _recv_exact(sock: socket.socket, n: int):
    """Read exactly ``n`` bytes, or return ``None`` if the peer closed."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def recv_msg(sock: socket.socket):
    """Read one framed message.

    Returns the decoded dict, or ``None`` when the connection is closed
    cleanly.  Raises :class:`ProtocolError` on a malformed frame.
    """
    header = _recv_exact(sock, _HEADER.size)
    if header is None:
        return None
    (length,) = _HEADER.unpack(header)
    if length > MAX_FRAME:
        raise ProtocolError(f"frame too large: {length} bytes")
    payload = _recv_exact(sock, length)
    if payload is None:
        return None
    try:
        return json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProtocolError(f"invalid JSON payload: {exc}") from exc
