"""SQLite persistence layer for termchat.

A single :class:`Database` instance is shared across all server threads.
SQLite connections are not thread-safe for concurrent use, so every
operation is serialised behind a lock.  Chat traffic is light and writes
are tiny, so this is more than fast enough while keeping the code simple
and correct.
"""

import hashlib
import hmac
import os
import sqlite3
import threading
import time

# PBKDF2 iteration count.  High enough to be a real speed bump for offline
# cracking, low enough to keep logins snappy.
_PBKDF2_ITERATIONS = 200_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS channels (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    content    TEXT NOT NULL,
    timestamp  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS memberships (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    joined_at  INTEGER NOT NULL,
    PRIMARY KEY (user_id, channel_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_channel
    ON messages(channel_id, id);
"""

# Channels created automatically on a brand-new database.
_DEFAULT_CHANNELS = ["general", "random", "help"]


def hash_password(password: str, salt: bytes = None) -> str:
    """Return a ``salt:hash`` string suitable for storing in the DB."""
    if salt is None:
        salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                             _PBKDF2_ITERATIONS)
    return f"{salt.hex()}:{dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Check ``password`` against a stored ``salt:hash`` string."""
    try:
        salt_hex, dk_hex = stored.split(":", 1)
        salt = bytes.fromhex(salt_hex)
    except (ValueError, TypeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                             _PBKDF2_ITERATIONS)
    return hmac.compare_digest(dk.hex(), dk_hex)


class Database:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.init_schema()

    # -- setup ---------------------------------------------------------------

    def init_schema(self):
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
            # Seed default channels only if the table is empty.
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM channels").fetchone()
            if row["n"] == 0:
                now = int(time.time())
                self._conn.executemany(
                    "INSERT INTO channels (name, created_by, created_at) "
                    "VALUES (?, NULL, ?)",
                    [(name, now) for name in _DEFAULT_CHANNELS],
                )
                self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    # -- users ---------------------------------------------------------------

    def create_user(self, username: str, password: str):
        """Return ``(user_id, None)`` on success or ``(None, error)``."""
        username = username.strip()
        if not username:
            return None, "Username cannot be empty."
        if len(username) > 32:
            return None, "Username must be 32 characters or fewer."
        if not password:
            return None, "Password cannot be empty."
        now = int(time.time())
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO users (username, password_hash, created_at) "
                    "VALUES (?, ?, ?)",
                    (username, hash_password(password), now),
                )
                self._conn.commit()
                return cur.lastrowid, None
            except sqlite3.IntegrityError:
                return None, "That username is already taken."

    def authenticate(self, username: str, password: str):
        """Return the user row on success, or ``None``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE username = ?",
                (username.strip(),),
            ).fetchone()
        if row is None:
            return None
        if not verify_password(password, row["password_hash"]):
            return None
        return row

    # -- channels ------------------------------------------------------------

    def list_channels(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT c.id, c.name, c.created_by, c.created_at, "
                "       COUNT(m.user_id) AS member_count "
                "FROM channels c "
                "LEFT JOIN memberships m ON m.channel_id = c.id "
                "GROUP BY c.id "
                "ORDER BY c.id"
            ).fetchall()
        return [dict(r) for r in rows]

    def create_channel(self, name: str, created_by: int):
        name = name.strip().lstrip("#").strip()
        if not name:
            return None, "Channel name cannot be empty."
        if len(name) > 32:
            return None, "Channel name must be 32 characters or fewer."
        now = int(time.time())
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO channels (name, created_by, created_at) "
                    "VALUES (?, ?, ?)",
                    (name, created_by, now),
                )
                self._conn.commit()
                return cur.lastrowid, None
            except sqlite3.IntegrityError:
                return None, "A channel with that name already exists."

    def get_channel(self, channel_id: int):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM channels WHERE id = ?", (channel_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_channel_by_name(self, name: str):
        name = name.strip().lstrip("#").strip()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM channels WHERE name = ?", (name,)
            ).fetchone()
        return dict(row) if row else None

    # -- memberships ---------------------------------------------------------

    def add_membership(self, user_id: int, channel_id: int):
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO memberships "
                "(user_id, channel_id, joined_at) VALUES (?, ?, ?)",
                (user_id, channel_id, now),
            )
            self._conn.commit()

    # -- messages ------------------------------------------------------------

    def add_message(self, channel_id: int, user_id: int, content: str) -> int:
        """Persist a message and return its timestamp (unix seconds)."""
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages (channel_id, user_id, content, timestamp) "
                "VALUES (?, ?, ?, ?)",
                (channel_id, user_id, content, now),
            )
            self._conn.commit()
        return now

    def get_history(self, channel_id: int, limit: int = 50):
        """Return the last ``limit`` messages in chronological order."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT m.channel_id, m.user_id, m.content, m.timestamp, "
                "       COALESCE(u.username, '(deleted)') AS username "
                "FROM messages m "
                "LEFT JOIN users u ON u.id = m.user_id "
                "WHERE m.channel_id = ? "
                "ORDER BY m.id DESC LIMIT ?",
                (channel_id, limit),
            ).fetchall()
        # Rows came back newest-first; flip to oldest-first for display.
        return [dict(r) for r in reversed(rows)]
