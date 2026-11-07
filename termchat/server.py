# termchat TCP server. one thread per client, shared state behind a lock.
# Database handles its own locking so we only worry about the client registry.

import argparse
import base64
import logging
import os
import re
import socket
import ssl
import threading
import time
import uuid

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
        self.role = None                # 'admin' | 'user' once authenticated
        self.channel_id = None          # channel the user is currently viewing
        # True only for a genuine loopback peer that is NOT being proxied
        # (see the transport layers). Gates passwordless local admin login.
        self.is_local = False
        self._send_lock = threading.Lock()

    @property
    def authenticated(self) -> bool:
        return self.user_id is not None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def user_row(self):
        """Minimal row shape the Database access checks expect."""
        return {"id": self.user_id, "role": self.role}

    def send(self, msg: dict) -> bool:
        """Send a framed message; return False if the socket is dead."""
        with self._send_lock:
            try:
                send_msg(self.conn, msg)
                return True
            except OSError:
                return False


class Server:
    def __init__(self, host: str, port: int, db_path: str,
                 admin_user: str = "admin", admin_pass: str = "admin",
                 uploads_dir: str = None, ephemeral: bool = False,
                 ssl_context=None):
        self.host = host
        self.port = port
        self.db_path = db_path
        self.ephemeral = ephemeral
        self.ssl_context = ssl_context
        self.db = Database(db_path)
        self.admin_user = admin_user
        self.admin_pass = admin_pass
        # Seed (or repair) the admin account so there is always exactly one
        # well-known admin to auto-login as locally and to manage everyone else.
        self.db.ensure_admin(admin_user, admin_pass)
        self.uploads_dir = uploads_dir or os.path.join(
            os.path.dirname(os.path.abspath(db_path)) or ".", "uploads")
        os.makedirs(self.uploads_dir, exist_ok=True)
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
        if self.ephemeral:
            self._wipe_data()

    def _wipe_data(self):
        """Ephemeral mode: delete the database and all uploads on shutdown so
        nothing persists between runs."""
        import shutil
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = self.db_path + suffix
            try:
                os.remove(path)
            except OSError:
                pass
        try:
            shutil.rmtree(self.uploads_dir)
        except OSError:
            pass
        log.info("ephemeral: wiped database and uploads")

    # -- client lifecycle ----------------------------------------------------

    def _handle_client(self, conn: socket.socket, addr):
        # Locality is decided from the raw peer address, before any TLS wrap.
        peer_ip = addr[0] if addr else ""
        local = peer_ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1")
        if self.ssl_context is not None:
            try:
                conn = self.ssl_context.wrap_socket(conn, server_side=True)
            except (ssl.SSLError, OSError) as exc:
                log.debug("TLS handshake failed from %s: %s", addr, exc)
                try:
                    conn.close()
                except OSError:
                    pass
                return
        state = ClientState(conn, addr)
        # Raw TCP (TUI/GUI): a loopback peer is the host operator. The TCP
        # port is not tunneled by ngrok, so this is safe for local admin.
        state.is_local = local
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

    # -- transport-agnostic hooks ---------------------------------------------
    # Used by alternate transports (e.g. the WebSocket bridge in web.py) that
    # frame messages differently but want the same session/routing logic as
    # raw TCP clients.

    def register_client(self, state: "ClientState"):
        with self._lock:
            self._clients[state.conn] = state

    def dispatch(self, state: "ClientState", msg: dict):
        self._dispatch(state, msg)

    def disconnect_client(self, state: "ClientState"):
        self._disconnect(state)

    # -- dispatch ------------------------------------------------------------

    def _dispatch(self, state: ClientState, msg: dict):
        mtype = msg.get("type")
        handler = {
            "register": self._on_register,
            "login": self._on_login,
            "auto_login": self._on_auto_login,
            "list_channels": self._on_list_channels,
            "create_channel": self._on_create_channel,
            "join_channel": self._on_join_channel,
            "send_message": self._on_send_message,
            "upload_file": self._on_upload_file,
            "get_history": self._on_get_history,
            "list_users": self._on_list_users,
            "logout": self._on_logout,
            # Admin-only management.
            "admin_list_users": self._on_admin_list_users,
            "admin_create_user": self._on_admin_create_user,
            "admin_delete_user": self._on_admin_delete_user,
            "admin_set_password": self._on_admin_set_password,
            "admin_set_channel_access": self._on_admin_set_channel_access,
            "admin_get_channel_access": self._on_admin_get_channel_access,
        }.get(mtype)

        if handler is None:
            state.send({"type": "error", "content": f"unknown message type: {mtype!r}"})
            return

        # These are reachable without being logged in; everything else needs auth.
        public = ("register", "login", "auto_login")
        if mtype not in public and not state.authenticated:
            state.send({"type": "error", "content": "you must log in first"})
            return

        # Admin-only gates.
        if mtype.startswith("admin_") and not state.is_admin:
            state.send({"type": "error", "content": "admin privileges required"})
            return

        handler(state, msg)

    # -- handlers ------------------------------------------------------------

    def _on_register(self, state, msg):
        # Self-registration is disabled: only an admin creates accounts
        # (admin_create_user). Reply with a clear error so old clients behave.
        state.send({"type": "register_result", "ok": False,
                    "error": "registration is disabled - ask an admin to "
                             "create your account"})

    def _finish_login(self, state, row):
        """Shared tail of login / auto_login once a user row is chosen."""
        state.user_id = row["id"]
        state.username = row["username"]
        state.role = row["role"]
        state.send({"type": "login_result", "ok": True,
                    "user_id": row["id"], "username": row["username"],
                    "role": row["role"], "is_admin": row["role"] == "admin",
                    "error": None})
        log.info("%s logged in as %s (%s)", state.username, state.role,
                 state.addr)
        self._send_channels(state)

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

        self._finish_login(state, row)

    def _on_auto_login(self, state, msg):
        """Passwordless admin login, honoured only for a genuine local peer
        (loopback and not proxied). This is how the host operator becomes
        admin without a password; remote/tunnelled peers are refused."""
        if state.authenticated:
            state.send({"type": "login_result", "ok": False,
                        "error": "already logged in"})
            return
        if not state.is_local:
            state.send({"type": "login_result", "ok": False,
                        "auto": True,
                        "error": "auto-login is only available locally"})
            return
        row = self.db.get_user_by_name(self.admin_user)
        if row is None:
            state.send({"type": "login_result", "ok": False,
                        "error": "no admin account configured"})
            return
        with self._lock:
            already = any(c.user_id == row["id"]
                          for c in self._clients.values())
        if already:
            state.send({"type": "login_result", "ok": False,
                        "error": "admin is already logged in elsewhere"})
            return
        self._finish_login(state, row)

    def _on_logout(self, state, msg):
        if state.channel_id is not None:
            old = state.channel_id
            state.channel_id = None
            self._broadcast_presence(old, state.username, "offline")
            self._broadcast_userlist(old)
        state.user_id = None
        state.username = None
        state.role = None

    def _on_list_channels(self, state, msg):
        self._send_channels(state)

    def _on_create_channel(self, state, msg):
        # Only admins create channels now.
        if not state.is_admin:
            state.send({"type": "error",
                        "content": "only an admin can create channels"})
            return
        name = str(msg.get("name", ""))
        is_private = bool(msg.get("is_private"))
        members = msg.get("members") or []   # user ids allowed in (private)
        channel_id, error = self.db.create_channel(
            name, state.user_id, is_private=is_private)
        if error is not None:
            state.send({"type": "error", "content": error})
            return
        if is_private:
            # The creator (admin) always sees it; add any selected members.
            ids = {int(u) for u in members if str(u).isdigit() or
                   isinstance(u, int)}
            self.db.set_channel_access(channel_id, ids)
        channel = self.db.get_channel(channel_id)
        channel["member_count"] = 0
        # Announce to everyone who can see it (admins included), so the
        # creator's own sidebar updates too - no separate send to the creator,
        # which would duplicate the event for them.
        self._broadcast_channel_created(channel)

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
        # Enforce private-channel visibility.
        if not self.db.can_access_channel(state.user_row, channel_id):
            state.send({"type": "join_result", "ok": False,
                        "channel_id": channel_id, "name": None,
                        "error": "you don't have access to that channel"})
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

    # -- file upload ---------------------------------------------------------

    _SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")
    _MAX_UPLOAD = 10 * 1024 * 1024   # 10 MB

    def _on_upload_file(self, state, msg):
        name = self._SAFE_NAME.sub("_", str(msg.get("name", "file"))[-80:]) \
            or "file"
        try:
            data = base64.b64decode(str(msg.get("data", "")), validate=True)
        except Exception:
            state.send({"type": "upload_result", "ok": False,
                        "error": "could not decode file data"})
            return
        if not data:
            state.send({"type": "upload_result", "ok": False,
                        "error": "empty file"})
            return
        if len(data) > self._MAX_UPLOAD:
            state.send({"type": "upload_result", "ok": False,
                        "error": "file too large (max 10 MB)"})
            return
        token = uuid.uuid4().hex
        folder = os.path.join(self.uploads_dir, token)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, name), "wb") as fh:
            fh.write(data)
        url = f"/uploads/{token}/{name}"
        log.info("%s uploaded %s (%d bytes) -> %s",
                 state.username, name, len(data), url)
        state.send({"type": "upload_result", "ok": True, "url": url,
                    "name": name, "size": len(data), "error": None})

    # -- admin ---------------------------------------------------------------

    def _on_admin_list_users(self, state, msg):
        state.send({"type": "admin_users", "users": self.db.list_users()})

    def _on_admin_create_user(self, state, msg):
        username = str(msg.get("username", ""))
        password = str(msg.get("password", ""))
        role = str(msg.get("role", "user"))
        uid, error = self.db.create_user(username, password, role=role)
        state.send({"type": "admin_create_result", "ok": error is None,
                    "error": error,
                    "user": self.db.get_user(uid) if uid else None})
        if uid:
            self._broadcast_admins(
                {"type": "admin_users", "users": self.db.list_users()})

    def _on_admin_delete_user(self, state, msg):
        try:
            uid = int(msg.get("user_id"))
        except (TypeError, ValueError):
            state.send({"type": "error", "content": "invalid user id"})
            return
        if uid == state.user_id:
            state.send({"type": "error",
                        "content": "you can't delete your own account"})
            return
        target = self.db.get_user(uid)
        if target and target["role"] == "admin" and self.db.count_admins() <= 1:
            state.send({"type": "error",
                        "content": "can't delete the last admin"})
            return
        self.db.delete_user(uid)
        # Boot any live session for that user.
        with self._lock:
            victims = [c for c in self._clients.values() if c.user_id == uid]
        for c in victims:
            try:
                c.conn.close()
            except OSError:
                pass
        state.send({"type": "admin_users", "users": self.db.list_users()})

    def _on_admin_set_password(self, state, msg):
        try:
            uid = int(msg.get("user_id"))
        except (TypeError, ValueError):
            state.send({"type": "error", "content": "invalid user id"})
            return
        error = self.db.set_password(uid, str(msg.get("password", "")))
        state.send({"type": "admin_action_result", "ok": error is None,
                    "error": error})

    def _on_admin_set_channel_access(self, state, msg):
        try:
            channel_id = int(msg.get("channel_id"))
        except (TypeError, ValueError):
            state.send({"type": "error", "content": "invalid channel id"})
            return
        members = msg.get("members") or []
        ids = {int(u) for u in members
               if isinstance(u, int) or str(u).isdigit()}
        self.db.set_channel_access(channel_id, ids)
        state.send({"type": "admin_action_result", "ok": True, "error": None})
        # Refresh everyone's sidebar so access changes take effect live.
        self._resend_channels_to_all()

    def _on_admin_get_channel_access(self, state, msg):
        try:
            channel_id = int(msg.get("channel_id"))
        except (TypeError, ValueError):
            state.send({"type": "error", "content": "invalid channel id"})
            return
        state.send({"type": "channel_access", "channel_id": channel_id,
                    "members": self.db.get_channel_access(channel_id)})

    # -- helpers -------------------------------------------------------------

    def _send_channels(self, state):
        state.send({"type": "channels",
                    "channels": self.db.list_channels(state.user_row)})

    def _resend_channels_to_all(self):
        with self._lock:
            clients = [c for c in self._clients.values() if c.authenticated]
        for c in clients:
            c.send({"type": "channels",
                    "channels": self.db.list_channels(c.user_row)})

    def _broadcast_admins(self, msg):
        with self._lock:
            targets = [c for c in self._clients.values() if c.is_admin]
        for c in targets:
            c.send(msg)

    def _broadcast_channel_created(self, channel):
        """Announce a new channel only to clients allowed to see it."""
        cid = channel["id"]
        with self._lock:
            targets = [c for c in self._clients.values() if c.authenticated]
        for c in targets:
            if self.db.can_access_channel(c.user_row, cid):
                c.send({"type": "channel_created", "channel": channel})

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


def _env_flag(name):
    """True only for a clearly-affirmative env value (1/true/yes/on)."""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def main(argv=None):
    parser = argparse.ArgumentParser(description="termchat server")
    parser.add_argument("--host", default="0.0.0.0",
                        help="interface to bind (default: 0.0.0.0, all)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"port to listen on (default: {DEFAULT_PORT})")
    parser.add_argument("--db", default="termchat.db",
                        help="path to the SQLite database (default: termchat.db)")
    parser.add_argument("--web-port", type=int, default=None,
                        help="also serve a browser client on this HTTP port "
                             "(shares the same chat with TCP clients)")
    parser.add_argument("--web-host", default=None,
                        help="interface for the browser client (default: same as --host)")
    parser.add_argument("--admin-user", default=os.environ.get(
                        "TERMCHAT_ADMIN_USER", "admin"),
                        help="admin account username (default: admin)")
    parser.add_argument("--admin-pass", default=os.environ.get(
                        "TERMCHAT_ADMIN_PASS", "admin"),
                        help="admin account password, used for remote admin "
                             "login (local host gets passwordless auto-login)")
    parser.add_argument("--uploads-dir", default=os.environ.get(
                        "TERMCHAT_UPLOADS"),
                        help="directory to store uploaded files "
                             "(default: <db dir>/uploads)")
    parser.add_argument("--ephemeral", action="store_true",
                        default=_env_flag("TERMCHAT_EPHEMERAL"),
                        help="wipe the database and all uploads on shutdown "
                             "(nothing persists between runs)")
    parser.add_argument("--tls", action="store_true",
                        default=_env_flag("TERMCHAT_TLS"),
                        help="encrypt all connections with TLS "
                             "(self-signed cert auto-generated if absent)")
    parser.add_argument("--certfile", default=os.environ.get("TERMCHAT_CERT"),
                        help="TLS certificate PEM (default: <db dir>/tls/cert.pem)")
    parser.add_argument("--keyfile", default=os.environ.get("TERMCHAT_KEY"),
                        help="TLS private key PEM (default: <db dir>/tls/key.pem)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="enable debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    ssl_context = None
    if args.tls:
        from . import tls
        db_dir = os.path.dirname(os.path.abspath(args.db)) or "."
        certfile = args.certfile or os.path.join(db_dir, "tls", "cert.pem")
        keyfile = args.keyfile or os.path.join(db_dir, "tls", "key.pem")
        tls.ensure_cert(certfile, keyfile)
        ssl_context = tls.server_context(certfile, keyfile)
        log.info("TLS enabled (cert: %s)", certfile)

    server = Server(args.host, args.port, args.db,
                    admin_user=args.admin_user, admin_pass=args.admin_pass,
                    uploads_dir=args.uploads_dir, ephemeral=args.ephemeral,
                    ssl_context=ssl_context)
    log.info("admin account: %r (local host auto-logs in as admin without a "
             "password)", args.admin_user)
    if args.ephemeral:
        log.info("ephemeral mode: database + uploads are wiped on shutdown")

    if args.web_port is not None:
        from .web import run_web_in_thread
        run_web_in_thread(server, args.web_host or args.host, args.web_port,
                          ssl_context=ssl_context)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
