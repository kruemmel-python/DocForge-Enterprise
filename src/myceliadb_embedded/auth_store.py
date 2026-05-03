from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class User:
    username: str
    role: str
    active: bool


class MyceliaIdentityStore:
    """Small local identity/session store for the DocForge WebGUI.

    Defensive guarantees:
    - Empty or malformed cookies never reach SQLite as invalid parameters.
    - All SQLite access is serialized with an RLock.
    - Expired or unknown sessions return None instead of raising.
    """

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(root / "mycelia_identity.sqlite3", check_same_thread=False)
        self._init()

    def _init(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                create table if not exists users(
                    username text primary key,
                    salt text,
                    password_hash text,
                    role text,
                    active integer,
                    created_at real
                );

                create table if not exists sessions(
                    token text primary key,
                    username text,
                    csrf text,
                    expires_at real,
                    created_at real
                );

                create table if not exists audit(
                    id integer primary key,
                    username text,
                    action text,
                    target text,
                    ip text,
                    metadata text,
                    created_at real
                );
                """
            )
            self.conn.commit()

    def user_count(self) -> int:
        with self._lock:
            return int(self.conn.execute("select count(*) from users").fetchone()[0])

    def _hash(self, password: str, salt: str) -> str:
        return hashlib.pbkdf2_hmac(
            "sha256",
            str(password).encode("utf-8"),
            base64.b64decode(salt),
            200_000,
        ).hex()

    def register(self, username: str, password: str, role: str = "viewer") -> None:
        username = str(username or "").strip()
        password = str(password or "")
        if not username:
            raise ValueError("username is required")
        if len(password) < 8:
            raise ValueError("password must contain at least 8 characters")
        if self.user_count() == 0:
            role = "admin"
        salt = base64.b64encode(os.urandom(16)).decode("ascii")
        password_hash = self._hash(password, salt)
        with self._lock:
            self.conn.execute(
                "insert into users values(?,?,?,?,?,?)",
                (username, salt, password_hash, role, 1, time.time()),
            )
            self.conn.commit()
        self.audit(username, "register", role)

    def verify(self, username: str, password: str) -> bool:
        username = str(username or "").strip()
        password = str(password or "")
        if not username or not password:
            return False
        with self._lock:
            row = self.conn.execute(
                "select username, salt, password_hash, role, active, created_at from users where username=? and active=1",
                (username,),
            ).fetchone()
        return bool(row and hmac.compare_digest(str(row[2]), self._hash(password, str(row[1]))))

    def role(self, username: str) -> str:
        username = str(username or "").strip()
        if not username:
            return "viewer"
        with self._lock:
            row = self.conn.execute("select role from users where username=?", (username,)).fetchone()
        return str(row[0]) if row else "viewer"

    def create_session(self, username: str, ttl: int = 28800) -> tuple[str, str]:
        username = str(username or "").strip()
        if not username:
            raise ValueError("username is required")
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        expires_at = time.time() + int(ttl)
        with self._lock:
            self.conn.execute(
                "insert into sessions values(?,?,?,?,?)",
                (token, username, csrf, expires_at, time.time()),
            )
            self.conn.commit()
        self.audit(username, "login", "webgui")
        return token, csrf

    def session(self, token: object) -> dict[str, str] | None:
        if not isinstance(token, str):
            return None
        token = token.strip()
        if not token:
            return None
        try:
            with self._lock:
                row = self.conn.execute(
                    "select username, csrf, expires_at from sessions where token=?",
                    (token,),
                ).fetchone()
                if not row:
                    return None
                if float(row[2]) < time.time():
                    self.conn.execute("delete from sessions where token=?", (token,))
                    self.conn.commit()
                    return None
                username = str(row[0])
                csrf = str(row[1])
            return {"username": username, "csrf": csrf, "role": self.role(username)}
        except sqlite3.Error:
            # A corrupted cookie/session must never crash the WebGUI request handler.
            return None

    def logout(self, token: object) -> None:
        session = self.session(token)
        if not isinstance(token, str) or not token.strip():
            return
        with self._lock:
            self.conn.execute("delete from sessions where token=?", (token.strip(),))
            self.conn.commit()
        if session:
            self.audit(session["username"], "logout", "webgui")

    def audit(self, username: str, action: str, target: str = "", ip: str = "", metadata: str = "") -> None:
        with self._lock:
            self.conn.execute(
                "insert into audit(username,action,target,ip,metadata,created_at) values(?,?,?,?,?,?)",
                (str(username or ""), str(action or ""), str(target or ""), str(ip or ""), str(metadata or ""), time.time()),
            )
            self.conn.commit()
