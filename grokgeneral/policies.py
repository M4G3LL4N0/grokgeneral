from __future__ import annotations

import copy
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping

from .errors import ValidationError
from .models import PolicyDecision, Project, Resource, Task
from .storage import StateStore, atomic_write_json, redact
from .timeutil import is_expired, parse_time


DEFAULT_POLICY: dict[str, Any] = {
    "version": 1,
    "global_objective": "minimize_cost",
    "objective": "minimize_cost",
    "cost_order": ["free", "local", "low", "medium", "high", "grokbot"],
    "preferred_cost_order": ["free", "local", "low", "medium", "high", "grokbot"],
    "routing": {
        "cost_order": ["free", "local", "low", "medium", "high", "grokbot"],
        "expiring_resource_preference": True,
        "cache_first": True,
        "grokbot_role": "orchestration_only",
    },
    "conservation": {
        "free_before_paid": True,
        "grokbot": {
            "mode": "preserve",
            "use_before_paid": True,
            "allow_spend": False,
        },
        "space_bunny": {"use_before_paid": True},
    },
    "capability_mapping": {
        "coding": ["coding"],
        "repo-analysis": ["repo-analysis"],
        "refactoring": ["refactoring", "coding"],
        "testing": ["testing", "coding"],
    },
    "fallback": {
        "enabled": True,
        "order": ["free", "low", "medium", "high"],
    },
    "retry": {
        "max_attempts": 2,
        "backoff": "exponential",
    },
    "concurrency": {
        "default": 1,
        "max": 1,
    },
    "limits": {
        "max_cost_class": "high",
        "max_cost": None,
        "max_context_tokens": None,
    },
    "safety": {
        "read": {"allowed": True, "requires_approval": False},
        "inspect": {"allowed": True, "requires_approval": False},
        "analyze": {"allowed": True, "requires_approval": False},
        "validate": {"allowed": True, "requires_approval": False},
        "modify": {"allowed": True, "requires_approval": True, "approval": "modify"},
        "commit": {"allowed": True, "requires_approval": True, "approval": "commit"},
        "deploy": {"allowed": True, "requires_approval": True, "approval": "deploy"},
        "external": {"allowed": True, "requires_approval": True, "approval": "external"},
        "local-test": {"allowed": True, "requires_approval": False},
        "network": {"allowed": True, "requires_approval": True, "approval": "network"},
        "destructive": {"allowed": True, "requires_approval": True, "approval": "destructive"},
        "push": {"allowed": True, "requires_approval": True, "approval": "push"},
        "spend": {"allowed": True, "requires_approval": True, "approval": "spend"},
        "post": {"allowed": True, "requires_approval": True, "approval": "post"},
        "privacy": {"allowed": True, "requires_approval": True, "approval": "privacy"},
    },
}


_ACTION_ALIASES = {
    "read": "read",
    "inspect": "read",
    "status": "read",
    "analyze": "analyze",
    "analysis": "analyze",
    "local-test": "local-test",
    "test": "local-test",
    "network": "network",
    "networking": "network",
    "internet": "network",
    "web": "network",
    "fetch": "network",
    "destructive": "destructive",
    "delete": "destructive",
    "remove": "destructive",
    "destroy": "destructive",
    "overwrite": "destructive",
    "reset": "destructive",
    "push": "push",
    "git-push": "push",
    "spend": "spend",
    "spending": "spend",
    "pay": "spend",
    "payment": "spend",
    "purchase": "spend",
    "post": "post",
    "publish": "post",
    "upload": "post",
    "privacy": "privacy",
    "private": "privacy",
    "secret": "privacy",
}


def default_policy() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_POLICY)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        if isinstance(result, Mapping):
            return dict(result)
    raise ValidationError("expected an object")


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        data = value.get("data")
        if isinstance(data, Mapping) and name not in value:
            return data.get(name, default)
        return value.get(name, default)
    return getattr(value, name, default)


def _merge_policy(value: Mapping[str, Any]) -> dict[str, Any]:
    merged = default_policy()
    for key, item in value.items():
        if isinstance(item, Mapping) and isinstance(merged.get(key), Mapping):
            nested = dict(merged[key])
            nested.update(copy.deepcopy(dict(item)))
            merged[key] = nested
        else:
            merged[key] = copy.deepcopy(item)
    return merged


def _validate_policy(value: Any, *, require_shape: bool = True) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError("policy must be an object")
    policy = _merge_policy(value)
    version = policy.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValidationError("policy version must be a positive integer")
    objective = policy.get("global_objective")
    if not isinstance(objective, str) or not objective.strip():
        raise ValidationError("policy global_objective must be a non-empty string")
    for key in ("cost_order", "preferred_cost_order"):
        order = policy.get(key)
        if not isinstance(order, list) or not order or not all(isinstance(item, str) and item for item in order):
            raise ValidationError(f"policy {key} must be a non-empty string list")
    if not isinstance(policy.get("conservation"), Mapping):
        raise ValidationError("policy conservation must be an object")
    if not isinstance(policy.get("safety"), Mapping):
        raise ValidationError("policy safety must be an object")
    if not isinstance(policy.get("capability_mapping"), Mapping):
        raise ValidationError("policy capability_mapping must be an object")
    if not isinstance(policy.get("fallback"), Mapping):
        raise ValidationError("policy fallback must be an object")
    if not isinstance(policy.get("retry"), Mapping):
        raise ValidationError("policy retry must be an object")
    if not isinstance(policy.get("concurrency"), Mapping):
        raise ValidationError("policy concurrency must be an object")
    for name, rule in policy["safety"].items():
        if not isinstance(name, str) or not name:
            raise ValidationError("policy safety rule names must be non-empty strings")
        if isinstance(rule, bool):
            continue
        if not isinstance(rule, Mapping):
            raise ValidationError(f"policy safety rule {name} must be an object or boolean")
        if "allowed" in rule and not isinstance(rule["allowed"], bool):
            raise ValidationError(f"policy safety rule {name}.allowed must be boolean")
        approval = rule.get("requires_approval", rule.get("require_approval", False))
        if not isinstance(approval, bool):
            raise ValidationError(f"policy safety rule {name}.requires_approval must be boolean")
    if require_shape:
        for required in ("global_objective", "cost_order", "conservation", "safety"):
            if required not in value:
                raise ValidationError(f"policy is missing {required}")
    return policy


def _normalize_token(value: Any) -> str:
    text = str(value).strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    for prefix in ("approval", "approved", "allow", "allowed", "permit", "permission"):
        if text.startswith(prefix + "-"):
            text = text[len(prefix) + 1 :]
            break
    for suffix in ("-approval", "-approved", "-allowed", "-permit", "-permission"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return text


def _normalise_action(value: Any) -> str:
    text = _normalize_token(value)
    if text in _ACTION_ALIASES:
        return _ACTION_ALIASES[text]
    for token, category in _ACTION_ALIASES.items():
        if token in text.split("-"):
            return category
    if any(part in text.split("-") for part in ("delete", "remove", "destroy", "overwrite")):
        return "destructive"
    if "network" in text or "internet" in text:
        return "network"
    if "push" in text:
        return "push"
    if any(part in text.split("-") for part in ("spend", "pay", "purchase", "cost")):
        return "spend"
    if any(part in text.split("-") for part in ("post", "publish", "upload")):
        return "post"
    return text


def _approvals(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {_normalize_token(value)}
    try:
        return {_normalize_token(item) for item in value if str(item).strip()}
    except TypeError as exc:
        raise ValidationError("approvals must be an iterable of strings") from exc


def _approval_matches(required: Any, approvals: set[str], action: str) -> bool:
    required_values = [required] if isinstance(required, str) else list(required or [])
    normalized = {_normalize_token(item) for item in required_values if str(item).strip()}
    normalized.add(action)
    return bool(normalized.intersection(approvals))


def _rule_for_action(action: str, policy: Mapping[str, Any]) -> tuple[str, Any]:
    safety = policy.get("safety", {})
    normalized = _normalize_token(action)
    if normalized in safety:
        return normalized, safety[normalized]
    for name, rule in safety.items():
        if _normalize_token(name) == normalized:
            return name, rule
    category = _normalise_action(action)
    if category in safety:
        return category, safety[category]
    for name, rule in safety.items():
        if _normalize_token(name) == category:
            return name, rule
    approval_actions = policy.get("approval_actions", [])
    if isinstance(approval_actions, list) and category in {
        _normalize_token(item) for item in approval_actions
    }:
        return category, {"allowed": True, "requires_approval": True, "approval": category}
    return "", None


def _deny_rule(rule: Any) -> bool:
    if isinstance(rule, bool):
        return not rule
    if not isinstance(rule, Mapping):
        return False
    if rule.get("deny") is True or rule.get("blocked") is True:
        return True
    if "allowed" in rule and rule.get("allowed") is False:
        return True
    return False


def _requires_approval(rule: Any) -> bool:
    if not isinstance(rule, Mapping):
        return False
    value = rule.get("requires_approval", rule.get("require_approval", False))
    return bool(value)


def _project_metadata(value: Any) -> dict[str, Any]:
    metadata = _field(value, "metadata", {})
    return dict(metadata) if isinstance(metadata, Mapping) else {}


def _task_metadata(value: Any) -> dict[str, Any]:
    metadata = _field(value, "metadata", {})
    return dict(metadata) if isinstance(metadata, Mapping) else {}


def _task_capabilities(task: Any) -> list[str]:
    value = _field(task, "required_capabilities", None)
    if value is None:
        value = _field(task, "capabilities", [])
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    return [str(item) for item in value if str(item).strip()]


def _resource_capabilities(resource: Any) -> set[str]:
    value = _field(resource, "capabilities", [])
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    return {str(item) for item in value if str(item).strip()}


def _cost_class(resource: Any) -> str:
    value = _field(resource, "cost_class", None)
    if value is None:
        marginal = _field(resource, "marginal_cost", None)
        if marginal is not None:
            try:
                return "free" if float(marginal) <= 0 else "paid"
            except (TypeError, ValueError):
                return "unknown"
        return "unknown"
    return str(value).strip().lower()


def _cost_rank(value: Any, order: list[str]) -> int | None:
    normalized = str(value).strip().lower() if value is not None else ""
    if normalized in order:
        return order.index(normalized)
    aliases = {"zero": "free", "none": "free", "low-cost": "low", "medium-cost": "medium", "high-cost": "high"}
    normalized = aliases.get(normalized, normalized)
    return order.index(normalized) if normalized in order else None


def _numeric(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _first_value(values: list[Any] | Mapping[str, Any], *names: str) -> Any:
    sources = [values] if isinstance(values, Mapping) else values
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        for name in names:
            if name in source and source[name] is not None:
                return source[name]
    return None


def _requested_context(task: Any, project: Any, policy: Mapping[str, Any]) -> int | None:
    task_data = _as_dict(task) if not isinstance(task, Mapping) and task is not None else dict(task or {})
    project_data = _as_dict(project) if project is not None and not isinstance(project, Mapping) else dict(project or {})
    task_metadata = _task_metadata(task)
    project_metadata = _project_metadata(project)
    values = [
        _first_value(task_data, "context_limit", "context_tokens", "estimated_context_tokens"),
        _first_value(task_metadata, "context_limit", "context_tokens", "estimated_context_tokens", "estimated_context"),
        _first_value(project_data, "context_limit", "context_tokens"),
        _first_value(project_metadata, "context_limit", "context_tokens"),
        _first_value(policy.get("limits", {}), "max_context_tokens", "context_limit"),
    ]
    for value in values:
        number = _numeric(value)
        if number is not None and number >= 0:
            return int(number)
    return None


def _network_requested(task: Any, project: Any) -> bool:
    task_data = _as_dict(task) if not isinstance(task, Mapping) and task is not None else dict(task or {})
    project_data = _as_dict(project) if project is not None and not isinstance(project, Mapping) else dict(project or {})
    values = [
        _first_value(task_data, "network_required", "requires_network", "network"),
        _first_value(_task_metadata(task), "network_required", "requires_network", "network"),
        _first_value(project_data, "network_required", "requires_network", "network"),
        _first_value(_project_metadata(project), "network_required", "requires_network", "network"),
    ]
    return any(value is True or str(value).lower() in {"true", "yes", "required"} for value in values)


def _privacy_requested(task: Any) -> bool:
    task_data = _as_dict(task) if not isinstance(task, Mapping) and task is not None else dict(task or {})
    metadata = _task_metadata(task)
    values = [
        _first_value(task_data, "privacy_required", "requires_privacy"),
        _first_value(metadata, "privacy_required", "requires_privacy"),
    ]
    return any(value is True or str(value).lower() in {"true", "yes", "required"} for value in values)


def _project_forbids(project: Any, action: str) -> bool:
    if project is None:
        return False
    data = _as_dict(project) if not isinstance(project, Mapping) else dict(project)
    metadata = _project_metadata(project)
    restrictions = _field(project, "restrictions", {})
    restrictions = dict(restrictions) if isinstance(restrictions, Mapping) else {}
    checks = {
        "network": ("allow_network", "network_allowed", "allow_external"),
        "push": ("allow_push", "push_allowed"),
        "spend": ("allow_spend", "spending_allowed"),
        "post": ("allow_post", "post_allowed", "allow_posting"),
        "privacy": ("allow_privacy",),
    }
    for key in checks.get(action, ()):
        for source in (data, metadata, restrictions):
            if key in source and source[key] is False:
                return True
    return False


def _resource_status(resource: Any) -> tuple[bool, str | None]:
    expiration = _field(resource, "expires_at")
    if expiration not in (None, ""):
        try:
            if is_expired(expiration):
                return False, "expired"
        except ValueError as exc:
            raise ValidationError(f"invalid resource expiration: {expiration}") from exc
    health = str(_field(resource, "health", "unknown") or "unknown").strip().lower()
    if health in {"unhealthy", "disabled", "offline", "failed", "down", "blocked"}:
        return False, "unhealthy"
    availability = str(_field(resource, "availability", "unknown") or "unknown").strip().lower()
    if availability in {
        "expired",
        "exhausted",
        "depleted",
        "unavailable",
        "disabled",
        "offline",
        "paused",
        "blocked",
    }:
        return False, "exhausted" if availability in {"exhausted", "depleted"} else availability
    remaining = _numeric(_field(resource, "remaining_capacity"))
    if remaining is not None and remaining <= 0:
        return False, "exhausted"
    return True, None


class PolicyEngine:
    def __init__(self, state: StateStore | str | Path) -> None:
        self.state = state if isinstance(state, StateStore) else StateStore(state)
        if not self.state._initialized:
            self.state.initialize()

    @property
    def policy_path(self) -> Path:
        return self.state.policy_path

    def load(self) -> dict[str, Any]:
        if not self.state._initialized:
            self.state.initialize()
        if not self.policy_path.exists():
            self.save(default_policy(), _merge_defaults=False)
        try:
            value = json.loads(self.policy_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"unable to load policy from {self.policy_path}") from exc
        policy = _validate_policy(value)
        return redact(policy)

    def save(self, policy: Mapping[str, Any], *, _merge_defaults: bool = True) -> dict[str, Any]:
        if not isinstance(policy, Mapping):
            raise ValidationError("policy must be an object")
        value = _merge_policy(policy) if _merge_defaults else dict(policy)
        validated = _validate_policy(value, require_shape=not _merge_defaults)
        atomic_write_json(self.policy_path, validated)
        return redact(copy.deepcopy(validated))

    def authorize(self, action: str, approvals: Any, policy: Mapping[str, Any]) -> PolicyDecision:
        if not isinstance(action, str) or not action.strip():
            raise ValidationError("action must be a non-empty string")
        normalized_policy = _validate_policy(policy, require_shape=False)
        category = _normalise_action(action)
        supplied = _approvals(approvals)
        name, rule = _rule_for_action(category, normalized_policy)
        if rule is None:
            if category in {"read", "analyze", "local-test"}:
                return PolicyDecision(True, False, ["safety.read"], [])
            return PolicyDecision(
                False,
                True,
                ["safety.unknown"],
                [f"action {category!r} is not explicitly allowed"],
            )
        if _deny_rule(rule):
            return PolicyDecision(False, False, [f"safety.{name}"], [f"action {category!r} is denied by policy"])
        required = _requires_approval(rule)
        if not required:
            return PolicyDecision(True, False, [f"safety.{name}"], [])
        approval_name = rule.get("approval", category) if isinstance(rule, Mapping) else category
        if _approval_matches(approval_name, supplied, category):
            return PolicyDecision(True, True, [f"safety.{name}"], [])
        return PolicyDecision(
            False,
            True,
            [f"safety.{name}"],
            [f"action {category!r} requires explicit approval"],
        )

    def evaluate(
        self,
        task: Task,
        project: Project | None,
        resource: Resource,
        approvals: Any,
    ) -> PolicyDecision:
        policy = self.load()
        supplied = _approvals(approvals)
        if resource is None:
            raise ValidationError("resource is required")
        matches = ["objective.minimize_cost"]
        reasons: list[str] = []
        allowed = True
        requires_approval = False

        required = _task_capabilities(task)
        available = _resource_capabilities(resource)
        mapping = policy["capability_mapping"]
        for capability in required:
            matches.append("capability.required")
            acceptable = mapping.get(capability, [capability])
            if isinstance(acceptable, str):
                acceptable = [acceptable]
            if not isinstance(acceptable, (list, tuple, set)):
                acceptable = [acceptable]
            if not available.intersection(str(item) for item in acceptable):
                allowed = False
                reasons.append(f"resource does not provide required capability {capability!r}")

        cost_class = _cost_class(resource)
        task_data = _as_dict(task) if not isinstance(task, Mapping) and task is not None else dict(task or {})
        task_metadata = _task_metadata(task)
        max_class = _first_value(
            [task_data, task_metadata, policy.get("limits", {})],
            "max_cost_class",
            "cost_ceiling",
            "preferred_cost_class",
        )
        order = policy.get("cost_order", [])
        required_rank = _cost_rank(max_class, order)
        resource_rank = _cost_rank(cost_class, order)
        if max_class is not None and required_rank is not None and resource_rank is not None and resource_rank > required_rank:
            allowed = False
            matches.append("cost.ceiling")
            reasons.append(f"resource cost class {cost_class!r} exceeds {max_class!r}")
        max_cost = _first_value(
            [task_data, task_metadata, policy.get("limits", {})],
            "max_cost",
            "cost_limit",
            "budget",
        )
        resource_cost = _numeric(_field(resource, "marginal_cost"))
        max_cost_number = _numeric(max_cost)
        if max_cost_number is not None:
            if resource_cost is not None and resource_cost > max_cost_number:
                allowed = False
                matches.append("cost.ceiling")
                reasons.append("resource cost exceeds the configured ceiling")
            elif resource_cost is None and max_cost_number <= 0 and cost_class not in {"free", "local"}:
                allowed = False
                matches.append("cost.ceiling")
                reasons.append("resource cost is unknown and the configured ceiling is zero")

        context_limit = _numeric(_field(resource, "context_limit"))
        requested_context = _requested_context(task, project, policy)
        if context_limit is not None and requested_context is not None and requested_context > context_limit:
            allowed = False
            matches.append("context.limit")
            reasons.append("task context requirement exceeds resource context limit")

        usable, reason = _resource_status(resource)
        if not usable:
            allowed = False
            if reason == "expired":
                matches.append("resource.expired")
                reasons.append("resource is expired")
            elif reason == "exhausted":
                matches.append("resource.exhausted")
                reasons.append("resource is exhausted")
            elif reason == "unhealthy":
                matches.append("resource.health")
                reasons.append("resource is unhealthy")
            else:
                matches.append("resource.availability")
                reasons.append(f"resource is not available: {reason}")

        if _network_requested(task, project):
            network_decision = self.authorize("network", supplied, policy)
            if not network_decision.allowed:
                allowed = False
                requires_approval = requires_approval or network_decision.requires_approval
                matches.extend(network_decision.matches)
                reasons.extend(network_decision.reasons)
            elif network_decision.requires_approval:
                requires_approval = True
                matches.extend(network_decision.matches)
        if _project_forbids(project, "network") and _network_requested(task, project):
            allowed = False
            matches.append("privacy.network")
            reasons.append("project policy forbids network access")
        if _privacy_requested(task):
            privacy_decision = self.authorize("privacy", supplied, policy)
            if not privacy_decision.allowed:
                allowed = False
                requires_approval = requires_approval or privacy_decision.requires_approval
                matches.extend(privacy_decision.matches)
                reasons.extend(privacy_decision.reasons)
            elif privacy_decision.requires_approval:
                requires_approval = True
                matches.extend(privacy_decision.matches)

        restrictions = _field(resource, "restrictions", {})
        if isinstance(restrictions, Mapping):
            task_metadata_values = {**task_data, **task_metadata}
            requested_actions = {
                "network": _network_requested(task, project),
                "privacy": _privacy_requested(task),
                "destructive": bool(task_metadata_values.get("destructive") or task_metadata_values.get("requires_destructive")),
                "push": bool(task_metadata_values.get("push") or task_metadata_values.get("requires_push")),
                "spend": bool(task_metadata_values.get("spend") or task_metadata_values.get("requires_spend")),
            }
            for action, requested in requested_actions.items():
                if requested and restrictions.get(action) is False:
                    allowed = False
                    matches.append(f"resource.restriction.{action}")
                    reasons.append(f"resource forbids {action}")
        resource_identity = f"{_field(resource, 'provider', '')} {_field(resource, 'name', '')}".lower()
        if "grokbot" in resource_identity:
            matches.append("grokbot.conserve")
            if task_metadata.get("spend") or task_metadata.get("requires_spend"):
                spend_decision = self.authorize("spend", supplied, policy)
                if not spend_decision.allowed:
                    allowed = False
                    requires_approval = requires_approval or spend_decision.requires_approval
                    matches.extend(spend_decision.matches)
                    reasons.extend(spend_decision.reasons)

        if not available:
            matches.append("resource.capabilities")
        return PolicyDecision(allowed, requires_approval, list(dict.fromkeys(matches)), list(dict.fromkeys(reasons)))
