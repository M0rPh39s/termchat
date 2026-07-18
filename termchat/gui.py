"""termchat desktop GUI client (Tkinter).

A point-and-click counterpart to the terminal client, sharing the same
wire protocol (:mod:`termchat.protocol`).  Runs on Windows/macOS/Linux —
no third-party dependencies, so it packages cleanly into a standalone
executable with PyInstaller.

Layout::

    +--------------+--------------------------------------+
    |  CHANNELS    |  # general                           |
    |  # general   |  [12:00] alice: hello everyone       |
    |  # random    |  [12:00] bob:   hi alice             |
    |  + new       |                                      |
    |              |                                      |
    |  ONLINE (2)  |                                      |
    |  * alice     +--------------------------------------+
    |  * bob       |  [ type a message...        ] [Send] |
    +--------------+--------------------------------------+

The receiver thread never touches Tk directly: it pushes decoded server
messages onto a queue that the UI drains with ``after()`` polling.

The login screen can also start an **embedded server** ("Host a local
server"), so a single executable can act as both server and client.
"""

import os
import queue
import socket
import sys
import threading
import time
import tkinter as tk
import zlib
from datetime import datetime
from tkinter import font as tkfont

from . import DEFAULT_HOST, DEFAULT_PORT, __version__
from .protocol import ProtocolError, recv_msg, send_msg

# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------

BG = "#16191d"          # window background
PANEL = "#1e2329"       # sidebar / card background
PANEL_2 = "#232930"     # chat background
FIELD = "#2a313a"       # entry background
BORDER = "#343c46"
FG = "#d7dce2"          # main text
FG_DIM = "#7a828d"      # timestamps, hints
ACCENT = "#35b8e0"      # brand cyan
ACCENT_DARK = "#2492b5"
GREEN = "#6fcf7c"
RED = "#e06c75"
YELLOW = "#e5c07b"

USER_COLORS = [
    "#4fc3f7", "#81c784", "#ffd54f", "#f06292", "#7986cb",
    "#e57373", "#aed581", "#64b5f6", "#ba68c8", "#4dd0e1",
]

FONT_FAMILY = "Segoe UI" if sys.platform == "win32" else "Helvetica"
MONO_FAMILY = "Consolas" if sys.platform == "win32" else "Menlo"


def _color_for(name: str) -> str:
    """Stable per-user colour (crc32 so it survives hash randomisation)."""
    return USER_COLORS[zlib.crc32(name.encode("utf-8")) % len(USER_COLORS)]


def _resource_path(rel: str) -> str:
    """Resolve a bundled asset both in source and in a PyInstaller exe."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(__file__)))
    return os.path.join(base, rel)


def _default_db_path() -> str:
    """Per-user writable location for the embedded server's database."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "TermChat")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "termchat.db")


def _lan_ip() -> str:
    """Best-effort LAN IP (for the 'others can connect to ...' hint)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return "127.0.0.1"


# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------

class GuiNetwork:
    """Socket owner: sync requests during login, async receiver afterwards."""

    def __init__(self):
        self.sock = None
        self.incoming = queue.Queue()
        self._send_lock = threading.Lock()
        self._recv_thread = None
        self.connected = False

    def connect(self, host: str, port: int):
        self.sock = socket.create_connection((host, port), timeout=10)
        self.sock.settimeout(None)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.connected = True

    def send(self, msg: dict) -> bool:
        if not self.connected:
            return False
        try:
            with self._send_lock:
                send_msg(self.sock, msg)
            return True
        except OSError:
            return False

    def request(self, msg: dict, expected_type: str, timeout: float = 10.0):
        """Blocking send + wait for a reply.  Only valid before the
        receiver thread starts (i.e. during the login handshake)."""
        self.send(msg)
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.sock.settimeout(max(0.1, deadline - time.time()))
            try:
                reply = recv_msg(self.sock)
            except (socket.timeout, TimeoutError):
                break
            except (ProtocolError, OSError):
                return None
            if reply is None:
                return None
            if reply.get("type") in (expected_type, "error"):
                self.sock.settimeout(None)
                return reply
        self.sock.settimeout(None)
        return None

    def start_receiver(self):
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()

    def _recv_loop(self):
        while True:
            try:
                msg = recv_msg(self.sock)
            except (ProtocolError, OSError):
                msg = None
            if msg is None:
                break
            self.incoming.put(msg)
        self.connected = False
        self.incoming.put({"type": "_disconnected"})

    def close(self):
        self.connected = False
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


# ---------------------------------------------------------------------------
# Small themed widgets
# ---------------------------------------------------------------------------

def _entry(parent, show=None, **kw):
    e = tk.Entry(
        parent, bg=FIELD, fg=FG, insertbackground=FG, relief="flat",
        highlightthickness=1, highlightbackground=BORDER,
        highlightcolor=ACCENT, font=(FONT_FAMILY, 11), **kw)
    if show:
        e.configure(show=show)
    return e


def _button(parent, text, command, primary=False, **kw):
    b = tk.Button(
        parent, text=text, command=command, relief="flat", cursor="hand2",
        bg=ACCENT if primary else FIELD,
        fg="#10222b" if primary else FG,
        activebackground=ACCENT_DARK if primary else BORDER,
        activeforeground="#10222b" if primary else FG,
        font=(FONT_FAMILY, 10, "bold" if primary else "normal"),
        bd=0, padx=14, pady=6, **kw)
    return b


class NameDialog(tk.Toplevel):
    """Dark modal prompt for a single text value (e.g. new channel name)."""

    def __init__(self, parent, title, label):
        super().__init__(parent)
        self.result = None
        self.title(title)
        self.configure(bg=PANEL, padx=18, pady=16)
        self.resizable(False, False)
        self.transient(parent)

        tk.Label(self, text=label, bg=PANEL, fg=FG,
                 font=(FONT_FAMILY, 10)).pack(anchor="w")
        self.entry = _entry(self, width=28)
        self.entry.pack(pady=(6, 12), ipady=4)
        row = tk.Frame(self, bg=PANEL)
        row.pack(fill="x")
        _button(row, "Cancel", self._cancel).pack(side="right", padx=(8, 0))
        _button(row, "Create", self._ok, primary=True).pack(side="right")

        self.entry.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self._cancel())
        self.entry.focus_set()
        self.grab_set()
        # Centre over the parent window.
        self.update_idletasks()
        px = parent.winfo_rootx() + parent.winfo_width() // 2
        py = parent.winfo_rooty() + parent.winfo_height() // 2
        self.geometry(f"+{px - self.winfo_width() // 2}"
                      f"+{py - self.winfo_height() // 2}")
        self.wait_window()

    def _ok(self):
        self.result = self.entry.get().strip()
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


# ---------------------------------------------------------------------------
# Login screen
# ---------------------------------------------------------------------------

class LoginFrame(tk.Frame):
    def __init__(self, app):
        super().__init__(app, bg=BG)
        self.app = app

        card = tk.Frame(self, bg=PANEL, padx=36, pady=30,
                        highlightthickness=1, highlightbackground=BORDER)
        card.place(relx=0.5, rely=0.5, anchor="center")

        tk.Label(card, text="TermChat", bg=PANEL, fg=ACCENT,
                 font=(FONT_FAMILY, 22, "bold")).pack()
        tk.Label(card, text="channel chat, TeamSpeak style", bg=PANEL,
                 fg=FG_DIM, font=(FONT_FAMILY, 10)).pack(pady=(0, 18))

        grid = tk.Frame(card, bg=PANEL)
        grid.pack()

        def lbl(text, r, c):
            tk.Label(grid, text=text, bg=PANEL, fg=FG_DIM,
                     font=(FONT_FAMILY, 9)).grid(
                row=r, column=c, sticky="w", pady=(6, 1))

        lbl("SERVER HOST", 0, 0)
        lbl("PORT", 0, 1)
        self.host_e = _entry(grid, width=22)
        self.host_e.insert(0, DEFAULT_HOST)
        self.host_e.grid(row=1, column=0, sticky="we", padx=(0, 8), ipady=4)
        self.port_e = _entry(grid, width=8)
        self.port_e.insert(0, str(DEFAULT_PORT))
        self.port_e.grid(row=1, column=1, sticky="we", ipady=4)

        lbl("USERNAME", 2, 0)
        self.user_e = _entry(grid, width=32)
        self.user_e.grid(row=3, column=0, columnspan=2, sticky="we", ipady=4)

        lbl("PASSWORD", 4, 0)
        self.pass_e = _entry(grid, width=32, show="•")
        self.pass_e.grid(row=5, column=0, columnspan=2, sticky="we", ipady=4)

        self.host_var = tk.BooleanVar(value=False)
        host_chk = tk.Checkbutton(
            card, text="Host a local server on this computer",
            variable=self.host_var, bg=PANEL, fg=FG, selectcolor=FIELD,
            activebackground=PANEL, activeforeground=FG,
            font=(FONT_FAMILY, 9), highlightthickness=0)
        host_chk.pack(anchor="w", pady=(12, 0))

        btns = tk.Frame(card, bg=PANEL)
        btns.pack(fill="x", pady=(16, 0))
        self.login_b = _button(btns, "Log in", self._login, primary=True)
        self.login_b.pack(side="left", expand=True, fill="x", padx=(0, 6))
        self.reg_b = _button(btns, "Register", self._register)
        self.reg_b.pack(side="left", expand=True, fill="x")

        self.status = tk.Label(card, text="", bg=PANEL, fg=RED,
                               font=(FONT_FAMILY, 9), wraplength=280,
                               justify="center")
        self.status.pack(pady=(12, 0))

        for w in (self.host_e, self.port_e, self.user_e, self.pass_e):
            w.bind("<Return>", lambda e: self._login())
        self.user_e.focus_set()

    # -- helpers ------------------------------------------------------------

    def set_status(self, text, color=RED):
        self.status.configure(text=text, fg=color)

    def _set_busy(self, busy: bool):
        state = "disabled" if busy else "normal"
        self.login_b.configure(state=state)
        self.reg_b.configure(state=state)

    def _validate(self):
        host = self.host_e.get().strip() or DEFAULT_HOST
        try:
            port = int(self.port_e.get().strip() or DEFAULT_PORT)
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            self.set_status("Port must be a number between 1 and 65535.")
            return None
        username = self.user_e.get().strip()
        password = self.pass_e.get()
        if not username:
            self.set_status("Username cannot be empty.")
            return None
        if not password:
            self.set_status("Password cannot be empty.")
            return None
        return host, port, username, password

    def _login(self):
        self._go(register_first=False)

    def _register(self):
        self._go(register_first=True)

    def _go(self, register_first: bool):
        creds = self._validate()
        if creds is None:
            return
        host, port, username, password = creds

        if self.host_var.get():
            err = self.app.ensure_local_server(port)
            if err:
                self.set_status(err)
                return
            self.set_status(
                f"Local server running — others can join at {_lan_ip()}:{port}",
                color=GREEN)

        self._set_busy(True)
        if not self.host_var.get():
            self.set_status("Connecting…", color=FG_DIM)
        threading.Thread(
            target=self._worker,
            args=(host, port, username, password, register_first),
            daemon=True).start()

    def _worker(self, host, port, username, password, register_first):
        """Connect + authenticate off the UI thread; report via after()."""
        net = GuiNetwork()
        try:
            net.connect(host, port)
        except OSError as exc:
            self._done(net, error=f"Could not connect to {host}:{port} — {exc}")
            return

        if register_first:
            reply = net.request(
                {"type": "register", "username": username,
                 "password": password}, "register_result")
            if reply is None:
                self._done(net, error="No response from server.")
                return
            if not reply.get("ok"):
                self._done(net, error=reply.get("error")
                           or reply.get("content") or "Registration failed.")
                return

        reply = net.request(
            {"type": "login", "username": username, "password": password},
            "login_result")
        if reply is None:
            self._done(net, error="No response from server.")
            return
        if not reply.get("ok"):
            self._done(net, error=reply.get("error")
                       or reply.get("content") or "Login failed.")
            return
        self._done(net, username=reply["username"])

    def _done(self, net, username=None, error=None):
        def apply():
            self._set_busy(False)
            if error:
                net.close()
                self.set_status(error)
            else:
                self.set_status("")
                self.app.on_logged_in(net, username)
        try:
            self.app.after(0, apply)
        except tk.TclError:
            net.close()


# ---------------------------------------------------------------------------
# Chat screen
# ---------------------------------------------------------------------------

class ChatFrame(tk.Frame):
    SIDEBAR_W = 200

    def __init__(self, app, username):
        super().__init__(app, bg=BG)
        self.app = app
        self.username = username

        self.channels = []              # channel dicts from the server
        self.active_channel_id = None
        self.active_channel_name = None
        self.messages = {}              # channel_id -> list of message dicts
        self.pending_create = None      # channel name we asked to create
        self._user_tags = set()

        # ---- sidebar ------------------------------------------------------
        side = tk.Frame(self, bg=PANEL, width=self.SIDEBAR_W)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)

        head = tk.Frame(side, bg=PANEL)
        head.pack(fill="x", padx=12, pady=(12, 4))
        tk.Label(head, text="TermChat", bg=PANEL, fg=ACCENT,
                 font=(FONT_FAMILY, 13, "bold")).pack(side="left")
        tk.Label(head, text=self.username, bg=PANEL,
                 fg=_color_for(self.username),
                 font=(FONT_FAMILY, 10, "bold")).pack(side="right")

        tk.Label(side, text="CHANNELS", bg=PANEL, fg=FG_DIM,
                 font=(FONT_FAMILY, 8, "bold")).pack(
            anchor="w", padx=12, pady=(10, 2))
        self.ch_list = tk.Listbox(
            side, bg=PANEL, fg=FG, relief="flat", highlightthickness=0,
            selectbackground=FIELD, selectforeground=ACCENT,
            activestyle="none", font=(FONT_FAMILY, 11), height=10,
            cursor="hand2")
        self.ch_list.pack(fill="x", padx=6)
        self.ch_list.bind("<<ListboxSelect>>", self._on_channel_click)

        _button(side, "+  New channel", self._new_channel).pack(
            fill="x", padx=12, pady=(6, 0))

        self.users_lbl = tk.Label(side, text="ONLINE (0)", bg=PANEL,
                                  fg=FG_DIM, font=(FONT_FAMILY, 8, "bold"))
        self.users_lbl.pack(anchor="w", padx=12, pady=(16, 2))
        self.user_list = tk.Listbox(
            side, bg=PANEL, fg=FG, relief="flat", highlightthickness=0,
            selectbackground=PANEL, selectforeground=FG,
            activestyle="none", font=(FONT_FAMILY, 10))
        self.user_list.pack(fill="both", expand=True, padx=6)

        _button(side, "Log out", self._logout).pack(
            fill="x", padx=12, pady=10)

        # ---- main column ----------------------------------------------------
        main = tk.Frame(self, bg=BG)
        main.pack(side="left", fill="both", expand=True)

        header = tk.Frame(main, bg=PANEL_2, height=44,
                          highlightthickness=1, highlightbackground=BORDER)
        header.pack(fill="x")
        header.pack_propagate(False)
        self.header_lbl = tk.Label(
            header, text="Pick a channel on the left", bg=PANEL_2, fg=FG,
            font=(FONT_FAMILY, 12, "bold"))
        self.header_lbl.pack(side="left", padx=14)

        # Bottom rows are packed first (side="bottom") so the expanding
        # chat body can never squeeze them out of a small window.
        self.status = tk.Label(main, text="", bg=BG, fg=YELLOW, anchor="w",
                               font=(FONT_FAMILY, 9))
        self.status.pack(side="bottom", fill="x", padx=14, pady=(0, 6))

        inrow = tk.Frame(main, bg=BG, pady=10)
        inrow.pack(side="bottom", fill="x", padx=12)
        self.input_e = _entry(inrow)
        self.input_e.pack(side="left", fill="x", expand=True,
                          ipady=7, padx=(0, 8))
        self.input_e.bind("<Return>", lambda e: self._send())
        _button(inrow, "Send", self._send, primary=True).pack(side="right")

        body = tk.Frame(main, bg=PANEL_2)
        body.pack(fill="both", expand=True)
        self.text = tk.Text(
            body, bg=PANEL_2, fg=FG, relief="flat", wrap="word", height=8,
            state="disabled", font=(FONT_FAMILY, 11), padx=14, pady=10,
            insertbackground=FG, highlightthickness=0, spacing1=2, spacing3=2)
        sb = tk.Scrollbar(body, command=self.text.yview,
                          troughcolor=PANEL_2, bg=PANEL, relief="flat",
                          activebackground=BORDER, width=10, bd=0)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

        self.text.tag_configure("ts", foreground=FG_DIM,
                                font=(MONO_FAMILY, 9))
        self.text.tag_configure("sys", foreground=FG_DIM,
                                font=(FONT_FAMILY, 10, "italic"))
        self.text.tag_configure("body", foreground=FG)

        self.input_e.focus_set()

    # -- server messages ----------------------------------------------------

    def handle(self, msg: dict):
        mtype = msg.get("type")
        if mtype == "channels":
            self.channels = msg.get("channels", [])
            self._render_channels()
            # First channel list after login: auto-join the first channel.
            if self.active_channel_id is None and self.channels:
                self._join(self.channels[0]["id"])
        elif mtype == "channel_created":
            ch = msg.get("channel")
            if ch and not any(c["id"] == ch["id"] for c in self.channels):
                self.channels.append(ch)
                self._render_channels()
            if ch:
                self.set_status(f"new channel #{ch['name']}")
                if self.pending_create and \
                        ch["name"].lower() == self.pending_create.lower():
                    self.pending_create = None
                    self._join(ch["id"])
        elif mtype == "join_result":
            if msg.get("ok"):
                self.active_channel_id = msg["channel_id"]
                self.active_channel_name = msg["name"]
                self.header_lbl.configure(text=f"#  {msg['name']}")
                self.set_status("")
                self._render_channels()
                self._render_messages()
            else:
                self.set_status(msg.get("error") or "could not join channel")
        elif mtype == "history":
            cid = msg["channel_id"]
            self.messages[cid] = list(msg.get("messages", []))
            if cid == self.active_channel_id:
                self._render_messages()
        elif mtype == "message":
            cid = msg["channel_id"]
            self.messages.setdefault(cid, []).append(msg)
            if cid == self.active_channel_id:
                self._append_message(msg)
        elif mtype == "users":
            if msg["channel_id"] == self.active_channel_id:
                self._render_users(msg.get("users", []))
        elif mtype == "presence":
            cid = msg["channel_id"]
            verb = "joined" if msg.get("status") == "online" else "left"
            note = {"system": True, "timestamp": int(time.time()),
                    "content": f"{msg.get('username')} {verb} the channel"}
            self.messages.setdefault(cid, []).append(note)
            if cid == self.active_channel_id:
                self._append_message(note)
        elif mtype == "system":
            note = {"system": True, "timestamp": int(time.time()),
                    "content": msg.get("content", "")}
            cid = msg.get("channel_id")
            if cid is None or cid == self.active_channel_id:
                self._append_message(note)
            if cid is not None:
                self.messages.setdefault(cid, []).append(note)
        elif mtype == "error":
            self.set_status(msg.get("content", "server error"))

    # -- rendering ----------------------------------------------------------

    def set_status(self, text):
        self.status.configure(text=text)

    def _render_channels(self):
        self.ch_list.delete(0, "end")
        for ch in self.channels:
            marker = "#  " if ch["id"] == self.active_channel_id else "#  "
            self.ch_list.insert("end", f"{marker}{ch['name']}")
            if ch["id"] == self.active_channel_id:
                self.ch_list.itemconfigure("end", foreground=ACCENT)
        self.ch_list.configure(height=max(4, min(len(self.channels), 12)))

    def _render_users(self, users):
        self.users_lbl.configure(text=f"ONLINE ({len(users)})")
        self.user_list.delete(0, "end")
        for name in users:
            self.user_list.insert("end", f"● {name}")
            self.user_list.itemconfigure("end", foreground=_color_for(name))

    def _tag_for_user(self, name):
        tag = "u_" + name
        if tag not in self._user_tags:
            self.text.tag_configure(
                tag, foreground=_color_for(name),
                font=(FONT_FAMILY, 11, "bold"))
            self._user_tags.add(tag)
        return tag

    def _render_messages(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for m in self.messages.get(self.active_channel_id, []):
            self._insert_message(m)
        self.text.configure(state="disabled")
        self.text.see("end")

    def _append_message(self, msg):
        # Stick to the bottom only if the user is already near it.
        follow = self.text.yview()[1] > 0.98
        self.text.configure(state="normal")
        self._insert_message(msg)
        self.text.configure(state="disabled")
        if follow:
            self.text.see("end")

    def _insert_message(self, msg):
        ts = datetime.fromtimestamp(
            msg.get("timestamp", 0)).strftime("%H:%M")
        if msg.get("system"):
            self.text.insert("end", f"  — {msg.get('content', '')} —\n", "sys")
            return
        author = msg.get("username", "?")
        self.text.insert("end", f"{ts}  ", "ts")
        self.text.insert("end", f"{author}  ", self._tag_for_user(author))
        self.text.insert("end", msg.get("content", "") + "\n", "body")

    # -- actions ------------------------------------------------------------

    def _on_channel_click(self, _event):
        sel = self.ch_list.curselection()
        if not sel:
            return
        idx = sel[0]
        if 0 <= idx < len(self.channels):
            self._join(self.channels[idx]["id"])

    def _join(self, channel_id):
        self.app.net.send({"type": "join_channel", "channel_id": channel_id})

    def _new_channel(self):
        dlg = NameDialog(self.app, "New channel", "Channel name")
        name = dlg.result
        if name:
            self.pending_create = name.lstrip("#").strip()
            self.app.net.send({"type": "create_channel", "name": name})

    def _send(self):
        text = self.input_e.get().strip()
        if not text:
            return
        if self.active_channel_id is None:
            self.set_status("join a channel first")
            return
        if self.app.net.send({"type": "send_message", "content": text}):
            self.input_e.delete(0, "end")
        else:
            self.set_status("not connected")

    def _logout(self):
        self.app.logout()


# ---------------------------------------------------------------------------
# Application shell
# ---------------------------------------------------------------------------

class TermChatApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"TermChat {__version__}")
        self.configure(bg=BG)
        self.geometry("980x620")
        self.minsize(720, 460)
        try:
            self.iconbitmap(_resource_path(os.path.join("assets",
                                                        "termchat.ico")))
        except tk.TclError:
            pass

        self.net = None
        self.local_server = None
        self.local_server_port = None
        self.frame = None

        self.show_login()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll()

    # -- screen switching ---------------------------------------------------

    def _swap(self, frame):
        if self.frame is not None:
            self.frame.destroy()
        self.frame = frame
        self.frame.pack(fill="both", expand=True)

    def show_login(self, status="", color=RED):
        login = LoginFrame(self)
        self._swap(login)
        if status:
            login.set_status(status, color)

    def on_logged_in(self, net: GuiNetwork, username: str):
        self.net = net
        chat = ChatFrame(self, username)
        self._swap(chat)
        net.start_receiver()
        net.send({"type": "list_channels"})

    # -- incoming message pump ---------------------------------------------

    def _poll(self):
        if self.net is not None:
            try:
                while True:
                    msg = self.net.incoming.get_nowait()
                    self._dispatch(msg)
            except queue.Empty:
                pass
        self.after(50, self._poll)

    def _dispatch(self, msg):
        if msg.get("type") == "_disconnected":
            self.net.close()
            self.net = None
            self.show_login("Disconnected from server.")
            return
        if isinstance(self.frame, ChatFrame):
            self.frame.handle(msg)

    # -- embedded server ----------------------------------------------------

    def ensure_local_server(self, port: int):
        """Start the embedded server once; return an error string or None."""
        if self.local_server is not None:
            if self.local_server_port == port:
                return None
            return (f"Local server already running on port "
                    f"{self.local_server_port} — use that port.")
        # Quick pre-check so a port conflict fails here, not in the thread.
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("0.0.0.0", port))
        except OSError:
            return (f"Port {port} is already in use — is a server "
                    f"already running?")
        finally:
            probe.close()

        from .server import Server
        server = Server("0.0.0.0", port, _default_db_path())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        # Give the listener a moment to come up before we connect to it.
        time.sleep(0.2)
        self.local_server = server
        self.local_server_port = port
        return None

    # -- teardown -----------------------------------------------------------

    def logout(self):
        if self.net is not None:
            self.net.send({"type": "logout"})
            self.net.close()
            self.net = None
        self.show_login("Logged out.", color=FG_DIM)

    def _on_close(self):
        if self.net is not None:
            self.net.send({"type": "logout"})
            self.net.close()
        if self.local_server is not None:
            try:
                self.local_server.shutdown()
            except Exception:
                pass
        self.destroy()


def main(argv=None):
    if sys.platform == "win32":
        # Crisp text on high-DPI displays.
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    app = TermChatApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
