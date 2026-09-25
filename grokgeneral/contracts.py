from __future__ import annotations

from typing import Any

from .errors import ApprovalError, GrokGeneralError, NotFoundError, SafetyBlockedError
from .storage import canonical_json, redact

_MAX_CONTRACT_BYTES = 8192


def _safe(value: Any, key: str | None = None) -> Any:
    if key in {"path", "raw_log_path", "content", "context", "command", "stdout", "stderr", "source"}:
        return "[omitted]"
    if isinstance(value, dict):
        return {str(item): _safe(child, str(item)) for item, child in value.items() if str(item) not in {"path", "raw_log_path", "content", "context", "command", "stdout", "stderr"}}
    if isinstance(value, list):
        return [_safe(item) for item in value[:100]]
    if isinstance(value, str) and value.startswith("/"):
        return "[omitted]"
    return redact(value)


def _envelope(result: Any = None, warnings: list[str] | None = None, error: dict[str, str] | None = None) -> dict[str, Any]:
    response = {"schema_version": "1", "ok": error is None, "result": _safe(result) if error is None else None, "warnings": list(warnings or [])}
    if error is not None:
        response["error"] = {"code": error["code"], "message": error["message"]}
    if len(canonical_json(response).encode("utf-8")) > _MAX_CONTRACT_BYTES:
        response["result"] = {"truncated": True}
        response["warnings"] = [*response["warnings"], "response truncated"]
    return response


class GrokBotContract:
    def __init__(self, service: Any) -> None:
        self.service = service

    def status(self) -> dict[str, Any]:
        return _envelope(self.service.status_snapshot())

    def submit_task(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict) or not isinstance(payload.get("goal"), str) or not payload["goal"].strip():
            return _envelope(error={"code": "INVALID_INPUT", "message": "goal must be a non-empty string"})
        allowed = {"goal", "project", "required_capabilities", "priority", "deadline", "task_type", "metadata"}
        data = {key: value for key, value in payload.items() if key in allowed}
        try:
            task = self.service.add_task(data)
        except GrokGeneralError as exc:
            return _envelope(error={"code": "INVALID_INPUT", "message": str(exc)[:240]})
        return _envelope({"task_id": task.id, "project": task.project, "status": task.status})

    def route_task(self, task_id: str) -> dict[str, Any]:
        try:
            value = self.service.route_task(task_id)
        except NotFoundError:
            return _envelope(error={"code": "NOT_FOUND", "message": "task not found"})
        except GrokGeneralError as exc:
            return _envelope(error={"code": "ROUTE_FAILED", "message": str(exc)[:240]})
        rationale = value.get("routing_rationale") if isinstance(value.get("routing_rationale"), dict) else {}
        return _envelope({"task_id": value.get("id"), "project": value.get("project"), "status": value.get("status"), "executor": value.get("assigned_executor"), "provider": rationale.get("provider"), "model": rationale.get("model"), "reason": rationale.get("reason"), "approval_needed": []})

    def request_execution(self, task_id: str, approval_ids: list[str] | None = None, options: dict[str, Any] | None = None) -> dict[str, Any]:
        approval_ids = list(approval_ids or [])
        options = options if isinstance(options, dict) else {}
        if not approval_ids:
            return _envelope(error={"code": "APPROVAL_REQUIRED", "message": "an explicit approval ID is required"})
        try:
            value = self.service.executor_run(task_id, approvals=options.get("approvals", []), approval_ids=approval_ids, allow_execution=bool(options.get("allow_execution", True)), model=options.get("model"), timeout=int(options.get("timeout", 120)))
        except ApprovalError as exc:
            return _envelope(error={"code": "APPROVAL_REJECTED", "message": str(exc)[:240]})
        except SafetyBlockedError as exc:
            return _envelope(error={"code": "SAFETY_BLOCKED", "message": str(exc)[:240]})
        except NotFoundError:
            return _envelope(error={"code": "NOT_FOUND", "message": "task not found"})
        except GrokGeneralError as exc:
            return _envelope(error={"code": "EXECUTION_FAILED", "message": str(exc)[:240]})
        receipt = value.get("receipt") if isinstance(value, dict) else None
        if isinstance(receipt, dict):
            return _envelope(self.service.compact_result(receipt["id"]))
        return _envelope({"task_id": task_id, "status": "unknown"})

    def result(self, task_id: str | None = None, execution_id: str | None = None) -> dict[str, Any]:
        try:
            if execution_id:
                return _envelope(self.service.compact_result(execution_id))
            if task_id:
                values = self.service.executions_list(task_id=task_id)
                if not values:
                    return _envelope(error={"code": "NOT_FOUND", "message": "no execution found"})
                return _envelope(self.service.compact_result(values[-1]["id"]))
        except NotFoundError:
            return _envelope(error={"code": "NOT_FOUND", "message": "execution not found"})
        except GrokGeneralError as exc:
            return _envelope(error={"code": "RESULT_FAILED", "message": str(exc)[:240]})
        return _envelope(error={"code": "INVALID_INPUT", "message": "task_id or execution_id is required"})

    def pending_approvals(self, limit: int = 20) -> dict[str, Any]:
        try:
            values = self.service.approvals_list(status="pending")[: max(0, min(int(limit), 100))]
        except (TypeError, ValueError):
            return _envelope(error={"code": "INVALID_INPUT", "message": "limit must be numeric"})
        return _envelope([{"id": item.get("id"), "task_id": item.get("task_id"), "project": item.get("project_id"), "actions": item.get("required_actions", []), "status": item.get("status")} for item in values])

    def global_changes(self, limit: int = 20, cursor: str | None = None) -> dict[str, Any]:
        try:
            count = max(0, min(int(limit), 100))
        except (TypeError, ValueError):
            return _envelope(error={"code": "INVALID_INPUT", "message": "limit must be numeric"})
        values = self.service.events.list(limit=count + 20)
        if cursor:
            values = [item for item in values if str(item.get("created_at") or "") > cursor]
        values = values[-count:] if count else []
        return _envelope([{"id": item.get("id"), "event_type": item.get("event_type"), "created_at": item.get("created_at")} for item in values])
