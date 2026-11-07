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
- **end-to-end encryption** (per-user keypairs; the server only ever sees
  ciphertext — see [Encryption](#end-to-end-encryption) below)
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
| `termchat/crypto.py` | end-to-end encryption (keypairs, message sealing) |
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
Each user row also carries their public key and a wrapped copy of their
private key (see below); the `messages.content` column holds ciphertext for
encrypted messages.

## End-to-end encryption

Messages can be end-to-end encrypted so that only channel members — never
the server or anyone with the database file — can read them.

**How it works**

- Every user has an **ECDH P-256 keypair**. The private key is stored as a
  PKCS8 blob wrapped with AES-256-GCM under a key derived (PBKDF2-SHA256,
  200k iterations) from an **encryption passphrase**. That passphrase is
  *separate from your login password* and is **never sent to the server**,
  so the server cannot unwrap your private key. The wrapped blob and your
  public key are stored server-side only so you can restore your identity on
  another device.
- Each message uses hybrid encryption: a random 256-bit content key seals the
  text with AES-256-GCM, and that content key is ECIES-wrapped (ephemeral
  ECDH → HKDF-SHA256 → AES-GCM) to every channel member's public key. The
  server stores and relays only the resulting `TCE1:…` ciphertext envelope.
- All three clients implement the exact same scheme — the Python clients via
  `cryptography`, the browser via the built-in WebCrypto API — so they
  interoperate byte for byte.

**Using it**

On login each client offers to set up encryption (first time) or unlock it
(after that) with your encryption passphrase. Skip it and you fall back to
plaintext; unlock it and messages are encrypted automatically. A lock
indicator shows the current state (🔒 e2e / 🔒 locked / 🔓 plaintext).

**Limitations, by design**

- A message is encrypted to whoever is a member *at send time*, so someone
  who joins a channel later cannot read older history. (New messages include
  them once their key is known.)
- Metadata is **not** encrypted: usernames, channel names, who's online, and
  message timestamps still travel and are stored in the clear. Use TLS/a
  tunnel (below) if that matters.
- The encryption passphrase cannot be recovered. Forget it and the encrypted
  messages addressed to you are unreadable — there is no backdoor.
- Users who never set up a key can't be sent readable messages; their client
  shows encrypted messages as locked placeholders.

## Setup

Needs Python 3.8+. Two third-party dependencies: `rich` (TUI client only)
and `cryptography` (end-to-end encryption, used by the server and the Python
clients). Everything else is stdlib. The browser client needs neither — it
uses the built-in WebCrypto API.

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

Tkinter UI (stdlib) plus `cryptography` for encryption; PyInstaller bundles
both into the exe. Runs on Windows/macOS/Linux. Tick "host a local
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

Heads up: message bodies are end-to-end encrypted once you unlock
encryption, but the transport itself is still plaintext JSON over TCP, and
metadata (usernames, channel names, presence, timestamps) travels in the
clear. Passwords are hashed at rest. If metadata privacy matters or you're
going over the open internet, tunnel it - SSH port forward, VPN, whatever.
Haven't gotten around to TLS.

## Layout

```
termchat/
├── termchat/
│   ├── protocol.py   # framing + message schema
│   ├── crypto.py     # end-to-end encryption
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
