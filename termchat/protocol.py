# wire protocol: everything is JSON with a 4-byte big-endian length prefix
# in front. message dicts all have a "type" field, see server.py/client.py
# for what each type carries.

import json
import socket
import struct

_HEADER = struct.Struct("!I")
MAX_FRAME = 16 * 1024 * 1024  # don't let a bad peer make us allocate forever


class ProtocolError(Exception):
    pass


def send_msg(sock: socket.socket, obj: dict) -> None:
    data = json.dumps(obj).encode("utf-8")
    if len(data) > MAX_FRAME:
        raise ProtocolError("outgoing message too large")
    sock.sendall(_HEADER.pack(len(data)) + data)


def _recv_exact(sock: socket.socket, n: int):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def recv_msg(sock: socket.socket):
    """Returns the decoded dict, or None if the peer closed cleanly."""
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
