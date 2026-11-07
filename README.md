# termchat

A little TeamSpeak-ish chat app. TCP server + SQLite, a terminal UI client,
a Tkinter desktop client, and a browser client that all talk to the same
server and share the same channels in real time.

```
+--------------+--------------------------------------+
|  CHANNELS    |  #general                            |
|  > # general |  [12:00:01] alice: hello everyone    |
|    # random  |  [12:00:12] bob:   hi alice          |
|    # help    |                                      |
|  ONLINE (2)  |                                      |
|  * alice     |                                      |
|  * bob       +--------------------------------------+
|              |  > type a message and press Enter    |
+--------------+--------------------------------------+
```

## Features

- accounts (passwords are PBKDF2-HMAC-SHA256 + salt, never stored plaintext)
- channels, create your own, ships with #general/#random/#help
- real-time messages + presence (who's online in a channel, live)
- last 50 messages load automatically when you join a channel, everything's in SQLite
- terminal UI (rich), desktop GUI (Tkinter, builds into a standalone .exe), and a browser client - same chat, pick your client
- one account can't be logged in twice at once

## How it's put together

```
clients (TUI / GUI / browser)  <-- TCP, length-prefixed JSON -->  server (threaded)  <-->  SQLite
```

| file | what it does |
|------|----|
| `termchat/protocol.py` | length-prefixed JSON framing |
| `termchat/database.py` | schema, password hashing, queries |
| `termchat/server.py` | TCP server, auth, routing, broadcast |
| `termchat/client.py` | the TUI client |
| `termchat/gui.py` | Tkinter desktop client (can also host the server) |
| `termchat/ws.py` | small stdlib websocket implementation |
| `termchat/web.py` | HTTP + ws bridge for the browser client |

Server is one thread per connection, shared state behind a lock. Both
clients run their UI on the main thread and read the socket on a
background thread.

Tables: `users`, `channels`, `messages`, `memberships` (who joined what).

## Setup

Needs Python 3.8+. `rich` is the only third-party dependency and it's
only used by the TUI client - everything else is stdlib.

```bash
git clone https://github.com/M0rPh39s/termchat.git
cd termchat
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

or `pip install -e .` if you want the `termchat-server` / `termchat-client`
/ `termchat-gui` commands.

## Running it

```bash
python run_server.py --host 0.0.0.0 --port 9009 --db termchat.db
```

Default host/port is `0.0.0.0:9009`. `--db` picks the sqlite file, `-v` for
debug logging. The schema and default channels get created automatically
on first run - `init_db.py` is only there if you want to pre-seed a user or
peek at the db (`--add-user`, `--show`).

Then from another terminal/machine:

```bash
python run_client.py --host 127.0.0.1 --port 9009
```

First time, pick `register` at the prompt, otherwise `login`.

### Terminal client

| key | does |
|-----|------|
| Enter | send the line / run a `/command` |
| Ctrl-N / ↓ | next channel |
| Ctrl-P / ↑ | previous channel |
| Ctrl-C / Ctrl-Q | quit |

Commands: `/help`, `/create <name>`, `/join <name|id>`, `/channels`,
`/users`, `/quit`.

Needs a real terminal (raw mode via termios), so no native Windows - use
WSL there.

### Browser client

Add `--web-port` to also serve a mobile-friendly web UI over plain HTTP,
no extra process or dependency:

```bash
python run_server.py --host 0.0.0.0 --port 9009 --web-port 8080
```

Open `http://<lan-ip>:8080` from any browser on the network - same
channels, same people, as a fourth transport into the same server.
`--web-host` if you want it on a different interface than `--host`.

### Desktop GUI

```bash
python run_gui.py
```

Stdlib only (Tkinter), runs on Windows/macOS/Linux. Tick "host a local
server" on the login screen and it'll spin up an embedded server on the
spot - handy for just handing a friend an .exe and a port number. The
embedded server's db lives in `%LOCALAPPDATA%\TermChat\` (or `~/TermChat`
if that's unset).

To build the standalone exe:

```powershell
pip install pyinstaller
python -m PyInstaller --noconfirm --onefile --windowed --name TermChat `
    --icon assets\termchat.ico --add-data "assets\termchat.ico;assets" run_gui.py
```

Comes out to `dist\TermChat.exe`, ~12MB, no Python needed on the other end.
Since it can host too, one exe is enough for a whole LAN chat.

## Running it on a server somewhere

Copy the repo over, `pip install -r requirements.txt`, open the port
(`sudo ufw allow 9009/tcp`), and keep it alive with something like:

```ini
# /etc/systemd/system/termchat.service
[Unit]
Description=termchat server
After=network.target

[Service]
WorkingDirectory=/opt/termchat
ExecStart=/opt/termchat/.venv/bin/python run_server.py --host 0.0.0.0 --port 9009 --db /var/lib/termchat/termchat.db
Restart=on-failure
User=termchat

[Install]
WantedBy=multi-user.target
```

`sudo systemctl enable --now termchat`, then clients connect with
`--host your.server.ip`.

### Docker

There's a `Dockerfile` and a `docker-compose.yml` that run the server (TCP
plus the browser client) with the SQLite database on a named volume, so it
survives restarts and rebuilds:

```bash
docker compose up -d        # build + start in the background
docker compose logs -f      # follow the logs
docker compose down         # stop (the volume keeps the database)
```

That exposes `9009` for TUI/GUI clients and `8080` for the browser client
(`http://localhost:8080`). Plain `docker` works too:

```bash
docker build -t termchat .
docker run -d --name termchat -p 9009:9009 -p 8080:8080 \
    -v termchat-data:/data termchat
```

Override the command to change ports or drop the web client, e.g.
`docker run ... termchat python run_server.py --host 0.0.0.0 --port 9009 --db /data/termchat.db`.

The `run-server` script can drive this for you, including the ngrok tunnel:

```bash
./run-server --docker            # build + start the container, host everything
./run-server --docker --no-ngrok # ...without the public tunnel
```

`--docker` brings the stack up with `docker compose up -d --build`, waits for
the web port, prints the local/LAN/public URLs, and tears the container back
down (`docker compose down`) on Ctrl+C. `TERMCHAT_PORT` / `TERMCHAT_WEB_PORT`
set the published host ports. Without `--docker` it runs from the local venv
instead.

Heads up: traffic is plaintext JSON over TCP (passwords are hashed at
rest, but the wire isn't encrypted). If this is going over the open
internet, tunnel it - SSH port forward, VPN, whatever. Haven't gotten
around to TLS.

## Layout

```
termchat/
├── termchat/
│   ├── protocol.py   # framing + message schema
│   ├── database.py   # sqlite + password hashing
│   ├── server.py     # TCP server
│   ├── client.py     # TUI client
│   ├── gui.py         # Tkinter client
│   ├── ws.py          # websocket bits
│   └── web.py         # browser client
├── assets/termchat.ico
├── init_db.py
├── run_server.py / run_client.py / run_gui.py
├── requirements.txt / pyproject.toml
├── Dockerfile / docker-compose.yml / .dockerignore
```

MIT.
