from __future__ import annotations

import hashlib
import inspect
import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import Any

from .errors import NotFoundError, ValidationError
from .storage import StateStore, redact
from .timeutil import isoformat, utc_now

PROJECT_DISCOVERED = "PROJECT_DISCOVERED"
RESOURCE_AVAILABLE = "RESOURCE_AVAILABLE"
RESOURCE_EXPIRING = "RESOURCE_EXPIRING"
RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"
RESOURCE_RESET = "RESOURCE_RESET"
MODEL_CHANGED = "MODEL_CHANGED"
PROJECT_BLOCKED = "PROJECT_BLOCKED"
PROJECT_COMPLETED = "PROJECT_COMPLETED"
DEPENDENCY_CHANGED = "DEPENDENCY_CHANGED"
SECURITY_ALERT = "SECURITY_ALERT"
TASK_CREATED = "TASK_CREATED"
TASK_UPDATED = "TASK_UPDATED"
TASK_ROUTED = "TASK_ROUTED"
TASK_STARTED = "TASK_STARTED"
TASK_VALIDATING = "TASK_VALIDATING"
TASK_COMPLETED = "TASK_COMPLETED"
TASK_FAILED = "TASK_FAILED"
TASK_CANCELLED = "TASK_CANCELLED"
ROUTE_DECIDED = "ROUTE_DECIDED"
CONTEXT_BUILT = "CONTEXT_BUILT"
USAGE_RECORDED = "USAGE_RECORDED"
CACHE_HIT = "CACHE_HIT"
CACHE_MISS = "CACHE_MISS"
POLICY_BLOCKED = "POLICY_BLOCKED"
OPPORTUNITY_DETECTED = "OPPORTUNITY_DETECTED"

EVENT_TYPES = frozenset(
    {
        PROJECT_DISCOVERED,
        RESOURCE_AVAILABLE,
        RESOURCE_EXPIRING,
        RESOURCE_EXHAUSTED,
        RESOURCE_RESET,
        MODEL_CHANGED,
        PROJECT_BLOCKED,
        PROJECT_COMPLETED,
        DEPENDENCY_CHANGED,
        SECURITY_ALERT,
        TASK_CREATED,
        TASK_UPDATED,
        TASK_ROUTED,
        TASK_STARTED,
        TASK_VALIDATING,
        TASK_COMPLETED,
        TASK_FAILED,
        TASK_CANCELLED,
        ROUTE_DECIDED,
        CONTEXT_BUILT,
        USAGE_RECORDED,
        CACHE_HIT,
        CACHE_MISS,
        POLICY_BLOCKED,
        OPPORTUNITY_DETECTED,
    }
)


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string")
    return value.strip()


def _payload(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValidationError("event payload must be a mapping")
    return redact(dict(value))


def _record_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    payload = record.get("payload")
    if isinstance(payload, dict):
        return redact(payload)
    data = record.get("data")
    if isinstance(data, dict):
        nested = data.get("payload")
        if isinstance(nested, dict):
            return redact(nested)
        return redact(data)
    return {}


def _event_record(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(record.get("id", "")),
        "event_type": str(record.get("event_type") or record.get("type") or ""),
        "payload": _record_payload(record),
        "created_at": record.get("created_at"),
        "delivered_at": record.get("delivered_at"),
    }


def _subscription_data(record: Mapping[str, Any]) -> dict[str, Any]:
    data = record.get("data")
    if not isinstance(data, dict):
        return {"delivered_event_ids": []}
    normalized = dict(data)
    delivered = normalized.get("delivered_event_ids", [])
    if not isinstance(delivered, list):
        delivered = []
    normalized["delivered_event_ids"] = [str(item) for item in delivered]
    return redact(normalized)


def _subscription_record(record: Mapping[str, Any]) -> dict[str, Any]:
    data = _subscription_data(record)
    return {
        "id": str(record.get("id", "")),
        "event_type": str(record.get("event_type") or record.get("type") or ""),
        "name": str(record.get("name") or record.get("id") or ""),
        "action": record.get("action") or "record",
        "enabled": bool(record.get("enabled", True)),
        "data": data,
        "delivered_event_ids": data["delivered_event_ids"],
    }


def _subscription_id(event_type: str, name: str) -> str:
    material = f"{event_type}\x00{name}".encode("utf-8")
    return f"subscription-{hashlib.sha256(material).hexdigest()}"


class EventBus:
    def __init__(self, state: StateStore) -> None:
        self.state = state
        self.state.initialize()
        self._local_actions: dict[str, Callable[[dict[str, Any]], Any]] = {}
        self._last_created_at = None
        self._delivery_lock = threading.RLock()

    def _next_created_at(self) -> str:
        current = utc_now()
        if self._last_created_at is not None and current <= self._last_created_at:
            current = self._last_created_at + timedelta(microseconds=1)
        self._last_created_at = current
        return isoformat(current) or ""

    def emit(
        self,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        event_type = _text(event_type, "event_type")
        safe_payload = _payload(payload)
        record_id = _text(event_id, "event_id") if event_id is not None else f"event-{uuid.uuid4().hex}"
        created_at = self._next_created_at()
        record = {
            "id": record_id,
            "event_type": event_type,
            "data": safe_payload,
            "created_at": created_at,
            "delivered_at": None,
        }
        with self.state.transaction() as connection:
            self.state.put_record(connection, "events", record)
        return {
            "id": record_id,
            "event_type": event_type,
            "payload": safe_payload,
            "created_at": created_at,
            "delivered_at": None,
        }

    def list(self, limit: int | None = None, event_type: str | None = None) -> list[dict[str, Any]]:
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
                raise ValidationError("limit must be a non-negative integer")
        records = self.state.list_records("events")
        events = [_event_record(record) for record in records]
        if event_type is not None:
            wanted = _text(event_type, "event_type")
            events = [event for event in events if event["event_type"] == wanted]
        events.sort(key=lambda event: (str(event.get("created_at") or ""), event["id"]))
        if limit is not None:
            events = events[-limit:] if limit else []
        return events

    def subscribe(
        self,
        event_type: str,
        name: str,
        action: Callable[[dict[str, Any]], Any] | str | None = None,
    ) -> dict[str, Any]:
        event_type = _text(event_type, "event_type")
        name = _text(name, "name")
        if action is None:
            action_name = "record"
        elif callable(action):
            action_name = getattr(action, "__name__", name)
        elif isinstance(action, str):
            action_name = _text(action, "action")
        else:
            raise ValidationError("action must be callable, a string, or None")
        subscription_id = _subscription_id(event_type, name)
        existing = self.state.get_record("subscriptions", subscription_id)
        existing_data = _subscription_data(existing) if existing else {"delivered_event_ids": []}
        record = {
            "id": subscription_id,
            "event_type": event_type,
            "name": name,
            "action": action_name,
            "enabled": True,
            "data": existing_data,
        }
        with self.state.transaction() as connection:
            self.state.put_record(connection, "subscriptions", record)
        if callable(action):
            self._local_actions[subscription_id] = action
        else:
            self._local_actions.pop(subscription_id, None)
        return _subscription_record(self.state.get_record("subscriptions", subscription_id) or record)

    def subscriptions(self, event_type: str | None = None) -> list[dict[str, Any]]:
        records = self.state.list_records("subscriptions")
        result = [_subscription_record(record) for record in records]
        if event_type is not None:
            wanted = _text(event_type, "event_type")
            result = [item for item in result if item["event_type"] == wanted]
        return result

    def unsubscribe(self, subscription_id: str) -> bool:
        subscription_id = _text(subscription_id, "subscription_id")
        with self.state.transaction() as connection:
            cursor = connection.execute("DELETE FROM subscriptions WHERE id=?", (subscription_id,))
        self._local_actions.pop(subscription_id, None)
        return cursor.rowcount > 0

    def _invoke(self, action: Callable[[dict[str, Any]], Any], event: dict[str, Any]) -> Any:
        try:
            signature = inspect.signature(action)
        except (TypeError, ValueError):
            return action(event)
        try:
            signature.bind(event)
        except TypeError:
            return action(event["payload"])
        return action(event)

    def deliver(self, event_id: str) -> dict[str, Any]:
        if isinstance(event_id, Mapping):
            event_id = str(event_id.get("id", ""))
        event_id = _text(event_id, "event_id")
        with self._delivery_lock:
            raw_event = self.state.get_record("events", event_id)
            if raw_event is None:
                raise NotFoundError(f"event not found: {event_id}")
            event = _event_record(raw_event)
            subscriptions = [
                item
                for item in self.subscriptions(event["event_type"])
                if item["enabled"]
            ]
            for subscription in subscriptions:
                delivered_ids = subscription["delivered_event_ids"]
                if event_id in delivered_ids:
                    continue
                action = self._local_actions.get(subscription["id"])
                if action is not None:
                    self._invoke(action, event)
                record = self.state.get_record("subscriptions", subscription["id"])
                if record is None:
                    continue
                data = _subscription_data(record)
                if event_id not in data["delivered_event_ids"]:
                    data["delivered_event_ids"].append(event_id)
                with self.state.transaction() as connection:
                    self.state.put_record(
                        connection,
                        "subscriptions",
                        {
                            "id": subscription["id"],
                            "event_type": subscription["event_type"],
                            "name": subscription["name"],
                            "action": subscription["action"],
                            "enabled": True,
                            "data": data,
                            "created_at": record.get("created_at"),
                        },
                    )
            current = self.state.get_record("events", event_id)
            if current is not None:
                current_subscriptions = [
                    item
                    for item in self.subscriptions(event["event_type"])
                    if item["enabled"]
                ]
                all_delivered = all(
                    event_id in item["delivered_event_ids"]
                    for item in current_subscriptions
                )
                delivered_at = current.get("delivered_at") or isoformat(utc_now())
                if all_delivered and current.get("delivered_at") is None:
                    with self.state.transaction() as connection:
                        self.state.put_record(
                            connection,
                            "events",
                            {
                                "id": event_id,
                                "event_type": event["event_type"],
                                "data": event["payload"],
                                "created_at": current.get("created_at"),
                                "delivered_at": delivered_at,
                            },
                        )
                elif current.get("delivered_at") is not None:
                    delivered_at = current.get("delivered_at")
                else:
                    delivered_at = None
                event["delivered_at"] = delivered_at
            return event
