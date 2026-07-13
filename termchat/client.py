"""termchat terminal client.

A full-screen TUI built on `rich`.  Layout::

    +--------------+--------------------------------------+
    |  CHANNELS    |  #general                            |
    |  > # general |  [12:00:01] alice: hello everyone    |
    |    # random  |  [12:00:12] bob:   hi alice          |
    |    # help    |                                      |
    |              |                                      |
    |  ONLINE (2)  |                                      |
    |  * alice     |                                      |
    |  * bob       +--------------------------------------+
    |              |  > type a message and press Enter    |
    +--------------+--------------------------------------+

The main thread renders and reads the keyboard in raw mode; a background
thread reads messages from the server and updates shared state.  The two
communicate through a lock-guarded :class:`ClientModel`.

Keyboard shortcuts
------------------

    Enter          send the current line (or run a /command)
    Ctrl-N / Down  switch to the next channel
    Ctrl-P / Up    switch to the previous channel
    Backspace      delete a character
    Ctrl-C / Ctrl-Q  quit

Slash commands
--------------

    /help                 show help
    /create <name>        create a new channel
    /join <name|id>       join a channel by name or number
    /channels             refresh the channel list
    /users                refresh the online-user list
    /quit                 disconnect and exit
"""

import argparse
import os
import select
import socket
import sys
import threading
import time
from datetime import datetime

from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from . import DEFAULT_HOST, DEFAULT_PORT
from .protocol import ProtocolError, recv_msg, send_msg

# Colours cycled through to give each speaker a stable, distinct colour.
_NAME_COLORS = [
    "cyan", "green", "yellow", "magenta", "blue", "bright_red",
    "bright_green", "bright_yellow", "bright_magenta", "bright_cyan",
]


def _color_for(name: str) -> str:
    return _NAME_COLORS[hash(name) % len(_NAME_COLORS)]


class ClientModel:
    """All UI state, guarded by a lock so the receiver thread can update it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.username = None
        self.channels = []              # list of channel dicts
        self.selected_index = 0         # index into self.channels (sidebar)
        self.active_channel_id = None   # channel we've actually joined
        self.active_channel_name = None
        self.messages = {}              # channel_id -> list of message dicts
        self.online = {}                # channel_id -> list of usernames
        self.input_buffer = ""
        self.status_line = ""
        self.running = True

    def selected_channel(self):
        with self.lock:
            if 0 <= self.selected_index < len(self.channels):
                return dict(self.channels[self.selected_index])
        return None


class NetworkClient:
    """Owns the socket and the background receive loop."""

    def __init__(self, host, port, model: ClientModel):
        self.host = host
        self.port = port
        self.model = model
        self.sock = None
        self._send_lock = threading.Lock()
        self._recv_thread = None
        # Simple request/response gate used during the login screen.
        self._pending = None
        self._pending_event = threading.Event()

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=10)
        self.sock.settimeout(None)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def send(self, msg: dict):
        with self._send_lock:
            send_msg(self.sock, msg)

    # -- synchronous request/response (used before the TUI starts) ----------

    def request(self, msg: dict, expected_type: str, timeout: float = 10.0):
        """Send ``msg`` and block until a reply of ``expected_type`` arrives.

        Only safe to use before :meth:`start_receiver` is running.
        """
        self.send(msg)
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.sock.settimeout(max(0.1, deadline - time.time()))
            try:
                reply = recv_msg(self.sock)
            except (socket.timeout, TimeoutError):
                break
            except ProtocolError:
                return None
            if reply is None:
                return None
            if reply.get("type") == expected_type:
                self.sock.settimeout(None)
                return reply
            if reply.get("type") == "error":
                self.sock.settimeout(None)
                return reply
            # Ignore anything unexpected (e.g. an early channels push).
        self.sock.settimeout(None)
        return None

    # -- async receive loop (used once the TUI is up) -----------------------

    def start_receiver(self):
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()

    def _recv_loop(self):
        try:
            while self.model.running:
                try:
                    msg = recv_msg(self.sock)
                except (ProtocolError, OSError):
                    break
                if msg is None:
                    break
                self._handle(msg)
        finally:
            with self.model.lock:
                if self.model.running:
                    self.model.status_line = "! disconnected from server"
                self.model.running = False

    def _handle(self, msg: dict):
        mtype = msg.get("type")
        m = self.model
        if mtype == "channels":
            with m.lock:
                m.channels = msg.get("channels", [])
                if m.selected_index >= len(m.channels):
                    m.selected_index = max(0, len(m.channels) - 1)
        elif mtype == "channel_created":
            ch = msg.get("channel")
            with m.lock:
                if ch and not any(c["id"] == ch["id"] for c in m.channels):
                    m.channels.append(ch)
                m.status_line = f"* new channel #{ch['name']}" if ch else m.status_line
        elif mtype == "join_result":
            if msg.get("ok"):
                with m.lock:
                    m.active_channel_id = msg["channel_id"]
                    m.active_channel_name = msg["name"]
                    m.status_line = f"joined #{msg['name']}"
            else:
                with m.lock:
                    m.status_line = f"! {msg.get('error')}"
        elif mtype == "history":
            cid = msg["channel_id"]
            with m.lock:
                m.messages[cid] = list(msg.get("messages", []))
        elif mtype == "message":
            cid = msg["channel_id"]
            with m.lock:
                m.messages.setdefault(cid, []).append(msg)
        elif mtype == "users":
            cid = msg["channel_id"]
            with m.lock:
                m.online[cid] = msg.get("users", [])
        elif mtype == "presence":
            cid = msg["channel_id"]
            verb = "joined" if msg.get("status") == "online" else "left"
            with m.lock:
                m.messages.setdefault(cid, []).append({
                    "system": True, "timestamp": int(time.time()),
                    "content": f"{msg.get('username')} {verb} the channel"})
        elif mtype == "system":
            cid = msg.get("channel_id")
            with m.lock:
                m.messages.setdefault(cid, []).append({
                    "system": True, "timestamp": int(time.time()),
                    "content": msg.get("content", "")})
        elif mtype == "error":
            with m.lock:
                m.status_line = f"! {msg.get('content')}"


# ---------------------------------------------------------------------------
# Login / registration screen (plain rich prompts, no raw mode yet)
# ---------------------------------------------------------------------------

def login_screen(net: NetworkClient, console: Console) -> str:
    """Run the login/registration flow.  Returns the logged-in username."""
    console.clear()
    console.print(Panel(
        Align.center(Text("termchat", style="bold cyan")
                     + Text("\na TeamSpeak-style terminal chat", style="dim")),
        border_style="cyan", padding=(1, 4)))

    while True:
        console.print()
        choice = Prompt.ask(
            "[bold]Choose[/bold]", choices=["login", "register", "quit"],
            default="login")
        if choice == "quit":
            console.print("bye!")
            raise SystemExit(0)

        username = Prompt.ask("username").strip()
        if not username:
            console.print("[red]username cannot be empty[/red]")
            continue
        password = Prompt.ask("password", password=True)

        if choice == "register":
            reply = net.request(
                {"type": "register", "username": username, "password": password},
                "register_result")
            if reply is None:
                console.print("[red]no response from server[/red]")
                continue
            if not reply.get("ok"):
                console.print(f"[red]{reply.get('error') or reply.get('content')}[/red]")
                continue
            console.print("[green]account created — logging in...[/green]")

        reply = net.request(
            {"type": "login", "username": username, "password": password},
            "login_result")
        if reply is None:
            console.print("[red]no response from server[/red]")
            continue
        if not reply.get("ok"):
            console.print(f"[red]{reply.get('error') or reply.get('content')}[/red]")
            continue
        return reply["username"]


# ---------------------------------------------------------------------------
# The main TUI
# ---------------------------------------------------------------------------

class ChatTUI:
    def __init__(self, net: NetworkClient, model: ClientModel, console: Console):
        self.net = net
        self.model = model
        self.console = console

    # -- rendering ----------------------------------------------------------

    def _render_sidebar(self) -> Panel:
        m = self.model
        with m.lock:
            channels = list(m.channels)
            selected = m.selected_index
            active = m.active_channel_id
            online = list(m.online.get(active, [])) if active is not None else []

        ch_table = Table.grid(padding=(0, 1))
        ch_table.add_column(no_wrap=True)
        ch_table.add_row(Text("CHANNELS", style="bold underline"))
        for i, ch in enumerate(channels):
            label = f"# {ch['name']}"
            if i == selected and ch["id"] == active:
                style = "bold black on cyan"
                marker = "> "
            elif i == selected:
                style = "bold cyan"
                marker = "> "
            elif ch["id"] == active:
                style = "green"
                marker = "  "
            else:
                style = "white"
                marker = "  "
            ch_table.add_row(Text(marker + label, style=style))

        ch_table.add_row(Text(""))
        ch_table.add_row(Text(f"ONLINE ({len(online)})", style="bold underline"))
        if online:
            for name in online:
                ch_table.add_row(Text("* " + name, style=_color_for(name)))
        else:
            ch_table.add_row(Text("  (nobody)", style="dim"))

        return Panel(ch_table, title="termchat", border_style="cyan")

    def _render_chat(self) -> Panel:
        m = self.model
        with m.lock:
            active = m.active_channel_id
            name = m.active_channel_name
            msgs = list(m.messages.get(active, [])) if active is not None else []

        # Only render the last N messages so we never overflow the panel.
        height = max(self.console.size.height - 6, 5)
        visible = msgs[-height:]

        lines = []
        for msg in visible:
            ts = datetime.fromtimestamp(msg.get("timestamp", 0)).strftime("%H:%M:%S")
            if msg.get("system"):
                lines.append(Text(f"  -- {msg['content']} --",
                                  style="dim italic"))
                continue
            author = msg.get("username", "?")
            line = Text()
            line.append(f"[{ts}] ", style="dim")
            line.append(f"{author}: ", style=f"bold {_color_for(author)}")
            line.append(msg.get("content", ""))
            lines.append(line)

        body = Group(*lines) if lines else Align.center(
            Text("No messages yet — say hello!", style="dim"))
        title = f"#{name}" if name else "no channel — press Ctrl-N to pick one"
        return Panel(body, title=title, border_style="green")

    def _render_input(self) -> Panel:
        m = self.model
        with m.lock:
            buf = m.input_buffer
            status = m.status_line
        prompt = Text("> ", style="bold cyan")
        prompt.append(buf)
        prompt.append("_", style="blink")
        if status:
            prompt.append("    ")
            prompt.append(status, style="yellow")
        return Panel(prompt, border_style="blue")

    def _render(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="body"),
            Layout(self._render_input(), name="input", size=3),
        )
        layout["body"].split_row(
            Layout(self._render_sidebar(), name="sidebar", size=24),
            Layout(self._render_chat(), name="chat"),
        )
        return layout

    # -- input handling -----------------------------------------------------

    def _switch_channel(self, delta: int):
        m = self.model
        with m.lock:
            if not m.channels:
                return
            m.selected_index = (m.selected_index + delta) % len(m.channels)
            target = m.channels[m.selected_index]["id"]
        self.net.send({"type": "join_channel", "channel_id": target})

    def _submit(self):
        m = self.model
        with m.lock:
            text = m.input_buffer.strip()
            m.input_buffer = ""
        if not text:
            return
        if text.startswith("/"):
            self._command(text)
        else:
            with m.lock:
                joined = m.active_channel_id is not None
            if not joined:
                with m.lock:
                    m.status_line = "! join a channel first (Ctrl-N)"
                return
            self.net.send({"type": "send_message", "content": text})

    def _command(self, text: str):
        parts = text[1:].split(maxsplit=1)
        cmd = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""
        m = self.model

        if cmd in ("quit", "exit", "q"):
            with m.lock:
                m.running = False
        elif cmd == "help":
            with m.lock:
                m.status_line = ("cmds: /create <n> /join <n|id> /channels "
                                 "/users /quit  |  Ctrl-N/P switch channel")
        elif cmd == "create":
            if arg:
                self.net.send({"type": "create_channel", "name": arg})
            else:
                with m.lock:
                    m.status_line = "usage: /create <name>"
        elif cmd == "join":
            self._join_by_arg(arg)
        elif cmd == "channels":
            self.net.send({"type": "list_channels"})
        elif cmd == "users":
            with m.lock:
                active = m.active_channel_id
            if active is not None:
                self.net.send({"type": "list_users", "channel_id": active})
        else:
            with m.lock:
                m.status_line = f"! unknown command /{cmd} (try /help)"

    def _join_by_arg(self, arg: str):
        m = self.model
        if not arg:
            with m.lock:
                m.status_line = "usage: /join <name|id>"
            return
        target = None
        with m.lock:
            channels = list(m.channels)
        if arg.isdigit():
            cid = int(arg)
            target = next((c for c in channels if c["id"] == cid), None)
        if target is None:
            wanted = arg.lstrip("#").lower()
            target = next(
                (c for c in channels if c["name"].lower() == wanted), None)
        if target is None:
            with m.lock:
                m.status_line = f"! no channel matching {arg!r}"
            return
        with m.lock:
            m.selected_index = channels.index(target)
        self.net.send({"type": "join_channel", "channel_id": target["id"]})

    def _process_input(self, data: bytes):
        """Parse a chunk of raw stdin bytes into actions."""
        m = self.model
        i = 0
        n = len(data)
        while i < n:
            b = data[i]
            if b == 0x1b:  # ESC — possibly an arrow-key sequence
                if i + 2 < n and data[i + 1] == ord("["):
                    code = data[i + 2]
                    if code == ord("A"):        # up
                        self._switch_channel(-1)
                    elif code == ord("B"):      # down
                        self._switch_channel(1)
                    # left/right (C/D) ignored
                    i += 3
                    continue
                i += 1
                continue
            if b in (0x03, 0x11):  # Ctrl-C / Ctrl-Q
                with m.lock:
                    m.running = False
                return
            if b == 0x0e:  # Ctrl-N
                self._switch_channel(1)
                i += 1
                continue
            if b == 0x10:  # Ctrl-P
                self._switch_channel(-1)
                i += 1
                continue
            if b in (0x0d, 0x0a):  # Enter
                self._submit()
                i += 1
                continue
            if b in (0x7f, 0x08):  # Backspace
                with m.lock:
                    m.input_buffer = m.input_buffer[:-1]
                i += 1
                continue
            if b < 0x20:  # other control chars: ignore
                i += 1
                continue
            # Printable run — collect consecutive non-control bytes and
            # decode them together so multi-byte UTF-8 survives.
            j = i
            while j < n and data[j] >= 0x20 and data[j] != 0x7f:
                j += 1
            try:
                chunk = data[i:j].decode("utf-8", errors="ignore")
            except Exception:
                chunk = ""
            with m.lock:
                m.input_buffer += chunk
                m.status_line = ""  # typing clears stale status
            i = j

    # -- main loop ----------------------------------------------------------

    def run(self):
        import termios
        import tty

        fd = sys.stdin.fileno()
        old_attrs = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            with Live(self._render(), console=self.console, screen=True,
                      refresh_per_second=20, transient=True) as live:
                while self.model.running:
                    r, _, _ = select.select([sys.stdin], [], [], 0.08)
                    if r:
                        try:
                            data = os.read(fd, 4096)
                        except OSError:
                            data = b""
                        if data:
                            self._process_input(data)
                    live.update(self._render())
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)


def main(argv=None):
    parser = argparse.ArgumentParser(description="termchat client")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"server host (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"server port (default: {DEFAULT_PORT})")
    args = parser.parse_args(argv)

    console = Console()
    if not sys.stdin.isatty():
        console.print("[red]termchat client needs an interactive terminal.[/red]")
        return 1

    model = ClientModel()
    net = NetworkClient(args.host, args.port, model)

    try:
        net.connect()
    except OSError as exc:
        console.print(f"[red]could not connect to {args.host}:{args.port}: {exc}[/red]")
        return 1

    try:
        username = login_screen(net, console)
    except (KeyboardInterrupt, EOFError):
        console.print("\nbye!")
        return 0

    with model.lock:
        model.username = username

    # Switch to async mode and enter the first channel automatically.
    net.start_receiver()
    net.send({"type": "list_channels"})
    time.sleep(0.15)
    first = model.selected_channel()
    if first is not None:
        net.send({"type": "join_channel", "channel_id": first["id"]})

    tui = ChatTUI(net, model, console)
    try:
        tui.run()
    except KeyboardInterrupt:
        pass
    finally:
        model.running = False
        try:
            net.send({"type": "logout"})
        except OSError:
            pass
        try:
            net.sock.close()
        except OSError:
            pass
    console.print(f"[cyan]disconnected. bye, {username}![/cyan]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
