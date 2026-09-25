from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from .cache import Cache
from .errors import NotFoundError, ValidationError
from .models import Resource
from .storage import StateStore
from .timeutil import isoformat, is_expired, parse_time, seconds_until, utc_now


def _id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("resource id must be a non-empty string")
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower()).strip("-.") or "resource"


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class ResourceRegistry:
    def __init__(self, state: StateStore, events: Any | None = None, cache: Cache | None = None) -> None:
        self.state = state
        self.events = events
        self.cache = cache
        self.state.initialize()

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.events is not None:
            self.events.emit(event_type, payload)

    def list(self, expiring: bool = False, expired: bool = False) -> list[Resource]:
        resources = [Resource.from_dict(item) for item in self.state.list_records("resources")]
        if expiring:
            resources = [item for item in resources if self.effective(item)["available"] and (seconds_until(item.expires_at) is not None and 0 < seconds_until(item.expires_at) <= 7 * 86400)]
        if expired:
            resources = [item for item in resources if self.effective(item)["expired"]]
        return resources

    def get(self, name: str) -> Resource:
        wanted = str(name).strip().lower()
        for item in self.state.list_records("resources"):
            resource = Resource.from_dict(item)
            if resource.id.lower() == wanted or resource.name.lower() == wanted:
                return resource
        raise NotFoundError(f"resource not found: {name}")

    def _save(self, resource: Resource, action: str) -> Resource:
        with self.state.transaction() as connection:
            self.state.put_record(connection, "resources", resource.to_record())
        self.state.audit(action, "resource", resource.id, {"name": resource.name, "status": resource.availability})
        return resource

    def add(self, data: dict[str, Any]) -> Resource:
        if not isinstance(data, dict):
            raise ValidationError("resource data must be an object")
        resource_id = _id(data.get("id") or data.get("name"))
        name = str(data.get("name") or resource_id).strip()
        if not name:
            raise ValidationError("resource name must be a non-empty string")
        try:
            if self.get(resource_id).id == resource_id:
                raise ValidationError(f"resource already exists: {resource_id}")
        except NotFoundError:
            pass
        values = {"id": resource_id, "name": name, **data}
        resource = Resource.from_dict(values)
        self._save(resource, "resource.added")
        if resource.availability in {"available", "unlimited", "limited"}:
            self._emit("RESOURCE_AVAILABLE", {"resource": resource.id, "expires_at": resource.expires_at})
        return resource

    def update(self, name: str, changes: dict[str, Any]) -> Resource:
        if not isinstance(changes, dict):
            raise ValidationError("resource changes must be an object")
        resource = self.get(name)
        values = resource.to_dict()
        values.update(changes)
        values["id"] = resource.id
        updated = Resource.from_dict(values)
        self._save(updated, "resource.updated")
        if updated.expires_at != resource.expires_at:
            self._emit("RESOURCE_RESET", {"resource": updated.id, "expires_at": updated.expires_at})
        if updated.model != resource.model or updated.executor != resource.executor:
            self._emit("MODEL_CHANGED", {"resource": updated.id, "provider": updated.provider, "model": updated.model, "executor": updated.executor})
        if updated.remaining_capacity is not None and _number(updated.remaining_capacity) == 0:
            self._emit("RESOURCE_EXHAUSTED", {"resource": updated.id})
        return updated

    def expire(self, name: str, at: Any = None) -> Resource:
        resource = self.get(name)
        expires_at = isoformat(utc_now()) if at is None else isoformat(parse_time(at))
        updated = self.update(name, {"expires_at": expires_at, "availability": "unavailable"})
        self._emit("RESOURCE_EXPIRING", {"resource": updated.id, "expires_at": updated.expires_at})
        return updated

    def effective(self, resource: Resource | dict[str, Any]) -> dict[str, Any]:
        value = resource if isinstance(resource, Resource) else Resource.from_dict(resource)
        expired = False
        try:
            expired = is_expired(value.expires_at)
        except ValueError as exc:
            raise ValidationError(f"invalid resource expiration: {value.expires_at}") from exc
        remaining = _number(value.remaining_capacity)
        exhausted = remaining is not None and remaining <= 0
        availability = str(value.availability or "unknown").lower()
        unavailable = availability in {"unavailable", "disabled", "offline", "blocked", "expired", "exhausted"}
        health_bad = str(value.health or "unknown").lower() in {"unhealthy", "disabled", "offline", "failed", "down", "blocked"}
        available = not expired and not exhausted and not unavailable and not health_bad
        reason = "available" if available else "expired" if expired else "exhausted" if exhausted else "unhealthy" if health_bad else availability
        return {
            "available": available,
            "expired": expired,
            "exhausted": exhausted,
            "reason": reason,
            "remaining_capacity": value.remaining_capacity,
            "expires_at": value.expires_at,
        }

    def refresh_expirations(self) -> list[dict[str, Any]]:
        emitted = []
        for resource in self.list():
            effective = self.effective(resource)
            event_type = None
            marker = None
            if effective["expired"]:
                event_type = "RESOURCE_EXPIRING"
                marker = f"expired:{resource.expires_at}"
            elif effective["exhausted"]:
                event_type = "RESOURCE_EXHAUSTED"
                marker = f"exhausted:{resource.remaining_capacity}"
            else:
                remaining = seconds_until(resource.expires_at)
                if remaining is not None and 0 < remaining <= 86400:
                    event_type = "RESOURCE_EXPIRING"
                    marker = f"expiring:{resource.expires_at}"
            if event_type is None or marker is None:
                continue
            metadata = dict(resource.metadata or {})
            notifications = dict(metadata.get("_notifications") or {}) if isinstance(metadata.get("_notifications"), dict) else {}
            if notifications.get(event_type) == marker:
                continue
            notifications[event_type] = marker
            metadata["_notifications"] = notifications
            values = resource.to_dict()
            values["metadata"] = metadata
            updated = Resource.from_dict(values)
            self._save(updated, "resource.event")
            if self.cache is not None:
                self.cache.invalidate(kind="route")
                self.cache.invalidate(kind="opportunity")
            self._emit(event_type, {"resource": updated.id, "expires_at": updated.expires_at, "reason": effective["reason"]})
            emitted.append({"event": event_type, "resource": updated.id})
        return emitted

    def seed_defaults(self) -> Resource:
        try:
            self.get("local")
        except NotFoundError:
            self.add({
                "id": "local",
                "name": "local",
                "provider": "local",
                "executor": "local",
                "model": "shell",
                "cost_class": "free",
                "marginal_cost": 0,
                "availability": "available",
                "health": "healthy",
                "capabilities": ["coding", "repo-analysis", "refactoring", "testing", "local-shell", "read", "build"],
                "metadata": {"seeded_by": "grokgeneral"},
            })
        try:
            return self.get("space-bunny")
        except NotFoundError:
            expires = isoformat(utc_now() + timedelta(days=7))
            return self.add({
                "id": "space-bunny",
                "name": "space-bunny",
                "provider": "opencode",
                "executor": "Space Bunny",
                "model": "opencode/space-bunny-free",
                "cost_class": "free",
                "marginal_cost": 0,
                "prepaid": False,
                "scarce": False,
                "availability": "unlimited",
                "remaining_capacity": "unlimited",
                "expires_at": expires,
                "capabilities": ["coding", "repo-analysis", "refactoring", "testing"],
                "quality_classes": ["capable"],
                "health": "unknown",
                "metadata": {"temporary": True, "seeded_by": "grokgeneral", "model_verified_at": isoformat(utc_now()), "model_source": "opencode models"},
            })
