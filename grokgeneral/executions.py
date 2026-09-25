from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any

from .errors import NotFoundError, ValidationError
from .storage import StateStore, atomic_write_json, canonical_json, redact
from .timeutil import isoformat, utc_now


class ExecutionRegistry:
    def __init__(self, state: StateStore) -> None:
        self.state = state
        self.state.initialize()
        self.log_dir = self.state.state_dir / "execution-logs"
        self.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.log_dir.chmod(0o700)
        except OSError:
            pass

    @staticmethod
    def _context_hash(context: Any) -> str:
        return hashlib.sha256(canonical_json(redact(context or {})).encode("utf-8")).hexdigest()

    def _public(self, record: dict[str, Any]) -> dict[str, Any]:
        data = record.get("data") if isinstance(record.get("data"), dict) else {}
        result = {key: value for key, value in record.items() if key != "data"}
        result.update(data)
        return redact(result)

    def _save(self, record: dict[str, Any], action: str) -> dict[str, Any]:
        with self.state.transaction() as connection:
            self.state.put_record(connection, "executions", record)
        self.state.audit(action, "execution", record.get("id"), {"status": record.get("status")})
        return self._public(record)

    def start(self, data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ValidationError("execution data must be an object")
        execution_id = f"execution-{uuid.uuid4().hex}"
        now = isoformat(utc_now())
        record = {
            "id": execution_id,
            "task_id": data.get("task_id") or data.get("task"),
            "project_id": data.get("project_id") or data.get("project"),
            "resource_id": data.get("resource_id") or data.get("resource"),
            "executor": data.get("executor"),
            "provider": data.get("provider"),
            "model": data.get("model"),
            "status": "running",
            "started_at": data.get("started_at") or now,
            "ended_at": None,
            "exit_code": None,
            "data": {
                "context_hash": data.get("context_hash") or self._context_hash(data.get("context")),
                "work_key": data.get("work_key"),
                "summary": "",
                "artifacts": [],
                "validation": {"status": "pending"},
                "usage": None,
                "error": None,
                "snapshot": data.get("snapshot"),
                "raw_log_path": None,
            },
            "created_at": now,
            "updated_at": now,
        }
        return self._save(record, "execution.started")

    def get(self, execution_id: str) -> dict[str, Any]:
        record = self.state.get_record("executions", str(execution_id))
        if record is None:
            raise NotFoundError(f"execution not found: {execution_id}")
        return self._public(record)

    def _write_raw(self, execution_id: str, events: Any) -> str | None:
        if events is None:
            return None
        path = (self.log_dir / f"{execution_id}.json").resolve()
        try:
            path.relative_to(self.log_dir.resolve())
        except ValueError as exc:
            raise ValidationError("execution log path escaped state directory") from exc
        atomic_write_json(path, {"execution_id": execution_id, "events": redact(events)})
        return str(path)

    def complete(self, execution_id: str, result: dict[str, Any]) -> dict[str, Any]:
        current = self.get(execution_id)
        if not isinstance(result, dict):
            raise ValidationError("execution result must be an object")
        raw_log_path = self._write_raw(execution_id, result.get("raw_events"))
        status = str(result.get("status") or ("completed" if result.get("exit_code") == 0 else "failed"))
        if status not in {"completed", "failed", "timeout", "blocked"}:
            raise ValidationError(f"invalid terminal execution status: {status}")
        record = self.state.get_record("executions", execution_id) or {}
        data = dict(record.get("data") or {})
        data.update({
            "summary": redact(result.get("summary", "")),
            "artifacts": redact(result.get("artifacts", [])),
            "validation": redact(result.get("validation") or {"status": "pending"}),
            "usage": redact(result.get("usage")),
            "snapshot": redact(result.get("snapshot") or data.get("snapshot")),
            "error": redact(result.get("error")),
            "raw_log_path": raw_log_path or data.get("raw_log_path"),
        })
        values = {
            "id": execution_id,
            "task_id": current.get("task_id"),
            "project_id": current.get("project_id"),
            "resource_id": current.get("resource_id"),
            "executor": current.get("executor"),
            "provider": current.get("provider"),
            "model": current.get("model"),
            "status": status,
            "started_at": current.get("started_at"),
            "ended_at": isoformat(utc_now()),
            "exit_code": result.get("exit_code"),
            "data": data,
            "created_at": current.get("created_at"),
            "updated_at": isoformat(utc_now()),
        }
        return self._save(values, "execution.completed")

    def fail(self, execution_id: str, error: str) -> dict[str, Any]:
        return self.complete(execution_id, {"status": "failed", "error": str(error), "exit_code": None})

    def list(self, task_id: str | None = None, project: str | None = None) -> list[dict[str, Any]]:
        clauses = []
        values = []
        if task_id is not None:
            clauses.append("task_id=?")
            values.append(task_id)
        if project is not None:
            clauses.append("project_id=?")
            values.append(project)
        records = self.state.list_records("executions", " AND ".join(clauses) if clauses else "", tuple(values))
        return [self._public(record) for record in records]
