"""Encrypted storage for browser sessions in a single API deployment."""

import json
import sqlite3
from contextlib import closing
from hashlib import sha256
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class BrowserSessionStore:
    def __init__(self, path: str, key: str, namespace: str):
        self.path = Path(path)
        self.cipher = Fernet(key.encode())
        self.namespace = namespace

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=3)
        try:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS browser_sessions (
                    key TEXT PRIMARY KEY, deadline REAL NOT NULL, ciphertext BLOB NOT NULL
                )
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS browser_sessions_deadline ON browser_sessions(deadline)
            """)
        except BaseException:
            connection.close()
            raise
        return connection

    def identity(self, session_id: str) -> str:
        return sha256(f"{self.namespace}\x00{session_id}".encode()).hexdigest()

    def read(self, session_id: str, now: float) -> dict | None:
        key = self.identity(session_id)
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT ciphertext FROM browser_sessions WHERE key=? AND deadline>?",
                (key, now),
            ).fetchone()
        if row is None:
            return None
        try:
            decoded = json.loads(self.cipher.decrypt(row[0]))
            if decoded["key"] == key and isinstance(decoded["session"], dict):
                return decoded["session"]
        except (InvalidToken, ValueError, KeyError, TypeError):
            return None
        return None

    def write(self, session_id: str, data: dict, deadline: float, now: float) -> None:
        key = self.identity(session_id)
        ciphertext = self.cipher.encrypt(json.dumps({"key": key, "session": data}).encode())
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM browser_sessions WHERE deadline<=?", (now,))
            db.execute(
                """
                INSERT INTO browser_sessions(key, deadline, ciphertext) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    deadline=excluded.deadline, ciphertext=excluded.ciphertext
            """,
                (key, deadline, ciphertext),
            )

    def delete(self, session_id: str) -> None:
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM browser_sessions WHERE key=?", (self.identity(session_id),))
