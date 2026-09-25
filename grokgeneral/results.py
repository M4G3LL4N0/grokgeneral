from __future__ import annotations

from typing import Any

from .storage import canonical_json, redact

_MAX_RESULT_BYTES = 16384
_MAX_SUMMARY_CHARS = 2048


def _files_changed(before: dict[str, Any] | None, after: dict[str, Any] | None) -> int:
    if not isinstance(after, dict):
        return 0
    changes = after.get("changes")
    if not isinstance(changes, list):
        return 0
    paths = set()
    for item in changes:
        text = str(item)
        path = text[3:].strip() if len(text) >= 3 else text.strip()
        if path:
            paths.add(path)
    return len(paths)


def _executor(receipt: dict[str, Any]) -> str:
    provider = str(receipt.get("provider") or "").strip()
    model = str(receipt.get("model") or "").strip()
    if model:
        return model if "/" in model else f"{provider}/{model}" if provider else model
    return str(receipt.get("executor") or "unknown")


def _test_result(validation: Any) -> str:
    if not isinstance(validation, dict) or not validation:
        return "not-run"
    explicit = validation.get("tests")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()[:128]
    status = str(validation.get("status") or "unknown")
    return {"passed": "passed", "failed": "failed", "timeout": "timeout", "blocked": "blocked"}.get(status, status[:128])


def compact_result(receipt: dict[str, Any], before_snapshot: dict[str, Any] | None = None, after_snapshot: dict[str, Any] | None = None, task: dict[str, Any] | Any | None = None) -> dict[str, Any]:
    receipt = receipt if isinstance(receipt, dict) else {}
    task_data = task.to_dict() if hasattr(task, "to_dict") else (task if isinstance(task, dict) else {})
    metadata = task_data.get("metadata") if isinstance(task_data.get("metadata"), dict) else {}
    validation = receipt.get("validation") or {}
    receipt_status = str(receipt.get("status") or "unknown")
    if receipt_status == "completed" and _test_result(validation) not in {"passed", "not-run"}:
        status = "failed"
    elif receipt_status == "completed":
        status = "success"
    elif receipt_status in {"failed", "timeout", "blocked"}:
        status = receipt_status
    else:
        status = receipt_status
    blockers = metadata.get("blockers", [])
    if not isinstance(blockers, list):
        blockers = []
    blockers = [str(item)[:256] for item in blockers[:20]]
    if receipt.get("error") and status != "success":
        blockers = [*blockers, str(receipt["error"])[:256]]
    next_action = None
    if status == "success":
        next_action = None
    elif status == "failed" and _test_result(validation) == "failed":
        next_action = "review validation failure"
    elif status in {"failed", "timeout", "blocked"}:
        next_action = "review execution result"
    result = {
        "schema_version": "1",
        "status": status,
        "project": receipt.get("project_id") or receipt.get("project") or task_data.get("project"),
        "executor": _executor(receipt),
        "files_changed": _files_changed(before_snapshot, after_snapshot),
        "tests": _test_result(validation),
        "commit": None,
        "blockers": blockers,
        "next_action": next_action,
        "execution_id": receipt.get("id"),
        "task_id": receipt.get("task_id") or task_data.get("id"),
        "summary": str(redact(receipt.get("summary") or ""))[:_MAX_SUMMARY_CHARS],
        "warnings": [],
    }
    if len(canonical_json(result).encode("utf-8")) > _MAX_RESULT_BYTES:
        result["summary"] = result["summary"][:256]
        result["blockers"] = result["blockers"][:5]
        result["warnings"].append("result compacted")
    if len(canonical_json(result).encode("utf-8")) > _MAX_RESULT_BYTES:
        result = {
            "schema_version": "1",
            "status": status,
            "project": result["project"],
            "executor": result["executor"],
            "files_changed": result["files_changed"],
            "tests": result["tests"],
            "commit": None,
            "blockers": [],
            "next_action": result["next_action"],
            "execution_id": result["execution_id"],
            "task_id": result["task_id"],
            "summary": "",
            "warnings": ["result truncated"],
        }
    return result
