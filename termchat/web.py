# browser client: tiny HTTP server serving a single-page chat UI, bridged
# to the same Server instance over a hand-rolled websocket (see ws.py).
# runs in-process with the TCP server so browser/TUI/GUI users all share
# the same chat.

import json
import logging
import mimetypes
import os
import socketserver
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import ws as wsproto
from .server import ClientState, Server

log = logging.getLogger("termchat.web")


class WSClientState(ClientState):
    """ClientState whose send() speaks websocket frames instead of raw TCP."""

    def send(self, msg: dict) -> bool:
        with self._send_lock:
            try:
                wsproto.send_frame(self.conn, json.dumps(msg).encode("utf-8"))
                return True
            except OSError:
                return False


def _serve_ws_client(chat_server: Server, conn, addr, is_local=False):
    state = WSClientState(conn, addr)
    state.is_local = is_local
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
            if self.path.startswith("/uploads/"):
                self._serve_upload()
                return
            self.send_error(404, "not found")

        def _serve_upload(self):
            # /uploads/<token>/<name> -> a file under the server's uploads dir.
            rel = self.path[len("/uploads/"):].split("?", 1)[0]
            rel = urllib.parse.unquote(rel)
            base = os.path.realpath(chat_server.uploads_dir)
            target = os.path.realpath(os.path.join(base, rel))
            # Contain the path: never serve outside the uploads directory.
            if target != base and not target.startswith(base + os.sep):
                self.send_error(403, "forbidden")
                return
            if not os.path.isfile(target):
                self.send_error(404, "not found")
                return
            ctype, _ = mimetypes.guess_type(target)
            try:
                with open(target, "rb") as fh:
                    body = fh.read()
            except OSError:
                self.send_error(404, "not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            # Files are user-supplied: force download / no inline script exec.
            self.send_header("Content-Security-Policy", "default-src 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _upgrade_to_websocket(self):
            if self.headers.get("Upgrade", "").lower() != "websocket":
                self.send_error(400, "expected a WebSocket upgrade")
                return
            key = self.headers.get("Sec-WebSocket-Key")
            if not key:
                self.send_error(400, "missing Sec-WebSocket-Key")
                return
            accept = wsproto.accept_key(key)
            # Safe local-admin detection: a genuine loopback peer that is NOT
            # being proxied. ngrok (and any reverse proxy) always adds an
            # X-Forwarded-For / Forwarded header, so tunnelled visitors - who
            # also arrive from 127.0.0.1 - never count as local.
            peer_ip = self.client_address[0] if self.client_address else ""
            forwarded = bool(self.headers.get("X-Forwarded-For")
                             or self.headers.get("Forwarded"))
            is_local = (peer_ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1")
                        and not forwarded)
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
            _serve_ws_client(chat_server, conn, addr, is_local=is_local)

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


def run_web(chat_server: Server, host: str, port: int, ssl_context=None):
    """Serve the browser client. Blocks; call via :func:`run_web_in_thread`."""
    httpd = _ThreadingHTTPServer((host, port), _make_handler(chat_server))
    scheme = "http"
    if ssl_context is not None:
        httpd.socket = ssl_context.wrap_socket(httpd.socket, server_side=True)
        scheme = "https"
    log.info("termchat browser client on %s://%s:%s", scheme, host, port)
    httpd.serve_forever()


def run_web_in_thread(chat_server: Server, host: str, port: int,
                      ssl_context=None) -> threading.Thread:
    t = threading.Thread(
        target=run_web, args=(chat_server, host, port),
        kwargs={"ssl_context": ssl_context}, daemon=True)
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

  /* ---- admin panel ---- */
  #admin {
    position: fixed; inset: 0; z-index: 40; background: rgba(0,0,0,.6);
    display: flex; align-items: flex-start; justify-content: center;
    overflow-y: auto; padding: 24px 12px;
  }
  .admin-box {
    width: min(560px, 96vw); background: var(--panel);
    border: 1px solid var(--border); border-radius: 8px; padding: 20px;
  }
  .admin-head { display: flex; align-items: center; justify-content: space-between; }
  .admin-head h1 { margin: 0; font-size: 18px; }
  #btn-admin-close { background: none; border: none; color: var(--dim); font-size: 24px; cursor: pointer; line-height: 1; }
  #admin h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .05em; color: var(--dim); margin: 18px 0 8px; }
  .admin-row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .admin-row input[type=text], .admin-row input:not([type]), .admin-row input[type=password], .admin-row select {
    flex: 1; min-width: 120px; padding: 8px 10px; background: #0d1117;
    border: 1px solid var(--border); border-radius: 4px; color: var(--text); font: inherit;
  }
  #admin button {
    padding: 8px 14px; min-height: 40px; border-radius: 4px; border: 1px solid var(--border);
    background: #1c2128; color: var(--text); font: inherit; cursor: pointer;
  }
  #admin button.primary { background: var(--accent); border-color: var(--accent); color: #06111f; font-weight: 600; }
  #admin button:hover { filter: brightness(1.15); }
  #admin .msg { min-height: 16px; margin-top: 8px; font-size: 12px; color: var(--danger); }
  #admin .msg.ok { color: var(--online); }
  #admin ul { list-style: none; margin: 0; padding: 0; }
  #admin-users li { display: flex; align-items: center; gap: 8px; padding: 6px 4px; border-bottom: 1px solid var(--border); font-size: 13px; }
  #admin-users li .role { color: var(--dim); font-size: 11px; }
  #admin-users li .grow { flex: 1; }
  #admin-users li button { min-height: 32px; padding: 4px 10px; }
  .member-pick { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
  .member-pick li { padding: 0; border: 0; }
  .member-pick label { display: inline-flex; align-items: center; gap: 4px; padding: 4px 8px; background: #0d1117; border: 1px solid var(--border); border-radius: 12px; font-size: 12px; cursor: pointer; }
  .chk { display: inline-flex; align-items: center; gap: 4px; color: var(--dim); font-size: 13px; }
  #btn-attach { padding: 10px 12px; min-height: 44px; border-radius: 4px; border: 1px solid var(--border); background: #1c2128; color: var(--text); cursor: pointer; }
  #btn-attach:disabled { opacity: .5; cursor: default; }
  #chan-lock { font-size: 13px; }
  .msg-line a { color: var(--accent); }
  .msg-line img.att { max-width: 240px; max-height: 240px; border-radius: 6px; display: block; margin-top: 4px; }

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
  </div>
  <p class="sub" style="margin-top:12px">Accounts are created by an admin.</p>
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
      <span>
        <button id="btn-admin" class="hidden">admin</button>
        <button id="btn-logout">log out</button>
      </span>
    </div>
  </div>
  <div id="main">
    <div id="channel-header"><button id="menu-btn" aria-label="channels">&#9776;</button><span id="channel-name">#</span><span id="chan-lock" class="hidden" title="private channel">&#128274;</span></div>
    <div id="messages"></div>
    <div id="input-row">
      <input id="file-input" type="file" class="hidden">
      <button id="btn-attach" title="attach a file" disabled>&#128206;</button>
      <input id="msg-input" placeholder="type a message and press Enter" disabled>
      <button id="btn-send" disabled>Send</button>
    </div>
  </div>
</div>

<div id="admin" class="hidden">
  <div class="admin-box">
    <div class="admin-head"><h1>Admin</h1><button id="btn-admin-close">&times;</button></div>

    <h2>Create account</h2>
    <div class="admin-row">
      <input id="nu-name" placeholder="username" autocomplete="off">
      <input id="nu-pass" type="password" placeholder="password" autocomplete="new-password">
      <select id="nu-role"><option value="user">user</option><option value="admin">admin</option></select>
      <button id="btn-nu-create" class="primary">Create</button>
    </div>
    <div class="msg" id="nu-msg"></div>

    <h2>Users</h2>
    <ul id="admin-users"></ul>

    <h2>Create channel</h2>
    <div class="admin-row">
      <input id="nc-name" placeholder="channel name">
      <label class="chk"><input id="nc-private" type="checkbox"> private</label>
      <button id="btn-nc-create" class="primary">Create</button>
    </div>
    <p class="sub" id="nc-members-hint hidden" style="margin:4px 0">Members (private channels only):</p>
    <ul id="nc-members" class="member-pick"></ul>
    <div class="msg" id="nc-msg"></div>

    <h2>Channel access</h2>
    <p class="sub">Edit who can see an existing private channel.</p>
    <div class="admin-row">
      <select id="ca-channel"></select>
      <button id="btn-ca-load">Load</button>
      <button id="btn-ca-save" class="primary">Save access</button>
    </div>
    <ul id="ca-members" class="member-pick"></ul>
    <div class="msg" id="ca-msg"></div>
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
  const newChannelBtn = el("new-channel");
  const attachBtn = el("btn-attach"), fileInput = el("file-input");
  const adminBtn = el("btn-admin"), adminModal = el("admin"), chanLock = el("chan-lock");

  const FILE_PREFIX = "TCFILE1:";
  const b64enc = (s) => btoa(unescape(encodeURIComponent(s)));
  const b64dec = (s) => decodeURIComponent(escape(atob(s)));
  function makeFileContent(info) { return FILE_PREFIX + b64enc(JSON.stringify(info)); }
  function parseFile(content) {
    if (typeof content !== "string" || !content.startsWith(FILE_PREFIX)) return null;
    try { return JSON.parse(b64dec(content.slice(FILE_PREFIX.length))); }
    catch (e) { return null; }
  }

  let isAdmin = false;
  let usersCache = [];          // [{id, username, role}]
  const nuMsg = el("nu-msg"), ncMsg = el("nc-msg"), caMsg = el("ca-msg");

  function openSidebar() { chatScreen.classList.add("sidebar-open"); }
  function closeSidebar() { chatScreen.classList.remove("sidebar-open"); }
  menuBtn.onclick = () => chatScreen.classList.toggle("sidebar-open");
  sidebarBackdrop.onclick = closeSidebar;

  let ws = null;
  let channels = [];
  let currentChannelId = null;
  let currentUsername = null;

  let skipAuto = false;   // set after an explicit logout so we don't re-grab admin
  function connect() {
    ws = new WebSocket(wsUrl);
    ws.onopen = () => {
      // Try passwordless admin login first; the server grants it only to a
      // genuine local (non-tunnelled) peer and otherwise refuses, which
      // simply drops us onto the normal login form.
      if (skipAuto) { authScreen.classList.remove("hidden"); return; }
      send({ type: "auto_login" });
    };
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
      case "login_result":
        if (!msg.ok) {
          // A refused passwordless auto-login just means "log in normally" -
          // show the form quietly rather than as an error.
          if (msg.auto) { authScreen.classList.remove("hidden"); return; }
          authMsg.textContent = msg.error || "login failed";
          return;
        }
        currentUsername = msg.username;
        isAdmin = !!msg.is_admin;
        meLabel.textContent = currentUsername + (isAdmin ? " (admin)" : "");
        adminBtn.classList.toggle("hidden", !isAdmin);
        newChannelBtn.classList.toggle("hidden", !isAdmin);
        authScreen.classList.add("hidden");
        revealChat();
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
        renderAccessChannelOptions();
        break;

      case "join_result":
        if (!msg.ok) {
          addSystemLine(msg.error || "could not join channel", true);
          return;
        }
        currentChannelId = msg.channel_id;
        channelHeader.textContent = "#" + msg.name;
        const cObj = channels.find((c) => c.id === msg.channel_id);
        chanLock.classList.toggle("hidden", !(cObj && cObj.is_private));
        messages.innerHTML = "";
        renderChannelList();
        break;

      case "upload_result":
        if (!msg.ok) { addSystemLine(msg.error || "upload failed", true); break; }
        send({ type: "send_message", content: makeFileContent(
          { url: msg.url, name: msg.name, size: msg.size }) });
        break;

      case "admin_users":
        usersCache = msg.users || [];
        renderAdminUsers();
        renderMemberPick();
        break;

      case "admin_create_result":
        nuMsg.textContent = msg.ok ? "created" : (msg.error || "failed");
        nuMsg.className = "msg" + (msg.ok ? " ok" : "");
        if (msg.ok) { el("nu-name").value = ""; el("nu-pass").value = ""; }
        break;

      case "admin_action_result":
        caMsg.textContent = msg.ok ? "saved" : (msg.error || "failed");
        caMsg.className = "msg" + (msg.ok ? " ok" : "");
        break;

      case "channel_access":
        renderChannelAccess(msg.channel_id, msg.members || []);
        break;

      case "history":
        if (msg.channel_id !== currentChannelId) return;
        renderHistory(msg.channel_id, msg.messages);
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
    div.append(ts, user);
    const file = parseFile(m.content || "");
    if (file) {
      const link = document.createElement("a");
      link.href = file.url; link.target = "_blank"; link.rel = "noopener";
      link.textContent = " \u{1F4CE} " + file.name
        + (file.size ? " (" + fmtSize(file.size) + ")" : "");
      div.append(link);
      if (/\.(png|jpe?g|gif|webp|bmp|svg)$/i.test(file.name)) {
        const img = document.createElement("img");
        img.className = "att"; img.src = file.url; img.alt = file.name;
        const wrap = document.createElement("a");
        wrap.href = file.url; wrap.target = "_blank"; wrap.rel = "noopener";
        wrap.append(img); div.append(wrap);
      }
    } else {
      const content = document.createElement("span");
      content.textContent = " " + (m.content || "");
      div.append(content);
    }
    messages.appendChild(div);
  }

  function fmtSize(n) {
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / 1024 / 1024).toFixed(1) + " MB";
  }

  function renderHistory(channelId, msgs) {
    messages.innerHTML = "";
    for (const m of msgs) {
      if (channelId !== currentChannelId) return;  // switched away
      addMessageLine(m);
    }
    scrollToBottom();
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

  el("btn-logout").onclick = () => {
    send({ type: "logout" });
    skipAuto = true;
    if (ws) ws.close();
    closeSidebar();
    currentChannelId = null;
    currentUsername = null;
    isAdmin = false;
    channels = [];
    channelList.innerHTML = "";
    userList.innerHTML = "";
    messages.innerHTML = "";
    chatScreen.classList.add("hidden");
    adminModal.classList.add("hidden");
    authScreen.classList.remove("hidden");
    passwordInput.value = "";
    connect();
  };

  newChannelBtn.onclick = () => { if (isAdmin) openAdmin(); };

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

  // ---- file upload ----
  attachBtn.onclick = () => fileInput.click();
  fileInput.onchange = () => {
    const f = fileInput.files && fileInput.files[0];
    fileInput.value = "";
    if (!f) return;
    if (f.size > 10 * 1024 * 1024) { addSystemLine("file too large (max 10 MB)", true); return; }
    const reader = new FileReader();
    reader.onload = () => {
      const b64 = String(reader.result).split(",", 2)[1] || "";
      send({ type: "upload_file", name: f.name, data: b64 });
    };
    reader.onerror = () => addSystemLine("could not read file", true);
    reader.readAsDataURL(f);
  };

  // ---- admin panel ----
  function openAdmin() {
    adminModal.classList.remove("hidden");
    nuMsg.textContent = ""; ncMsg.textContent = ""; caMsg.textContent = "";
    send({ type: "admin_list_users" });
    renderAccessChannelOptions();
  }
  adminBtn.onclick = openAdmin;
  el("btn-admin-close").onclick = () => adminModal.classList.add("hidden");
  adminModal.onclick = (e) => { if (e.target === adminModal) adminModal.classList.add("hidden"); };

  function renderAdminUsers() {
    const ul = el("admin-users"); ul.innerHTML = "";
    usersCache.forEach((u) => {
      const li = document.createElement("li");
      const name = document.createElement("span");
      name.className = "grow"; name.textContent = u.username;
      const role = document.createElement("span");
      role.className = "role"; role.textContent = u.role;
      const del = document.createElement("button");
      del.textContent = "delete";
      del.onclick = () => {
        if (u.username === currentUsername) { return; }
        if (confirm("Delete user " + u.username + "?"))
          send({ type: "admin_delete_user", user_id: u.id });
      };
      li.append(name, role, del);
      ul.appendChild(li);
    });
  }

  function renderMemberPick() {
    // Checkboxes of non-admin... actually all users, for new private channel.
    const ul = el("nc-members"); ul.innerHTML = "";
    usersCache.forEach((u) => {
      const li = document.createElement("li");
      const label = document.createElement("label");
      const cb = document.createElement("input");
      cb.type = "checkbox"; cb.value = u.id; cb.dataset.pick = "nc";
      label.append(cb, document.createTextNode(u.username));
      li.append(label); ul.appendChild(li);
    });
  }

  el("btn-nu-create").onclick = () => {
    const u = el("nu-name").value.trim(), p = el("nu-pass").value;
    const role = el("nu-role").value;
    if (!u || !p) { nuMsg.textContent = "username and password required"; nuMsg.className = "msg"; return; }
    send({ type: "admin_create_user", username: u, password: p, role: role });
  };

  el("btn-nc-create").onclick = () => {
    const name = el("nc-name").value.trim();
    if (!name) { ncMsg.textContent = "channel name required"; ncMsg.className = "msg"; return; }
    const isPriv = el("nc-private").checked;
    const members = [...document.querySelectorAll('input[data-pick="nc"]:checked')]
      .map((c) => parseInt(c.value, 10));
    send({ type: "create_channel", name: name, is_private: isPriv, members: members });
    ncMsg.textContent = "created"; ncMsg.className = "msg ok";
    el("nc-name").value = ""; el("nc-private").checked = false;
    document.querySelectorAll('input[data-pick="nc"]:checked').forEach((c) => c.checked = false);
  };

  function renderAccessChannelOptions() {
    const sel = el("ca-channel"); if (!sel) return;
    const prev = sel.value;
    sel.innerHTML = "";
    channels.filter((c) => c.is_private).forEach((c) => {
      const opt = document.createElement("option");
      opt.value = c.id; opt.textContent = "# " + c.name;
      sel.appendChild(opt);
    });
    if (prev) sel.value = prev;
  }

  el("btn-ca-load").onclick = () => {
    const cid = parseInt(el("ca-channel").value, 10);
    if (cid) send({ type: "admin_get_channel_access", channel_id: cid });
  };

  function renderChannelAccess(channelId, memberIds) {
    const set = new Set(memberIds);
    const ul = el("ca-members"); ul.innerHTML = "";
    ul.dataset.channel = channelId;
    usersCache.forEach((u) => {
      const li = document.createElement("li");
      const label = document.createElement("label");
      const cb = document.createElement("input");
      cb.type = "checkbox"; cb.value = u.id; cb.dataset.pick = "ca";
      if (set.has(u.id)) cb.checked = true;
      label.append(cb, document.createTextNode(u.username));
      li.append(label); ul.appendChild(li);
    });
  }

  el("btn-ca-save").onclick = () => {
    const cid = parseInt(el("ca-members").dataset.channel || el("ca-channel").value, 10);
    if (!cid) { caMsg.textContent = "load a channel first"; caMsg.className = "msg"; return; }
    const members = [...document.querySelectorAll('input[data-pick="ca"]:checked')]
      .map((c) => parseInt(c.value, 10));
    send({ type: "admin_set_channel_access", channel_id: cid, members: members });
  };

  function revealChat() {
    chatScreen.classList.remove("hidden");
    msgInput.disabled = false;
    sendBtn.disabled = false;
    attachBtn.disabled = false;
    msgInput.focus();
    if (currentChannelId === null && channels.length > 0) joinChannel(channels[0].id);
  }

  connect();
})();
</script>
</body>
</html>
"""
