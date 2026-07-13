# termchat

A **TeamSpeak-style terminal chat application** — a multi-user, channel-based
chat system with a TCP server, a SQLite database, and a beautiful full-screen
terminal UI (TUI) client for Linux.

```
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
```

## Features

- **User accounts** — register and log in with a password (hashed with
  PBKDF2-HMAC-SHA256 + per-user salt; plaintext passwords are never stored).
- **Channels/rooms** — like TeamSpeak. Ships with `#general`, `#random`,
  `#help`; create your own on the fly.
- **Real-time messaging** — messages broadcast instantly to everyone viewing
  the same channel.
- **Online presence** — see who is currently in each channel, live.
- **Message history** — the last 50 messages load automatically when you open
  a channel; everything is persisted in SQLite.
- **Beautiful TUI** — left sidebar with channels + online users, main chat
  area, input line, per-user colours, keyboard navigation.
- **Graceful disconnects** — leaving or dropping a connection updates
  everyone else's user list automatically.
- **Single account, single session** — the same account can't be logged in
  from two places at once.

## Architecture

```
        TCP (length-prefixed JSON frames)
 client  <----------------------------------->  server  <-->  SQLite DB
 (rich TUI)                                   (threaded)      (users, channels,
                                                               messages,
                                                               memberships)
```

| Component | File | Responsibility |
|-----------|------|----------------|
| Wire protocol | `termchat/protocol.py` | 4-byte length-prefixed JSON framing + message schema |
| Database | `termchat/database.py` | SQLite schema, password hashing, all queries |
| Server | `termchat/server.py` | TCP server, sessions, auth, routing, broadcast |
| Client | `termchat/client.py` | rich TUI, login screen, keyboard handling |

The server is **threaded** — one thread per connected client — with all shared
state guarded by a lock. The client runs the renderer + keyboard loop on the
main thread and a background thread for receiving server pushes.

### Database schema

```sql
users       (id, username UNIQUE, password_hash, created_at)
channels    (id, name UNIQUE, created_by -> users.id, created_at)
messages    (id, channel_id -> channels.id, user_id -> users.id, content, timestamp)
memberships (user_id, channel_id, joined_at)   -- who has joined which channel
```

## Requirements

- **Python 3.8+**
- **[`rich`](https://github.com/Textualize/rich)** — the only third-party
  dependency. Everything else (`socket`, `sqlite3`, `hashlib`, `threading`,
  `termios`) is in the standard library.
- A Linux/macOS terminal for the client (it uses raw-mode `termios`, so it does
  not run on native Windows — use WSL there).

## Installation

```bash
git clone <this-repo> termchat
cd termchat

# recommended: a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# install the one dependency
pip install -r requirements.txt
```

Optionally install it as a package to get the `termchat-server` /
`termchat-client` console commands:

```bash
pip install -e .
```

## Running

### 1. (Optional) initialise the database

The server creates the database and default channels automatically on first
start, so this step is optional. Use it to pre-seed data or add a user:

```bash
python init_db.py                    # create schema + default channels
python init_db.py --add-user alice   # also create a user (prompts for pw)
python init_db.py --show             # print a summary of the database
```

### 2. Start the server

```bash
python run_server.py --host 0.0.0.0 --port 9009 --db termchat.db
# or, if installed as a package:
termchat-server --host 0.0.0.0 --port 9009
```

- `--host 0.0.0.0` listens on all interfaces (use this when hosting for other
  machines). Default is also `0.0.0.0`.
- `--port` defaults to `9009`.
- `--db` path to the SQLite file (default `termchat.db`).
- `-v` for debug logging.

### 3. Connect with the client

In another terminal (or from another machine):

```bash
python run_client.py --host 127.0.0.1 --port 9009
# or:
termchat-client --host chat.example.com --port 9009
```

You'll get a login screen — choose **register** the first time to create an
account, then log straight in.

## Using the client

Once logged in you're dropped into the first channel.

### Keyboard shortcuts

| Key | Action |
|-----|--------|
| `Enter` | Send the current line (or run a `/command`) |
| `Ctrl-N` / `↓` | Switch to the next channel |
| `Ctrl-P` / `↑` | Switch to the previous channel |
| `Backspace` | Delete a character |
| `Ctrl-C` / `Ctrl-Q` | Quit |

### Slash commands

| Command | Action |
|---------|--------|
| `/help` | Show a quick command reference |
| `/create <name>` | Create a new channel |
| `/join <name\|id>` | Join a channel by name or number |
| `/channels` | Refresh the channel list |
| `/users` | Refresh the online-user list |
| `/quit` | Disconnect and exit |

## Example session

Terminal 1 — the server:

```bash
$ python run_server.py
12:00:00 INFO    termchat server listening on 0.0.0.0:9009
12:00:07 INFO    connection from ('127.0.0.1', 51234)
12:00:12 INFO    alice logged in (('127.0.0.1', 51234))
```

Terminal 2 — Alice:

```
Choose [login/register/quit] (login): register
username: alice
password: ****
account created — logging in...
   ... TUI opens, Alice is in #general ...
> hello everyone
```

Terminal 3 — Bob (after registering/logging in and pressing `Ctrl-N` to
land in `#general`) sees Alice's message appear in real time, and Alice's
sidebar shows `ONLINE (2)  * alice  * bob`. Bob types:

```
> /create dev
> /join dev
> anyone around?
```

Alice switches with `Ctrl-N` until she reaches `#dev` and sees the history.

## Hosting on a Linux server

1. Copy the project to the server and install the dependency
   (`pip install -r requirements.txt`).
2. Run the server bound to a public interface:
   `python run_server.py --host 0.0.0.0 --port 9009 --db /var/lib/termchat/termchat.db`
3. Open the port in your firewall (e.g. `sudo ufw allow 9009/tcp`).
4. Keep it running with a process manager. Example `systemd` unit:

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

```bash
sudo systemctl enable --now termchat
```

Clients then connect with
`python run_client.py --host your.server.ip --port 9009`.

> **Security note:** traffic is sent as plaintext JSON over TCP. Passwords are
> hashed at rest, but for use over an untrusted network you should tunnel the
> connection (e.g. run the client over SSH with a port forward, or put the
> server behind a VPN). TLS is a natural next step.

## Project layout

```
termchat/
├── termchat/
│   ├── __init__.py       # package metadata + defaults
│   ├── protocol.py       # framing + message schema (shared)
│   ├── database.py       # SQLite layer + password hashing
│   ├── server.py         # TCP server
│   └── client.py         # rich TUI client
├── init_db.py            # database init / inspection tool
├── run_server.py         # server launcher
├── run_client.py         # client launcher
├── requirements.txt
├── pyproject.toml        # packaging + console scripts
└── README.md
```

## License

MIT
