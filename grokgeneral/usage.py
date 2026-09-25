from __future__ import annotations

import hashlib
import uuid
from typing import Any

from .errors import ValidationError
from .storage import StateStore, redact
from .timeutil import isoformat, utc_now

_SOURCES = {"measured", "reported", "user-entered", "estimated", "unknown"}
_UNITS = {"tokens", "credits", "seconds", "unknown"}


def _number(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValidationError(f"{field} must be numeric or null")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} must be numeric or null") from exc
    if result < 0:
        raise ValidationError(f"{field} must not be negative")
    return result


def _unit(value: Any) -> str:
    if value is None:
        return "unknown"
    result = str(value).strip().lower()
    if result not in _UNITS:
        raise ValidationError(f"unit must be one of {sorted(_UNITS)}")
    return result


class UsageLedger:
    def __init__(self, state: StateStore) -> None:
        self.state = state
        self.state.initialize()

    @staticmethod
    def _public(record: dict[str, Any]) -> dict[str, Any]:
        data = record.get("data") if isinstance(record.get("data"), dict) else {}
        return {
            "id": record.get("id"),
            "project": record.get("project_id"),
            "project_id": record.get("project_id"),
            "resource": record.get("resource_id"),
            "resource_id": record.get("resource_id"),
            "task_id": record.get("task_id"),
            "execution_id": data.get("execution_id"),
            "provider": data.get("provider"),
            "model": data.get("model"),
            "source": record.get("source"),
            "unit": data.get("unit", "unknown"),
            "units": data.get("units"),
            "cost": data.get("cost"),
            "currency": data.get("currency"),
            "duration_seconds": data.get("duration_seconds"),
            "input_tokens": data.get("input_tokens"),
            "output_tokens": data.get("output_tokens"),
            "total_tokens": data.get("total_tokens"),
            "cost_class": data.get("cost_class"),
            "status": data.get("status"),
            "metadata": data.get("metadata", {}),
            "created_at": record.get("created_at"),
        }

    def record(
        self,
        source: str,
        units: Any = None,
        cost: Any = None,
        project: str | None = None,
        resource: str | None = None,
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        execution_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        unit: str | None = None,
        duration_seconds: Any = None,
        input_tokens: Any = None,
        output_tokens: Any = None,
        total_tokens: Any = None,
        currency: str | None = None,
        cost_class: str | None = None,
        status: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if source not in _SOURCES:
            raise ValidationError(f"source must be one of {sorted(_SOURCES)}")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValidationError("metadata must be an object")
        unit_value = _unit(unit)
        unit_amount = _number(units, "units")
        if unit_value == "tokens" and unit_amount is None:
            unit_amount = _number(total_tokens, "total_tokens")
        duration_value = _number(duration_seconds, "duration_seconds")
        input_value = _number(input_tokens, "input_tokens")
        output_value = _number(output_tokens, "output_tokens")
        total_value = _number(total_tokens, "total_tokens")
        cost_value = _number(cost, "cost")
        key = idempotency_key or (f"execution:{execution_id}:{unit_value}" if execution_id else None)
        record_id = f"usage-{hashlib.sha256(key.encode('utf-8')).hexdigest()}" if key else f"usage-{uuid.uuid4().hex}"
        existing = self.state.get_record("usage", record_id)
        if existing is not None:
            return self._public(existing)
        now = isoformat(utc_now())
        safe_metadata = redact(metadata or {})
        record = {
            "id": record_id,
            "project_id": project,
            "resource_id": resource,
            "task_id": task_id,
            "source": source,
            "data": {
                "units": unit_amount,
                "unit": unit_value,
                "cost": cost_value,
                "currency": currency,
                "execution_id": execution_id,
                "provider": provider,
                "model": model,
                "duration_seconds": duration_value,
                "input_tokens": input_value,
                "output_tokens": output_value,
                "total_tokens": total_value,
                "cost_class": cost_class,
                "status": status,
                "idempotency_key": key,
                "metadata": safe_metadata,
            },
            "created_at": now,
        }
        with self.state.transaction() as connection:
            self.state.put_record(connection, "usage", record)
        return self._public(record)

    def list(self, project: str | None = None, resource: str | None = None, task_id: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        for column, value in (("project_id", project), ("resource_id", resource), ("task_id", task_id)):
            if value is not None:
                clauses.append(f"{column}=?")
                values.append(value)
        records = self.state.list_records("usage", " AND ".join(clauses) if clauses else "", tuple(values))
        return [self._public(record) for record in records]

    @staticmethod
    def _add_record(bucket: dict[str, Any], record: dict[str, Any]) -> None:
        bucket["count"] += 1
        unit = record["unit"]
        unit_bucket = bucket.setdefault("by_unit", {}).setdefault(unit, {"count": 0, "units": 0.0, "known_units": 0.0, "unknown_units": 0, "cost": 0.0, "known_cost": 0.0, "unknown_cost": 0})
        unit_bucket["count"] += 1
        if record["units"] is None:
            unit_bucket["unknown_units"] += 1
        else:
            unit_bucket["known_units"] += float(record["units"])
            unit_bucket["units"] += float(record["units"])
        if record["cost"] is None:
            unit_bucket["unknown_cost"] += 1
        else:
            unit_bucket["known_cost"] += float(record["cost"])
            unit_bucket["cost"] += float(record["cost"])
        bucket["known_units"] = bucket.get("known_units", 0.0) + (float(record["units"]) if record["units"] is not None else 0.0)
        bucket["known_cost"] = bucket.get("known_cost", 0.0) + (float(record["cost"]) if record["cost"] is not None else 0.0)
        bucket["unknown_units"] = bucket.get("unknown_units", 0) + (1 if record["units"] is None else 0)
        bucket["unknown_cost"] = bucket.get("unknown_cost", 0) + (1 if record["cost"] is None else 0)

    def summary(self, project: str | None = None, resource: str | None = None, task_id: str | None = None) -> dict[str, Any]:
        records = self.list(project=project, resource=resource, task_id=task_id)
        by_source: dict[str, dict[str, Any]] = {}
        by_unit: dict[str, dict[str, Any]] = {}
        by_resource: dict[str, dict[str, Any]] = {}
        by_provider: dict[str, dict[str, Any]] = {}
        by_model: dict[str, dict[str, Any]] = {}
        by_project: dict[str, dict[str, Any]] = {}
        for record in records:
            self._add_record(by_source.setdefault(str(record["source"]), {"count": 0}), record)
            self._add_record(by_unit.setdefault(str(record["unit"]), {"count": 0}), record)
            self._add_record(by_resource.setdefault(str(record["resource"] or "unknown"), {"count": 0}), record)
            self._add_record(by_provider.setdefault(str(record["provider"] or "unknown"), {"count": 0}), record)
            self._add_record(by_model.setdefault(str(record["model"] or "unknown"), {"count": 0}), record)
            self._add_record(by_project.setdefault(str(record["project"] or "unknown"), {"count": 0}), record)
        for bucket in [*by_source.values(), *by_unit.values(), *by_resource.values(), *by_provider.values(), *by_model.values(), *by_project.values()]:
            unit_buckets = bucket.get("by_unit", {})
            for unit_bucket in unit_buckets.values():
                unit_bucket["units"] = unit_bucket["known_units"] if unit_bucket["unknown_units"] == 0 else None
                unit_bucket["cost"] = unit_bucket["known_cost"] if unit_bucket["unknown_cost"] == 0 else None
            if len(unit_buckets) == 1:
                only = next(iter(unit_buckets.values()))
                bucket["units"] = only["units"]
                bucket["cost"] = only["cost"]
            else:
                bucket["units"] = None
                bucket["cost"] = None
        unit_names = set(by_unit)
        total_units = None
        total_cost = None
        if len(unit_names) == 1:
            only = next(iter(by_unit.values()))
            if only.get("unknown_units", 0) == 0:
                total_units = only.get("known_units", 0.0)
            if only.get("unknown_cost", 0) == 0:
                total_cost = only.get("known_cost", 0.0)
        return {
            "count": len(records),
            "units": total_units,
            "cost": total_cost,
            "known_units": sum(float(record["units"]) for record in records if record["units"] is not None),
            "known_cost": sum(float(record["cost"]) for record in records if record["cost"] is not None),
            "unknown_units": sum(1 for record in records if record["units"] is None),
            "unknown_cost": sum(1 for record in records if record["cost"] is None),
            "by_source": by_source,
            "by_unit": by_unit,
            "by_resource": by_resource,
            "by_provider": by_provider,
            "by_model": by_model,
            "by_project": by_project,
        }
