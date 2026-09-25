from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


@dataclass
class Project:
    id: str
    name: str
    path: str
    repository: str | None = None
    description: str = ""
    tags: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    status: str = "unknown"
    priority: int = 50
    capabilities_needed: list[str] = field(default_factory=list)
    preferred_executors: list[str] = field(default_factory=list)
    fallback_executors: list[str] = field(default_factory=list)
    active_tasks: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    last_activity: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    discovered: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Project:
        data = _dict(value.get("data"))
        merged = {**data, **{k: v for k, v in value.items() if k != "data"}}
        return cls(
            id=str(merged.get("id", "")),
            name=str(merged.get("name", merged.get("id", ""))),
            path=str(merged.get("path", "")),
            repository=merged.get("repository"),
            description=str(merged.get("description", "")),
            tags=_list(merged.get("tags")),
            domains=_list(merged.get("domains")),
            status=str(merged.get("status", "unknown")),
            priority=int(merged.get("priority", 50)),
            capabilities_needed=_list(merged.get("capabilities_needed")),
            preferred_executors=_list(merged.get("preferred_executors")),
            fallback_executors=_list(merged.get("fallback_executors")),
            active_tasks=_list(merged.get("active_tasks")),
            blockers=_list(merged.get("blockers")),
            dependencies=_list(merged.get("dependencies")),
            last_activity=merged.get("last_activity"),
            metadata=_dict(merged.get("metadata")),
            discovered=bool(merged.get("discovered", False)),
        )

    def to_record(self) -> dict[str, Any]:
        value = self.to_dict()
        return {
            "id": self.id,
            "name": self.name,
            "path": self.path,
            "repository": self.repository,
            "status": self.status,
            "priority": self.priority,
            "last_activity": self.last_activity,
            "data": {key: item for key, item in value.items() if key not in {"id", "name", "path", "repository", "status", "priority"}},
        }


@dataclass
class Resource:
    id: str
    name: str
    provider: str = "unknown"
    executor: str = ""
    model: str = ""
    cost_class: str = "unknown"
    marginal_cost: float | None = None
    prepaid: bool = False
    scarce: bool = False
    availability: str = "unknown"
    remaining_capacity: float | int | str | None = None
    reset_at: str | None = None
    expires_at: str | None = None
    rate_limits: dict[str, Any] = field(default_factory=dict)
    latency_ms: int | None = None
    capabilities: list[str] = field(default_factory=list)
    quality_classes: list[str] = field(default_factory=list)
    context_limit: int | None = None
    restrictions: dict[str, Any] = field(default_factory=dict)
    health: str = "unknown"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Resource:
        data = _dict(value.get("data"))
        merged = {**data, **{k: v for k, v in value.items() if k != "data"}}
        marginal = merged.get("marginal_cost")
        return cls(
            id=str(merged.get("id", "")),
            name=str(merged.get("name", merged.get("id", ""))),
            provider=str(merged.get("provider", "unknown")),
            executor=str(merged.get("executor", merged.get("name", ""))),
            model=str(merged.get("model", "")),
            cost_class=str(merged.get("cost_class", "unknown")),
            marginal_cost=float(marginal) if marginal is not None else None,
            prepaid=bool(merged.get("prepaid", False)),
            scarce=bool(merged.get("scarce", False)),
            availability=str(merged.get("availability", "unknown")),
            remaining_capacity=merged.get("remaining_capacity"),
            reset_at=merged.get("reset_at"),
            expires_at=merged.get("expires_at"),
            rate_limits=_dict(merged.get("rate_limits")),
            latency_ms=int(merged["latency_ms"]) if merged.get("latency_ms") is not None else None,
            capabilities=[str(item) for item in _list(merged.get("capabilities"))],
            quality_classes=[str(item) for item in _list(merged.get("quality_classes"))],
            context_limit=int(merged["context_limit"]) if merged.get("context_limit") is not None else None,
            restrictions=_dict(merged.get("restrictions")),
            health=str(merged.get("health", "unknown")),
            metadata=_dict(merged.get("metadata")),
        )

    def to_record(self) -> dict[str, Any]:
        value = self.to_dict()
        indexed = {"id", "name", "expires_at", "health"}
        return {
            "id": self.id,
            "name": self.name,
            "status": self.availability,
            "expires_at": self.expires_at,
            "health": self.health,
            "data": {key: item for key, item in value.items() if key not in indexed},
        }


@dataclass
class Task:
    id: str
    goal: str
    project: str | None = None
    task_type: str = "general"
    priority: int = 50
    status: str = "proposed"
    required_capabilities: list[str] = field(default_factory=list)
    estimated_effort: float | None = None
    dependencies: list[str] = field(default_factory=list)
    deadline: str | None = None
    context_references: list[str] = field(default_factory=list)
    assigned_executor: str | None = None
    routing_rationale: dict[str, Any] = field(default_factory=dict)
    outputs: list[Any] = field(default_factory=list)
    attempts: int = 0
    created_at: str | None = None
    updated_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Task:
        data = _dict(value.get("data"))
        merged = {**data, **{k: v for k, v in value.items() if k != "data"}}
        effort = merged.get("estimated_effort")
        return cls(
            id=str(merged.get("id", "")),
            goal=str(merged.get("goal", "")),
            project=merged.get("project"),
            task_type=str(merged.get("task_type", "general")),
            priority=int(merged.get("priority", 50)),
            status=str(merged.get("status", "proposed")),
            required_capabilities=[str(item) for item in _list(merged.get("required_capabilities"))],
            estimated_effort=float(effort) if effort is not None else None,
            dependencies=[str(item) for item in _list(merged.get("dependencies"))],
            deadline=merged.get("deadline"),
            context_references=[str(item) for item in _list(merged.get("context_references"))],
            assigned_executor=merged.get("assigned_executor"),
            routing_rationale=_dict(merged.get("routing_rationale")),
            outputs=_list(merged.get("outputs")),
            attempts=int(merged.get("attempts", 0)),
            created_at=merged.get("created_at"),
            updated_at=merged.get("updated_at"),
            metadata=_dict(merged.get("metadata")),
        )

    def to_record(self) -> dict[str, Any]:
        value = self.to_dict()
        indexed = {"id", "project", "status", "priority", "created_at", "updated_at"}
        return {
            "id": self.id,
            "project": self.project,
            "status": self.status,
            "priority": self.priority,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "data": {key: item for key, item in value.items() if key not in indexed},
        }


@dataclass
class PolicyDecision:
    allowed: bool
    requires_approval: bool = False
    matches: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RouteDecision:
    executor: str
    provider: str
    model: str
    reason: str
    fallbacks: list[dict[str, Any]] = field(default_factory=list)
    context_pack: str | None = None
    estimated_cost_class: str = "unknown"
    policy_matches: list[str] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    cache_hit: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Event:
    id: str
    event_type: str
    payload: dict[str, Any]
    created_at: str
    delivered_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class UsageRecord:
    id: str
    project: str | None
    resource: str | None
    task_id: str | None
    source: str
    units: float | None
    cost: float | None
    cost_class: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ContextPack:
    task_id: str
    path: str | None
    content: dict[str, Any]
    approx_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
