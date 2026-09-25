from __future__ import annotations

import uuid
from typing import Any

from .errors import ValidationError
from .storage import StateStore, redact
from .timeutil import isoformat, utc_now

_SOURCES = {"measured", "user-entered", "estimated", "unknown"}


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


class UsageLedger:
    def __init__(self, state: StateStore) -> None:
        self.state = state
        self.state.initialize()

    def record(
        self,
        source: str,
        units: Any = None,
        cost: Any = None,
        project: str | None = None,
        resource: str | None = None,
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if source not in _SOURCES:
            raise ValidationError(f"source must be one of {sorted(_SOURCES)}")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValidationError("metadata must be an object")
        unit_value = _number(units, "units")
        cost_value = _number(cost, "cost")
        now = isoformat(utc_now())
        record_id = f"usage-{uuid.uuid4().hex}"
        safe_metadata = redact(metadata or {})
        record = {
            "id": record_id,
            "project_id": project,
            "resource_id": resource,
            "task_id": task_id,
            "source": source,
            "data": {"units": unit_value, "cost": cost_value, "metadata": safe_metadata},
            "created_at": now,
        }
        with self.state.transaction() as connection:
            self.state.put_record(connection, "usage", record)
        return {
            "id": record_id,
            "project": project,
            "project_id": project,
            "resource": resource,
            "resource_id": resource,
            "task_id": task_id,
            "source": source,
            "units": unit_value,
            "cost": cost_value,
            "metadata": safe_metadata,
            "created_at": now,
        }

    def list(self, project: str | None = None, resource: str | None = None, task_id: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        for column, value in (("project_id", project), ("resource_id", resource), ("task_id", task_id)):
            if value is not None:
                clauses.append(f"{column}=?")
                values.append(value)
        records = self.state.list_records("usage", " AND ".join(clauses) if clauses else "", tuple(values))
        result = []
        for record in records:
            data = record.get("data") if isinstance(record.get("data"), dict) else {}
            result.append({
                "id": record.get("id"),
                "project": record.get("project_id"),
                "project_id": record.get("project_id"),
                "resource": record.get("resource_id"),
                "resource_id": record.get("resource_id"),
                "task_id": record.get("task_id"),
                "source": record.get("source"),
                "units": data.get("units"),
                "cost": data.get("cost"),
                "metadata": data.get("metadata", {}),
                "created_at": record.get("created_at"),
            })
        return result

    def summary(self, project: str | None = None, resource: str | None = None, task_id: str | None = None) -> dict[str, Any]:
        records = self.list(project=project, resource=resource, task_id=task_id)
        by_source: dict[str, dict[str, Any]] = {}
        known_units = 0.0
        known_cost = 0.0
        unknown_units = 0
        unknown_cost = 0
        for record in records:
            source = str(record["source"])
            bucket = by_source.setdefault(source, {"count": 0, "units": 0.0, "cost": 0.0})
            bucket["count"] += 1
            if record["units"] is None:
                unknown_units += 1
            else:
                known_units += float(record["units"])
                bucket["units"] += float(record["units"])
            if record["cost"] is None:
                unknown_cost += 1
            else:
                known_cost += float(record["cost"])
                bucket["cost"] += float(record["cost"])
        return {
            "count": len(records),
            "units": known_units if records and unknown_units == 0 else None,
            "cost": known_cost if records and unknown_cost == 0 else None,
            "known_units": known_units,
            "known_cost": known_cost,
            "unknown_units": unknown_units,
            "unknown_cost": unknown_cost,
            "by_source": by_source,
        }
