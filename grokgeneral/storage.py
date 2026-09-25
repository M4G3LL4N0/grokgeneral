from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import StateError, ValidationError
from .timeutil import isoformat, utc_now

_SECRET_KEY = re.compile(r"(?:^|_)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|token|secret|password|authorization|cookie|credential|private[_-]?key)(?:$|_)", re.I)
_SECRET_ASSIGNMENT = re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|token|secret|password|authorization|cookie)\s*([:=])\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)")
_TABLES = {
    "projects", "roots", "resources", "tasks", "policies", "events", "subscriptions",
    "usage", "cache_entries", "opportunities", "executions", "approvals", "scheduler_runs", "task_claims", "audit_log",
    "project_health", "status_digest",
}
_COMMON_COLUMNS = {
    "id", "name", "path", "repository", "status", "priority", "project_id",
    "resource_id", "task_id", "kind", "event_type", "source", "expires_at",
    "health", "last_activity", "created_at", "updated_at", "score", "data", "key", "value", "payload", "metadata", "action", "enabled", "delivered_at", "executor", "provider", "model", "ended_at", "exit_code", "started_at",
}


def _json_default(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_json_default)


def is_secret_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
    if not normalized:
        return False
    if "context" in normalized and "token" in normalized:
        return False
    if normalized.startswith("token_") and normalized.endswith(("_count", "_limit", "_usage", "_available", "_remaining")):
        return False
    return bool(_SECRET_KEY.search(normalized))


def redact(value: Any, key: str | None = None) -> Any:
    if key and is_secret_key(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): redact(item, str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list):
        result = []
        redact_next = False
        for item in value:
            if redact_next:
                result.append("[REDACTED]")
                redact_next = False
                continue
            if isinstance(item, str) and item.startswith("-") and is_secret_key(item.lstrip("-").split("=", 1)[0]):
                if "=" in item:
                    result.append(item.split("=", 1)[0] + "=[REDACTED]")
                else:
                    result.append(item)
                    redact_next = True
                continue
            result.append(redact(item))
        return result
    if isinstance(value, tuple):
        return redact(list(value))
    if isinstance(value, str):
        return _SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]", value)
    return value


def atomic_write_json(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = canonical_json(redact(value)) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


class StateStore:
    def __init__(self, state_dir: Path | str | None = None) -> None:
        configured = state_dir or os.environ.get("GG_STATE_DIR") or Path.home() / ".grokgeneral"
        self.state_dir = Path(configured).expanduser().resolve()
        self.db_path = self.state_dir / "state.db"
        self.policy_path = self.state_dir / "policies.json"
        self.cache_dir = self.state_dir / "cache"
        self._initialized = False

    def initialize(self) -> StateStore:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.state_dir.chmod(0o700)
        except OSError:
            pass
        self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.cache_dir.chmod(0o700)
        except OSError:
            pass
        connection = self.connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, name TEXT, path TEXT, repository TEXT,
                    status TEXT, priority INTEGER, data TEXT NOT NULL,
                    created_at TEXT, updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS roots (
                    id TEXT PRIMARY KEY, name TEXT, path TEXT, enabled INTEGER,
                    data TEXT NOT NULL, created_at TEXT, updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS resources (
                    id TEXT PRIMARY KEY, name TEXT, status TEXT, expires_at TEXT,
                    health TEXT, data TEXT NOT NULL, created_at TEXT, updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, name TEXT, project_id TEXT, status TEXT,
                    priority INTEGER, data TEXT NOT NULL, created_at TEXT, updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS policies (
                    id TEXT PRIMARY KEY, name TEXT, data TEXT NOT NULL,
                    created_at TEXT, updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, event_type TEXT, payload TEXT NOT NULL,
                    created_at TEXT, delivered_at TEXT
                );
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id TEXT PRIMARY KEY, event_type TEXT, name TEXT, action TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1, data TEXT NOT NULL,
                    created_at TEXT, updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS usage (
                    id TEXT PRIMARY KEY, project_id TEXT, resource_id TEXT, task_id TEXT,
                    source TEXT, data TEXT NOT NULL, created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS cache_entries (
                    key TEXT PRIMARY KEY, kind TEXT, value TEXT NOT NULL,
                    expires_at TEXT, metadata TEXT NOT NULL, created_at TEXT, updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS opportunities (
                    id TEXT PRIMARY KEY, resource_id TEXT, task_id TEXT, score REAL,
                    data TEXT NOT NULL, created_at TEXT, updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS executions (
                    id TEXT PRIMARY KEY, task_id TEXT, project_id TEXT, resource_id TEXT,
                    executor TEXT, provider TEXT, model TEXT, status TEXT,
                    started_at TEXT, ended_at TEXT, exit_code INTEGER, data TEXT NOT NULL,
                    created_at TEXT, updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY, task_id TEXT, status TEXT,
                    data TEXT NOT NULL, created_at TEXT, updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS scheduler_runs (
                    id TEXT PRIMARY KEY, status TEXT, started_at TEXT, ended_at TEXT,
                    data TEXT NOT NULL, created_at TEXT, updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS task_claims (
                    id TEXT PRIMARY KEY, task_id TEXT, project_id TEXT, status TEXT,
                    data TEXT NOT NULL, created_at TEXT, updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT, entity_type TEXT,
                    entity_id TEXT, data TEXT NOT NULL, created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS project_health (
                    id TEXT PRIMARY KEY, project_id TEXT, status TEXT, health TEXT, last_activity TEXT,
                    data TEXT NOT NULL, created_at TEXT, updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS status_digest (
                    id TEXT PRIMARY KEY, kind TEXT, data TEXT NOT NULL, created_at TEXT, updated_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_projects_path ON projects(path);
                CREATE INDEX IF NOT EXISTS idx_resources_expiry ON resources(expires_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
                CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
                CREATE INDEX IF NOT EXISTS idx_usage_resource ON usage(resource_id);
                CREATE INDEX IF NOT EXISTS idx_executions_task ON executions(task_id);
                 CREATE INDEX IF NOT EXISTS idx_executions_project ON executions(project_id);
                 CREATE INDEX IF NOT EXISTS idx_approvals_task ON approvals(task_id);
                 CREATE INDEX IF NOT EXISTS idx_task_claims_task ON task_claims(task_id);
                 CREATE INDEX IF NOT EXISTS idx_task_claims_status ON task_claims(status);

                """
            )
            connection.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', '1')")
            connection.commit()
        finally:
            connection.close()
        self._initialized = True
        return self

    def connect(self) -> sqlite3.Connection:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA synchronous=NORMAL")
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _ensure_table(self, table: str) -> None:
        if table not in _TABLES:
            raise ValidationError(f"unsupported state table: {table}")

    @staticmethod
    def _record_data(record: dict[str, Any]) -> dict[str, Any]:
        data = dict(record.get("data") or {})
        for key, value in record.items():
            if key not in _COMMON_COLUMNS and key != "data":
                data[key] = value
        return redact(data)

    def put_record(self, connection: sqlite3.Connection, table: str, record: dict[str, Any]) -> None:
        self._ensure_table(table)
        if not isinstance(record, dict) or not record.get("id"):
            raise ValidationError(f"{table} record requires an id")
        record_id = str(record["id"])
        now = isoformat(utc_now())
        data = canonical_json(self._record_data(record))
        values: dict[str, Any] = {
            "id": record_id,
            "name": record.get("name", record_id),
            "path": record.get("path"),
            "repository": record.get("repository"),
            "status": record.get("status"),
            "priority": record.get("priority"),
            "project_id": record.get("project") or record.get("project_id"),
            "resource_id": record.get("resource") or record.get("resource_id"),
            "task_id": record.get("task_id"),
            "executor": record.get("executor"),
            "provider": record.get("provider"),
            "model": record.get("model"),
            "ended_at": record.get("ended_at"),
            "started_at": record.get("started_at"),
            "exit_code": record.get("exit_code"),
            "kind": record.get("kind"),
            "event_type": record.get("event_type") or record.get("type"),
            "source": record.get("source"),
            "expires_at": record.get("expires_at"),
            "health": record.get("health"),
            "last_activity": record.get("last_activity"),
            "created_at": record.get("created_at") or now,
            "updated_at": record.get("updated_at") or now,
            "score": record.get("score"),
            "attempt": record.get("attempt"),
            "project_key": record.get("project_key"),
            "scheduler_run_id": record.get("scheduler_run_id"),
            "lease_expires_at": record.get("lease_expires_at"),
            "mutation": record.get("mutation"),
            "completed_at": record.get("completed_at"),
            "actor": record.get("actor"),
            "required_actions": record.get("required_actions"),
            "payload_hash": record.get("payload_hash"),
            "work_key": record.get("work_key"),
            "duration_seconds": record.get("duration_seconds"),
            "unit": record.get("unit"),
            "status_reason": record.get("status_reason"),
            "selection": record.get("selection"),
            "completed_count": record.get("completed_count"),
            "failed_count": record.get("failed_count"),
            "skipped_count": record.get("skipped_count"),
            "data": data,
        }
        if table == "projects":
            columns = ["id", "name", "path", "repository", "status", "priority", "data", "created_at", "updated_at"]
        elif table == "roots":
            values["enabled"] = int(record.get("enabled", True))
            columns = ["id", "name", "path", "enabled", "data", "created_at", "updated_at"]
        elif table == "resources":
            columns = ["id", "name", "status", "expires_at", "health", "data", "created_at", "updated_at"]
        elif table == "tasks":
            columns = ["id", "name", "project_id", "status", "priority", "data", "created_at", "updated_at"]
        elif table == "opportunities":
            columns = ["id", "resource_id", "task_id", "score", "data", "created_at", "updated_at"]
        elif table == "executions":
            columns = ["id", "task_id", "project_id", "resource_id", "executor", "provider", "model", "status", "started_at", "ended_at", "exit_code", "data", "created_at", "updated_at"]
        elif table == "approvals":
            columns = ["id", "task_id", "status", "data", "created_at", "updated_at"]
        elif table == "scheduler_runs":
            columns = ["id", "status", "started_at", "ended_at", "data", "created_at", "updated_at"]
        elif table == "task_claims":
            columns = ["id", "task_id", "project_id", "status", "data", "created_at", "updated_at"]
        elif table == "cache_entries":
            values["key"] = record_id
            values["value"] = data
            values["metadata"] = canonical_json(redact(record.get("metadata") or {}))
            columns = ["key", "kind", "value", "expires_at", "metadata", "created_at", "updated_at"]
        elif table == "events":
            values["payload"] = data
            values["delivered_at"] = record.get("delivered_at")
            columns = ["id", "event_type", "payload", "created_at", "delivered_at"]
        elif table == "usage":
            columns = ["id", "project_id", "resource_id", "task_id", "source", "data", "created_at"]
        elif table == "audit_log":
            values["action"] = record.get("action", "audit")
            values["entity_type"] = record.get("entity_type")
            values["entity_id"] = record.get("entity_id")
            columns = ["id", "action", "entity_type", "entity_id", "data", "created_at"]
        elif table == "subscriptions":
            values["enabled"] = int(record.get("enabled", True))
            values["action"] = record.get("action", "record")
            columns = ["id", "event_type", "name", "action", "enabled", "data", "created_at", "updated_at"]
        elif table == "policies":
            columns = ["id", "name", "data", "created_at", "updated_at"]
        elif table == "project_health":
            columns = ["id", "project_id", "status", "health", "last_activity", "data", "created_at", "updated_at"]
        elif table == "status_digest":
            columns = ["id", "kind", "data", "created_at", "updated_at"]
        placeholders = ",".join("?" for _ in columns)
        updates = ",".join(f"{column}=excluded.{column}" for column in columns if column not in {"id", "key"})
        if table == "cache_entries":
            sql = f"INSERT INTO cache_entries ({','.join(columns)}) VALUES ({placeholders}) ON CONFLICT(key) DO UPDATE SET kind=excluded.kind,value=excluded.value,expires_at=excluded.expires_at,metadata=excluded.metadata,updated_at=excluded.updated_at"
        else:
            sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders}) ON CONFLICT(id) DO UPDATE SET {updates}"
        connection.execute(sql, [values.get(column) for column in columns])

    def get_record(self, table: str, record_id: str) -> dict[str, Any] | None:
        self._ensure_table(table)
        if not self._initialized:
            self.initialize()
        connection = self.connect()
        try:
            if table == "cache_entries":
                row = connection.execute("SELECT * FROM cache_entries WHERE key=?", (record_id,)).fetchone()
            else:
                row = connection.execute(f"SELECT * FROM {table} WHERE id=?", (record_id,)).fetchone()
            return self._row_to_record(table, row) if row else None
        finally:
            connection.close()

    def list_records(self, table: str, where: str = "", parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        self._ensure_table(table)
        if not self._initialized:
            self.initialize()
        connection = self.connect()
        try:
            query = f"SELECT * FROM {table}"
            if where:
                query += f" WHERE {where}"
            if table == "audit_log":
                query += " ORDER BY id ASC"
            elif table == "cache_entries":
                query += " ORDER BY created_at ASC, key ASC"
            else:
                query += " ORDER BY created_at ASC, id ASC"
            return [self._row_to_record(table, row) for row in connection.execute(query, parameters).fetchall()]
        finally:
            connection.close()

    def delete_record(self, table: str, record_id: str) -> None:
        self._ensure_table(table)
        with self.transaction() as connection:
            connection.execute(f"DELETE FROM {table} WHERE id=?", (record_id,))

    def audit(self, action: str, entity_type: str, entity_id: str | None = None, data: dict[str, Any] | None = None) -> None:
        now = isoformat(utc_now())
        record = {
            "id": f"audit-{now}-{action}",
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "data": data or {},
            "created_at": now,
        }
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO audit_log(action, entity_type, entity_id, data, created_at) VALUES(?,?,?,?,?)",
                (action, entity_type, entity_id, canonical_json(redact(data or {})), now),
            )

    @staticmethod
    def _row_to_record(table: str, row: sqlite3.Row) -> dict[str, Any]:
        values = dict(row)
        raw_data = values.pop("data", None)
        if raw_data is None and table == "events":
            raw_data = values.pop("payload", None)
        if raw_data is None and table == "cache_entries":
            raw_data = values.pop("value", None)
        try:
            data = json.loads(raw_data) if raw_data else {}
        except json.JSONDecodeError as exc:
            raise StateError(f"malformed {table} record {values.get('id')}") from exc
        result = dict(data)
        result["data"] = data
        for key, value in values.items():
            if key not in {"payload", "value", "metadata"}:
                result[key] = value
        if table == "events":
            result["payload"] = data
            result["event_type"] = values.get("event_type")
        if table == "cache_entries":
            result["value"] = data
            result["metadata"] = json.loads(values.get("metadata") or "{}")
            result["key"] = values.get("key")
        return result

    def export_snapshot(self) -> dict[str, Any]:
        if not self._initialized:
            self.initialize()
        snapshot: dict[str, Any] = {"schema_version": 1, "tables": {}}
        for table in sorted(_TABLES):
            snapshot["tables"][table] = self.list_records(table)
        if self.policy_path.exists():
            snapshot["policies"] = json.loads(self.policy_path.read_text(encoding="utf-8"))
        return redact(snapshot)

    def import_snapshot(self, snapshot: dict[str, Any]) -> None:
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("tables", {}), dict):
            raise ValidationError("invalid state snapshot")
        if snapshot.get("schema_version") not in (None, 1):
            raise ValidationError("unsupported state snapshot schema")
        with self.transaction() as connection:
            for table in _TABLES:
                connection.execute(f"DELETE FROM {table}")
            for table, records in snapshot.get("tables", {}).items():
                self._ensure_table(table)
                if not isinstance(records, list):
                    raise ValidationError(f"invalid records for {table}")
                for record in records:
                    if table == "cache_entries" and isinstance(record, dict) and not record.get("id") and record.get("key"):
                        record = {**record, "id": record["key"]}
                    self.put_record(connection, table, record)
            if snapshot.get("policies") is not None:
                atomic_write_json(self.policy_path, snapshot["policies"])
        self.audit("import", "state", data={"tables": sorted(snapshot.get("tables", {}))})

    def get_meta(self, key: str) -> str | None:
        if not self._initialized:
            self.initialize()
        connection = self.connect()
        try:
            row = connection.execute("SELECT value FROM meta WHERE key=?", (str(key),)).fetchone()
            return str(row[0]) if row else None
        finally:
            connection.close()

    def set_meta(self, key: str, value: str) -> None:
        with self.transaction() as connection:
            connection.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (str(key), str(value)))

    def health(self) -> dict[str, Any]:
        if not self._initialized:
            self.initialize()
        connection = self.connect()
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            version = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            return {
                "ok": integrity == "ok",
                "integrity": integrity,
                "schema_version": int(version[0]) if version else None,
                "path": str(self.db_path),
                "writable": os.access(self.state_dir, os.W_OK),
            }
        finally:
            connection.close()

    def close(self) -> None:
        return None
