"""Browser client for termchat: a tiny HTTP server that serves a
single-page chat UI and bridges it to the chat :class:`~termchat.server.Server`
over a hand-rolled WebSocket connection (see :mod:`termchat.ws`).

Runs in the same process as the TCP server and shares the same
``Server`` instance, so browser users, TUI users and GUI users are all
in the same chat, seeing each other's presence and messages live.
"""

import json
import logging
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import ws as wsproto
from .server import ClientState, Server

log = logging.getLogger("termchat.web")


class WSClientState(ClientState):
    """A :class:`ClientState` whose ``send`` speaks WebSocket frames."""

    def send(self, msg: dict) -> bool:
        with self._send_lock:
            try:
                wsproto.send_frame(self.conn, json.dumps(msg).encode("utf-8"))
                return True
            except OSError:
                return False


def _serve_ws_client(chat_server: Server, conn, addr):
    state = WSClientState(conn, addr)
    chat_server.register_client(state)
    log.info("browser connection from %s", addr)
    try:
        while True:
            frame = wsproto.recv_frame(conn)
            if frame is None:
                break
            opcode, payload = frame
            if opcode == wsproto.OP_CLOSE:
                break
            if opcode == wsproto.OP_PING:
                try:
                    wsproto.send_frame(conn, payload, wsproto.OP_PONG)
                except OSError:
                    break
                continue
            if opcode != wsproto.OP_TEXT:
                continue
            try:
                msg = json.loads(payload.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                state.send({"type": "error", "content": "malformed message"})
                continue
            if not isinstance(msg, dict):
                state.send({"type": "error", "content": "malformed message"})
                continue
            chat_server.dispatch(state, msg)
    except OSError:
        pass
    finally:
        chat_server.disconnect_client(state)
        log.info("browser disconnected %s", addr)


def _make_handler(chat_server: Server):
    class Handler(BaseHTTPRequestHandler):
        server_version = "termchat-web/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log.debug("%s - %s", self.address_string(), fmt % args)

        def do_GET(self):
            if self.path == "/ws":
                self._upgrade_to_websocket()
                return
            if self.path in ("/", "/index.html"):
                body = INDEX_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(404, "not found")

        def _upgrade_to_websocket(self):
            if self.headers.get("Upgrade", "").lower() != "websocket":
                self.send_error(400, "expected a WebSocket upgrade")
                return
            key = self.headers.get("Sec-WebSocket-Key")
            if not key:
                self.send_error(400, "missing Sec-WebSocket-Key")
                return
            accept = wsproto.accept_key(key)
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            # Hand the raw connection off to the WS/chat bridge; the
            # HTTP server must not touch it again after this point.
            conn = self.connection
            addr = self.client_address
            self.close_connection = True
            _serve_ws_client(chat_server, conn, addr)

    return Handler


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def shutdown_request(self, request):
        # The WS bridge already closed the socket itself once the chat
        # session ends; the base implementation would try to shut it
        # down again and raise "Bad file descriptor".
        try:
            super().shutdown_request(request)
        except OSError:
            pass


def run_web(chat_server: Server, host: str, port: int):
    """Serve the browser client. Blocks; call via :func:`run_web_in_thread`."""
    httpd = _ThreadingHTTPServer((host, port), _make_handler(chat_server))
    log.info("termchat browser client on http://%s:%s", host, port)
    httpd.serve_forever()


def run_web_in_thread(chat_server: Server, host: str, port: int) -> threading.Thread:
    t = threading.Thread(
        target=run_web, args=(chat_server, host, port), daemon=True)
    t.start()
    return t


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover">
<title>termchat</title>
<style>
  :root {
    --bg: #10141a; --panel: #161b22; --border: #2a3038;
    --text: #d6dce5; --dim: #7a8494; --accent: #4f9dff;
    --online: #3fb950; --danger: #ff6b6b;
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body { height: 100%; margin: 0; overflow-x: hidden; }
  body {
    background: var(--bg); color: var(--text);
    font: 14px/1.4 "SF Mono", Consolas, "Liberation Mono", monospace;
    -webkit-text-size-adjust: 100%; text-size-adjust: 100%;
    display: flex; align-items: center; justify-content: center;
    padding: env(safe-area-inset-top) env(safe-area-inset-right)
             env(safe-area-inset-bottom) env(safe-area-inset-left);
  }
  .hidden { display: none !important; }

  /* ---- auth screen ---- */
  #auth {
    width: min(320px, 92vw); padding: 24px; background: var(--panel);
    border: 1px solid var(--border); border-radius: 8px;
  }
  #auth h1 { margin: 0 0 4px; font-size: 18px; }
  #auth p.sub { margin: 0 0 16px; color: var(--dim); font-size: 12px; }
  #auth input {
    width: 100%; padding: 10px; margin-bottom: 10px; font-size: 16px;
    background: #0d1117; border: 1px solid var(--border); border-radius: 4px;
    color: var(--text); font-family: inherit;
  }
  #auth .row { display: flex; gap: 8px; }
  #auth button {
    flex: 1; padding: 10px; min-height: 44px; border-radius: 4px; border: 1px solid var(--border);
    background: #1c2128; color: var(--text); font: inherit; cursor: pointer;
  }
  #auth button.primary { background: var(--accent); border-color: var(--accent); color: #06111f; font-weight: 600; }
  #auth button:hover { filter: brightness(1.15); }
  #auth .msg { min-height: 18px; margin-top: 10px; font-size: 12px; color: var(--danger); }

  /* ---- chat screen ---- */
  #chat {
    width: 100vw; height: 100vh; height: 100dvh; display: flex; overflow: hidden;
    position: relative;
  }
  #sidebar {
    width: 220px; flex: none; background: var(--panel);
    border-right: 1px solid var(--border); display: flex; flex-direction: column;
    padding-top: env(safe-area-inset-top); padding-left: env(safe-area-inset-left);
  }
  #sidebar-backdrop { display: none; }
  #menu-btn { display: none; }
  #sidebar h2 {
    font-size: 11px; letter-spacing: .06em; text-transform: uppercase;
    color: var(--dim); margin: 16px 12px 6px;
  }
  #channel-list, #user-list { list-style: none; margin: 0; padding: 0 6px; overflow-y: auto; }
  #channel-list { flex: 1; }
  #user-list { max-height: 30%; }
  #channel-list li, #user-list li {
    padding: 6px 8px; min-height: 36px; display: flex; align-items: center;
    border-radius: 4px; cursor: pointer; color: var(--dim);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  #channel-list li:hover { background: #1c2128; color: var(--text); }
  #channel-list li.active { background: #1c2128; color: var(--accent); font-weight: 600; }
  #user-list li { cursor: default; color: var(--text); }
  #user-list li::before { content: "\25CF "; color: var(--online); }
  #new-channel {
    margin: 8px; padding: 6px 8px; border-radius: 4px; border: 1px dashed var(--border);
    background: transparent; color: var(--dim); font: inherit; cursor: pointer;
  }
  #new-channel:hover { color: var(--text); border-color: var(--accent); }
  #whoami {
    padding: 10px 12px; border-top: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between;
    font-size: 12px; color: var(--dim);
  }
  #whoami button { background: none; border: none; color: var(--dim); cursor: pointer; font: inherit; }
  #whoami button:hover { color: var(--danger); }

  #main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  #channel-header {
    padding: 12px 16px; padding-top: calc(12px + env(safe-area-inset-top));
    padding-right: calc(16px + env(safe-area-inset-right));
    border-bottom: 1px solid var(--border); font-weight: 600;
    display: flex; align-items: center; gap: 10px; flex: none;
  }
  #menu-btn {
    background: none; border: none; color: var(--text); font-size: 20px;
    line-height: 1; cursor: pointer; padding: 4px 6px; margin: -4px -6px -4px 0;
  }
  #messages { flex: 1; overflow-y: auto; -webkit-overflow-scrolling: touch; padding: 12px 16px; }
  .msg-line { margin-bottom: 6px; word-wrap: break-word; overflow-wrap: anywhere; }
  .msg-line .ts { color: var(--dim); margin-right: 6px; font-size: 12px; }
  .msg-line .user { font-weight: 600; margin-right: 4px; }
  .msg-line.system { color: var(--dim); font-style: italic; }
  .msg-line.error { color: var(--danger); }
  #input-row {
    display: flex; gap: 8px; padding: 12px 16px; flex: none;
    padding-bottom: calc(12px + env(safe-area-inset-bottom));
    padding-left: calc(16px + env(safe-area-inset-left));
    padding-right: calc(16px + env(safe-area-inset-right));
    border-top: 1px solid var(--border);
  }
  #input-row input {
    flex: 1; padding: 10px 12px; min-height: 44px; font-size: 16px;
    background: #0d1117; border: 1px solid var(--border);
    border-radius: 4px; color: var(--text); font-family: inherit;
  }
  #input-row button {
    padding: 10px 16px; min-height: 44px; border-radius: 4px; border: none;
    background: var(--accent); color: #06111f; font-weight: 600; cursor: pointer;
  }
  #input-row button:disabled { opacity: .5; cursor: default; }

  /* ---- narrow / mobile screens: sidebar becomes an off-canvas drawer ---- */
  @media (max-width: 760px) {
    #sidebar {
      position: fixed; top: 0; left: 0; bottom: 0; z-index: 20;
      width: min(280px, 82vw); transform: translateX(-100%);
      transition: transform .2s ease; box-shadow: 2px 0 16px rgba(0,0,0,.4);
    }
    #chat.sidebar-open #sidebar { transform: translateX(0); }
    #sidebar-backdrop {
      display: none; position: fixed; inset: 0; background: rgba(0,0,0,.5); z-index: 15;
    }
    #chat.sidebar-open #sidebar-backdrop { display: block; }
    #menu-btn { display: block; }
    #channel-header { padding-left: 12px; }
  }
</style>
</head>
<body>

<div id="auth">
  <h1>termchat</h1>
  <p class="sub">connecting to <span id="auth-target"></span></p>
  <input id="username" placeholder="username" autocomplete="username">
  <input id="password" type="password" placeholder="password" autocomplete="current-password">
  <div class="row">
    <button id="btn-login" class="primary">Log in</button>
    <button id="btn-register">Register</button>
  </div>
  <div class="msg" id="auth-msg"></div>
</div>

<div id="chat" class="hidden">
  <div id="sidebar-backdrop"></div>
  <div id="sidebar">
    <h2>Channels</h2>
    <ul id="channel-list"></ul>
    <button id="new-channel">+ new channel</button>
    <h2>Online</h2>
    <ul id="user-list"></ul>
    <div id="whoami">
      <span id="me"></span>
      <button id="btn-logout">log out</button>
    </div>
  </div>
  <div id="main">
    <div id="channel-header"><button id="menu-btn" aria-label="channels">&#9776;</button><span id="channel-name">#</span></div>
    <div id="messages"></div>
    <div id="input-row">
      <input id="msg-input" placeholder="type a message and press Enter" disabled>
      <button id="btn-send" disabled>Send</button>
    </div>
  </div>
</div>

<script>
(function () {
  "use strict";
  const wsUrl = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";
  document.getElementById("auth-target").textContent = location.host;

  const el = (id) => document.getElementById(id);
  const authScreen = el("auth"), chatScreen = el("chat");
  const authMsg = el("auth-msg");
  const usernameInput = el("username"), passwordInput = el("password");
  const channelList = el("channel-list"), userList = el("user-list");
  const channelHeader = el("channel-name"), messages = el("messages");
  const msgInput = el("msg-input"), sendBtn = el("btn-send");
  const meLabel = el("me");
  const menuBtn = el("menu-btn"), sidebarBackdrop = el("sidebar-backdrop");

  function openSidebar() { chatScreen.classList.add("sidebar-open"); }
  function closeSidebar() { chatScreen.classList.remove("sidebar-open"); }
  menuBtn.onclick = () => chatScreen.classList.toggle("sidebar-open");
  sidebarBackdrop.onclick = closeSidebar;

  let ws = null;
  let channels = [];
  let currentChannelId = null;
  let currentUsername = null;
  let pendingRegisterThenLogin = false;

  function connect() {
    ws = new WebSocket(wsUrl);
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      handleMessage(msg);
    };
    ws.onclose = () => {
      authMsg.textContent = "disconnected from server";
      chatScreen.classList.add("hidden");
      authScreen.classList.remove("hidden");
      msgInput.disabled = true;
      sendBtn.disabled = true;
    };
    ws.onerror = () => { authMsg.textContent = "connection error"; };
  }

  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }

  function handleMessage(msg) {
    switch (msg.type) {
      case "register_result":
        if (!msg.ok) {
          authMsg.textContent = msg.error || "registration failed";
          pendingRegisterThenLogin = false;
        } else if (pendingRegisterThenLogin) {
          pendingRegisterThenLogin = false;
          send({ type: "login", username: usernameInput.value.trim(), password: passwordInput.value });
        }
        break;

      case "login_result":
        if (!msg.ok) {
          authMsg.textContent = msg.error || "login failed";
          return;
        }
        currentUsername = msg.username;
        meLabel.textContent = currentUsername;
        authScreen.classList.add("hidden");
        chatScreen.classList.remove("hidden");
        msgInput.disabled = false;
        sendBtn.disabled = false;
        msgInput.focus();
        break;

      case "channels":
        channels = msg.channels;
        renderChannelList();
        if (currentChannelId === null && channels.length > 0) {
          joinChannel(channels[0].id);
        }
        break;

      case "channel_created":
        if (!channels.some((c) => c.id === msg.channel.id)) {
          channels.push(msg.channel);
        }
        renderChannelList();
        break;

      case "join_result":
        if (!msg.ok) {
          addSystemLine(msg.error || "could not join channel", true);
          return;
        }
        currentChannelId = msg.channel_id;
        channelHeader.textContent = "#" + msg.name;
        messages.innerHTML = "";
        renderChannelList();
        break;

      case "history":
        if (msg.channel_id !== currentChannelId) return;
        messages.innerHTML = "";
        msg.messages.forEach(addMessageLine);
        scrollToBottom();
        break;

      case "message":
        if (msg.channel_id !== currentChannelId) return;
        addMessageLine(msg);
        scrollToBottom();
        break;

      case "users":
        if (msg.channel_id !== currentChannelId) return;
        renderUserList(msg.users);
        break;

      case "presence":
        break; // the server follows up with a full "users" list

      case "system":
        addSystemLine(msg.content, false);
        break;

      case "error":
        addSystemLine(msg.content, true);
        break;
    }
  }

  function renderChannelList() {
    channelList.innerHTML = "";
    channels.slice().sort((a, b) => a.id - b.id).forEach((c) => {
      const li = document.createElement("li");
      li.textContent = "# " + c.name;
      if (c.id === currentChannelId) li.classList.add("active");
      li.onclick = () => joinChannel(c.id);
      channelList.appendChild(li);
    });
  }

  function renderUserList(users) {
    userList.innerHTML = "";
    users.forEach((u) => {
      const li = document.createElement("li");
      li.textContent = u;
      userList.appendChild(li);
    });
  }

  function joinChannel(id) {
    send({ type: "join_channel", channel_id: id });
    closeSidebar();
  }

  function fmtTime(ts) {
    const d = new Date(ts * 1000);
    return d.toTimeString().slice(0, 8);
  }

  function addMessageLine(m) {
    const div = document.createElement("div");
    div.className = "msg-line";
    const ts = document.createElement("span");
    ts.className = "ts"; ts.textContent = "[" + fmtTime(m.timestamp) + "]";
    const user = document.createElement("span");
    user.className = "user"; user.textContent = m.username + ":";
    const content = document.createElement("span");
    content.textContent = " " + m.content;
    div.append(ts, user, content);
    messages.appendChild(div);
  }

  function addSystemLine(text, isError) {
    const div = document.createElement("div");
    div.className = "msg-line " + (isError ? "error" : "system");
    div.textContent = text;
    messages.appendChild(div);
    scrollToBottom();
  }

  function scrollToBottom() { messages.scrollTop = messages.scrollHeight; }

  el("btn-login").onclick = () => {
    const u = usernameInput.value.trim(), p = passwordInput.value;
    if (!u || !p) { authMsg.textContent = "enter a username and password"; return; }
    authMsg.textContent = "";
    send({ type: "login", username: u, password: p });
  };

  el("btn-register").onclick = () => {
    const u = usernameInput.value.trim(), p = passwordInput.value;
    if (!u || !p) { authMsg.textContent = "enter a username and password"; return; }
    authMsg.textContent = "";
    pendingRegisterThenLogin = true;
    send({ type: "register", username: u, password: p });
  };

  el("btn-logout").onclick = () => {
    send({ type: "logout" });
    if (ws) ws.close();
    closeSidebar();
    currentChannelId = null;
    currentUsername = null;
    channels = [];
    channelList.innerHTML = "";
    userList.innerHTML = "";
    messages.innerHTML = "";
    chatScreen.classList.add("hidden");
    authScreen.classList.remove("hidden");
    passwordInput.value = "";
    connect();
  };

  el("new-channel").onclick = () => {
    const name = prompt("New channel name:");
    if (name && name.trim()) send({ type: "create_channel", name: name.trim() });
  };

  function sendCurrentMessage() {
    const text = msgInput.value;
    if (!text.trim()) return;
    send({ type: "send_message", content: text });
    msgInput.value = "";
  }
  sendBtn.onclick = sendCurrentMessage;
  msgInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") sendCurrentMessage();
  });
  [usernameInput, passwordInput].forEach((inp) => {
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") el("btn-login").click();
    });
  });

  connect();
})();
</script>
</body>
</html>
"""
