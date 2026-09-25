from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from .backlog import BacklogInspector
from .errors import NotFoundError, ValidationError
from .executions import ExecutionRegistry
from .models import Project, Resource, Task
from .projects import ProjectRegistry
from .resources import ResourceRegistry
from .router import Router
from .storage import StateStore, canonical_json
from .tasks import TaskRegistry
from .timeutil import parse_time, seconds_until, utc_now

_CAPABILITIES = {
    "failing_build": ["coding", "testing"],
    "failing_tests": ["testing"],
    "ci_failure": ["coding", "repo-analysis"],
    "missing_tests": ["testing"],
    "missing_ci": ["repo-analysis"],
    "missing_license": ["repo-analysis"],
    "incomplete_scripts": ["coding", "testing"],
    "malformed_manifest": ["coding"],
    "stale_docs": ["repo-analysis"],
    "dependency_cleanup": ["coding"],
    "architecture_review": ["repo-analysis"],
    "todo": ["coding"],
    "task": ["repo-analysis"],
}
_INTENTS = {
    "failing_build": "repair failing build",
    "failing_tests": "repair failing tests",
    "ci_failure": "repair CI",
    "missing_tests": "add focused tests",
    "missing_ci": "repair CI coverage",
    "missing_license": "complete repository hygiene",
    "incomplete_scripts": "repair build/test scripts",
    "malformed_manifest": "repair package manifest",
    "stale_docs": "refresh repository documentation",
    "dependency_cleanup": "clean up dependencies",
    "architecture_review": "perform focused architecture review",
    "todo": "finish documented TODO",
    "task": "complete recorded task",
}
_SEVERITY = {"low": 0.45, "medium": 0.7, "warning": 0.55, "high": 0.95, "error": 1.0}
_COST_RANK = {"free": 0, "local": 0, "cheap": 1, "low": 1, "medium": 2, "standard": 2, "high": 3, "premium": 3, "grokbot": 4, "unknown": 2}


def _text(value: Any, fallback: str = "") -> str:
    return str(value).strip() if value is not None and str(value).strip() else fallback


def _kind(value: Any) -> str:
    kind = _text(value, "task").lower().replace("-", "_")
    return "todo" if kind == "documented_todo" else kind


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class OpportunityEngineV2:
    def __init__(
        self,
        state: StateStore,
        projects: ProjectRegistry,
        resources: ResourceRegistry,
        tasks: TaskRegistry | None,
        router: Router,
        backlog: BacklogInspector,
        executions: ExecutionRegistry | None = None,
    ) -> None:
        self.state = state
        self.projects = projects
        self.resources = resources
        self.tasks = tasks
        self.router = router
        self.backlog = backlog
        self.executions = executions
        self.state.initialize()

    def normalize_evidence(self, finding: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(finding, dict):
            raise ValidationError("opportunity evidence must be an object")
        kind = _kind(finding.get("kind"))
        path_value = finding.get("path")
        path = str(path_value).strip() if isinstance(path_value, str) and path_value.strip() else None
        line = _int(finding.get("line"))
        marker = _text(finding.get("marker")) or None
        detail = _text(finding.get("detail") or finding.get("message") or finding.get("evidence"), kind.replace("_", " "))
        observed_at = _text(finding.get("observed_at")) or utc_now().isoformat()
        material = {"project": _text(finding.get("project")), "kind": kind, "path": path or "", "line": line, "marker": marker or "", "intent": _INTENTS.get(kind, kind)}
        fingerprint = _text(finding.get("fingerprint")) or hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()
        return {
            "project": _text(finding.get("project")),
            "kind": kind,
            "severity": _text(finding.get("severity"), "low").lower(),
            "path": path,
            "line": line,
            "marker": marker,
            "detail": detail[:1000],
            "observed_at": observed_at,
            "source": _text(finding.get("source"), "backlog"),
            "fingerprint": fingerprint,
            "intent": _INTENTS.get(kind, kind),
        }

    def work_key(self, project: Project | str, evidence: dict[str, Any]) -> str:
        target = project if isinstance(project, Project) else self.projects.get(project)
        normalized = self.normalize_evidence(evidence)
        material = {
            "project": target.canonical_id or target.id,
            "kind": normalized["kind"],
            "path": normalized["path"] or "",
            "line": normalized["line"],
            "marker": normalized["marker"] or "",
            "intent": normalized["intent"],
        }
        return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()

    def _project_health(self, project: Project) -> dict[str, Any]:
        metadata = project.metadata if isinstance(project.metadata, dict) else {}
        reasons: list[str] = []
        score = 0.5
        status = "unknown"
        if project.status in {"archived", "inactive", "blocked"}:
            status = project.status
            score = 0.1
            reasons.append(f"project status is {project.status}")
        elif project.blockers:
            status = "blocked"
            score = 0.15
            reasons.extend(str(item) for item in project.blockers)
        for key in ("build_status", "test_status", "ci_status"):
            value = str(metadata.get(key, "")).lower()
            if value in {"failed", "failing", "broken", "unhealthy", "error"}:
                status = "failing"
                score = min(score, 0.2)
                reasons.append(f"{key}={value}")
            elif value in {"healthy", "passing", "passed", "green", "ok"}:
                status = "healthy"
                score = max(score, 0.8)
        if project.status == "active" and not reasons:
            status = "active"
            score = max(score, 0.7)
        return {"status": status, "score": round(score, 4), "reasons": reasons}

    def _resource(self, decision: dict[str, Any]) -> Resource | None:
        for candidate in decision.get("candidates", []):
            if candidate.get("executor") == decision.get("executor") and candidate.get("provider") == decision.get("provider"):
                try:
                    return self.resources.get(str(candidate.get("id")))
                except NotFoundError:
                    pass
        for resource in self.resources.list():
            if resource.provider == decision.get("provider") and (resource.executor == decision.get("executor") or resource.name == decision.get("executor")):
                return resource
        return None

    def _evidence_for_project(self, project: Project) -> list[dict[str, Any]]:
        values = [self.normalize_evidence(item) for item in self.backlog.inspect(project)]
        if self.tasks is not None:
            for task in self.tasks.list(project=project.id):
                if task.status in {"completed", "cancelled", "running", "validating", "blocked"}:
                    continue
                goal = task.goal.lower()
                if "build" in goal and any(word in goal for word in ("fail", "fix", "broken")):
                    kind = "failing_build"
                elif "test" in goal and any(word in goal for word in ("fail", "fix", "repair")):
                    kind = "failing_tests"
                elif "ci" in goal or "pipeline" in goal:
                    kind = "ci_failure"
                elif "depend" in goal:
                    kind = "dependency_cleanup"
                elif "architect" in goal:
                    kind = "architecture_review"
                else:
                    kind = "task"
                values.append(self.normalize_evidence({"project": project.id, "kind": kind, "path": task.id, "marker": task.id, "detail": task.goal, "severity": "medium", "source": "task"}))
        return values

    def _task_for(self, project: Project, evidence: dict[str, Any]) -> Task:
        if evidence.get("source") == "task" and evidence.get("marker"):
            try:
                return self.tasks.get(str(evidence["marker"])) if self.tasks is not None else Task(id=str(evidence["marker"]), goal=evidence["detail"], project=project.id)
            except NotFoundError:
                pass
        return Task(id=f"proposal-{self.work_key(project, evidence)[:16]}", goal=evidence["intent"], project=project.id, required_capabilities=_CAPABILITIES.get(evidence["kind"], ["repo-analysis"]))

    def _prior_executions(self, task: Task, project: Project, work_key: str) -> list[dict[str, Any]]:
        if self.executions is None:
            return []
        records = self.executions.list(task_id=task.id, project=project.id) if self.tasks is not None and evidence_task_id(task) else self.executions.list(project=project.id)
        return [record for record in records if record.get("work_key") == work_key or record.get("task_id") == task.id]

    def _approval_actions(self, task: Task, commands: list[list[str]]) -> list[str]:
        metadata = task.metadata if isinstance(task.metadata, dict) else {}
        actions: set[str] = set()
        if metadata.get("modify") or metadata.get("requires_modify"):
            actions.add("modify")
        if self.tasks is not None:
            command = metadata.get("command")
            if isinstance(command, (list, tuple)):
                actions.update(self.tasks.classify_command_actions(list(command)))
        for key in ("network", "push", "destructive", "spend", "commit", "deploy", "external", "privacy", "dirty-repo"):
            if metadata.get(key) or metadata.get(f"requires_{key.replace('-', '_')}"):
                actions.add(key)
        if commands:
            actions.add("validate")
        return sorted(actions)

    def _candidate(self, project: Project, evidence: dict[str, Any]) -> dict[str, Any]:
        normalized = self.normalize_evidence(evidence)
        work_key = self.work_key(project, normalized)
        task = self._task_for(project, normalized)
        commands = project.metadata.get("validation_commands", []) if isinstance(project.metadata, dict) else []
        if not isinstance(commands, list):
            commands = []
        blocked_reasons: list[str] = []
        if project.blockers:
            blocked_reasons.extend(f"blocker: {item}" for item in project.blockers)
        if project.status in {"archived", "inactive", "blocked"}:
            blocked_reasons.append(f"project status is {project.status}")
        if task.status in {"blocked", "cancelled", "completed"}:
            blocked_reasons.append(f"task status is {task.status}")
        if self.tasks is not None and any(self.tasks.get(dependency).status != "completed" for dependency in task.dependencies):
            blocked_reasons.append("task dependencies are incomplete")
        if task.deadline:
            try:
                if parse_time(task.deadline) <= utc_now():
                    blocked_reasons.append("task deadline has passed")
            except ValueError:
                blocked_reasons.append("task deadline is invalid")
        decision = self.router.route(task, project=project, approvals=set(), persist=False)
        resource = self._resource(decision.to_dict())
        if decision.executor == "unassigned" or resource is None:
            blocked_reasons.append(decision.reason)
        effective = self.resources.effective(resource) if resource is not None else {"available": False, "reason": "unassigned"}
        if resource is not None and not effective["available"]:
            blocked_reasons.append(f"resource is {effective['reason']}")
        prior = self._prior_executions(task, project, work_key)
        completed = [record for record in prior if record.get("status") == "completed" and (record.get("validation") or {}).get("status") == "passed"]
        failed = [record for record in prior if record.get("status") in {"failed", "timeout"}]
        if completed:
            blocked_reasons.append("a validated previous execution completed this work")
        health = self._project_health(project)
        severity = _SEVERITY.get(normalized["severity"], 0.45)
        priority = project.priority_score / 100
        task_value = max(0.1, min(1.0, task.priority / 100))
        capability_fit = 1.0 if decision.executor != "unassigned" else 0.0
        cost_rank = _COST_RANK.get(str(resource.cost_class if resource else "unknown").lower(), 2)
        cost_value = 1 / (1 + cost_rank)
        remaining = seconds_until(resource.expires_at) if resource is not None else None
        urgency = 1.0 if remaining is not None and 0 < remaining <= 86400 else 0.8 if remaining is not None and remaining <= 7 * 86400 else 0.2
        validation_value = 1.0 if commands else 0.35
        history_value = 0.45 if failed else 0.2 if completed else 1.0
        duplicate_value = 1.0
        components = {
            "project_priority": round(priority, 4),
            "project_health": health["score"],
            "evidence_strength": severity,
            "task_value": round(task_value, 4),
            "capability_fit": capability_fit,
            "resource_cost": round(cost_value, 4),
            "expiration_urgency": urgency,
            "validation_readiness": validation_value,
            "execution_history": history_value,
            "duplicate_penalty": duplicate_value,
        }
        weights = {"project_priority": 0.15, "project_health": 0.12, "evidence_strength": 0.15, "task_value": 0.15, "capability_fit": 0.12, "resource_cost": 0.08, "expiration_urgency": 0.08, "validation_readiness": 0.1, "execution_history": 0.03, "duplicate_penalty": 0.02}
        score = sum(components[key] * weight for key, weight in weights.items()) / sum(weights.values())
        if blocked_reasons:
            score *= 0.2
        reason_parts = [normalized["intent"], f"project {health['status']}"]
        if commands:
            reason_parts.append("validation configured")
        else:
            reason_parts.append("validation not configured")
        if resource is not None:
            reason_parts.append(f"{resource.cost_class} {resource.executor or resource.name}")
        if remaining is not None and 0 < remaining <= 7 * 86400:
            reason_parts.append("resource expiring soon")
        if blocked_reasons:
            reason_parts.append("not execution-ready")
        approval_needed = self._approval_actions(task, commands)
        proposal = {"id": task.id, "task_id": task.id, "project": project.id, "goal": task.goal, "priority": task.priority, "required_capabilities": task.required_capabilities}
        return {
            "id": f"opportunity-{work_key[:16]}",
            "work_key": work_key,
            "task": task.id,
            "task_id": task.id if evidence.get("source") == "task" else None,
            "project": project.id,
            "executor": decision.executor,
            "provider": decision.provider,
            "model": decision.model,
            "resource": resource.id if resource is not None else None,
            "reason": "; ".join(reason_parts),
            "cost_class": resource.cost_class if resource is not None else "unknown",
            "approval_needed": approval_needed,
            "score": round(score, 4),
            "components": components,
            "evidence": normalized,
            "validation_ready": bool(commands),
            "project_health": health,
            "resource_health": effective,
            "previous_executions": [{"id": item.get("id"), "status": item.get("status"), "validation": (item.get("validation") or {}).get("status")} for item in prior],
            "blocked_reasons": blocked_reasons,
            "eligible": not blocked_reasons,
            "proposed_work": [proposal],
        }

    def _projects(self, projects: Any = None) -> list[Project]:
        if projects is None:
            values = self.projects.list()
        elif isinstance(projects, (str, Project)):
            values = [projects if isinstance(projects, Project) else self.projects.get(projects)]
        else:
            values = [self.projects.get(item) if isinstance(item, str) else item for item in projects]
        return sorted(values, key=lambda item: (-item.priority_score, item.id))

    def list(self, projects: Any = None, resource: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValidationError("limit must be a non-negative integer")
        values: list[dict[str, Any]] = []
        seen: set[str] = set()
        for project in self._projects(projects):
            for evidence in self._evidence_for_project(project):
                candidate = self._candidate(project, evidence)
                if "a validated previous execution completed this work" in candidate.get("blocked_reasons", []):
                    continue
                if resource and candidate.get("resource") != resource and candidate.get("executor") != resource:
                    continue
                if candidate["work_key"] in seen:
                    continue
                seen.add(candidate["work_key"])
                values.append(candidate)
        values.sort(key=lambda item: (-item["score"], item["project"], item["work_key"]))
        return values[:limit]

    def _queue(self, item: dict[str, Any]) -> bool:
        if self.tasks is None:
            return False
        try:
            existing = self.tasks.get(item["task"])
        except NotFoundError:
            existing = None
        if existing is not None:
            return False
        proposal = item["proposed_work"][0]
        self.tasks.add({
            "id": item["task"],
            "goal": proposal["goal"],
            "project": item["project"],
            "required_capabilities": proposal.get("required_capabilities", []),
            "priority": proposal.get("priority", 50),
            "status": "queued",
            "metadata": {"work_key": item["work_key"], "opportunity_id": item["id"], "source": "opportunity-v2", "evidence": item["evidence"]},
        })
        return True

    def optimize(self, max_tasks: int = 4, execute: bool = False, queue: bool | None = None) -> dict[str, Any]:
        if isinstance(max_tasks, bool) or not isinstance(max_tasks, int) or max_tasks < 0:
            raise ValidationError("max_tasks must be a non-negative integer")
        queue_requested = execute if queue is None else queue
        candidates = self.list(limit=max(20, max_tasks * 5))
        selected = [item for item in candidates if item["eligible"]][:max_tasks]
        queued = 0
        if queue_requested:
            for item in selected:
                if self._queue(item):
                    queued += 1
        return {
            "executed": False,
            "queue_requested": queue_requested,
            "max_tasks": max_tasks,
            "queued": queued,
            "items": selected,
            "opportunities": candidates,
            "disclaimer": "Planning does not spend resources; queueing does not execute providers.",
        }


def evidence_task_id(task: Task) -> bool:
    return bool(task.id and not task.id.startswith("proposal-"))
