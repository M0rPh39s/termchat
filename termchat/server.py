"""termchat TCP server.

Threaded, one thread per connected client.  All shared state (the table
of connected clients) is guarded by a single lock.  Persistence goes
through :class:`~termchat.database.Database`, which serialises its own
access, so the server code only has to worry about the in-memory client
registry.

Run it with::

    python -m termchat.server --host 0.0.0.0 --port 9009 --db termchat.db
"""

import argparse
import logging
import socket
import threading

from . import DEFAULT_HOST, DEFAULT_PORT
from .database import Database
from .protocol import ProtocolError, recv_msg, send_msg

log = logging.getLogger("termchat.server")


class ClientState:
    """Per-connection state held by the server."""

    def __init__(self, conn: socket.socket, addr):
        self.conn = conn
        self.addr = addr
        self.user_id = None
        self.username = None
        self.channel_id = None          # channel the user is currently viewing
        self._send_lock = threading.Lock()

    @property
    def authenticated(self) -> bool:
        return self.user_id is not None

    def send(self, msg: dict) -> bool:
        """Send a framed message; return False if the socket is dead."""
        with self._send_lock:
            try:
                send_msg(self.conn, msg)
                return True
            except OSError:
                return False


class Server:
    def __init__(self, host: str, port: int, db_path: str):
        self.host = host
        self.port = port
        self.db = Database(db_path)
        self._clients = {}              # conn -> ClientState
        self._lock = threading.Lock()
        self._sock = None
        self._running = False

    # -- lifecycle -----------------------------------------------------------

    def serve_forever(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(128)
        self._running = True
        log.info("termchat server listening on %s:%s", self.host, self.port)
        try:
            while self._running:
                try:
                    conn, addr = self._sock.accept()
                except OSError:
                    break
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                t = threading.Thread(
                    target=self._handle_client, args=(conn, addr), daemon=True)
                t.start()
        finally:
            self.shutdown()

    def shutdown(self):
        self._running = False
        with self._lock:
            clients = list(self._clients.values())
        for c in clients:
            try:
                c.conn.close()
            except OSError:
                pass
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self.db.close()

    # -- client lifecycle ----------------------------------------------------

    def _handle_client(self, conn: socket.socket, addr):
        state = ClientState(conn, addr)
        with self._lock:
            self._clients[conn] = state
        log.info("connection from %s", addr)
        try:
            while True:
                try:
                    msg = recv_msg(conn)
                except ProtocolError as exc:
                    state.send({"type": "error", "content": str(exc)})
                    break
                if msg is None:
                    break
                if not isinstance(msg, dict):
                    state.send({"type": "error", "content": "malformed message"})
                    continue
                self._dispatch(state, msg)
        except OSError:
            pass
        finally:
            self._disconnect(state)

    def _disconnect(self, state: ClientState):
        with self._lock:
            self._clients.pop(state.conn, None)
        # Announce departure to whichever channel they were viewing.
        if state.authenticated and state.channel_id is not None:
            self._broadcast_presence(state.channel_id, state.username, "offline")
            self._broadcast_userlist(state.channel_id)
        try:
            state.conn.close()
        except OSError:
            pass
        if state.username:
            log.info("%s disconnected (%s)", state.username, state.addr)
        else:
            log.info("connection closed %s", state.addr)

    # -- dispatch ------------------------------------------------------------

    def _dispatch(self, state: ClientState, msg: dict):
        mtype = msg.get("type")
        handler = {
            "register": self._on_register,
            "login": self._on_login,
            "list_channels": self._on_list_channels,
            "create_channel": self._on_create_channel,
            "join_channel": self._on_join_channel,
            "send_message": self._on_send_message,
            "get_history": self._on_get_history,
            "list_users": self._on_list_users,
            "logout": self._on_logout,
        }.get(mtype)

        if handler is None:
            state.send({"type": "error", "content": f"unknown message type: {mtype!r}"})
            return

        # Everything except register/login requires authentication.
        if mtype not in ("register", "login") and not state.authenticated:
            state.send({"type": "error", "content": "you must log in first"})
            return

        handler(state, msg)

    # -- handlers ------------------------------------------------------------

    def _on_register(self, state, msg):
        if state.authenticated:
            state.send({"type": "register_result", "ok": False,
                        "error": "already logged in"})
            return
        username = str(msg.get("username", ""))
        password = str(msg.get("password", ""))
        user_id, error = self.db.create_user(username, password)
        state.send({"type": "register_result", "ok": error is None,
                    "error": error})

    def _on_login(self, state, msg):
        if state.authenticated:
            state.send({"type": "login_result", "ok": False,
                        "error": "already logged in"})
            return
        username = str(msg.get("username", ""))
        password = str(msg.get("password", ""))
        row = self.db.authenticate(username, password)
        if row is None:
            state.send({"type": "login_result", "ok": False,
                        "error": "invalid username or password"})
            return

        # Reject a second concurrent login for the same account.
        with self._lock:
            already = any(
                c.user_id == row["id"] for c in self._clients.values())
        if already:
            state.send({"type": "login_result", "ok": False,
                        "error": "this account is already logged in elsewhere"})
            return

        state.user_id = row["id"]
        state.username = row["username"]
        state.send({"type": "login_result", "ok": True,
                    "user_id": row["id"], "username": row["username"],
                    "error": None})
        log.info("%s logged in (%s)", state.username, state.addr)
        self._send_channels(state)

    def _on_logout(self, state, msg):
        if state.channel_id is not None:
            old = state.channel_id
            state.channel_id = None
            self._broadcast_presence(old, state.username, "offline")
            self._broadcast_userlist(old)
        state.user_id = None
        state.username = None

    def _on_list_channels(self, state, msg):
        self._send_channels(state)

    def _on_create_channel(self, state, msg):
        name = str(msg.get("name", ""))
        channel_id, error = self.db.create_channel(name, state.user_id)
        if error is not None:
            state.send({"type": "error", "content": error})
            return
        channel = self.db.get_channel(channel_id)
        channel["member_count"] = 0
        # Tell everyone a new channel exists so their sidebars update.
        self._broadcast_all({"type": "channel_created", "channel": channel})

    def _on_join_channel(self, state, msg):
        try:
            channel_id = int(msg.get("channel_id"))
        except (TypeError, ValueError):
            state.send({"type": "error", "content": "invalid channel id"})
            return
        channel = self.db.get_channel(channel_id)
        if channel is None:
            state.send({"type": "join_result", "ok": False,
                        "channel_id": channel_id, "name": None,
                        "error": "no such channel"})
            return

        previous = state.channel_id
        if previous == channel_id:
            # Already here: just refresh history and the user list.
            self._send_history(state, channel_id)
            self._broadcast_userlist(channel_id)
            return

        # Leave the old channel.
        if previous is not None:
            state.channel_id = None
            self._broadcast_presence(previous, state.username, "offline")
            self._broadcast_userlist(previous)

        # Enter the new one.
        state.channel_id = channel_id
        self.db.add_membership(state.user_id, channel_id)
        state.send({"type": "join_result", "ok": True,
                    "channel_id": channel_id, "name": channel["name"],
                    "error": None})
        self._send_history(state, channel_id)
        self._broadcast_presence(channel_id, state.username, "online")
        self._broadcast_userlist(channel_id)

    def _on_send_message(self, state, msg):
        if state.channel_id is None:
            state.send({"type": "error", "content": "join a channel first"})
            return
        content = str(msg.get("content", "")).rstrip("\n")
        if not content:
            return
        if len(content) > 4000:
            content = content[:4000]
        ts = self.db.add_message(state.channel_id, state.user_id, content)
        out = {"type": "message", "channel_id": state.channel_id,
               "user_id": state.user_id, "username": state.username,
               "content": content, "timestamp": ts}
        self._broadcast_channel(state.channel_id, out)

    def _on_get_history(self, state, msg):
        try:
            channel_id = int(msg.get("channel_id"))
        except (TypeError, ValueError):
            state.send({"type": "error", "content": "invalid channel id"})
            return
        self._send_history(state, channel_id)

    def _on_list_users(self, state, msg):
        try:
            channel_id = int(msg.get("channel_id"))
        except (TypeError, ValueError):
            state.send({"type": "error", "content": "invalid channel id"})
            return
        state.send({"type": "users", "channel_id": channel_id,
                    "users": self._online_users(channel_id)})

    # -- helpers -------------------------------------------------------------

    def _send_channels(self, state):
        state.send({"type": "channels", "channels": self.db.list_channels()})

    def _send_history(self, state, channel_id):
        messages = self.db.get_history(channel_id)
        state.send({"type": "history", "channel_id": channel_id,
                    "messages": messages})

    def _online_users(self, channel_id):
        with self._lock:
            names = sorted(
                c.username for c in self._clients.values()
                if c.channel_id == channel_id and c.username)
        return names

    def _broadcast_channel(self, channel_id, msg):
        with self._lock:
            targets = [c for c in self._clients.values()
                       if c.channel_id == channel_id]
        for c in targets:
            c.send(msg)

    def _broadcast_all(self, msg):
        with self._lock:
            targets = [c for c in self._clients.values() if c.authenticated]
        for c in targets:
            c.send(msg)

    def _broadcast_presence(self, channel_id, username, status):
        self._broadcast_channel(channel_id, {
            "type": "presence", "channel_id": channel_id,
            "username": username, "status": status})

    def _broadcast_userlist(self, channel_id):
        self._broadcast_channel(channel_id, {
            "type": "users", "channel_id": channel_id,
            "users": self._online_users(channel_id)})


def main(argv=None):
    parser = argparse.ArgumentParser(description="termchat server")
    parser.add_argument("--host", default="0.0.0.0",
                        help="interface to bind (default: 0.0.0.0, all)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"port to listen on (default: {DEFAULT_PORT})")
    parser.add_argument("--db", default="termchat.db",
                        help="path to the SQLite database (default: termchat.db)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="enable debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    server = Server(args.host, args.port, args.db)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
