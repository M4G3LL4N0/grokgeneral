from __future__ import annotations

from typing import Any

from .router import Router
from .storage import StateStore
from .tasks import TaskRegistry
from .timeutil import parse_time, utc_now


class Scheduler:
    def __init__(self, state: StateStore, tasks: TaskRegistry, router: Router) -> None:
        self.state = state
        self.tasks = tasks
        self.router = router
        self.state.initialize()

    def plan(self) -> list[dict[str, Any]]:
        values = []
        for task in sorted(self.tasks.list(), key=lambda item: (-item.priority, item.created_at or "", item.id)):
            if task.status not in {"proposed", "queued", "failed"}:
                continue
            entry: dict[str, Any] = {
                "task_id": task.id,
                "goal": task.goal,
                "status": task.status,
                "priority": task.priority,
                "deadline": task.deadline,
                "execute_requested": False,
            }
            try:
                ready = all(self.tasks.get(dependency).status == "completed" for dependency in task.dependencies)
            except Exception:
                ready = False
            if not ready:
                entry["blocked"] = "dependencies are not complete"
                values.append(entry)
                continue
            decision = self.router.route(task)
            entry["route"] = decision.to_dict()
            entry["can_execute"] = isinstance(task.metadata.get("command"), list)
            if task.deadline:
                try:
                    entry["deadline_expired"] = (parse_time(task.deadline) <= utc_now())
                except ValueError:
                    entry["deadline_expired"] = None
            values.append(entry)
        return values

    def tick(self, execute: bool = False, approvals: Any = None) -> list[dict[str, Any]]:
        plan = self.plan()
        if not execute:
            return plan
        results = []
        for entry in plan:
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
