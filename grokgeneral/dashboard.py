from __future__ import annotations

from typing import Any

from .timeutil import isoformat, seconds_until, utc_now


def _task_summary(task: Any) -> dict[str, Any]:
    return {"id": task.id, "project": task.project, "goal": task.goal[:240], "status": task.status, "priority": task.priority}


def _execution_summary(execution: dict[str, Any]) -> dict[str, Any]:
    return {"id": execution.get("id"), "task_id": execution.get("task_id"), "project": execution.get("project_id"), "executor": execution.get("executor"), "status": execution.get("status"), "ended_at": execution.get("ended_at"), "validation": (execution.get("validation") or {}).get("status")}


def build_status_snapshot(service: Any, full: bool = False) -> dict[str, Any]:
    projects = service.projects.list()
    tasks = service.tasks.list()
    resources = service.resources.list()
    executions = service.executions.list()
    approvals = service.approvals.list(status="pending")
    project_items = []
    for project in sorted(projects, key=lambda item: (-item.priority_score, item.id)):
        if project.status in {"archived", "inactive"} and not full:
            continue
        project_items.append({"id": project.id, "name": project.name, "priority": project.priority, "priority_tier": project.priority_tier, "kind": project.kind, "status": project.status, "blocker_count": len(project.blockers)})
    project_items = project_items[:10]
    blockers = [{"project": project.id, "items": [str(item)[:240] for item in project.blockers[:10]]} for project in projects if project.blockers]
    blockers.extend({"task": task.id, "items": [str(task.metadata.get("error", "blocked"))[:240]]} for task in tasks if task.status == "blocked")
    running = [_task_summary(task) for task in tasks if task.status in {"running", "validating"}]
    pending = [{"id": item.get("id"), "task_id": item.get("task_id"), "actions": item.get("required_actions", []), "project": item.get("project_id"), "status": item.get("status")} for item in approvals[:20]]
    free = []
    expiring = []
    for resource in resources:
        effective = service.resources.effective(resource)
        if effective["available"] and str(resource.cost_class).lower() in {"free", "local"}:
            free.append({"id": resource.id, "name": resource.name, "provider": resource.provider, "executor": resource.executor, "cost_class": resource.cost_class, "expires_at": resource.expires_at})
        remaining = seconds_until(resource.expires_at)
        if effective["available"] and remaining is not None and 0 < remaining <= 7 * 86400:
            expiring.append({"id": resource.id, "name": resource.name, "expires_at": resource.expires_at, "seconds_remaining": remaining})
    recent = [_execution_summary(item) for item in executions if item.get("status") in {"completed", "failed", "timeout", "blocked"}][-10:]
    digest = service.status_model.opportunity_digest()
    opportunities = list(digest.get("items") or [])
    health_freshness = service.status_model.health_overview()
    project_health = service.status_model.summaries()
    next_actions = [{"task": item.get("task"), "project": item.get("project"), "action": item.get("reason"), "score": item.get("score")} for item in opportunities[:5]]
    health_state = service.state.health()
    health = {key: health_state.get(key) for key in ("ok", "integrity", "schema_version", "writable")}
    result = {
        "generated_at": isoformat(utc_now()),
        "health": health,
        "projects": {"count": len(projects), "items": project_items},
        "tasks": len(tasks),
        "tasks_by_status": {status: sum(1 for task in tasks if task.status == status) for status in sorted({task.status for task in tasks})},
        "blockers": blockers[:20],
        "running_tasks": running[:20],
        "pending_approvals": pending,
        "free_resources": free[:20],
        "expiring_resources": expiring[:20],
        "recent_completions": recent,
        "top_opportunities": opportunities[:10],
        "opportunity_digest": {"status": digest.get("status"), "generated_at": digest.get("generated_at"), "age_seconds": digest.get("age_seconds"), "count": digest.get("count", 0)},
        "health_freshness": health_freshness,
        "project_health": [project_health[project.id] for project in sorted(projects, key=lambda item: (-item.priority_score, item.id)) if project.id in project_health][:10],
        "next_actions": next_actions,
        "usage": service.usage.summary(),
    }
    if full:
        result["project_details"] = [{"id": project.id, "name": project.name, "status": project.status, "priority": project.priority, "priority_tier": project.priority_tier, "kind": project.kind, "blockers": project.blockers[:10]} for project in sorted(projects, key=lambda item: (-item.priority_score, item.id))[:50]]
        result["execution_details"] = [_execution_summary(item) for item in executions[-50:]]
    return result
