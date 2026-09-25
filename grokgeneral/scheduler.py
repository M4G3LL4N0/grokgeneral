from __future__ import annotations

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from .errors import ValidationError
from .models import Task
from .router import Router
from .storage import StateStore
from .tasks import TaskRegistry
from .timeutil import isoformat, parse_time, utc_now


@dataclass(frozen=True)
class SchedulerConfig:
    concurrency: int = 2
    max_tasks: int = 4
    max_seconds: float = 300.0
    max_attempts: int = 2
    retry_failed: bool = False

    def validate(self) -> SchedulerConfig:
        if isinstance(self.concurrency, bool) or not isinstance(self.concurrency, int) or not 1 <= self.concurrency <= 4:
            raise ValidationError("scheduler concurrency must be between 1 and 4")
        if isinstance(self.max_tasks, bool) or not isinstance(self.max_tasks, int) or not 0 <= self.max_tasks <= 100:
            raise ValidationError("scheduler max_tasks must be between 0 and 100")
        if isinstance(self.max_seconds, bool) or not isinstance(self.max_seconds, (int, float)) or self.max_seconds <= 0 or self.max_seconds > 3600:
            raise ValidationError("scheduler max_seconds must be between 0 and 3600")
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int) or not 1 <= self.max_attempts <= 5:
            raise ValidationError("scheduler max_attempts must be between 1 and 5")
        return self


class Scheduler:
    def __init__(self, state: StateStore, tasks: TaskRegistry, router: Router, service: Any | None = None, dispatcher: Callable[[Task, list[str]], Any] | None = None) -> None:
        self.state = state
        self.tasks = tasks
        self.router = router
        self.service = service
        self.dispatcher = dispatcher
        self.state.initialize()

    def _project(self, task: Task):
        if self.service is None or not task.project:
            return None
        try:
            return self.service.projects.get(task.project)
        except Exception:
            return None

    def _route(self, task: Task, project: Any = None):
        return self.router.route(task, project=project, approvals=set(), persist=False)

    def _project_key(self, task: Task, project: Any = None) -> str:
        if project is not None:
            return str(Path(project.path).expanduser().resolve())
        return f"task:{task.id}"

    def _command(self, task: Task) -> list[str] | None:
        value = task.metadata.get("command") if isinstance(task.metadata, dict) else None
        if isinstance(value, (list, tuple)) and value and all(isinstance(item, str) and item for item in value):
            return list(value)
        return None

    def _mutation(self, task: Task) -> bool:
        metadata = task.metadata if isinstance(task.metadata, dict) else {}
        if metadata.get("modify") or metadata.get("requires_modify"):
            return True
        command = self._command(task)
        if command is not None:
            actions = set(self.tasks.classify_command_actions(command))
            return bool(actions.intersection({"modify", "commit", "push", "deploy", "destructive", "external"}))
        return False

    def _approval_actions(self, task: Task, project: Any = None) -> list[str]:
        metadata = task.metadata if isinstance(task.metadata, dict) else {}
        actions: set[str] = set()
        if metadata.get("modify") or metadata.get("requires_modify"):
            actions.add("modify")
        command = self._command(task)
        if command is not None:
            actions.update(self.tasks.classify_command_actions(command))
        for key in ("network", "push", "destructive", "spend", "commit", "deploy", "external", "privacy", "dirty-repo"):
            if metadata.get(key) or metadata.get(f"requires_{key.replace('-', '_')}"):
                actions.add(key)
        if project is not None and isinstance(getattr(project, "metadata", None), dict):
            if project.metadata.get("validation_commands"):
                actions.add("validate")
        return sorted(actions)

    def _approval_ids_for_task(self, task_id: str, approval_ids: list[str] | None) -> list[str]:
        if not approval_ids or self.service is None:
            return list(approval_ids or [])
        values = []
        for approval_id in approval_ids:
            try:
                approval = self.service.approvals.show(approval_id)
            except Exception:
                continue
            if approval.get("task_id") == task_id:
                values.append(approval_id)
        return values

    def _deadline_expired(self, task: Task) -> bool | None:
        if not task.deadline:
            return None
        try:
            return parse_time(task.deadline) <= utc_now()
        except ValueError:
            return None

    def plan(self) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        eligible_statuses = {"proposed", "queued", "failed", "awaiting_approval"}
        for task in sorted(self.tasks.list(), key=lambda item: (-item.priority, item.created_at or "", item.id)):
            if task.status not in eligible_statuses:
                continue
            project = self._project(task)
            entry: dict[str, Any] = {
                "task_id": task.id,
                "goal": task.goal,
                "status": task.status,
                "priority": task.priority,
                "deadline": task.deadline,
                "deadline_expired": self._deadline_expired(task),
                "project": task.project,
                "project_key": self._project_key(task, project),
                "execute_requested": False,
                "approval_needed": self._approval_actions(task, project),
                "mutation": self._mutation(task),
            }
            ready = all(self.tasks.get(dependency).status == "completed" for dependency in task.dependencies)
            if not ready:
                entry["blocked"] = "dependencies are not complete"
                values.append(entry)
                continue
            decision = self._route(task, project)
            route = decision.to_dict()
            entry["route"] = route
            backend = "local" if self._command(task) is not None else "opencode" if decision.provider == "opencode" else "route_only"
            entry["backend"] = backend
            entry["can_execute"] = backend in {"local", "opencode"} and decision.executor != "unassigned"
            if self.service is not None and decision.executor != "unassigned":
                try:
                    resource = self.service.resources.get(str(next((item.get("id") for item in decision.candidates if item.get("executor") == decision.executor and item.get("provider") == decision.provider), "")))
                    entry["resource_health"] = self.service.resources.effective(resource)
                    if not entry["resource_health"]["available"]:
                        entry["can_execute"] = False
                        entry["blocked"] = f"resource is {entry['resource_health']['reason']}"
                except Exception:
                    entry["resource_health"] = {"available": False, "reason": "resource unavailable"}
                    entry["can_execute"] = False
            values.append(entry)
        return values

    def _control(self) -> dict[str, Any]:
        connection = self.state.connect()
        try:
            row = connection.execute("SELECT value FROM meta WHERE key='scheduler.control'").fetchone()
            if row is None:
                return {"state": "running", "stop": False}
            value = json.loads(row[0])
            return value if isinstance(value, dict) else {"state": "running", "stop": False}
        except Exception:
            return {"state": "running", "stop": False}
        finally:
            connection.close()

    def _set_control(self, **changes: Any) -> dict[str, Any]:
        value = {"state": "running", "stop": False, **self._control(), **changes}
        connection = self.state.connect()
        try:
            connection.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('scheduler.control', ?)", (json.dumps(value, sort_keys=True),))
            connection.commit()
        finally:
            connection.close()
        return value

    def pause(self) -> dict[str, Any]:
        return self._set_control(state="paused", stop=False)

    def resume(self) -> dict[str, Any]:
        return self._set_control(state="running", stop=False)

    def stop(self) -> dict[str, Any]:
        return self._set_control(state="stopped", stop=True)

    def control_status(self) -> dict[str, Any]:
        return self._control()

    def _claim(self, entry: dict[str, Any], run_id: str, config: SchedulerConfig, approval_ids: list[str] | None = None) -> tuple[dict[str, Any] | None, str | None]:
        task_id = str(entry["task_id"])
        now = utc_now()
        lease = isoformat(now + timedelta(seconds=config.max_seconds))
        with self.state.transaction() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None or row["status"] not in {"queued", "awaiting_approval", "failed"}:
                return None, "not_claimable"
            task_record = self.state._row_to_record("tasks", row)
            task = Task.from_dict(task_record)
            if task.status == "failed" and (not config.retry_failed or task.attempts >= config.max_attempts):
                return None, "retry_limit"
            if task.status == "awaiting_approval" and entry.get("approval_needed") and not approval_ids:
                return None, "awaiting_approval"
            for claim_row in connection.execute("SELECT * FROM task_claims").fetchall():
                claim = self.state._row_to_record("task_claims", claim_row)
                data = claim.get("data") if isinstance(claim.get("data"), dict) else {}
                expires = data.get("lease_expires_at")
                live = claim.get("status") == "active" and (not expires or parse_time(expires) > now)
                if not live:
                    continue
                if claim.get("task_id") == task_id:
                    return None, "already_claimed"
                if entry.get("mutation") and data.get("mutation") and data.get("project_key") == entry.get("project_key"):
                    return None, "same_repo_conflict"
            claim_id = f"claim-{uuid.uuid4().hex}"
            claim_data = {"attempt": task.attempts + 1, "project_key": entry.get("project_key"), "scheduler_run_id": run_id, "lease_expires_at": lease, "mutation": bool(entry.get("mutation")), "created_at": isoformat(now)}
            self.state.put_record(connection, "task_claims", {"id": claim_id, "task_id": task_id, "project_id": task.project, "status": "active", "data": claim_data, "created_at": isoformat(now), "updated_at": isoformat(now)})
            return {"id": claim_id, "task_id": task_id, "data": claim_data}, None

    def _release_claim(self, claim_id: str, status: str, result: Any = None) -> None:
        record = self.state.get_record("task_claims", claim_id) or {}
        data = dict(record.get("data") or {})
        data.update({"completed_at": isoformat(utc_now()), "result_status": status})
        with self.state.transaction() as connection:
            self.state.put_record(connection, "task_claims", {**record, "status": status, "data": data, "updated_at": isoformat(utc_now())})

    @staticmethod
    def _result_status(result: Any) -> str:
        if isinstance(result, dict):
            if result.get("status"):
                return str(result["status"])
            task_value = result.get("task")
            if isinstance(task_value, dict) and task_value.get("status"):
                return str(task_value["status"])
            receipt = result.get("receipt")
            if isinstance(receipt, dict) and receipt.get("status"):
                return str(receipt["status"])
        return str(getattr(result, "status", "unknown"))

    def _mark_result(self, task_id: str, result: Any) -> str:
        status = result.get("status") if isinstance(result, dict) else getattr(result, "status", None)
        if status == "completed":
            task = self.tasks.get(task_id)
            if task.status == "queued":
                task = self.tasks.update(task_id, {"status": "routed"})
            if task.status == "routed":
                task = self.tasks.update(task_id, {"status": "running", "attempts": task.attempts + 1})
            if task.status == "running":
                self.tasks.complete(task_id, [result] if isinstance(result, dict) else [result.to_dict()])
            return "completed"
        if status in {"failed", "timeout", "blocked"}:
            try:
                self.tasks.fail(task_id, str(result.get("error", status)) if isinstance(result, dict) else status, increment_attempt=False)
            except Exception:
                pass
            return status
        return str(status or "unknown")

    def _dispatch(self, task: Task, entry: dict[str, Any], approval_ids: list[str], approvals: Any = None) -> Any:
        if self.dispatcher is not None:
            return self.dispatcher(task, approval_ids)
        if entry.get("backend") == "local":
            if self.service is not None:
                return self.service.run_task(task.id, approvals=approvals)
            return self.tasks.run(task.id, approvals=approvals, project_path=None)
        if entry.get("backend") == "opencode" and self.service is not None:
            return self.service.executor_run(task.id, approvals=approvals, approval_ids=approval_ids, allow_execution=True)
        return self.tasks.route(task.id)

    def _save_run(self, run_id: str, status: str, started_at: str, data: dict[str, Any]) -> None:
        with self.state.transaction() as connection:
            self.state.put_record(connection, "scheduler_runs", {"id": run_id, "status": status, "started_at": started_at, "ended_at": isoformat(utc_now()), "data": data, "created_at": started_at, "updated_at": isoformat(utc_now())})

    def run_cycle(self, config: SchedulerConfig | None = None, approval_ids: list[str] | None = None, execute: bool = False, approvals: Any = None) -> dict[str, Any]:
        config = (config or SchedulerConfig()).validate()
        started = utc_now()
        started_at = isoformat(started)
        run_id = f"scheduler-run-{uuid.uuid4().hex}"
        control = self._control()
        if control.get("stop") or control.get("state") == "stopped":
            return {"run_id": run_id, "status": "stopped", "concurrency": config.concurrency, "max_tasks": config.max_tasks, "max_seconds": config.max_seconds, "selected": 0, "completed": 0, "failed": 0, "skipped": 0, "same_repo_conflicts": 0, "items": [], "warnings": ["scheduler is stopped"]}
        if control.get("state") == "paused":
            return {"run_id": run_id, "status": "paused", "concurrency": config.concurrency, "max_tasks": config.max_tasks, "max_seconds": config.max_seconds, "selected": 0, "completed": 0, "failed": 0, "skipped": 0, "same_repo_conflicts": 0, "items": [], "warnings": ["scheduler is paused"]}
        if not execute:
            entries = self.plan()
            return {"run_id": run_id, "status": "planned", "concurrency": config.concurrency, "max_tasks": config.max_tasks, "max_seconds": config.max_seconds, "selected": 0, "completed": 0, "failed": 0, "skipped": len(entries), "same_repo_conflicts": 0, "items": entries, "warnings": []}
        if self.service is not None:
            self.service.resources.refresh_expirations()
        entries = [entry for entry in self.plan() if entry.get("can_execute") and not entry.get("blocked") and entry.get("deadline_expired") is not True]
        entries.sort(key=lambda entry: (-int(entry.get("priority", 0)), entry.get("project", ""), entry.get("task_id", "")))
        selected: list[dict[str, Any]] = []
        skipped = 0
        same_repo_conflicts = 0
        awaiting_approval = 0
        cycle_deadline = time.monotonic() + config.max_seconds
        for entry in entries:
            if len(selected) >= config.max_tasks or time.monotonic() >= cycle_deadline:
                skipped += 1
                continue
            if self._control().get("stop") or self._control().get("state") == "paused":
                skipped += 1
                continue
            task_approval_ids = self._approval_ids_for_task(str(entry["task_id"]), approval_ids)
            if entry.get("approval_needed") and not task_approval_ids:
                try:
                    if entry.get("status") == "queued":
                        self.tasks.update(entry["task_id"], {"status": "awaiting_approval"})
                except Exception:
                    pass
                awaiting_approval += 1
                skipped += 1
                continue
            claim, reason = self._claim(entry, run_id, config, task_approval_ids)
            if claim is None:
                if reason == "same_repo_conflict":
                    same_repo_conflicts += 1
                skipped += 1
                continue
            if entry.get("status") in {"awaiting_approval", "failed"}:
                try:
                    self.tasks.update(entry["task_id"], {"status": "queued"})
                except Exception:
                    self._release_claim(claim["id"], "failed")
                    skipped += 1
                    continue
            selected.append({**entry, "claim": claim, "approval_ids": task_approval_ids})
        completed = 0
        failed = 0
        retry_queued = 0
        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=config.concurrency) as pool:
            futures = {}
            for entry in selected:
                task = self.tasks.get(entry["task_id"])
                futures[pool.submit(self._dispatch, task, entry, entry.get("approval_ids", []), approvals)] = entry
            for future in as_completed(futures):
                entry = futures[future]
                try:
                    result = future.result()
                    status = self._mark_result(entry["task_id"], result) if self.dispatcher is not None else self._result_status(result)
                    error_text = str(result.get("error", "")) if isinstance(result, dict) else ""
                    retryable = not any(token in error_text.lower() for token in ("approval", "safety", "dirty", "validation", "deadline", "health"))
                    if status in {"failed", "timeout"} and config.retry_failed and retryable:
                        current = self.tasks.get(entry["task_id"])
                        if current.status == "failed" and current.attempts < config.max_attempts:
                            self.tasks.update(entry["task_id"], {"status": "queued", "metadata": {**(current.metadata or {}), "retry_count": current.attempts + 1}})
                            retry_queued += 1
                    if status == "completed":
                        completed += 1
                    elif status in {"failed", "timeout", "blocked"}:
                        failed += 1
                    results.append({"task_id": entry["task_id"], "project": entry.get("project"), "executor": (result.get("route", {}).get("executor") if isinstance(result, dict) else None), "status": status})
                except Exception as exc:
                    failed += 1
                    try:
                        self.tasks.fail(entry["task_id"], str(exc), increment_attempt=False)
                    except Exception:
                        pass
                    results.append({"task_id": entry["task_id"], "project": entry.get("project"), "status": "failed", "error": str(exc)})
                finally:
                    self._release_claim(entry["claim"]["id"], "completed" if any(item.get("task_id") == entry["task_id"] and item.get("status") == "completed" for item in results) else "failed")
        status = "completed" if not self._control().get("stop") else "stopped"
        data = {"items": results, "skipped": skipped, "awaiting_approval": awaiting_approval, "retry_queued": retry_queued, "same_repo_conflicts": same_repo_conflicts}
        self._save_run(run_id, status, started_at, data)
        return {"run_id": run_id, "status": status, "concurrency": config.concurrency, "max_tasks": config.max_tasks, "max_seconds": config.max_seconds, "selected": len(selected), "completed": completed, "failed": failed, "retry_queued": retry_queued, "skipped": skipped, "awaiting_approval": awaiting_approval, "same_repo_conflicts": same_repo_conflicts, "items": results, "warnings": []}

    def tick(self, execute: bool = False, approvals: Any = None) -> list[dict[str, Any]]:
        if not execute:
            return self.plan()
        if self.service is not None:
            return self.run_cycle(execute=True, approvals=approvals)["items"]
        results = []
        for entry in self.plan():
            task_id = entry["task_id"]
            try:
                if entry.get("can_execute"):
                    task = self.tasks.run(task_id, approvals=approvals)
                    results.append({"task_id": task_id, "status": task.status, "executed": True})
                else:
                    task = self.tasks.route(task_id)
                    results.append({"task_id": task_id, "status": task.status, "executed": False})
            except Exception as exc:
                results.append({"task_id": task_id, "status": "failed", "executed": False, "error": str(exc)})
        return results
