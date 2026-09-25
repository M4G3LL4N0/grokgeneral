from __future__ import annotations

from typing import Any

from .storage import canonical_json, redact

_MAX_RESULT_BYTES = 16384
_MAX_SUMMARY_CHARS = 2048
_MAX_CHANGED_PATHS = 50


def _snapshot_state(snapshot: dict[str, Any] | None) -> tuple[dict[str, str], set[tuple[str, str]]] | None:
    """Split a git status snapshot into changed paths with status codes and rename pairs."""
    if not isinstance(snapshot, dict):
        return None
    changes = snapshot.get("changes")
    if not isinstance(changes, list):
        return None
    paths: dict[str, str] = {}
    renames: set[tuple[str, str]] = set()
    for item in changes:
        text = str(item)
        code = text[:2] if len(text) >= 3 and text[2] == " " else "  "
        path = text[3:].strip() if len(text) >= 3 and text[2] == " " else text.strip()
        if not path:
            continue
        if " -> " in path:
            source, destination = (part.strip() for part in path.split(" -> ", 1))
            if source and destination:
                renames.add((source, destination))
            continue
        paths[path] = code.strip() or "M"
    return paths, renames


def _file_attribution(before_snapshot: dict[str, Any] | None, after_snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """Attribute file changes to this execution by diffing before and after snapshots.

    A repository that was already dirty must not make untouched files look like
    execution output, so only the difference between the two snapshots is reported.
    Status codes separate created, deleted, and modified files, and a tracked
    change that merely disappeared is treated as reverted rather than deleted.
    When either snapshot is missing, attribution is unknown rather than guessed.
    """
    before = _snapshot_state(before_snapshot)
    after = _snapshot_state(after_snapshot)
    if before is None or after is None:
        return {"known": False, "preexisting_dirty": None, "changed": None, "added": None, "deleted": None, "count": None}
    before_paths, before_renames = before
    after_paths, after_renames = after
    added: set[str] = set()
    deleted: set[str] = set()
    changed: set[str] = set()
    for path, code in after_paths.items():
        if path in before_paths:
            continue
        if code == "??" or code.startswith("A"):
            added.add(path)
        elif code.startswith("D"):
            deleted.add(path)
        else:
            changed.add(path)
    for path, code in before_paths.items():
        if path in after_paths:
            continue
        if code == "??" or code.startswith("A"):
            deleted.add(path)
    for _source, destination in after_renames - before_renames:
        changed.add(destination)
    return {
        "known": True,
        "preexisting_dirty": bool(before_paths or before_renames),
        "changed": sorted(changed | added | deleted),
        "added": sorted(added),
        "deleted": sorted(deleted),
        "count": len(changed | added | deleted),
    }


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
    approval_needed = None
    attribution = _file_attribution(before_snapshot, after_snapshot)
    warnings: list[str] = []
    if not attribution["known"]:
        warnings.append("file attribution unavailable")
        changed_paths: list[str] | None = None
        added_paths: list[str] | None = None
        deleted_paths: list[str] | None = None
    else:
        changed_paths = attribution["changed"]
        added_paths = attribution["added"]
        deleted_paths = attribution["deleted"]
        if len(changed_paths) > _MAX_CHANGED_PATHS:
            changed_paths = changed_paths[:_MAX_CHANGED_PATHS]
            added_paths = (added_paths or [])[:_MAX_CHANGED_PATHS]
            deleted_paths = (deleted_paths or [])[:_MAX_CHANGED_PATHS]
            warnings.append("file list truncated")
    result = {
        "schema_version": "1",
        "status": status,
        "project": receipt.get("project_id") or receipt.get("project") or task_data.get("project"),
        "executor": _executor(receipt),
        "files_changed": attribution["count"],
        "preexisting_dirty": attribution["preexisting_dirty"],
        "files_changed_by_execution": changed_paths,
        "files_added_by_execution": added_paths,
        "files_deleted_by_execution": deleted_paths,
        "tests": _test_result(validation),
        "commit": None,
        "blockers": blockers,
        "next_action": next_action,
        "execution_id": receipt.get("id"),
        "task_id": receipt.get("task_id") or task_data.get("id"),
        "summary": str(redact(receipt.get("summary") or ""))[:_MAX_SUMMARY_CHARS],
        "warnings": warnings,
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
            "preexisting_dirty": result["preexisting_dirty"],
            "files_changed_by_execution": None,
            "files_added_by_execution": None,
            "files_deleted_by_execution": None,
            "tests": result["tests"],
            "commit": None,
            "blockers": [],
            "next_action": result["next_action"],
            "execution_id": result["execution_id"],
            "task_id": result["task_id"],
            "summary": "",
            "warnings": [*result["warnings"], "result truncated"],
        }
    return result
