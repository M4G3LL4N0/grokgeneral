from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from .errors import NotFoundError
from .models import Project, Resource, Task
from .projects import ProjectRegistry
from .resources import ResourceRegistry
from .router import Router
from .storage import StateStore
from .tasks import TaskRegistry
from .timeutil import isoformat, parse_time, seconds_until, utc_now

_COST_RANK = {"free": 0, "local": 0, "cheap": 1, "low": 1, "standard": 2, "medium": 2, "premium": 3, "high": 3, "grokbot": 4, "unknown": 2}


def _project_value(project: Project | str | None, projects: ProjectRegistry) -> str | None:
    if isinstance(project, Project):
        return project.id
    return project


def _capabilities(resource: Resource) -> set[str]:
    return {str(item).lower() for item in resource.capabilities}


class OpportunityEngine:
    def __init__(self, state: StateStore, projects: ProjectRegistry, resources: ResourceRegistry, tasks: TaskRegistry | None, router: Router | None) -> None:
        self.state = state
        self.projects = projects
        self.resources = resources
        self.tasks = tasks
        self.router = router
        self.state.initialize()

    def _proposal(self, resource: Resource, project: Project, index: int = 0) -> dict[str, Any]:
        capabilities = sorted(_capabilities(resource))
        coding = bool(set(capabilities).intersection({"coding", "repo-analysis", "refactoring", "testing"}))
        task_type = "repo-audit" if coding else "resource-work"
        required = capabilities[:2] if capabilities else ["research"]
        return {
            "id": f"proposal-{resource.id}-{project.id}-{index}",
            "project": project.id,
            "goal": f"Use {resource.name} for a high-value {task_type} on {project.name}",
            "task_type": task_type,
            "priority": max(40, min(95, project.priority)),
            "required_capabilities": required,
            "metadata": {"opportunity_resource": resource.id, "source": "opportunity-engine"},
        }

    def _task_matches(self, task: Task, resource: Resource) -> bool:
        required = {str(item).lower() for item in task.required_capabilities}
        if not required:
            return True
        return required.issubset(_capabilities(resource))

    def _urgency(self, resource: Resource) -> tuple[float, str]:
        remaining = seconds_until(resource.expires_at)
        if remaining is None:
            return 0.0, "no expiration recorded"
        if remaining <= 0:
            return 1.0, "expired"
        days = remaining / 86400
        if days <= 1:
            return 1.0, "expires within one day"
        if days <= 7:
            return 0.8, "expires within seven days"
        return 0.2, "expires later"
        return 0.0, ""

    def _score(self, resource: Resource, project: Project | None, suitability: float) -> tuple[float, dict[str, float], list[str]]:
        importance = (project.priority if project else 50) / 100
        urgency, urgency_reason = self._urgency(resource)
        value = importance * suitability
        cost = resource.marginal_cost if resource.marginal_cost is not None else (0 if resource.cost_class == "free" else float(_COST_RANK.get(resource.cost_class, 2)))
        effective_cost = max(float(cost), 0.01)
        score = value * (1 + urgency) / effective_cost
        components = {
            "project_importance": round(importance, 4),
            "suitability": round(suitability, 4),
            "expiration_urgency": round(urgency, 4),
            "effective_cost": round(effective_cost, 4),
            "raw_value": round(value, 4),
        }
        return round(score, 4), components, [urgency_reason]

    def list(self, resource: str | None = None) -> list[dict[str, Any]]:
        self.resources.refresh_expirations()
        projects = self.projects.list()
        tasks = self.tasks.list() if self.tasks is not None else []
        resources = self.resources.list()
        if resource:
            wanted = resource.lower()
            resources = [item for item in resources if item.id.lower() == wanted or item.name.lower() == wanted]
        results = []
        for item in resources:
            effective = self.resources.effective(item)
            if not effective["available"]:
                continue
            matching = [task for task in tasks if self._task_matches(task, item) and task.status not in {"completed", "cancelled"}]
            project_values = []
            for task in matching:
                project = next((value for value in projects if value.id == task.project), None)
                if project is not None:
                    project_values.append(project)
            if not project_values:
                project_values = projects[:5]
            proposals = []
            candidates = []
            for project in project_values:
                if any(task.project == project.id for task in matching):
                    task = next(task for task in matching if task.project == project.id)
                    proposals.append({"id": f"task-{task.id}", "task_id": task.id, "project": project.id, "goal": task.goal, "priority": task.priority})
                    suitability = 1.0
                else:
                    proposal = self._proposal(item, project)
                    proposals.append(proposal)
                    suitability = 0.8 if _capabilities(item) else 0.4
                score, components, reasons = self._score(item, project, suitability)
                candidates.append({"project": project.id, "score": score, "components": components, "reasons": reasons})
            if not candidates:
                continue
            candidates.sort(key=lambda value: (-value["score"], value["project"]))
            best = candidates[0]
            result = {
                "resource": item.id,
                "provider": item.provider,
                "cost_class": item.cost_class,
                "expires_at": item.expires_at,
                "score": best["score"],
                "components": best["components"],
                "reasons": best["reasons"],
                "proposed_work": proposals,
                "project_matches": candidates,
            }
            results.append(result)
        results.sort(key=lambda value: (-value["score"], value["resource"]))
        return results

    def optimize(self, max_tasks: int = 20, execute: bool = False) -> dict[str, Any]:
        if not isinstance(max_tasks, int) or isinstance(max_tasks, bool) or max_tasks < 0:
            raise ValueError("max_tasks must be a non-negative integer")
        opportunities = self.list()
        items = []
        queued = 0
        for opportunity in opportunities:
            for proposal in opportunity["proposed_work"]:
                if len(items) >= max_tasks:
                    break
                item = {"resource": opportunity["resource"], **proposal, "score": opportunity["score"]}
                if execute and self.tasks is not None and "task_id" not in proposal:
                    try:
                        self.tasks.get(proposal["id"])
                    except NotFoundError:
                        task_data = {**proposal, "status": "queued", "metadata": {**proposal.get("metadata", {}), "queued_by": "optimize"}}
                        self.tasks.add(task_data)
                        item["queued"] = True
                        queued += 1
                    else:
                        item["queued"] = False
                items.append(item)
            if len(items) >= max_tasks:
                break
        return {"executed": bool(execute), "max_tasks": max_tasks, "queued": queued, "items": items, "opportunities": opportunities, "disclaimer": "Planning does not spend resources; execution only queues local/proposed work."}
