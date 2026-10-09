# termchat

A **TeamSpeak-style chat application with a web interface** — a multi-user,
channel-based chat you run with one command and share over the internet
through an **[ngrok](https://ngrok.com)** tunnel. Anyone you send the link to
opens it in their browser, registers, and starts chatting in real time. No
install, no app, no account anywhere but your own server.

Under the hood it's a small threaded Python server backed by SQLite; the
browser UI talks to it over WebSocket. Terminal (TUI) and desktop (GUI)
clients also exist and share the exact same chat (see
[Other clients](#other-clients)).

```
            you                                   your friends
             │                                         │
   ./run-server                                   open the link
             │                                         │
   ┌─────────▼──────────┐   https tunnel    ┌──────────▼──────────┐
   │ termchat server    │◀─────ngrok───────▶│ browser (phone / PC)│
   │ + web UI (:8080)   │                   │  register • chat    │
   │ + SQLite database  │                   └─────────────────────┘
   └────────────────────┘
```

## Features

- **Web interface** — a clean, dark, mobile-friendly chat UI served straight
  from the server. Left sidebar with channels + who's online, main chat area,
  message input. Works on phones (off-canvas sidebar) and desktops alike.
- **Public link via ngrok** — the `run-server` script starts the server *and*
  an ngrok tunnel, then prints a `https://…ngrok…` URL you can share with
  anyone, anywhere — no port forwarding, no firewall changes, no fixed IP.
- **User accounts** — register and log in with a password (hashed with
  PBKDF2-HMAC-SHA256 + per-user salt; plaintext passwords are never stored).
- **Channels/rooms** — like TeamSpeak. Ships with `#general`, `#random`,
  `#help`; create your own on the fly.
- **Real-time messaging & presence** — messages broadcast instantly to
  everyone in the channel; the online list updates live as people come and go.
- **Message history** — the last 50 messages load automatically when you open
  a channel; everything is persisted in SQLite.
- **One shared chat, many front-ends** — browser, terminal (TUI) and desktop
  (GUI) clients all connect to the same server and see each other live.
- **Graceful disconnects** — leaving or dropping a connection updates
  everyone else's user list automatically.

## Requirements

- **Python 3.8+**
- **[ngrok](https://ngrok.com/download)** — for the public link. Free account;
  one-time `ngrok config add-authtoken …`. (Optional — pass `--no-ngrok` to run
  LAN-only.)
- **[`rich`](https://github.com/Textualize/rich)** — the only Python
  dependency, and only needed by the *terminal* client. The server, the web UI
  and its WebSocket layer are pure standard library (`socket`, `http.server`,
  `sqlite3`, `hashlib`, `threading`).

## Quick start

```bash
git clone https://github.com/M0rPh39s/termchat.git
cd termchat

# one-time setup
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# install ngrok and authenticate (one-time), then:
./run-server
```

`run-server` boots the chat server, serves the web UI, opens an ngrok tunnel,
and prints something like:

```
starting termchat server (tcp:9009, web:8080, db:.../termchat.db)...

  local:  http://127.0.0.1:8080
  lan:    http://192.168.1.20:8080
starting ngrok tunnel...
  public: https://a1b2-c3d4.ngrok-free.app

tui/gui clients connect on port 9009. press Ctrl+C to stop everything.
```

Open the **public** URL (or send it to a friend), click **Register** the first
time to create an account, and you're chatting. `Ctrl+C` stops the server and
the tunnel together.

### Setting up ngrok

1. Install it — see <https://ngrok.com/download> (e.g. `snap install ngrok`, a
   Homebrew/Chocolatey package, or the direct download). It just needs to be on
   your `PATH`.
2. Create a free account and add your authtoken once:
   ```bash
   ngrok config add-authtoken <YOUR_TOKEN>
   ```
3. Run `./run-server`. If ngrok isn't on `PATH`, the script still serves the
   local/LAN URLs and just skips the public tunnel.

### `run-server` options & environment

```bash
./run-server            # server + web UI + ngrok public link
./run-server --no-ngrok # local/LAN only, no public tunnel
```

| Variable | Default | Meaning |
|----------|---------|---------|
| `TERMCHAT_WEB_PORT` | `8080` | HTTP port for the web UI (what ngrok tunnels) |
| `TERMCHAT_PORT` | `9009` | TCP port for the terminal/desktop clients |
| `TERMCHAT_HOST` | `0.0.0.0` | interface to bind |
| `TERMCHAT_DB` | `./termchat.db` | SQLite database path |

```bash
TERMCHAT_WEB_PORT=3000 ./run-server      # serve/tunnel on port 3000 instead
```

## How it works

The web server (`termchat/web.py`) runs **in the same process** as the chat
server and shares one `Server` instance, so browser users are just another
kind of client:

```
                         ngrok (public https)
                                 │
  browser ──WebSocket──▶ web.py (HTTP + WS) ─┐
  TUI     ──TCP JSON───▶ server.py ──────────┼──▶ Server ──▶ SQLite DB
  GUI     ──TCP JSON───▶ server.py ──────────┘      (sessions, auth,
                                                     channels, routing)
```

| Component | File | Responsibility |
|-----------|------|----------------|
| Browser client | `termchat/web.py` | HTTP server, single-page UI, WebSocket bridge into the shared `Server` |
| WebSocket framing | `termchat/ws.py` | minimal stdlib RFC 6455 handshake + frames |
| Server | `termchat/server.py` | sessions, auth, channels, message routing/broadcast (TCP + a transport-agnostic dispatch used by the web bridge) |
| Database | `termchat/database.py` | SQLite schema, password hashing, all queries |
| Wire protocol | `termchat/protocol.py` | 4-byte length-prefixed JSON framing for TCP clients |
| TUI client | `termchat/client.py` | rich terminal client |
| GUI client | `termchat/gui.py` | Tkinter desktop client (can also embed a server) |

The server is **threaded** — one thread per connected client — with all shared
state guarded by a single lock. Browser messages arrive as WebSocket text
frames, are decoded to the same JSON message types the TCP clients use, and
flow through the identical dispatch/broadcast logic — so there is only ever one
chat, regardless of how people connect.

### Database schema

```sql
users       (id, username UNIQUE, password_hash, created_at)
channels    (id, name UNIQUE, created_by -> users.id, created_at)
messages    (id, channel_id -> channels.id, user_id -> users.id, content, timestamp)
memberships (user_id, channel_id, joined_at)   -- who has joined which channel
```

The database and default channels are created automatically on first start.
To pre-seed or inspect it:

```bash
python init_db.py                    # create schema + default channels
python init_db.py --add-user alice   # also create a user (prompts for pw)
python init_db.py --show             # print a summary of the database
```

## Running the server manually

`run-server` is just a convenience wrapper. You can start the server directly
and add the web UI with `--web-port`:

```bash
python run_server.py --host 0.0.0.0 --port 9009 --web-port 8080 --db termchat.db
```

Then expose the web port yourself however you like — your own ngrok invocation,
a reverse proxy, or a LAN address:

```bash
ngrok http 8080            # public https URL for the web UI
```

- `--web-port` — serve the browser UI on this HTTP port (omit it for a
  TCP-only server with no web interface).
- `--web-host` — bind the web UI to a different interface than `--host`
  (defaults to the same one).
- `--port` — TCP port for terminal/desktop clients (default `9009`).
- `--db` — SQLite path (default `termchat.db`). `-v` for debug logging.

> **Security note:** between your server and ngrok's edge, traffic to the
> browser is plain HTTP/WS, but ngrok terminates **HTTPS** at its edge, so the
> public link your friends use is encrypted. Passwords are always hashed at
> rest. If you expose the raw `--web-port` directly (no ngrok), put it behind a
> TLS reverse proxy (nginx/Caddy) for untrusted networks.

## Other clients

The browser is the main way in, but the same server also speaks a simple
length-prefixed JSON protocol over TCP, which the terminal and desktop clients
use. They share the same accounts, channels and messages as browser users.

### Terminal (TUI) client

A full-screen `rich` client for Linux/macOS terminals (uses raw-mode
`termios`, so on native Windows use WSL):

```bash
python run_client.py --host 127.0.0.1 --port 9009
# or, installed as a package: termchat-client --host your.server --port 9009
```

| Key | Action | | Command | Action |
|-----|--------|---|---------|--------|
| `Enter` | send line / run `/command` | | `/create <name>` | create a channel |
| `Ctrl-N` / `↓` | next channel | | `/join <name\|id>` | join a channel |
| `Ctrl-P` / `↑` | previous channel | | `/channels` `/users` | refresh lists |
| `Ctrl-C` / `Ctrl-Q` | quit | | `/help` `/quit` | help / exit |

### Desktop (GUI) client

A dark-themed Tkinter app (standard library only) that can also host an
embedded server, and builds into a standalone `TermChat.exe` for Windows:

```bash
python run_gui.py
# or, installed as a package: termchat-gui
```

```powershell
# build a single-file Windows executable
pip install pyinstaller
python -m PyInstaller --noconfirm --onefile --windowed --name TermChat `
    --icon assets\termchat.ico --add-data "assets\termchat.ico;assets" run_gui.py
```

Install the console/GUI commands with `pip install -e .`.

## Project layout

```
termchat/
├── termchat/
│   ├── __init__.py       # package metadata + defaults
│   ├── protocol.py       # TCP framing + message schema
│   ├── database.py       # SQLite layer + password hashing
│   ├── server.py         # chat server (sessions, auth, routing)
│   ├── web.py            # browser client: HTTP server + single-page UI + WS bridge
│   ├── ws.py             # minimal RFC 6455 WebSocket framing
│   ├── client.py         # rich terminal (TUI) client
│   └── gui.py            # Tkinter desktop (GUI) client
├── assets/
│   └── termchat.ico      # app icon (window + exe)
├── run-server            # start server + web UI + ngrok tunnel (main entry point)
├── run_server.py         # server launcher (python)
├── run_client.py         # TUI client launcher
├── run_gui.py            # GUI launcher / PyInstaller entry point
├── init_db.py            # database init / inspection tool
├── requirements.txt
├── pyproject.toml        # packaging + console scripts
└── README.md
```

## License

MIT
