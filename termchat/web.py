"""termchat web UI.

A browser front-end for termchat.  Rather than re-implement the chat
logic, this module runs a thin HTTP bridge in front of the existing TCP
:class:`~termchat.server.Server`:

    browser  <--HTTP/SSE-->  web bridge  <--TCP frames-->  termchat server

* Each browser tab gets a *web session* — a real TCP connection to the
  termchat server, exactly like the TUI/GUI clients open.
* Server -> client pushes (channels, messages, presence, ...) are streamed
  to the browser with **Server-Sent Events** (``GET /api/stream``).
* Client -> server actions are plain JSON ``POST``s (``/api/send``) that get
  framed straight onto that session's socket.

Because it speaks the same wire protocol, every server feature (auth,
single-session, channels, history, presence) works unchanged, and the web
UI can talk to a server it starts itself (the default) or to one already
running elsewhere (``--no-embed``).

Run it with::

    python -m termchat.web --port 8080            # embeds a server
    python -m termchat.web --port 8080 --ngrok     # ...and opens a tunnel

Only the Python standard library is required (``pyngrok`` is needed solely
for the optional ``--ngrok`` tunnel).
"""

import argparse
import json
import logging
import queue
import socket
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import DEFAULT_PORT
from .protocol import ProtocolError, recv_msg, send_msg
from .server import Server

log = logging.getLogger("termchat.web")

# How many server pushes we buffer per browser session before dropping the
# oldest.  A browser that vanishes without closing the SSE stream cleanly
# must not let the queue grow without bound.
_QUEUE_MAX = 1000


class WebSession:
    """One browser tab's TCP connection to the termchat server."""

    def __init__(self, server_host: str, server_port: int):
        self.sock = socket.create_connection((server_host, server_port),
                                              timeout=10)
        self.sock.settimeout(None)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.queue = queue.Queue(maxsize=_QUEUE_MAX)
        self.alive = True
        self._send_lock = threading.Lock()
        self._recv_thread = threading.Thread(target=self._recv_loop,
                                              daemon=True)
        self._recv_thread.start()

    def _recv_loop(self):
        """Pump server pushes onto the queue until the socket closes."""
        try:
            while self.alive:
                msg = recv_msg(self.sock)
                if msg is None:
                    break
                self._offer(msg)
        except (ProtocolError, OSError):
            pass
        finally:
            self.alive = False
            # Sentinel so the SSE generator knows to shut down.
            self._offer(None)

    def _offer(self, msg):
        """Enqueue ``msg``, dropping the oldest item if the queue is full."""
        try:
            self.queue.put_nowait(msg)
        except queue.Full:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.queue.put_nowait(msg)
            except queue.Full:
                pass

    def send(self, msg: dict) -> bool:
        """Frame ``msg`` onto the socket; return False if it's dead."""
        with self._send_lock:
            try:
                send_msg(self.sock, msg)
                return True
            except OSError:
                return False

    def close(self):
        self.alive = False
        try:
            self.sock.close()
        except OSError:
            pass


class WebApp:
    """Holds the session registry and (optionally) the embedded server."""

    def __init__(self, server_host: str, server_port: int):
        self.server_host = server_host
        self.server_port = server_port
        self._sessions = {}          # sid -> WebSession
        self._lock = threading.Lock()

    def create_session(self) -> str:
        session = WebSession(self.server_host, self.server_port)
        sid = uuid.uuid4().hex
        with self._lock:
            self._sessions[sid] = session
        return sid

    def get_session(self, sid):
        with self._lock:
            return self._sessions.get(sid)

    def drop_session(self, sid):
        with self._lock:
            session = self._sessions.pop(sid, None)
        if session is not None:
            session.close()


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

def _parse_cookies(header: str) -> dict:
    """Parse a ``Cookie`` request header into a plain dict."""
    cookies = {}
    if not header:
        return cookies
    for part in header.split(";"):
        if "=" in part:
            name, _, value = part.strip().partition("=")
            cookies[name] = value
    return cookies


class Handler(BaseHTTPRequestHandler):
    # Set by make_handler().
    app: WebApp = None

    protocol_version = "HTTP/1.1"
    server_version = "termchat-web"

    def log_message(self, fmt, *args):  # quieter than the default stderr spew
        log.debug("%s - %s", self.address_string(), fmt % args)

    # -- helpers ------------------------------------------------------------

    def _sid(self):
        return _parse_cookies(self.headers.get("Cookie", "")).get(
            "termchat_sid")

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            obj = json.loads(raw.decode("utf-8"))
            return obj if isinstance(obj, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    # -- routing ------------------------------------------------------------

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._serve_page()
        elif path == "/api/stream":
            self._serve_stream()
        else:
            self._send_json({"error": "not found"}, status=404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/connect":
            self._connect()
        elif path == "/api/send":
            self._send()
        elif path == "/api/disconnect":
            self._disconnect()
        else:
            self._send_json({"error": "not found"}, status=404)

    # -- endpoints ----------------------------------------------------------

    def _serve_page(self):
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _connect(self):
        # A fresh connect always starts a clean session; drop any old one.
        old = self._sid()
        if old:
            self.app.drop_session(old)
        try:
            sid = self.app.create_session()
        except OSError as exc:
            self._send_json({"ok": False, "error": f"cannot reach server: {exc}"},
                            status=502)
            return
        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # HttpOnly + SameSite so the token isn't reachable from JS or sent
        # cross-site; the EventSource/fetch calls are all same-origin.
        self.send_header("Set-Cookie",
                         f"termchat_sid={sid}; Path=/; HttpOnly; SameSite=Lax")
        self.end_headers()
        self.wfile.write(body)

    def _send(self):
        session = self.app.get_session(self._sid())
        if session is None:
            self._send_json({"ok": False, "error": "no session"}, status=401)
            return
        msg = self._read_body()
        if not msg.get("type"):
            self._send_json({"ok": False, "error": "missing type"}, status=400)
            return
        ok = session.send(msg)
        self._send_json({"ok": ok})

    def _disconnect(self):
        self.app.drop_session(self._sid())
        self._send_json({"ok": True})

    def _serve_stream(self):
        session = self.app.get_session(self._sid())
        if session is None:
            self._send_json({"error": "no session"}, status=401)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            # Nudge the browser's EventSource open immediately.
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    msg = session.queue.get(timeout=15)
                except queue.Empty:
                    # Heartbeat comment keeps proxies (and ngrok) from
                    # killing an idle stream.
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if msg is None:  # socket closed on the server side
                    break
                payload = json.dumps(msg).encode("utf-8")
                self.wfile.write(b"data: " + payload + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # browser navigated away / closed the tab


def make_handler(app: WebApp):
    return type("BoundHandler", (Handler,), {"app": app})


# ---------------------------------------------------------------------------
# Embedded HTML/CSS/JS front-end
# ---------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>termchat</title>
<style>
  :root {
    --bg: #0d1117; --panel: #161b22; --panel2: #1c2330; --border: #30363d;
    --text: #e6edf3; --dim: #8b949e; --accent: #2f81f7; --accent2: #1f6feb;
    --ok: #3fb950; --err: #f85149;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    background: var(--bg); color: var(--text);
    font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    display: flex; align-items: center; justify-content: center;
  }
  button {
    font: inherit; cursor: pointer; border: 1px solid var(--border);
    background: var(--panel2); color: var(--text); border-radius: 6px;
    padding: 8px 14px;
  }
  button.primary { background: var(--accent2); border-color: var(--accent);
    color: #fff; }
  button:hover { border-color: var(--accent); }
  input {
    font: inherit; background: var(--bg); color: var(--text);
    border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px;
    width: 100%;
  }
  input:focus { outline: none; border-color: var(--accent); }

  /* login */
  #login {
    width: 340px; background: var(--panel); border: 1px solid var(--border);
    border-radius: 12px; padding: 28px;
  }
  #login h1 { margin: 0 0 4px; color: var(--accent); font-size: 24px; }
  #login .sub { color: var(--dim); margin-bottom: 22px; }
  #login label { display: block; margin: 12px 0 4px; color: var(--dim); }
  #login .row { display: flex; gap: 10px; margin-top: 20px; }
  #login .row button { flex: 1; }
  #status { min-height: 20px; margin-top: 14px; font-size: 13px; }
  #status.err { color: var(--err); } #status.ok { color: var(--ok); }

  /* chat */
  #chat { display: none; width: 100vw; height: 100vh; }
  #chat.on { display: grid; grid-template-columns: 240px 1fr;
    grid-template-rows: 100vh; }
  #sidebar { background: var(--panel); border-right: 1px solid var(--border);
    display: flex; flex-direction: column; overflow: hidden; }
  #sidebar h2 { font-size: 11px; text-transform: uppercase; letter-spacing: 1px;
    color: var(--dim); margin: 16px 14px 6px; }
  .list { overflow-y: auto; }
  .chan, .user { padding: 7px 14px; cursor: pointer; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis; }
  .chan:hover { background: var(--panel2); }
  .chan.active { background: var(--accent2); color: #fff; }
  .user { cursor: default; }
  .user::before { content: "\2022"; color: var(--ok); margin-right: 7px; }
  #newchan { margin: 8px 14px; }
  .who { color: var(--dim); font-size: 12px; padding: 4px 14px; }
  .main { display: flex; flex-direction: column; min-width: 0; }
  .topbar { padding: 12px 18px; border-bottom: 1px solid var(--border);
    display: flex; justify-content: space-between; align-items: center; }
  .topbar .name { font-weight: bold; }
  .topbar .me { color: var(--dim); font-size: 13px; }
  #messages { flex: 1; overflow-y: auto; padding: 14px 18px; }
  .msg { margin: 2px 0; word-wrap: break-word; }
  .msg .ts { color: var(--dim); margin-right: 8px; }
  .msg .from { font-weight: bold; margin-right: 6px; }
  .msg.system { color: var(--dim); font-style: italic; }
  .inputbar { display: flex; gap: 10px; padding: 12px 18px;
    border-top: 1px solid var(--border); }
  .inputbar input { flex: 1; }
</style>
</head>
<body>
  <div id="login">
    <h1>termchat</h1>
    <div class="sub">a TeamSpeak-style chat &mdash; web</div>
    <label for="u">username</label>
    <input id="u" autocomplete="username" maxlength="32">
    <label for="p">password</label>
    <input id="p" type="password" autocomplete="current-password">
    <div class="row">
      <button id="btn-login" class="primary">Log in</button>
      <button id="btn-register">Register</button>
    </div>
    <div id="status"></div>
  </div>

  <div id="chat">
    <div id="sidebar">
      <h2>Channels</h2>
      <div id="channels" class="list"></div>
      <button id="newchan">+ New channel</button>
      <h2>Online</h2>
      <div id="users" class="list"></div>
    </div>
    <div class="main">
      <div class="topbar">
        <span class="name" id="chan-name">&mdash;</span>
        <span class="me">logged in as <b id="me"></b> &middot;
          <a href="#" id="logout" style="color:var(--accent)">log out</a></span>
      </div>
      <div id="messages"></div>
      <div class="inputbar">
        <input id="msg" placeholder="type a message and press Enter"
               maxlength="4000" disabled>
        <button id="send" disabled>Send</button>
      </div>
    </div>
  </div>

<script>
const NAME_COLORS = ["#79c0ff","#56d364","#e3b341","#d2a8ff","#58a6ff",
  "#ff7b72","#7ee787","#f2cc60","#ffa657","#a5d6ff"];
function colorFor(name){
  let h = 0; for (const c of name) h = (h*31 + c.charCodeAt(0)) & 0xffffffff;
  return NAME_COLORS[Math.abs(h) % NAME_COLORS.length];
}
function esc(s){ const d = document.createElement("div"); d.textContent = s;
  return d.innerHTML; }
function fmtTime(ts){
  const d = new Date(ts*1000);
  return d.toTimeString().slice(0,8);
}

const state = {
  me: null, channels: [], activeId: null, activeName: null,
  messages: {}, online: {},
};
const $ = (id) => document.getElementById(id);

async function api(path, body){
  const opt = { method: "POST", headers: {"Content-Type": "application/json"} };
  if (body !== undefined) opt.body = JSON.stringify(body);
  const r = await fetch(path, opt);
  return r.json().catch(() => ({}));
}
function send(msg){ return api("/api/send", msg); }

// -- login flow ------------------------------------------------------------
let stream = null;

function setStatus(text, cls){
  const el = $("status"); el.textContent = text; el.className = cls || "";
}

async function go(register){
  const username = $("u").value.trim();
  const password = $("p").value;
  if (!username || !password){ setStatus("enter a username and password","err");
    return; }
  setStatus("connecting...", "");
  const conn = await api("/api/connect");
  if (!conn.ok){ setStatus(conn.error || "cannot connect", "err"); return; }
  openStream();
  if (register){
    setStatus("registering...", "");
    await send({type:"register", username, password});
  } else {
    setStatus("logging in...", "");
    await send({type:"login", username, password});
  }
}

function openStream(){
  if (stream) stream.close();
  stream = new EventSource("/api/stream");
  stream.onmessage = (e) => handle(JSON.parse(e.data));
  stream.onerror = () => { /* browser auto-reconnects */ };
}

// -- incoming server frames ------------------------------------------------
let pendingLoginUser = null;

function handle(msg){
  switch(msg.type){
    case "register_result":
      if (msg.ok){
        // Registration succeeds; log straight in with the same creds.
        setStatus("account created, logging in...", "ok");
        send({type:"login", username:$("u").value.trim(),
              password:$("p").value});
      } else {
        setStatus(msg.error || "registration failed", "err");
      }
      break;
    case "login_result":
      if (msg.ok){ enterChat(msg.username); }
      else { setStatus(msg.error || "login failed", "err"); }
      break;
    case "channels":
      state.channels = msg.channels || [];
      renderChannels();
      // Auto-join the first channel on first load.
      if (state.activeId === null && state.channels.length){
        joinChannel(state.channels[0].id);
      }
      break;
    case "channel_created": {
      const ch = msg.channel;
      if (ch && !state.channels.some(c => c.id === ch.id)){
        state.channels.push(ch); renderChannels();
      }
      break;
    }
    case "join_result":
      if (msg.ok){
        state.activeId = msg.channel_id; state.activeName = msg.name;
        $("chan-name").textContent = "#" + msg.name;
        $("msg").disabled = false; $("send").disabled = false;
        renderChannels(); renderMessages();
      }
      break;
    case "history":
      state.messages[msg.channel_id] = msg.messages || [];
      if (msg.channel_id === state.activeId) renderMessages();
      break;
    case "message":
      (state.messages[msg.channel_id] ||= []).push(msg);
      if (msg.channel_id === state.activeId) renderMessages();
      break;
    case "users":
      state.online[msg.channel_id] = msg.users || [];
      if (msg.channel_id === state.activeId) renderUsers();
      break;
    case "presence": {
      const verb = msg.status === "online" ? "joined" : "left";
      (state.messages[msg.channel_id] ||= []).push(
        {system:true, timestamp: Math.floor(Date.now()/1000),
         content: `${msg.username} ${verb} the channel`});
      if (msg.channel_id === state.activeId) renderMessages();
      break;
    }
    case "system":
      (state.messages[msg.channel_id] ||= []).push(
        {system:true, timestamp: Math.floor(Date.now()/1000),
         content: msg.content || ""});
      if (msg.channel_id === state.activeId) renderMessages();
      break;
    case "error":
      // Surface server errors in the chat if we're in, else on login.
      if ($("chat").classList.contains("on")){
        (state.messages[state.activeId] ||= []).push(
          {system:true, timestamp: Math.floor(Date.now()/1000),
           content: "! " + (msg.content || "error")});
        renderMessages();
      } else {
        setStatus(msg.content || "error", "err");
      }
      break;
  }
}

// -- views -----------------------------------------------------------------
function enterChat(username){
  state.me = username;
  $("me").textContent = username;
  $("login").style.display = "none";
  $("chat").classList.add("on");
  $("msg").focus();
}

function renderChannels(){
  const box = $("channels"); box.innerHTML = "";
  for (const c of state.channels){
    const el = document.createElement("div");
    el.className = "chan" + (c.id === state.activeId ? " active" : "");
    el.textContent = "# " + c.name;
    el.onclick = () => joinChannel(c.id);
    box.appendChild(el);
  }
}
function renderUsers(){
  const box = $("users"); box.innerHTML = "";
  const users = state.online[state.activeId] || [];
  for (const u of users){
    const el = document.createElement("div");
    el.className = "user"; el.textContent = u;
    el.style.color = colorFor(u);
    box.appendChild(el);
  }
}
function renderMessages(){
  const box = $("messages"); box.innerHTML = "";
  const msgs = state.messages[state.activeId] || [];
  for (const m of msgs){
    const el = document.createElement("div");
    if (m.system){
      el.className = "msg system";
      el.innerHTML = `<span class="ts">${fmtTime(m.timestamp)}</span>`
        + esc(m.content);
    } else {
      el.className = "msg";
      el.innerHTML = `<span class="ts">${fmtTime(m.timestamp)}</span>`
        + `<span class="from" style="color:${colorFor(m.username)}">`
        + esc(m.username) + "</span>" + esc(m.content);
    }
    box.appendChild(el);
  }
  box.scrollTop = box.scrollHeight;
}

// -- actions ---------------------------------------------------------------
function joinChannel(id){
  if (id === state.activeId){ return; }
  send({type:"join_channel", channel_id: id});
}
function sendMessage(){
  const inp = $("msg"); const text = inp.value.trim();
  if (!text || state.activeId === null) return;
  send({type:"send_message", content: text});
  inp.value = "";
}
async function newChannel(){
  const name = prompt("New channel name:");
  if (name && name.trim()) await send({type:"create_channel", name: name.trim()});
}
async function logout(){
  if (stream) stream.close();
  await api("/api/disconnect");
  location.reload();
}

// -- wiring ----------------------------------------------------------------
$("btn-login").onclick = () => go(false);
$("btn-register").onclick = () => go(true);
$("p").addEventListener("keydown", (e) => { if (e.key === "Enter") go(false); });
$("u").addEventListener("keydown", (e) => { if (e.key === "Enter") $("p").focus(); });
$("send").onclick = sendMessage;
$("msg").addEventListener("keydown", (e) => { if (e.key === "Enter") sendMessage(); });
$("newchan").onclick = newChannel;
$("logout").onclick = (e) => { e.preventDefault(); logout(); };
window.addEventListener("beforeunload", () => {
  navigator.sendBeacon && navigator.sendBeacon("/api/disconnect");
});
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _start_embedded_server(host, port, db_path):
    """Start a termchat TCP server in a background thread."""
    server = Server(host, port, db_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="termchat web UI")
    parser.add_argument("--host", default="0.0.0.0",
                        help="interface for the web server (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080,
                        help="port for the web server (default: 8080)")
    parser.add_argument("--db", default="termchat.db",
                        help="SQLite database path for the embedded server")
    parser.add_argument("--server-host", default="127.0.0.1",
                        help="termchat server host to bridge to "
                             "(default: 127.0.0.1)")
    parser.add_argument("--server-port", type=int, default=DEFAULT_PORT,
                        help=f"termchat server port (default: {DEFAULT_PORT})")
    parser.add_argument("--no-embed", action="store_true",
                        help="do not start a server; bridge to an existing one")
    parser.add_argument("--ngrok", action="store_true",
                        help="open a public ngrok tunnel to the web server")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="enable debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.no_embed:
        _start_embedded_server(args.server_host, args.server_port, args.db)
        log.info("embedded termchat server on %s:%s",
                 args.server_host, args.server_port)

    app = WebApp(args.server_host, args.server_port)
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    httpd.daemon_threads = True

    tunnel = None
    if args.ngrok:
        try:
            from pyngrok import ngrok
        except ImportError:
            log.error("--ngrok requires pyngrok: pip install pyngrok")
            return 1
        tunnel = ngrok.connect(args.port, "http")
        log.info("public URL: %s", tunnel.public_url)
        print(f"\n  termchat is live at: {tunnel.public_url}\n")

    log.info("termchat web UI on http://%s:%s", args.host, args.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        if tunnel is not None:
            try:
                from pyngrok import ngrok
                ngrok.disconnect(tunnel.public_url)
            except Exception:
                pass
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
