from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from .cache import Cache
from .models import Project, Resource, RouteDecision, Task
from .policies import PolicyEngine
from .resources import ResourceRegistry
from .storage import StateStore
from .timeutil import isoformat, parse_time, seconds_until, utc_now


_COST_ORDER = {"free": 0, "local": 0, "cheap": 1, "low": 1, "standard": 2, "medium": 2, "premium": 3, "high": 3, "grokbot": 4, "unknown": 2}
_CAPABILITY_WORDS = {
    "audit": "repo-analysis",
    "review": "repo-analysis",
    "analyze": "repo-analysis",
    "test": "testing",
    "testing": "testing",
    "build": "coding",
    "fix": "coding",
    "refactor": "refactoring",
    "refactoring": "refactoring",
    "code": "coding",
    "coding": "coding",
    "research": "research",
    "strategy": "strategy",
}


def _as_task(value: Task | dict[str, Any]) -> Task:
    return value if isinstance(value, Task) else Task.from_dict(value)


def _as_project(value: Project | dict[str, Any] | None) -> Project | None:
    if value is None or isinstance(value, Project):
        return value
    return Project.from_dict(value)


def _cost_rank(resource: Resource) -> int:
    return _COST_ORDER.get(str(resource.cost_class or "unknown").lower(), 2)


def _inferred_capabilities(task: Task) -> list[str]:
    if task.required_capabilities:
        return list(dict.fromkeys(str(item).lower() for item in task.required_capabilities))
    text = task.goal.lower()
    found = []
    for word, capability in _CAPABILITY_WORDS.items():
        if re_search(text, word):
            found.append(capability)
    return list(dict.fromkeys(found))


def re_search(text: str, word: str) -> bool:
    return word in text


class Router:
    def __init__(self, state: StateStore, policies: PolicyEngine, resources: ResourceRegistry, cache: Cache | None = None) -> None:
        self.state = state
        self.policies = policies
        self.resources = resources
        self.cache = cache

    def _all_resources(self) -> list[Resource]:
        values = {resource.id: resource for resource in self.resources.list()}
        values.setdefault("local", Resource(id="local", name="local", provider="local", executor="local", model="shell", cost_class="free", availability="available", health="healthy", capabilities=["coding", "repo-analysis", "refactoring", "testing", "local-shell", "read", "build"]))
        return list(values.values())

    def _cache_key(self, task: Task, project: Project | None, resources: list[Resource], approvals: Any = None) -> str | None:
        if self.cache is None:
            return None
        supplied = sorted({str(item).strip().lower() for item in (approvals or []) if str(item).strip()}) if not isinstance(approvals, str) else [approvals.strip().lower()]
        payload = {
            "task": task.to_dict(),
            "project": project.to_dict() if project else None,
            "policy": self.policies.load(),
            "approvals": supplied,
            "resources": [{"id": item.id, "availability": item.availability, "expires_at": item.expires_at, "health": item.health, "remaining_capacity": item.remaining_capacity, "cost_class": item.cost_class, "capabilities": sorted(item.capabilities)} for item in sorted(resources, key=lambda item: item.id)],
        }
        return self.cache.key("route", payload)

    def _audit(self, task: Task, decision: RouteDecision) -> None:
        self.state.audit("route.decided", "task", task.id, {"executor": decision.executor, "provider": decision.provider, "cost_class": decision.estimated_cost_class, "cache_hit": decision.cache_hit})

    def _cached(self, key: str | None) -> RouteDecision | None:
        if key is None or self.cache is None:
            return None
        value = self.cache.get(key)
        if not isinstance(value, dict) or not value.get("executor"):
            return None
        try:
            return RouteDecision.from_dict(value)
        except AttributeError:
            return RouteDecision(
                executor=str(value["executor"]),
                provider=str(value.get("provider", "unknown")),
                model=str(value.get("model", "")),
                reason=str(value.get("reason", "cached decision")),
                fallbacks=list(value.get("fallbacks", [])),
                context_pack=value.get("context_pack"),
                estimated_cost_class=str(value.get("estimated_cost_class", "unknown")),
                policy_matches=list(value.get("policy_matches", [])),
                candidates=list(value.get("candidates", [])),
                cache_hit=True,
            )

    def route(self, task: Task | dict[str, Any], project: Project | dict[str, Any] | None = None, approvals: Any = None, persist: bool = True) -> RouteDecision:
        normalized_task = _as_task(task)
        normalized_project = _as_project(project)
        if not normalized_task.required_capabilities:
            normalized_task.required_capabilities = _inferred_capabilities(normalized_task)
        all_resources = self._all_resources()
        key = self._cache_key(normalized_task, normalized_project, all_resources, approvals) if persist else None
        cached = self._cached(key)
        if cached is not None:
            if persist:
                self._audit(normalized_task, cached)
            return cached
        candidates: list[dict[str, Any]] = []
        eligible: list[tuple[float, Resource, PolicyEngine]] = []
        rejections: list[str] = []
        policy_matches: list[str] = []
        for resource in all_resources:
            effective = self.resources.effective(resource)
            if not effective["available"]:
                rejections.append(f"{resource.id}: {effective['reason']}")
                continue
            decision = self.policies.evaluate(normalized_task, normalized_project, resource, approvals or set())
            if not decision.allowed:
                rejections.append(f"{resource.id}: {'; '.join(decision.reasons) or 'policy denied'}")
                continue
            policy_matches.extend(decision.matches)
            score = float(_cost_rank(resource))
            reasons = [f"cost class {resource.cost_class}"]
            if normalized_project and normalized_project.preferred_executors and resource.id in normalized_project.preferred_executors:
                score -= 2
                reasons.append("project preferred executor")
                policy_matches.append("preferred_executor")
            if normalized_project and normalized_project.fallback_executors and resource.id in normalized_project.fallback_executors:
                score += 0.25
                reasons.append("project fallback executor")
            remaining = seconds_until(resource.expires_at)
            if remaining is not None and remaining <= timedelta(days=7).total_seconds():
                score -= 0.5
                reasons.append("expiring capacity")
            if resource.id == "local":
                score += 0.05
                reasons.append("deterministic local shell")
            if resource.provider == "grokbot":
                score += 2
                reasons.append("GrokBot conservation")
            eligible.append((score, resource, decision))
            candidates.append({
                "id": resource.id,
                "executor": resource.executor or resource.name,
                "provider": resource.provider,
                "model": resource.model,
                "cost_class": resource.cost_class,
                "score": round(score, 4),
                "reason": "; ".join(reasons),
                "policy_matches": decision.matches,
            })
        if not eligible:
            reason = "no capable available resource"
            if rejections:
                reason += ": " + "; ".join(rejections[:4])
            decision = RouteDecision(
                executor="unassigned",
                provider="none",
                model="",
                reason=reason,
                fallbacks=[],
                context_pack=None,
                estimated_cost_class="unknown",
                policy_matches=list(dict.fromkeys(policy_matches)),
                candidates=[],
                cache_hit=False,
            )
            if persist:
                self._audit(normalized_task, decision)
            return decision
        eligible.sort(key=lambda item: (item[0], item[1].id))
        candidates.sort(key=lambda item: (item["score"], item["id"]))
        selected_score, selected, selected_policy = eligible[0]
        fallbacks = [
            {"id": resource.id, "executor": resource.executor or resource.name, "provider": resource.provider, "model": resource.model, "cost_class": resource.cost_class}
            for _, resource, _ in eligible[1:]
        ]
        reason_parts = [part for item in candidates if item["id"] == selected.id for part in item["reason"].split("; ")]
        if rejections:
            reason_parts.append("filtered unavailable or policy-incompatible candidates")
        decision = RouteDecision(
            executor=selected.executor or selected.name,
            provider=selected.provider,
            model=selected.model,
            reason="; ".join(dict.fromkeys(reason_parts)),
            fallbacks=fallbacks,
            context_pack=None,
            estimated_cost_class=selected.cost_class,
            policy_matches=list(dict.fromkeys([*selected_policy.matches, *policy_matches])),
            candidates=candidates,
            cache_hit=False,
        )
        if key is not None and self.cache is not None:
            self.cache.put(key, decision.to_dict(), "route", metadata={"task_id": normalized_task.id})
        if persist:
            self._audit(normalized_task, decision)
        return decision

    def route_goal(self, goal: str, project: Project | dict[str, Any] | None = None) -> RouteDecision:
        task = Task(id=f"ephemeral-{uuid.uuid4().hex}", goal=goal, project=project.id if isinstance(project, Project) else project.get("id") if isinstance(project, dict) else None)
        return self.route(task, project=project)
