from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .adapters import AdapterRegistry
from .approvals import ApprovalRegistry
from .backlog import BacklogInspector
from .cache import Cache
from .context import ContextBuilder
from .doctor import Doctor
from .errors import NotFoundError
from .events import EventBus
from .executions import ExecutionRegistry
from .executor import Executor
from .models import Project, Task
from .opportunities import OpportunityEngine
from .opportunity_v2 import OpportunityEngineV2
from .policies import PolicyEngine
from .projects import ProjectRegistry, RootRegistry
from .resources import ResourceRegistry
from .router import Router
from .scheduler import Scheduler, SchedulerConfig
from .storage import StateStore, atomic_write_json
from .tasks import TaskRegistry
from .timeutil import seconds_until
from .usage import UsageLedger


class GrokGeneral:
    def __init__(self, state_dir: str | Path | None = None, offline: bool = False) -> None:
        self.state = StateStore(state_dir)
        self.offline = offline
        self.state.initialize()
        self.events = EventBus(self.state)
        self.approvals = ApprovalRegistry(self.state, self.events)
        self.policies = PolicyEngine(self.state)
        self.policies.load()
        self.cache = Cache(self.state)
        self.roots = RootRegistry(self.state)
        self.projects = ProjectRegistry(self.state, self.events, self.cache)
        self.resources = ResourceRegistry(self.state, self.events, self.cache)
        self.usage = UsageLedger(self.state)
        self.executions = ExecutionRegistry(self.state)
        self.router = Router(self.state, self.policies, self.resources, self.cache)
        self.tasks = TaskRegistry(self.state, self.events, self.policies, self.resources, self.router, self.usage)
        self.adapters = AdapterRegistry(self.state, self.policies)
        self.executor = Executor(self)
        self.scheduler = Scheduler(self.state, self.tasks, self.router, service=self)
        self.context_builder = ContextBuilder(self.state, self.projects, self.cache)
        self.backlog = BacklogInspector(self.state, self.projects)
        self.opportunities_engine = OpportunityEngineV2(self.state, self.projects, self.resources, self.tasks, self.router, self.backlog, self.executions)
        self._doctor = Doctor(self.state, self.projects, self.resources, self.events, self.adapters)

    @property
    def state_dir(self) -> Path:
        return self.state.state_dir

    def initialize(self, seed: bool = True) -> dict[str, Any]:
        if seed:
            self.resources.seed_defaults()
        return {"state_dir": str(self.state_dir), "resources": len(self.resources.list()), "projects": len(self.projects.list()), "offline": self.offline}

    def startup_root(self) -> Path:
        configured = os.environ.get("GG_STARTUPS_ROOT")
        return Path(configured).expanduser().resolve() if configured else (Path.home() / "startups").resolve()

    def scan_projects(self, root: str | Path | None = None, include_all: bool = False) -> list[Project]:
        if root is not None:
            return self.projects.scan(root, include_all=include_all)
        configured = self.roots.list()
        if configured:
            found: list[Project] = []
            seen: set[str] = set()
            for item in configured:
                for project in self.projects.scan(item["path"], include_all=include_all, root_label=item.get("name")):
                    if project.id not in seen:
                        seen.add(project.id)
                        found.append(project)
            return found
        return self.projects.scan(self.startup_root(), include_all=include_all)

    def add_project(self, data: dict[str, Any]) -> Project:
        return self.projects.add(data)

    def update_project(self, identifier: str, changes: dict[str, Any]) -> Project:
        return self.projects.update(identifier, changes)

    def add_project_alias(self, identifier: str, alias: str) -> Project:
        return self.projects.add_alias(identifier, alias)

    def set_project_priority(self, identifier: str, priority: str) -> Project:
        return self.projects.set_priority(identifier, priority)

    def set_project_validation(self, identifier: str, commands: list[list[str]]) -> Project:
        project = self.projects.get(identifier)
        if not isinstance(commands, list) or not commands or any(isinstance(command, str) or not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command) for command in commands):
            from .errors import ValidationError
            raise ValidationError("validation commands must be a non-empty list of argv arrays")
        metadata = dict(project.metadata or {})
        metadata["validation_commands"] = commands
        return self.projects.update(project.id, {"metadata": metadata})

    def project_validation(self, identifier: str) -> list[list[str]]:
        project = self.projects.get(identifier)
        value = project.metadata.get("validation_commands", []) if isinstance(project.metadata, dict) else []
        return value if isinstance(value, list) else []

    def add_resource(self, data: dict[str, Any]):
        return self.resources.add(data)

    def update_resource(self, name: str, changes: dict[str, Any]):
        return self.resources.update(name, changes)

    def expire_resource(self, name: str, at: Any = None):
        return self.resources.expire(name, at)

    def add_task(self, data: dict[str, Any]) -> Task:
        return self.tasks.add(data)

    def request_approval(self, task_id: str, actions: Any, attempt: int | None = None, payload_hash: str | None = None, work_key: str | None = None, expires_at: str | None = None) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        return self.approvals.request(task, attempt if attempt is not None else task.attempts + 1, actions, payload_hash=payload_hash, project_id=task.project, work_key=work_key, expires_at=expires_at)

    def approvals_list(self, status: str | None = None, task_id: str | None = None) -> list[dict[str, Any]]:
        self.approvals.expire()
        return self.approvals.list(status=status, task_id=task_id)

    def approval_show(self, approval_id: str) -> dict[str, Any]:
        return self.approvals.show(approval_id)

    def approval_approve(self, approval_id: str, actor: str = "cli") -> dict[str, Any]:
        return self.approvals.approve(approval_id, actor=actor)

    def approval_reject(self, approval_id: str, reason: str | None = None, actor: str = "cli") -> dict[str, Any]:
        return self.approvals.reject(approval_id, reason=reason, actor=actor)

    def update_task(self, task_id: str, changes: dict[str, Any]) -> Task:
        return self.tasks.update(task_id, changes)

    def route_task(self, task_id: str) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        project = None
        if task.project:
            try:
                project = self.projects.get(task.project)
            except NotFoundError:
                project = None
        return self.tasks.route(task_id).to_dict() if project is None else self._route_with_project(task_id, project)

    def _route_with_project(self, task_id: str, project: Project) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        decision = self.router.route(task, project=project)
        values = task.to_dict()
        values["assigned_executor"] = decision.executor
        values["routing_rationale"] = decision.to_dict()
        values["status"] = "queued" if decision.executor != "unassigned" else "blocked"
        pack = self.context_builder.build(task, project=project, write=True)
        values["routing_rationale"]["context_pack"] = pack.path
        self.tasks._save(Task.from_dict(values), "task.routed", "TASK_ROUTED")
        self.events.emit("CONTEXT_BUILT", {"task_id": task.id, "path": pack.path, "approx_bytes": pack.approx_bytes})
        self.events.emit("ROUTE_DECIDED", {"task_id": task.id, "executor": decision.executor, "provider": decision.provider, "reason": decision.reason})
        return self.tasks.get(task_id).to_dict()

    def route_goal(self, goal: str, project: str | None = None) -> dict[str, Any]:
        target = self.projects.get(project) if project else None
        return self.router.route_goal(goal, project=target).to_dict()

    def run_task(self, task_id: str, approvals: Any = None) -> Task:
        task = self.tasks.get(task_id)
        project_path = None
        if task.project:
            try:
                project_path = self.projects.get(task.project).path
            except NotFoundError:
                project_path = None
        return self.tasks.run(task_id, approvals=approvals, project_path=project_path)

    def executor_run(self, task_id: str, dry_run: bool = False, approvals: Any = None, approval_ids: list[str] | None = None, allow_execution: bool = False, model: str | None = None, timeout: int = 120) -> dict[str, Any]:
        return self.executor.run(task_id, dry_run=dry_run, approvals=approvals, approval_ids=approval_ids, allow_execution=allow_execution, model=model, timeout=timeout)

    def executions_list(self, task_id: str | None = None, project: str | None = None) -> list[dict[str, Any]]:
        return self.executions.list(task_id=task_id, project=project)

    def execution_show(self, execution_id: str) -> dict[str, Any]:
        return self.executions.get(execution_id)

    def schedule(self, execute: bool = False, approvals: Any = None) -> list[dict[str, Any]]:
        return self.scheduler.tick(execute=execute, approvals=approvals)

    def schedule_cycle(self, execute: bool = False, approvals: Any = None, approval_ids: list[str] | None = None, config: SchedulerConfig | None = None) -> dict[str, Any]:
        return self.scheduler.run_cycle(config=config, approval_ids=approval_ids, execute=execute, approvals=approvals)

    def context(self, task_id: str) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        project = self.projects.get(task.project) if task.project else None
        pack = self.context_builder.build(task, project=project, write=True)
        self.events.emit("CONTEXT_BUILT", {"task_id": task.id, "path": pack.path, "approx_bytes": pack.approx_bytes})
        result = dict(pack.content)
        result["task_id"] = pack.task_id
        result["path"] = pack.path
        result["content"] = pack.content
        result["approx_bytes"] = pack.approx_bytes
        return result

    def opportunities(self, resource: str | None = None, project: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        return self.opportunities_engine.list(projects=project, resource=resource, limit=limit)

    def optimize(self, max_tasks: int = 4, execute: bool = False, queue: bool | None = None) -> dict[str, Any]:
        return self.opportunities_engine.optimize(max_tasks=max_tasks, execute=execute, queue=queue)

    def status(self) -> dict[str, Any]:
        self.resources.refresh_expirations()
        self.tasks.reroute_queued()
        projects = self.projects.list()
        tasks = self.tasks.list()
        resources = self.resources.list()
        resource_summary = []
        expiring = []
        for resource in resources:
            value = resource.to_dict()
            value["effective"] = self.resources.effective(resource)
            resource_summary.append(value)
            remaining = seconds_until(resource.expires_at)
            if remaining is not None and 0 < remaining <= 7 * 86400 and value["effective"]["available"]:
                expiring.append({"id": resource.id, "name": resource.name, "expires_at": resource.expires_at, "seconds_remaining": remaining})
        opportunities = self.opportunities()
        usage = self.usage.summary()
        blockers = [{"project": project.id, "items": project.blockers} for project in projects if project.blockers]
        blockers.extend({"task": task.id, "items": [task.metadata.get("error", "blocked")] } for task in tasks if task.status == "blocked")
        next_actions = []
        for opportunity in opportunities[:3]:
            proposed = opportunity.get("proposed_work", [])
            next_actions.append({"resource": opportunity["resource"], "action": proposed[0].get("goal") if proposed else "Review suitable backlog work", "score": opportunity["score"]})
        if not next_actions and projects:
            next_actions.append({"action": "Run gg projects scan and create a focused task", "score": None})
        return {
            "projects": len(projects),
            "tasks": len(tasks),
            "tasks_by_status": {status: sum(1 for task in tasks if task.status == status) for status in sorted({task.status for task in tasks})},
            "blockers": blockers,
            "resources": len(resources),
            "resource_summary": resource_summary,
            "expiring_resources": expiring,
            "opportunities": opportunities[:10],
            "usage": usage,
            "health": self.state.health(),
            "next_actions": next_actions,
        }

    def doctor(self) -> list[dict[str, Any]]:
        return self._doctor.run()

    def ask(self, question: str) -> dict[str, Any]:
        if not isinstance(question, str) or not question.strip():
            return {"question": question, "answer": "Ask a non-empty question.", "evidence": [], "ai_used": False, "method": "deterministic"}
        text = question.lower()
        projects = self.projects.list()
        if "space bunny" in text or "space-bunny" in text:
            values = self.opportunities(resource="space-bunny")
            if values:
                proposal = values[0]["proposed_work"][0] if values[0]["proposed_work"] else {}
                return {"question": question, "answer": proposal.get("goal", "Review the Space Bunny opportunity queue."), "evidence": values, "ai_used": False, "method": "deterministic"}
            return {"question": question, "answer": "Space Bunny is not currently available in the resource registry.", "evidence": [], "ai_used": False, "method": "deterministic"}
        if "cursor" in text and ("credit" in text or "value" in text):
            values = self.opportunities(resource="cursor")
            return {"question": question, "answer": "Use Cursor only where its recorded capabilities and cost class justify the work." if values else "No Cursor resource is currently recorded.", "evidence": values, "ai_used": False, "method": "deterministic"}
        if "test" in text:
            findings = []
            for project in projects:
                values = self.backlog.inspect(project)
                findings.extend(item for item in values if item.get("kind") in {"missing_tests", "todo"})
            names = sorted({item["project"] for item in findings})
            return {"question": question, "answer": ", ".join(names) if names else "No project has recorded testing gaps.", "evidence": findings, "ai_used": False, "method": "deterministic"}
        if "unfinished build" in text or "build" in text and "repo" in text:
            values = []
            for project in projects:
                metadata = project.metadata if isinstance(project.metadata, dict) else {}
                if metadata.get("build_status") in {"failed", "unfinished", "broken"}:
                    values.append({"project": project.id, "build_status": metadata["build_status"]})
            return {"question": question, "answer": ", ".join(item["project"] for item in values) if values else "No unfinished build evidence is recorded; run a project inspection before assuming status.", "evidence": values, "ai_used": False, "method": "deterministic"}
        return {"question": question, "answer": "I can answer deterministic project, task, resource, testing, opportunity, and recorded build-status questions locally.", "evidence": [], "ai_used": False, "method": "deterministic"}

    def export_snapshot(self, path: str | Path | None = None) -> dict[str, Any]:
        snapshot = self.state.export_snapshot()
        if path is not None:
            atomic_write_json(Path(path), snapshot)
        return snapshot

    def import_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.state.import_snapshot(snapshot)

    def close(self) -> None:
        self.state.close()
