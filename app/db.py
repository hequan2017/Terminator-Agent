"""SQLite 存储层。单用户起步，但表结构保留 user 维度以便后续扩展多用户。"""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'admin',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS hosts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    hostname TEXT NOT NULL,
    port INTEGER NOT NULL DEFAULT 22,
    username TEXT NOT NULL,
    auth_type TEXT NOT NULL DEFAULT 'key',
    private_key_enc TEXT,
    passphrase_enc TEXT,
    password_enc TEXT,
    tags TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS apps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    host_ids TEXT NOT NULL DEFAULT '[]',
    deploy_script TEXT NOT NULL,
    rollback_script TEXT NOT NULL DEFAULT '',
    health_check TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS deployments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id INTEGER NOT NULL,
    version TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT 'deploy',
    host_ids TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    log TEXT NOT NULL DEFAULT '',
    triggered_by TEXT NOT NULL DEFAULT 'user',
    created_at REAL NOT NULL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL DEFAULT '新对话',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    actor TEXT NOT NULL,
    source TEXT NOT NULL,
    host TEXT NOT NULL,
    command TEXT NOT NULL,
    risk TEXT NOT NULL,
    decision TEXT NOT NULL,
    exit_code INTEGER,
    output TEXT NOT NULL DEFAULT ''
);
"""


class Database:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    @contextmanager
    def _tx(self):
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def execute(self, sql: str, params: tuple = ()) -> int:
        with self._tx() as c:
            cur = c.execute(sql, params)
            return cur.lastrowid

    def one(self, sql: str, params: tuple = ()) -> dict | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # ---------- users ----------
    def get_user(self, username: str) -> dict | None:
        return self.one("SELECT * FROM users WHERE username=?", (username,))

    def upsert_user(self, username: str, password_hash: str, role: str = "admin") -> None:
        self.execute(
            "INSERT INTO users(username,password_hash,role,created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash",
            (username, password_hash, role, time.time()),
        )

    # ---------- hosts ----------
    def list_hosts(self) -> list[dict]:
        return self.all("SELECT * FROM hosts ORDER BY name")

    def get_host(self, host_id: int) -> dict | None:
        return self.one("SELECT * FROM hosts WHERE id=?", (host_id,))

    def get_host_by_name(self, name: str) -> dict | None:
        return self.one("SELECT * FROM hosts WHERE name=?", (name,))

    def create_host(self, data: dict[str, Any]) -> int:
        return self.execute(
            "INSERT INTO hosts(name,hostname,port,username,auth_type,private_key_enc,passphrase_enc,"
            "password_enc,tags,description,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                data["name"], data["hostname"], data.get("port", 22), data["username"],
                data.get("auth_type", "key"), data.get("private_key_enc"), data.get("passphrase_enc"),
                data.get("password_enc"), data.get("tags", ""), data.get("description", ""), time.time(),
            ),
        )

    def update_host(self, host_id: int, data: dict[str, Any]) -> None:
        allowed = ["name", "hostname", "port", "username", "auth_type", "private_key_enc",
                   "passphrase_enc", "password_enc", "tags", "description"]
        fields = [k for k in allowed if k in data]
        if not fields:
            return
        sets = ",".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE hosts SET {sets} WHERE id=?", tuple(data[k] for k in fields) + (host_id,))

    def delete_host(self, host_id: int) -> None:
        self.execute("DELETE FROM hosts WHERE id=?", (host_id,))

    # ---------- apps ----------
    @staticmethod
    def _json_hosts(row: dict | None) -> dict | None:
        if row:
            row["host_ids"] = json.loads(row["host_ids"])
        return row

    def list_apps(self) -> list[dict]:
        return [self._json_hosts(r) for r in self.all("SELECT * FROM apps ORDER BY name")]

    def get_app(self, app_id: int) -> dict | None:
        return self._json_hosts(self.one("SELECT * FROM apps WHERE id=?", (app_id,)))

    def get_app_by_name(self, name: str) -> dict | None:
        return self._json_hosts(self.one("SELECT * FROM apps WHERE name=?", (name,)))

    def create_app(self, data: dict[str, Any]) -> int:
        return self.execute(
            "INSERT INTO apps(name,description,host_ids,deploy_script,rollback_script,health_check,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                data["name"], data.get("description", ""), json.dumps(data.get("host_ids", [])),
                data["deploy_script"], data.get("rollback_script", ""), data.get("health_check", ""), time.time(),
            ),
        )

    def update_app(self, app_id: int, data: dict[str, Any]) -> None:
        allowed = ["name", "description", "host_ids", "deploy_script", "rollback_script", "health_check"]
        fields = [k for k in allowed if k in data]
        if not fields:
            return
        values = [json.dumps(data[k]) if k == "host_ids" else data[k] for k in fields]
        sets = ",".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE apps SET {sets} WHERE id=?", tuple(values) + (app_id,))

    def delete_app(self, app_id: int) -> None:
        self.execute("DELETE FROM apps WHERE id=?", (app_id,))

    # ---------- deployments ----------
    def create_deployment(self, app_id: int, version: str, host_ids: list[int], action: str,
                          triggered_by: str) -> int:
        return self.execute(
            "INSERT INTO deployments(app_id,version,action,host_ids,status,triggered_by,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (app_id, version, action, json.dumps(host_ids), "pending", triggered_by, time.time()),
        )

    def update_deployment(self, dep_id: int, status: str | None = None, append_log: str | None = None,
                          finished: bool = False) -> None:
        with self._tx() as c:
            if status:
                c.execute("UPDATE deployments SET status=? WHERE id=?", (status, dep_id))
            if append_log:
                c.execute("UPDATE deployments SET log=log||? WHERE id=?", (append_log, dep_id))
            if finished:
                c.execute("UPDATE deployments SET finished_at=? WHERE id=?", (time.time(), dep_id))

    def get_deployment(self, dep_id: int) -> dict | None:
        return self._json_hosts(self.one(
            "SELECT d.*, a.name AS app_name FROM deployments d LEFT JOIN apps a ON a.id=d.app_id WHERE d.id=?",
            (dep_id,),
        ))

    def list_deployments(self, app_id: int | None = None, limit: int = 50) -> list[dict]:
        sql = ("SELECT d.id,d.app_id,d.version,d.action,d.host_ids,d.status,d.triggered_by,d.created_at,"
               "d.finished_at,a.name AS app_name FROM deployments d LEFT JOIN apps a ON a.id=d.app_id")
        params: tuple = ()
        if app_id is not None:
            sql += " WHERE d.app_id=?"
            params = (app_id,)
        sql += " ORDER BY d.id DESC LIMIT ?"
        return [self._json_hosts(r) for r in self.all(sql, params + (limit,))]

    def last_successful_version(self, app_id: int, before_id: int | None = None) -> str | None:
        sql = "SELECT version FROM deployments WHERE app_id=? AND action='deploy' AND status='success'"
        params: tuple = (app_id,)
        if before_id is not None:
            sql += " AND id<?"
            params += (before_id,)
        row = self.one(sql + " ORDER BY id DESC LIMIT 1", params)
        return row["version"] if row else None

    # ---------- conversations ----------
    def create_conversation(self, title: str = "新对话") -> int:
        now = time.time()
        return self.execute("INSERT INTO conversations(title,created_at,updated_at) VALUES(?,?,?)",
                            (title, now, now))

    def list_conversations(self) -> list[dict]:
        return self.all("SELECT * FROM conversations ORDER BY updated_at DESC")

    def get_conversation(self, conv_id: int) -> dict | None:
        return self.one("SELECT * FROM conversations WHERE id=?", (conv_id,))

    def rename_conversation(self, conv_id: int, title: str) -> None:
        self.execute("UPDATE conversations SET title=? WHERE id=?", (title, conv_id))

    def delete_conversation(self, conv_id: int) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM messages WHERE conversation_id=?", (conv_id,))
            c.execute("DELETE FROM conversations WHERE id=?", (conv_id,))

    def add_message(self, conv_id: int, message: dict) -> int:
        now = time.time()
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO messages(conversation_id,payload,created_at) VALUES(?,?,?)",
                (conv_id, json.dumps(message, ensure_ascii=False), now),
            )
            c.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
            return cur.lastrowid

    def get_messages(self, conv_id: int) -> list[dict]:
        rows = self.all("SELECT payload FROM messages WHERE conversation_id=? ORDER BY id", (conv_id,))
        return [json.loads(r["payload"]) for r in rows]

    # ---------- audit ----------
    def add_audit(self, actor: str, source: str, host: str, command: str, risk: str, decision: str,
                  exit_code: int | None = None, output: str = "") -> int:
        return self.execute(
            "INSERT INTO audit_logs(ts,actor,source,host,command,risk,decision,exit_code,output) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (time.time(), actor, source, host, command, risk, decision, exit_code, output[:4000]),
        )

    def list_audit(self, limit: int = 200) -> list[dict]:
        return self.all("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,))
