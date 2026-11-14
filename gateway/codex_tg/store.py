from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    thread_id TEXT,
    model TEXT,
    reasoning_effort TEXT,
    plan_reasoning_effort TEXT,
    model_verified INTEGER NOT NULL DEFAULT 0,
    active_engine TEXT NOT NULL DEFAULT 'codex',
    claude_session_id TEXT,
    claude_conversation_key TEXT,
    claude_model TEXT,
    claude_effort TEXT,
    claude_verified INTEGER NOT NULL DEFAULT 0,
    tavily_enabled INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    last_active_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    thread_id TEXT,
    thread_task_number INTEGER,
    model TEXT,
    reasoning_effort TEXT,
    plan_reasoning_effort TEXT,
    engine TEXT NOT NULL DEFAULT 'codex',
    session_id TEXT,
    effort TEXT,
    text TEXT NOT NULL,
    attachments_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    completed_at INTEGER,
    error TEXT,
    final_response TEXT,
    codex_prompt_ref TEXT,
    FOREIGN KEY(chat_id) REFERENCES chats(chat_id)
);
CREATE INDEX IF NOT EXISTS idx_tasks_status_id ON tasks(status,id);
CREATE TABLE IF NOT EXISTS thread_counters (
    thread_id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    last_task_number INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    last_selected_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at DESC);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS codex_prompt_state (
    id INTEGER PRIMARY KEY CHECK(id=1),
    enabled INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER,
    length INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER REFERENCES tasks(id), chat_id INTEGER NOT NULL,
    name TEXT NOT NULL, source_path TEXT NOT NULL, stored_path TEXT NOT NULL,
    size INTEGER NOT NULL, sha256 TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'READY', error TEXT,
    created_at INTEGER NOT NULL, delivered_at INTEGER,
    UNIQUE(task_id,source_path,sha256)
);
CREATE TABLE IF NOT EXISTS artifact_parts (
    artifact_id INTEGER NOT NULL REFERENCES artifacts(id), number INTEGER NOT NULL,
    path TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
    message_id INTEGER, file_id TEXT, attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(artifact_id,number)
);
CREATE TABLE IF NOT EXISTS upload_batches (
 id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER UNIQUE REFERENCES tasks(id),
 chat_id INTEGER NOT NULL, mode TEXT NOT NULL, group_id TEXT, state TEXT NOT NULL DEFAULT 'OPEN',
 updated_at REAL NOT NULL, progress_id INTEGER
);
CREATE TABLE IF NOT EXISTS upload_members (
 chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, batch_id INTEGER NOT NULL REFERENCES upload_batches(id),
 message_json TEXT NOT NULL, attachments_json TEXT, error TEXT, state TEXT NOT NULL DEFAULT 'PENDING',
 PRIMARY KEY(chat_id,message_id)
);
"""


@dataclass(frozen=True, slots=True)
class ChatSettings:
    model: str
    reasoning_effort: str
    plan_reasoning_effort: str
    model_verified: bool = False
    active_engine: str = "codex"
    claude_model: str = "sonnet"
    claude_effort: str = "high"
    claude_verified: bool = False
    tavily_enabled: bool = False

    def for_engine(self, engine: str) -> tuple[str, str, bool]:
        if engine == "claude":
            return self.claude_model, self.claude_effort, self.claude_verified
        return self.model, self.reasoning_effort, self.model_verified


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        if "thread_id" not in columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN thread_id TEXT")
        if "thread_task_number" not in columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN thread_task_number INTEGER")
        for column in ("model", "reasoning_effort", "plan_reasoning_effort"):
            if column not in columns:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} TEXT")
        chat_columns = {row["name"] for row in conn.execute("PRAGMA table_info(chats)")}
        for column in ("model", "reasoning_effort", "plan_reasoning_effort"):
            if column not in chat_columns:
                conn.execute(f"ALTER TABLE chats ADD COLUMN {column} TEXT")
        if "model_verified" not in chat_columns:
            conn.execute("ALTER TABLE chats ADD COLUMN model_verified INTEGER NOT NULL DEFAULT 0")
        for column, definition in (
            ("active_engine", "TEXT NOT NULL DEFAULT 'codex'"),
            ("claude_session_id", "TEXT"),
            ("claude_conversation_key", "TEXT"),
            ("claude_model", "TEXT"),
            ("claude_effort", "TEXT"),
            ("claude_verified", "INTEGER NOT NULL DEFAULT 0"),
            ("tavily_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("codex_cwd", "TEXT NOT NULL DEFAULT '/'"),
            ("claude_cwd", "TEXT NOT NULL DEFAULT '/'"),
        ):
            if column not in chat_columns:
                conn.execute(f"ALTER TABLE chats ADD COLUMN {column} {definition}")
        for column, definition in (
            ("engine", "TEXT NOT NULL DEFAULT 'codex'"),
            ("session_id", "TEXT"),
            ("effort", "TEXT"),
            ("endpoint_ref", "TEXT"),
            ("search_ref", "TEXT"),
            ("codex_prompt_ref", "TEXT"),
            ("cwd", "TEXT NOT NULL DEFAULT '/'"),
            ("output_dir", "TEXT"),
            ("files_collected", "INTEGER NOT NULL DEFAULT 0"),
            ("kind", "TEXT NOT NULL DEFAULT 'task'"),
        ):
            if column not in columns:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {definition}")
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_thread_number
               ON tasks(thread_id,thread_task_number) WHERE thread_id IS NOT NULL"""
        )
        conn.execute("INSERT OR IGNORE INTO codex_prompt_state(id) VALUES(1)")

    def workdir(self, chat_id: int, engine: str) -> str:
        if engine not in {'codex', 'claude'}:
            raise ValueError('invalid engine')
        with self.connect() as conn:
            row = conn.execute(f'SELECT {engine}_cwd FROM chats WHERE chat_id=?', (chat_id,)).fetchone()
        return str(row[0] or '/') if row else '/'

    def set_workdir(self, chat_id: int, engine: str, path: str) -> None:
        if engine not in {'codex', 'claude'}:
            raise ValueError('invalid engine')
        with self.connect() as conn:
            conn.execute(f'UPDATE chats SET {engine}_cwd=? WHERE chat_id=?', (path, chat_id))

    def set_task_directory(self, task_id: int, path: str) -> None:
        with self.connect() as conn:
            conn.execute('UPDATE tasks SET output_dir=? WHERE id=?', (path, task_id))

    def uncollected_outputs(self) -> list[dict]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT id,chat_id,output_dir FROM tasks WHERE status='DONE' AND output_dir IS NOT NULL AND files_collected=0 ORDER BY id")]

    def mark_files_collected(self, task_id: int) -> None:
        with self.connect() as conn:
            conn.execute('UPDATE tasks SET files_collected=1 WHERE id=?', (task_id,))

    def task(self, task_id: int, chat_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM tasks WHERE id=? AND chat_id=?', (task_id, chat_id)).fetchone()
        return dict(row) if row else None

    def set_batch_task(self, batch_id: int, task_id: int) -> None:
        with self.connect() as conn:
            conn.execute('UPDATE upload_batches SET task_id=? WHERE id=?', (task_id, batch_id))

    def task_history(self, chat_id: int, limit: int = 8) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute('''SELECT t.*, (SELECT COUNT(*) FROM artifacts a WHERE a.task_id=t.id) AS files,
                (SELECT COUNT(*) FROM artifacts a WHERE a.task_id=t.id AND a.status='DELIVERED') AS delivered
                FROM tasks t WHERE chat_id=? ORDER BY id DESC LIMIT ?''', (chat_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def add_artifact(self, task_id: int | None, chat_id: int, name: str, source: str,
                     stored: str, size: int, digest: str) -> int:
        with self.connect() as conn:
            cursor = conn.execute('''INSERT OR IGNORE INTO artifacts(task_id,chat_id,name,source_path,stored_path,size,sha256,created_at)
                VALUES(?,?,?,?,?,?,?,?)''', (task_id, chat_id, name, source, stored, size, digest, int(time.time())))
            if not cursor.rowcount:
                return int(conn.execute('SELECT id FROM artifacts WHERE task_id=? AND source_path=? AND sha256=?', (task_id, source, digest)).fetchone()[0])
        return int(cursor.lastrowid)

    def artifacts(self, chat_id: int, limit: int = 10) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute('SELECT * FROM artifacts WHERE chat_id=? ORDER BY id DESC LIMIT ?', (chat_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def artifact(self, artifact_id: int, chat_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM artifacts WHERE id=? AND chat_id=?', (artifact_id, chat_id)).fetchone()
        return dict(row) if row else None

    def claim_artifact(self, chat_id: int | None = None) -> dict | None:
        with self._lock, self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT * FROM artifacts WHERE status='READY'" + (' AND chat_id=?' if chat_id is not None else '')
                               + ' ORDER BY id LIMIT 1', (chat_id,) if chat_id is not None else ()).fetchone()
            if row:
                conn.execute("UPDATE artifacts SET status='SENDING',error=NULL WHERE id=?", (row['id'],))
            conn.commit()
        return dict(row) if row else None

    def artifact_state(self, artifact_id: int, status: str, error: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute('UPDATE artifacts SET status=?,error=?,delivered_at=? WHERE id=?',
                         (status, error, int(time.time()) if status == 'DELIVERED' else None, artifact_id))

    def resend_artifact(self, artifact_id: int, chat_id: int) -> bool:
        with self._lock, self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT status FROM artifacts WHERE id=? AND chat_id=?', (artifact_id, chat_id)).fetchone()
            if not row or row[0] in {'READY', 'SENDING'}:
                conn.rollback()
                return False
            if row[0] == 'DELIVERED':
                conn.execute('UPDATE artifact_parts SET message_id=NULL,file_id=NULL WHERE artifact_id=?', (artifact_id,))
            conn.execute("UPDATE artifacts SET status='READY',error=NULL,delivered_at=NULL WHERE id=?", (artifact_id,))
            conn.commit()
        return True

    def recover_deliveries(self) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE artifacts SET status='UNKNOWN',error='发送期间重启；请核对 Telegram 后手动重发' WHERE status='SENDING'")

    def delivery_busy(self) -> bool:
        with self.connect() as conn:
            return conn.execute("SELECT 1 FROM artifacts WHERE status='SENDING' LIMIT 1").fetchone() is not None

    def artifact_parts(self, artifact_id: int) -> list[dict]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM artifact_parts WHERE artifact_id=? ORDER BY number', (artifact_id,))]

    def save_artifact_part(self, artifact_id: int, number: int, path: str, digest: str, size: int) -> None:
        with self.connect() as conn:
            conn.execute('INSERT OR IGNORE INTO artifact_parts(artifact_id,number,path,sha256,size) VALUES(?,?,?,?,?)',
                         (artifact_id, number, path, digest, size))

    def save_artifact_parts(self, artifact_id: int, parts: list[tuple]) -> None:
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.executemany('INSERT OR IGNORE INTO artifact_parts(artifact_id,number,path,sha256,size) VALUES(?,?,?,?,?)',
                             [(artifact_id, *part) for part in parts])
            conn.commit()

    def acknowledge_part(self, artifact_id: int, number: int, message_id: int, file_id: str) -> None:
        with self.connect() as conn:
            conn.execute('UPDATE artifact_parts SET message_id=?,file_id=? WHERE artifact_id=? AND number=?',
                         (message_id, file_id, artifact_id, number))

    def begin_part_attempt(self, artifact_id: int, number: int) -> None:
        with self.connect() as conn:
            conn.execute('UPDATE artifact_parts SET attempts=attempts+1 WHERE artifact_id=? AND number=?', (artifact_id, number))

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def set_meta_batch(self, values: dict[str, str]) -> None:
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.executemany('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', list(values.items()))
            conn.commit()

    def ensure_chat(
        self,
        user_id: int,
        chat_id: int,
        defaults: ChatSettings | None = None,
    ) -> None:
        now = int(time.time())
        defaults = defaults or ChatSettings("default", "medium", "medium", False)
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO chats(
                    chat_id,user_id,model,reasoning_effort,plan_reasoning_effort,model_verified,
                    active_engine,claude_model,claude_effort,claude_verified,tavily_enabled,
                    created_at,last_active_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    user_id=excluded.user_id,
                    model_verified=CASE
                        WHEN chats.model IS NULL THEN excluded.model_verified
                        ELSE chats.model_verified
                    END,
                    model=COALESCE(chats.model,excluded.model),
                    reasoning_effort=COALESCE(chats.reasoning_effort,excluded.reasoning_effort),
                    plan_reasoning_effort=COALESCE(
                        chats.plan_reasoning_effort,excluded.plan_reasoning_effort
                    ),
                    claude_model=COALESCE(chats.claude_model,excluded.claude_model),
                    claude_effort=COALESCE(chats.claude_effort,excluded.claude_effort),
                    last_active_at=excluded.last_active_at""",
                (
                    chat_id,
                    user_id,
                    defaults.model,
                    defaults.reasoning_effort,
                    defaults.plan_reasoning_effort,
                    int(defaults.model_verified),
                    defaults.active_engine,
                    defaults.claude_model,
                    defaults.claude_effort,
                    int(defaults.claude_verified),
                    int(defaults.tavily_enabled),
                    now,
                    now,
                ),
            )

    def chat_settings(self, chat_id: int) -> ChatSettings:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT model,reasoning_effort,plan_reasoning_effort,model_verified,
                          active_engine,claude_model,claude_effort,claude_verified,tavily_enabled
                   FROM chats WHERE chat_id=?""",
                (chat_id,),
            ).fetchone()
        if not row:
            raise KeyError(f"chat {chat_id} is not registered")
        reasoning = row["reasoning_effort"] or "medium"
        return ChatSettings(
            model=row["model"] or "default",
            reasoning_effort=reasoning,
            plan_reasoning_effort=row["plan_reasoning_effort"] or reasoning,
            model_verified=bool(row["model_verified"]),
            active_engine=row["active_engine"] or "codex",
            claude_model=row["claude_model"] or "sonnet",
            claude_effort=row["claude_effort"] or "high",
            claude_verified=bool(row["claude_verified"]),
            tavily_enabled=bool(row["tavily_enabled"]),
        )

    def update_chat_settings(
        self,
        chat_id: int,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
        plan_reasoning_effort: str | None = None,
        model_verified: bool | None = None,
        active_engine: str | None = None,
        claude_model: str | None = None,
        claude_effort: str | None = None,
        claude_verified: bool | None = None,
        tavily_enabled: bool | None = None,
    ) -> ChatSettings:
        assignments: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("model", model),
            ("reasoning_effort", reasoning_effort),
            ("plan_reasoning_effort", plan_reasoning_effort),
            ("active_engine", active_engine),
            ("claude_model", claude_model),
            ("claude_effort", claude_effort),
        ):
            if value is not None:
                assignments.append(f"{column}=?")
                values.append(value)
        if model_verified is not None:
            assignments.append("model_verified=?")
            values.append(int(model_verified))
        if claude_verified is not None:
            assignments.append("claude_verified=?")
            values.append(int(claude_verified))
        if tavily_enabled is not None:
            assignments.append("tavily_enabled=?")
            values.append(int(tavily_enabled))
        if not assignments:
            return self.chat_settings(chat_id)
        assignments.append("last_active_at=?")
        values.extend((int(time.time()), chat_id))
        with self.connect() as conn:
            cursor = conn.execute(
                f"UPDATE chats SET {','.join(assignments)} WHERE chat_id=?", values
            )
            if cursor.rowcount != 1:
                raise KeyError(f"chat {chat_id} is not registered")
        return self.chat_settings(chat_id)

    def active_engine(self, chat_id: int) -> str:
        return self.chat_settings(chat_id).active_engine

    def set_active_engine(self, chat_id: int, engine: str) -> None:
        if engine not in {"codex", "claude"}:
            raise ValueError("unsupported engine")
        self.update_chat_settings(chat_id, active_engine=engine)

    def engine_session(self, chat_id: int, engine: str) -> str | None:
        column = "thread_id" if engine == "codex" else "claude_session_id"
        with self.connect() as conn:
            row = conn.execute(f"SELECT {column} FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
        return row[column] if row else None

    def set_engine_session(self, chat_id: int, engine: str, session_id: str | None) -> None:
        column = "thread_id" if engine == "codex" else "claude_session_id"
        with self.connect() as conn:
            conn.execute(
                f"UPDATE chats SET {column}=?,last_active_at=? WHERE chat_id=?",
                (session_id, int(time.time()), chat_id),
            )

    def claude_conversation_key(self, chat_id: int) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT claude_conversation_key FROM chats WHERE chat_id=?", (chat_id,)
            ).fetchone()
        return row["claude_conversation_key"] if row else None

    def set_claude_conversation_key(self, chat_id: int, key: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE chats SET claude_conversation_key=?,last_active_at=? WHERE chat_id=?",
                (key, int(time.time()), chat_id),
            )

    def select_claude_session(self, chat_id: int, session_id: str, baseline: int) -> None:
        now = int(time.time())
        with self._lock, self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('''UPDATE chats SET claude_session_id=?,claude_conversation_key=?,last_active_at=?
                            WHERE chat_id=?''', (session_id, session_id, now, chat_id))
            conn.execute('''INSERT INTO thread_counters(thread_id,chat_id,last_task_number,created_at,last_selected_at)
                            VALUES(?,?,?,?,?) ON CONFLICT(thread_id) DO UPDATE SET
                            last_selected_at=excluded.last_selected_at''',
                         (session_id, chat_id, max(0, baseline), now, now))

    def thread_id(self, chat_id: int) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT thread_id FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
            return row["thread_id"] if row else None

    def set_thread(self, chat_id: int, thread_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE chats SET thread_id=?,last_active_at=? WHERE chat_id=?",
                (thread_id, int(time.time()), chat_id),
            )

    def has_thread_counter(self, thread_id: str) -> bool:
        with self.connect() as conn:
            return conn.execute(
                "SELECT 1 FROM thread_counters WHERE thread_id=?", (thread_id,)
            ).fetchone() is not None

    def register_thread(self, chat_id: int, thread_id: str, baseline: int = 0) -> None:
        now = int(time.time())
        baseline = max(0, int(baseline))
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO thread_counters(
                    thread_id,chat_id,last_task_number,created_at,last_selected_at
                ) VALUES(?,?,?,?,?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    chat_id=excluded.chat_id,
                    last_task_number=MAX(thread_counters.last_task_number,excluded.last_task_number),
                    last_selected_at=excluded.last_selected_at""",
                (thread_id, chat_id, baseline, now, now),
            )

    def thread_task_number(self, thread_id: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT last_task_number FROM thread_counters WHERE thread_id=?", (thread_id,)
            ).fetchone()
        return int(row["last_task_number"]) if row else 0

    def enqueue(
        self,
        user_id: int,
        chat_id: int,
        thread_id: str,
        text: str,
        attachments: list[dict[str, str]],
        settings: ChatSettings | None = None,
        engine: str | None = None,
        session_id: str | None = None,
        endpoint_ref: str | None = None,
        search_ref: str | None = None,
        codex_prompt_ref: str | None = None,
        cwd: str | None = None,
        kind: str = 'task',
        status: str = 'QUEUED',
    ) -> tuple[int, int, int]:
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if settings is None:
                row = conn.execute(
                    """SELECT model,reasoning_effort,plan_reasoning_effort,model_verified,
                              active_engine,claude_model,claude_effort,claude_verified,tavily_enabled
                       FROM chats WHERE chat_id=?""",
                    (chat_id,),
                ).fetchone()
                if not row:
                    conn.rollback()
                    raise KeyError(f"chat {chat_id} is not registered")
                reasoning = row["reasoning_effort"] or "medium"
                settings = ChatSettings(
                    row["model"] or "default",
                    reasoning,
                    row["plan_reasoning_effort"] or reasoning,
                    bool(row["model_verified"]),
                    row["active_engine"] or "codex",
                    row["claude_model"] or "sonnet",
                    row["claude_effort"] or "high",
                    bool(row["claude_verified"]),
                    bool(row["tavily_enabled"]),
                )
            now = int(time.time())
            engine = engine or settings.active_engine
            if engine not in {"codex", "claude"}:
                raise ValueError("unsupported engine")
            task_effort = settings.claude_effort if engine == "claude" else settings.reasoning_effort
            conn.execute(
                """INSERT INTO thread_counters(
                    thread_id,chat_id,last_task_number,created_at,last_selected_at
                ) VALUES(?,?,0,?,?) ON CONFLICT(thread_id) DO NOTHING""",
                (thread_id, chat_id, now, now),
            )
            conn.execute(
                """UPDATE thread_counters SET last_task_number=last_task_number+1,
                   last_selected_at=? WHERE thread_id=?""",
                (now, thread_id),
            )
            thread_number = int(
                conn.execute(
                    "SELECT last_task_number FROM thread_counters WHERE thread_id=?", (thread_id,)
                ).fetchone()["last_task_number"]
            )
            cursor = conn.execute(
                """INSERT INTO tasks(
                    chat_id,user_id,thread_id,thread_task_number,model,reasoning_effort,
                    plan_reasoning_effort,engine,session_id,effort,endpoint_ref,search_ref,codex_prompt_ref,text,attachments_json,status,created_at,cwd,kind
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    chat_id,
                    user_id,
                    thread_id,
                    thread_number,
                    settings.claude_model if engine == "claude" else settings.model,
                    task_effort,
                    settings.plan_reasoning_effort,
                    engine,
                    session_id,
                    task_effort,
                    endpoint_ref,
                    search_ref,
                    codex_prompt_ref,
                    text,
                    json.dumps(attachments),
                    status,
                    now,
                    cwd or self.workdir(chat_id, engine),
                    kind,
                ),
            )
            task_id = int(cursor.lastrowid)
            position = int(
                conn.execute("SELECT count(*) AS n FROM tasks WHERE status='QUEUED' AND id<=?", (task_id,)).fetchone()["n"]
            )
            conn.commit()
        return task_id, position, thread_number

    def codex_prompt_state(self) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT enabled,updated_at,length,sha256 FROM codex_prompt_state WHERE id=1").fetchone()
        return dict(row) if row else {"enabled": 0, "updated_at": None, "length": 0, "sha256": ""}

    def codex_prompt_references(self) -> set[str]:
        with self.connect() as conn:
            return {row[0] for row in conn.execute('''SELECT DISTINCT codex_prompt_ref FROM tasks
                WHERE status IN ('QUEUED','RUNNING','STOPPING') AND codex_prompt_ref IS NOT NULL''') if row[0]}

    def set_codex_prompt_state(self, *, enabled: bool, updated_at: int, length: int, sha256: str) -> None:
        with self.connect() as conn:
            conn.execute("""INSERT INTO codex_prompt_state(id,enabled,updated_at,length,sha256)
                VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled,
                updated_at=excluded.updated_at,length=excluded.length,sha256=excluded.sha256""",
                         (int(enabled), updated_at, length, sha256))

    def claim_next(self) -> dict[str, Any] | None:
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM tasks WHERE status IN ('QUEUED','RECEIVING') ORDER BY id LIMIT 1").fetchone()
            if not row or row['status'] == 'RECEIVING':
                conn.rollback()
                return None
            conn.execute(
                "UPDATE tasks SET status='RUNNING',started_at=? WHERE id=?",
                (int(time.time()), row["id"]),
            )
            conn.commit()
            result = dict(row)
            result["status"] = "RUNNING"
            result["attachments"] = json.loads(result.pop("attachments_json"))
            return result

    def active(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE status IN ('RUNNING','STOPPING') ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def set_stopping(self, task_id: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE tasks SET status='STOPPING' WHERE id=? AND status='RUNNING'", (task_id,))

    def complete(self, task_id: int, response: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE tasks SET status='DONE',completed_at=?,final_response=? WHERE id=?""",
                (int(time.time()), response[-20000:], task_id),
            )

    def fail(self, task_id: int, error: str, *, interrupted: bool = False) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE tasks SET status=?,completed_at=?,error=? WHERE id=?",
                ("INTERRUPTED" if interrupted else "ERROR", int(time.time()), error[:2000], task_id),
            )

    def recover_interrupted(self) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """UPDATE tasks SET status='INTERRUPTED',completed_at=?,
                   error='Gateway or server restarted; task was not retried.'
                   WHERE status IN ('RUNNING','STOPPING')""",
                (int(time.time()),),
            )
            return cursor.rowcount

    def queue_count(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT count(*) AS n FROM tasks WHERE status='QUEUED'").fetchone()["n"])

    def queued_tasks(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT id,thread_id,thread_task_number,text,created_at FROM tasks
                   WHERE status='QUEUED' ORDER BY id LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_queue(self) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET status='CANCELLED',completed_at=? WHERE status='QUEUED'",
                (int(time.time()),),
            )
            return cursor.rowcount

    def add_event(self, kind: str, summary: str, task_id: int | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO events(task_id,kind,summary,created_at) VALUES(?,?,?,?)",
                (task_id, kind[:40], summary[:1000], int(time.time())),
            )

    def recent_events(self, limit: int = 12) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT task_id,kind,summary,created_at FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(row) for row in reversed(rows)]

    def recent_tasks(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT id,thread_id,thread_task_number,text,status,created_at,started_at,completed_at,error
                   FROM tasks ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def last_task(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT id,thread_id,thread_task_number,text,status,created_at,started_at,completed_at,error
                   FROM tasks ORDER BY id DESC LIMIT 1"""
            ).fetchone()
        return dict(row) if row else None

    def healthy(self) -> bool:
        try:
            with self.connect() as conn:
                return conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        except sqlite3.Error:
            return False

    def get_meta(self, key: str, default: str = "") -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
