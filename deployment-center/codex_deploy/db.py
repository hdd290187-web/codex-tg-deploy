from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id_hash TEXT PRIMARY KEY,
    csrf_hash TEXT NOT NULL,
    ip TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip TEXT NOT NULL,
    attempted_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_ip_time ON login_attempts(ip, attempted_at);
CREATE TABLE IF NOT EXISTS bot_bindings (
    session_hash TEXT PRIMARY KEY,
    token_encrypted TEXT NOT NULL,
    bot_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    display_name TEXT NOT NULL,
    update_offset INTEGER NOT NULL DEFAULT 0,
    user_id INTEGER,
    chat_id INTEGER,
    expires_at INTEGER NOT NULL,
    FOREIGN KEY(session_hash) REFERENCES sessions(id_hash) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS deployments (
    id TEXT PRIMARY KEY,
    token_hash TEXT UNIQUE NOT NULL,
    secret_encrypted TEXT,
    status TEXT NOT NULL,
    gateway_version TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    claimed_at INTEGER,
    completed_at INTEGER,
    install_session_hash TEXT,
    install_session_expires_at INTEGER,
    progress_json TEXT NOT NULL DEFAULT '[]',
    error_summary TEXT
);
CREATE INDEX IF NOT EXISTS idx_deployments_created ON deployments(created_at DESC);
CREATE TABLE IF NOT EXISTS config_templates (
    engine TEXT NOT NULL, version INTEGER NOT NULL, content TEXT NOT NULL,
    sha256 TEXT NOT NULL, created_at INTEGER NOT NULL,
    PRIMARY KEY(engine, version)
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def cleanup(self, now: int | None = None) -> None:
        now = now or int(time.time())
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
            conn.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (now - 86400,))
            conn.execute("DELETE FROM bot_bindings WHERE expires_at < ?", (now,))
            conn.execute(
                "UPDATE deployments SET status='EXPIRED', secret_encrypted=NULL "
                "WHERE status='WAITING' AND expires_at < ?",
                (now,),
            )
            conn.execute(
                """UPDATE deployments SET status='FAILED',completed_at=?,secret_encrypted=NULL,
                   install_session_hash=NULL,install_session_expires_at=NULL,
                   error_summary=COALESCE(error_summary,'installation session expired')
                   WHERE status='CLAIMED' AND install_session_expires_at IS NOT NULL
                     AND install_session_expires_at < ?""",
                (now, now),
            )
            conn.execute(
                "UPDATE deployments SET install_session_hash=NULL, install_session_expires_at=NULL "
                "WHERE install_session_expires_at IS NOT NULL AND install_session_expires_at < ?",
                (now,),
            )
            conn.commit()

    def create_session(self, sid_hash: str, csrf_hash: str, ip: str, expires_at: int) -> None:
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO sessions(id_hash,csrf_hash,ip,created_at,expires_at) VALUES(?,?,?,?,?)",
                (sid_hash, csrf_hash, ip, now, expires_at),
            )

    def session(self, sid_hash: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM sessions WHERE id_hash=? AND expires_at>=?",
                (sid_hash, int(time.time())),
            ).fetchone()

    def delete_session(self, sid_hash: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE id_hash=?", (sid_hash,))

    def failed_login_count(self, ip: str, since: int) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT count(*) AS n FROM login_attempts WHERE ip=? AND attempted_at>=?", (ip, since)
            ).fetchone()
            return int(row["n"])

    def record_failed_login(self, ip: str) -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO login_attempts(ip,attempted_at) VALUES(?,?)", (ip, int(time.time())))

    def clear_failed_logins(self, ip: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM login_attempts WHERE ip=?", (ip,))

    def put_binding(self, values: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO bot_bindings(
                    session_hash,token_encrypted,bot_id,username,display_name,update_offset,expires_at
                ) VALUES(:session_hash,:token_encrypted,:bot_id,:username,:display_name,:update_offset,:expires_at)
                ON CONFLICT(session_hash) DO UPDATE SET
                    token_encrypted=excluded.token_encrypted, bot_id=excluded.bot_id,
                    username=excluded.username, display_name=excluded.display_name,
                    update_offset=excluded.update_offset, user_id=NULL, chat_id=NULL,
                    expires_at=excluded.expires_at""",
                values,
            )

    def binding(self, session_hash: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM bot_bindings WHERE session_hash=? AND expires_at>=?",
                (session_hash, int(time.time())),
            ).fetchone()

    def update_binding_poll(
        self, session_hash: str, offset: int, user_id: int | None = None, chat_id: int | None = None
    ) -> None:
        with self.connect() as conn:
            if user_id is None:
                conn.execute(
                    "UPDATE bot_bindings SET update_offset=? WHERE session_hash=?", (offset, session_hash)
                )
            else:
                conn.execute(
                    "UPDATE bot_bindings SET update_offset=?,user_id=?,chat_id=? WHERE session_hash=?",
                    (offset, user_id, chat_id, session_hash),
                )

    def create_deployment(self, values: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO deployments(
                    id,token_hash,secret_encrypted,status,gateway_version,created_at,expires_at,progress_json
                ) VALUES(:id,:token_hash,:secret_encrypted,'WAITING',:gateway_version,:created_at,:expires_at,'[]')""",
                values,
            )

    def deployment(self, deployment_id: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM deployments WHERE id=?", (deployment_id,)).fetchone()

    def deployment_by_token(self, hashed: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM deployments WHERE token_hash=?", (hashed,)).fetchone()

    def claim(self, hashed: str, session_hash: str, session_expires: int) -> sqlite3.Row | None:
        now = int(time.time())
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM deployments WHERE token_hash=?", (hashed,)).fetchone()
            if not row or row["status"] != "WAITING" or row["expires_at"] < now or not row["secret_encrypted"]:
                conn.rollback()
                return None
            updated = conn.execute(
                """UPDATE deployments SET status='CLAIMED',claimed_at=?,install_session_hash=?,
                   install_session_expires_at=?,progress_json=?
                   WHERE id=? AND status='WAITING'""",
                (
                    now,
                    session_hash,
                    session_expires,
                    json.dumps([{"stage": "CLAIMED", "at": now}], separators=(",", ":")),
                    row["id"],
                ),
            )
            if updated.rowcount != 1:
                conn.rollback()
                return None
            conn.commit()
            return row

    def destroy_claimed_secret(self, deployment_id: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE deployments SET secret_encrypted=NULL WHERE id=?", (deployment_id,))

    def install_session(self, deployment_id: str, session_hash: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM deployments WHERE id=? AND install_session_hash=?
                   AND install_session_expires_at>=?""",
                (deployment_id, session_hash, int(time.time())),
            ).fetchone()

    def add_progress(self, deployment_id: str, stage: str, detail: str = "") -> None:
        with self._lock, self.connect() as conn:
            row = conn.execute("SELECT progress_json FROM deployments WHERE id=?", (deployment_id,)).fetchone()
            if not row:
                return
            progress = json.loads(row["progress_json"])
            progress.append({"stage": stage[:48], "detail": detail[:240], "at": int(time.time())})
            conn.execute(
                "UPDATE deployments SET progress_json=? WHERE id=?",
                (json.dumps(progress[-80:], separators=(",", ":"), ensure_ascii=False), deployment_id),
            )

    def finish(self, deployment_id: str, success: bool, error: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE deployments SET status=?,completed_at=?,error_summary=?,
                   install_session_hash=NULL,install_session_expires_at=NULL,secret_encrypted=NULL
                   WHERE id=?""",
                ("SUCCESS" if success else "FAILED", int(time.time()), (error or "")[:500] or None, deployment_id),
            )

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT id,status,gateway_version,created_at,completed_at,error_summary
                   FROM deployments ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def config_templates(self, engine: str, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute('SELECT engine,version,content,sha256,created_at FROM config_templates WHERE engine=? ORDER BY version DESC LIMIT ?', (engine, limit)).fetchall()
        return [dict(row) for row in rows]

    def config_template(self, engine: str, version: int | None = None) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute('SELECT engine,version,content,sha256,created_at FROM config_templates WHERE engine=? ORDER BY version DESC LIMIT 1' if version is None else 'SELECT engine,version,content,sha256,created_at FROM config_templates WHERE engine=? AND version=?', (engine,) if version is None else (engine, version)).fetchone()
        return dict(row) if row else None

    def put_config_template(self, engine: str, content: str, sha256: str) -> int:
        with self._lock, self.connect() as conn:
            version = int(conn.execute('SELECT COALESCE(MAX(version),0)+1 FROM config_templates WHERE engine=?', (engine,)).fetchone()[0])
            conn.execute('INSERT INTO config_templates(engine,version,content,sha256,created_at) VALUES(?,?,?,?,?)', (engine, version, content, sha256, int(time.time())))
        return version
