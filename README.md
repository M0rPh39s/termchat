# termchat

A **TeamSpeak-style chat application** — a multi-user, channel-based chat
system with a TCP server, a SQLite database, a full-screen terminal UI (TUI)
client for Linux, and a **cross-platform desktop GUI client** that can be
packaged into a standalone Windows executable.

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
- **Desktop GUI app** — a dark-themed point-and-click client (Tkinter) that
  can also **host the server itself**, and builds into a single standalone
  `TermChat.exe` for Windows — no Python needed on the target machine.
- **Graceful disconnects** — leaving or dropping a connection updates
  everyone else's user list automatically.
- **Single account, single session** — the same account can't be logged in
  from two places at once.

## Architecture

```
          TCP (length-prefixed JSON frames)
 clients  <---------------------------------->  server  <-->  SQLite DB
 (rich TUI /                                  (threaded)      (users, channels,
  Tkinter GUI)                                                 messages,
                                                               memberships)
```

| Component | File | Responsibility |
|-----------|------|----------------|
| Wire protocol | `termchat/protocol.py` | 4-byte length-prefixed JSON framing + message schema |
| Database | `termchat/database.py` | SQLite schema, password hashing, all queries |
| Server | `termchat/server.py` | TCP server, sessions, auth, routing, broadcast |
| TUI client | `termchat/client.py` | rich TUI, login screen, keyboard handling |
| GUI client | `termchat/gui.py` | Tkinter desktop app; can also embed the server |

The server is **threaded** — one thread per connected client — with all shared
state guarded by a lock. Both clients run their UI on the main thread and a
background thread for receiving server pushes (the GUI hands them to Tk
through a queue).

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
  dependency, and only needed by the **TUI** client. Everything else
  (`socket`, `sqlite3`, `hashlib`, `threading`, `termios`, `tkinter`) is in
  the standard library.
- The **TUI client** needs a Linux/macOS terminal (raw-mode `termios`, so it
  does not run on native Windows — use WSL there). The **GUI client** and the
  **server** run anywhere, including native Windows.

## Installation

```bash
git clone https://github.com/M0rPh39s/termchat.git
cd termchat

# recommended: a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# install the one dependency
pip install -r requirements.txt
```

Optionally install it as a package to get the `termchat-server`,
`termchat-client` and `termchat-gui` commands:

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

## Desktop GUI app

The GUI client (`termchat/gui.py`) is a point-and-click alternative to the
TUI. It uses only the Python standard library (Tkinter), so it runs on
Windows, macOS and Linux with no extra dependencies:

```bash
python run_gui.py
# or, if installed as a package:
termchat-gui
```

- **Log in / Register** — enter the server host and port, pick a username and
  password, and click *Register* the first time (it logs you straight in).
- **Host a local server** — tick the checkbox on the login screen and the app
  starts an embedded server on the chosen port before connecting. Friends on
  your network can then join with the LAN address shown in the status line.
  The embedded server stores its database in `%LOCALAPPDATA%\TermChat\`
  (or `~/TermChat` if that variable is unset).
- **Channels** — click a channel in the sidebar to join it; *+ New channel*
  creates (and auto-joins) one. The online list and messages update live.
- **Send** — type in the input bar and press Enter or click *Send*.

### Building a standalone Windows executable

```powershell
pip install pyinstaller
python -m PyInstaller --noconfirm --onefile --windowed --name TermChat `
    --icon assets\termchat.ico --add-data "assets\termchat.ico;assets" run_gui.py
```

The result is a single self-contained `dist\TermChat.exe` (~12 MB, no Python
installation required on the target machine). Because it can also host the
server, one exe is enough to run a whole chat for your LAN.

## Using the terminal client

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
│   ├── client.py         # rich TUI client
│   └── gui.py            # Tkinter desktop GUI client
├── assets/
│   └── termchat.ico      # app icon (window + exe)
├── init_db.py            # database init / inspection tool
├── run_server.py         # server launcher
├── run_client.py         # TUI client launcher
├── run_gui.py            # GUI launcher / PyInstaller entry point
├── requirements.txt
├── pyproject.toml        # packaging + console scripts
└── README.md
```

## License

MIT
