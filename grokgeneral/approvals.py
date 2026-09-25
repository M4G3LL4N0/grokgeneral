from __future__ import annotations

import hashlib
import uuid
from typing import Any

from .errors import ApprovalError, NotFoundError, ValidationError
from .events import EventBus
from .models import Task
from .storage import StateStore, canonical_json, redact
from .timeutil import isoformat, parse_time, utc_now

_ACTIONS = {
    "modify",
    "commit",
    "push",
    "deploy",
    "spend",
    "external",
    "network",
    "destructive",
    "post",
    "privacy",
    "validate",
    "dirty-repo",
    "local-test",
}


def _task(value: Task | dict[str, Any]) -> Task:
    if isinstance(value, Task):
        return value
    if isinstance(value, dict):
        return Task.from_dict(value)
    raise ValidationError("task must be an object")


def _action(value: Any) -> str:
    action = str(value).strip().lower().replace("_", "-")
    if action not in _ACTIONS:
        raise ValidationError(f"unsupported approval action: {action}")
    return action


def _actions(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    else:
        try:
            values = list(value or [])
        except TypeError as exc:
            raise ValidationError("approval actions must be an iterable") from exc
    result = sorted({_action(item) for item in values if str(item).strip()})
    if not result:
        raise ValidationError("approval actions must not be empty")
    return result


def _text(value: Any, field: str, limit: int = 512) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string")
    result = value.strip()
    if len(result) > limit:
        raise ValidationError(f"{field} must be at most {limit} characters")
    return result


def _payload_hash(task: Task, attempt: int, actions: list[str], payload_hash: str | None) -> str:
    if payload_hash is not None:
        return _text(payload_hash, "payload_hash", 256) or ""
    material = {"task_id": task.id, "attempt": attempt, "actions": actions}
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


class ApprovalRegistry:
    def __init__(self, state: StateStore, events: EventBus | None = None) -> None:
        self.state = state
        self.events = events
        self.state.initialize()

    def _public(self, record: dict[str, Any]) -> dict[str, Any]:
        data = record.get("data") if isinstance(record.get("data"), dict) else {}
        return redact({key: value for key, value in record.items() if key != "data"} | data)

    def _save(self, record: dict[str, Any], action: str, event_type: str | None = None) -> dict[str, Any]:
        with self.state.transaction() as connection:
            self.state.put_record(connection, "approvals", record)
        self.state.audit(action, "approval", record.get("id"), {"status": record.get("status")})
        if event_type is not None and self.events is not None:
            self.events.emit(event_type, {"approval_id": record.get("id"), "task_id": record.get("task_id"), "status": record.get("status")})
        return self._public(record)

    def request(
        self,
        task: Task | dict[str, Any],
        attempt: int,
        actions: Any,
        payload_hash: str | None = None,
        project_id: str | None = None,
        work_key: str | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        normalized_task = _task(task)
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValidationError("attempt must be a positive integer")
        normalized_actions = _actions(actions)
        if expires_at is not None:
            parse_time(expires_at)
        now = isoformat(utc_now())
        approval_id = f"approval-{uuid.uuid4().hex}"
        record = {
            "id": approval_id,
            "task_id": normalized_task.id,
            "status": "pending",
            "data": {
                "attempt": attempt,
                "project_id": project_id if project_id is not None else normalized_task.project,
                "required_actions": normalized_actions,
                "payload_hash": _payload_hash(normalized_task, attempt, normalized_actions, payload_hash),
                "work_key": work_key,
                "actor": None,
                "reason": None,
                "created_at": now,
                "decided_at": None,
                "expires_at": expires_at,
                "consumed_at": None,
            },
            "created_at": now,
            "updated_at": now,
        }
        return self._save(record, "approval.requested", "APPROVAL_REQUESTED")

    def list(self, status: str | None = None, task_id: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if status is not None:
            if status not in {"pending", "approved", "rejected", "expired", "consumed"}:
                raise ValidationError("invalid approval status")
            clauses.append("status=?")
            values.append(status)
        if task_id is not None:
            clauses.append("task_id=?")
            values.append(str(task_id))
        records = self.state.list_records("approvals", " AND ".join(clauses) if clauses else "", tuple(values))
        return [self._public(record) for record in records]

    def show(self, approval_id: str) -> dict[str, Any]:
        record = self.state.get_record("approvals", str(approval_id))
        if record is None:
            raise NotFoundError(f"approval not found: {approval_id}")
        return self._public(record)

    def _decide(self, approval_id: str, status: str, actor: str, reason: str | None) -> dict[str, Any]:
        if status not in {"approved", "rejected"}:
            raise ValidationError("approval decision must be approved or rejected")
        current = self.show(approval_id)
        if current.get("status") != "pending":
            raise ApprovalError(f"approval is not pending: {current.get('status')}")
        expires_at = current.get("expires_at")
        if expires_at and parse_time(expires_at) <= utc_now():
            self.expire()
            raise ApprovalError("approval has expired")
        record = self.state.get_record("approvals", str(approval_id)) or {}
        data = dict(record.get("data") or {})
        data.update({"actor": _text(actor, "actor", 128), "reason": _text(reason, "reason", 1024), "decided_at": isoformat(utc_now())})
        record.update({"status": status, "data": data, "updated_at": isoformat(utc_now())})
        return self._save(record, f"approval.{status}", "APPROVAL_DECIDED")

    def approve(self, approval_id: str, actor: str = "cli") -> dict[str, Any]:
        return self._decide(approval_id, "approved", actor, None)

    def reject(self, approval_id: str, reason: str | None = None, actor: str = "cli") -> dict[str, Any]:
        return self._decide(approval_id, "rejected", actor, reason)

    def expire(self) -> list[dict[str, Any]]:
        now = utc_now()
        expired: list[dict[str, Any]] = []
        for record in self.state.list_records("approvals"):
            data = record.get("data") if isinstance(record.get("data"), dict) else {}
            expires_at = data.get("expires_at")
            if record.get("status") not in {"pending", "approved"} or not expires_at:
                continue
            if parse_time(expires_at) > now:
                continue
            values = dict(record.get("data") or {})
            values["expired_at"] = isoformat(now)
            updated = {**record, "status": "expired", "data": values, "updated_at": isoformat(now)}
            self._save(updated, "approval.expired", "APPROVAL_DECIDED")
            expired.append(self._public(updated))
        return expired

    def consume(
        self,
        approval_ids: list[str] | tuple[str, ...],
        task: Task | dict[str, Any],
        attempt: int,
        actions: Any,
        payload_hash: str | None = None,
    ) -> set[str]:
        normalized_task = _task(task)
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValidationError("attempt must be a positive integer")
        requested = _actions(actions)
        expected_hash = _payload_hash(normalized_task, attempt, requested, payload_hash)
        identifiers = [str(item) for item in approval_ids if str(item).strip()]
        if not identifiers:
            raise ApprovalError("at least one approval ID is required")
        if len(set(identifiers)) != len(identifiers):
            raise ApprovalError("approval IDs must be unique")
        covered: set[str] = set()
        with self.state.transaction() as connection:
            for approval_id in identifiers:
                record = self.state.get_record("approvals", approval_id)
                if record is None:
                    raise ApprovalError(f"approval not found: {approval_id}")
                data = record.get("data") if isinstance(record.get("data"), dict) else {}
                if record.get("status") != "approved":
                    raise ApprovalError(f"approval is not approved: {approval_id}")
                if record.get("task_id") != normalized_task.id or int(data.get("attempt", 0)) != attempt:
                    raise ApprovalError("approval scope does not match task attempt")
                if data.get("project_id") != normalized_task.project:
                    raise ApprovalError("approval scope does not match project")
                if data.get("payload_hash") != expected_hash:
                    raise ApprovalError("approval scope does not match payload")
                required = set(data.get("required_actions") or [])
                allowed = required.intersection(requested)
                if not allowed:
                    raise ApprovalError("approval does not cover a requested action")
                expires_at = data.get("expires_at")
                if expires_at and parse_time(expires_at) <= utc_now():
                    raise ApprovalError("approval has expired")
                covered.update(allowed)
                data["consumed_at"] = isoformat(utc_now())
                self.state.put_record(connection, "approvals", {**record, "status": "consumed", "data": data, "updated_at": isoformat(utc_now())})
            if not set(requested).issubset(covered):
                raise ApprovalError("approval does not cover all requested actions")
        self.state.audit("approval.consumed", "approval", identifiers[0], {"actions": sorted(covered)})
        if self.events is not None:
            self.events.emit("APPROVAL_CONSUMED", {"approval_ids": identifiers, "task_id": normalized_task.id, "actions": sorted(covered)})
        return covered
