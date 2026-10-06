"""Cashier / manager authentication for the OfflinePOS web app.

- Passwords: PBKDF2-HMAC-SHA256 with per-user salt (stdlib only).
- Sessions: random bearer tokens, in-memory with 8h expiry.
- Roles: ``cashier`` (billing + assistant) and ``manager`` (everything,
  incl. sync control, price changes, conflict review, demos).

Seeded demo accounts (change in production!):
  manager / admin123   (manager)
  cashier / cashier123 (cashier)
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

SESSION_TTL_S = 8 * 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    username  TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    role      TEXT NOT NULL CHECK(role IN ('cashier','manager')),
    salt      BLOB NOT NULL,
    pw_hash   BLOB NOT NULL,
    created_at REAL NOT NULL
);
"""

ROLES = ("cashier", "manager")


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)


class AuthStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.Lock()
        init = sqlite3.connect(self.path, timeout=30.0)
        init.executescript(SCHEMA)
        init.commit()
        init.close()
        self._sessions: dict[str, dict] = {}

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=30.0, check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c

    # -- users ---------------------------------------------------------------
    def create_user(self, username: str, password: str, role: str, name: str):
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        if len(password) < 6:
            raise ValueError("password must be at least 6 characters")
        salt = os.urandom(16)
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO users(username,name,role,salt,pw_hash,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (username, name, role, salt, _hash(password, salt), time.time()))
            if cur.rowcount == 0:
                raise ValueError(f"user {username!r} already exists")

    def verify(self, username: str, password: str) -> dict | None:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT * FROM users WHERE username=?",
                            (username,)).fetchone()
        if row is None:
            return None
        if not hmac.compare_digest(_hash(password, row["salt"]), row["pw_hash"]):
            return None
        return {"username": row["username"], "name": row["name"],
                "role": row["role"]}

    def seed_defaults(self):
        for username, password, role, name in (
                ("manager", "admin123", "manager", "Store Manager"),
                ("cashier", "cashier123", "cashier", "Cashier")):
            try:
                self.create_user(username, password, role, name)
            except ValueError:
                pass  # already seeded

    # -- sessions --------------------------------------------------------------
    def new_session(self, user: dict) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = {**user, "expires": time.time() + SESSION_TTL_S}
        return token

    def get_session(self, token: str | None) -> dict | None:
        if not token:
            return None
        with self._lock:
            s = self._sessions.get(token)
            if s is None or s["expires"] < time.time():
                self._sessions.pop(token, None)
                return None
            return s

    def end_session(self, token: str | None):
        with self._lock:
            self._sessions.pop(token, None)
