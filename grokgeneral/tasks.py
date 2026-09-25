from __future__ import annotations

import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from .errors import NotFoundError, SafetyBlockedError, ValidationError
from .events import EventBus
from .models import Project, Task
from .policies import PolicyEngine
from .resources import ResourceRegistry
from .router import Router
from .storage import StateStore, redact
from .timeutil import isoformat, utc_now
from .usage import UsageLedger

_STATUSES = {"proposed", "queued", "running", "completed", "failed", "blocked", "cancelled"}
_TRANSITIONS = {
    "proposed": {"queued", "running", "blocked", "cancelled"},
    "queued": {"running", "blocked", "cancelled", "failed"},
    "running": {"completed", "failed", "blocked", "cancelled"},
    "blocked": {"queued", "cancelled"},
    "failed": {"queued", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}


class TaskRegistry:
    def __init__(self, state: StateStore, events: EventBus, policies: PolicyEngine, resources: ResourceRegistry, router: Router | None = None, usage: UsageLedger | None = None, adapters: Any | None = None) -> None:
        self.state = state
        self.events = events
        self.policies = policies
        self.resources = resources
        self.router = router
        self.usage = usage
        self.adapters = adapters
        self.state.initialize()

    def list(self, status: str | None = None, project: str | None = None) -> list[Task]:
        clauses = []
        values = []
        if status is not None:
            clauses.append("status=?")
            values.append(status)
        if project is not None:
            clauses.append("project_id=?")
            values.append(project)
        records = self.state.list_records("tasks", " AND ".join(clauses) if clauses else "", tuple(values))
        return [Task.from_dict(item) for item in records]

    def get(self, task_id: str) -> Task:
        record = self.state.get_record("tasks", str(task_id))
        if record is None:
            raise NotFoundError(f"task not found: {task_id}")
        return Task.from_dict(record)

    def _save(self, task: Task, action: str, event_type: str | None = None) -> Task:
        with self.state.transaction() as connection:
            self.state.put_record(connection, "tasks", task.to_record())
        self.state.audit(action, "task", task.id, {"status": task.status, "goal": task.goal})
        if event_type:
            self.events.emit(event_type, {"task_id": task.id, "status": task.status})
        return task

    def add(self, data: dict[str, Any]) -> Task:
        if not isinstance(data, dict):
            raise ValidationError("task data must be an object")
        goal = data.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            raise ValidationError("task goal must be a non-empty string")
        task_id = str(data.get("id") or f"task-{uuid.uuid4().hex}")
        values = {**data, "id": task_id, "goal": goal.strip(), "status": data.get("status", "proposed")}
        if values["status"] not in _STATUSES:
            raise ValidationError(f"invalid task status: {values['status']}")
        now = isoformat(utc_now())
        values.setdefault("created_at", now)
        values["updated_at"] = now
        task = Task.from_dict(values)
        for dependency in task.dependencies:
            if self.state.get_record("tasks", dependency) is None:
                raise ValidationError(f"task dependency not found: {dependency}")
        return self._save(task, "task.created", "TASK_CREATED")

    def update(self, task_id: str, changes: dict[str, Any]) -> Task:
        if not isinstance(changes, dict):
            raise ValidationError("task changes must be an object")
        task = self.get(task_id)
        new_status = changes.get("status", task.status)
        if new_status not in _STATUSES:
            raise ValidationError(f"invalid task status: {new_status}")
        if new_status != task.status and new_status not in _TRANSITIONS[task.status]:
            raise ValidationError(f"invalid task transition: {task.status} -> {new_status}")
        values = task.to_dict()
        values.update(changes)
        values["id"] = task.id
        values["updated_at"] = isoformat(utc_now())
        updated = Task.from_dict(values)
        return self._save(updated, "task.updated", "TASK_UPDATED")

    def _dependencies_ready(self, task: Task) -> bool:
        return all(self.get(dependency).status == "completed" for dependency in task.dependencies)

    def route(self, task_id: str) -> Task:
        if self.router is None:
            raise ValidationError("router is not configured")
        task = self.get(task_id)
        if not self._dependencies_ready(task):
            raise ValidationError("task dependencies are not complete")
        decision = self.router.route(task)
        values = task.to_dict()
        values["assigned_executor"] = decision.executor
        values["routing_rationale"] = decision.to_dict()
        values["status"] = "queued" if decision.executor != "unassigned" else "blocked"
        values["updated_at"] = isoformat(utc_now())
        updated = Task.from_dict(values)
        self._save(updated, "task.routed", "TASK_ROUTED")
        self.events.emit("ROUTE_DECIDED", {"task_id": task.id, "executor": decision.executor, "provider": decision.provider, "reason": decision.reason})
        return updated

    def _approval_for_command(self, command: list[str]) -> str | None:
        text = " ".join(command).lower()
        if any(token in text for token in ("git push", "push ")):
            return "push"
        if any(token in text for token in ("curl", "wget", "git fetch", "git pull", "git clone", "ssh ", "scp ", "npm publish", "pip install", "docker pull", "gh ", "http://", "https://")):
            return "network"
        if any(token in text for token in ("rm ", "rmdir", "unlink", "reset --hard", "delete ", "mv ", "cp ", "touch ", "mkdir ", "tee ")) or re.search(r"\b(?:rmtree|unlink|remove|rm)\s*\(", text) or re.search(r"open\s*\([^\n]*['\"](?:w|a|x)", text):
            return "destructive"
        if any(token in text for token in ("stripe", "purchase", "payment", "pay ")):
            return "spend"
        return None

    def run(self, task_id: str, approvals: Any = None, project_path: str | None = None) -> Task:
        task = self.get(task_id)
        if not self._dependencies_ready(task):
            raise ValidationError("task dependencies are not complete")
        command = task.metadata.get("command") if isinstance(task.metadata, dict) else None
        if command is None:
            if self.router is None:
                raise ValidationError("task has no executable command and router is not configured")
            return self.route(task.id)
        if isinstance(command, str) or not isinstance(command, (list, tuple)) or not command or not all(isinstance(item, str) and item for item in command):
            raise ValidationError("task command must be a non-empty list of strings")
        command = list(command)
        approval = self._approval_for_command(command)
        supplied = set(approvals or [])
        if approval:
            decision = self.policies.authorize(approval, supplied, self.policies.load())
            if not decision.allowed:
                raise SafetyBlockedError(f"task command requires {approval} approval: {'; '.join(decision.reasons)}")
        if task.status not in {"queued", "proposed", "failed"}:
            raise ValidationError(f"task cannot run from status {task.status}")
        values = task.to_dict()
        values["status"] = "running"
        values["attempts"] = task.attempts + 1
        values["updated_at"] = isoformat(utc_now())
        running = Task.from_dict(values)
        self._save(running, "task.started", "TASK_STARTED")
        cwd = Path(project_path).expanduser().resolve() if project_path else None
        if cwd is not None and not cwd.is_dir():
            return self.fail(task.id, f"project path does not exist: {cwd}", increment_attempt=False)
        environment = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
        started = time.perf_counter()
        try:
            result = subprocess.run(command, cwd=cwd, env=environment, capture_output=True, text=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return self.fail(task.id, str(exc), increment_attempt=False)
        output = {"stdout": redact(result.stdout), "stderr": redact(result.stderr), "returncode": result.returncode, "command": command}
        if result.returncode != 0:
            failed = self.fail(task.id, f"command exited with {result.returncode}", increment_attempt=False)
            failed.metadata["output"] = output
            return self._save(Task.from_dict(failed.to_dict()), "task.failed", "TASK_FAILED")
        completed = self.complete(task.id, [output])
        if self.usage is not None:
            self.usage.record(source="measured", units=time.perf_counter() - started, cost=None, project=completed.project, task_id=completed.id, metadata={"kind": "local_process", "returncode": 0})
        return completed

    def complete(self, task_id: str, outputs: list[Any] | None = None) -> Task:
        task = self.get(task_id)
        if task.status != "running":
            raise ValidationError("only running tasks can complete")
        values = task.to_dict()
        values["status"] = "completed"
        values["outputs"] = outputs or []
        values["updated_at"] = isoformat(utc_now())
        return self._save(Task.from_dict(values), "task.completed", "TASK_COMPLETED")

    def fail(self, task_id: str, error: str, increment_attempt: bool = True) -> Task:
        task = self.get(task_id)
        if task.status not in {"running", "queued", "proposed"}:
            raise ValidationError(f"task cannot fail from status {task.status}")
        values = task.to_dict()
        values["status"] = "failed"
        values["attempts"] = task.attempts + (1 if increment_attempt else 0)
        values["metadata"] = {**(task.metadata or {}), "error": str(error)}
        values["updated_at"] = isoformat(utc_now())
        return self._save(Task.from_dict(values), "task.failed", "TASK_FAILED")
