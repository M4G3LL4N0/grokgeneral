from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .cache import Cache
from .errors import ValidationError
from .models import ContextPack, Project, Task
from .storage import StateStore, atomic_write_json, canonical_json, redact

_MAX_FILE_BYTES = 16_000
_SAFE_MARKERS = ("README.md", "README", "package.json", "pyproject.toml", "go.mod", "Cargo.toml", "Makefile", "src", "app", "apps", "packages", "cmd", "internal")


class ContextBuilder:
    def __init__(self, state: StateStore, projects: Any, cache: Cache | None = None) -> None:
        self.state = state
        self.projects = projects
        self.cache = cache
        self.state.initialize()

    def _policy(self) -> dict[str, Any]:
        if not self.state.policy_path.exists():
            return {"objective": "safe work", "safety": "conservative"}
        try:
            value = json.loads(self.state.policy_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError("unable to read policy for context pack") from exc
        if not isinstance(value, dict):
            raise ValidationError("policy must be an object")
        return redact(value)

    def _project(self, task: Task, project: Any) -> Project:
        if isinstance(project, Project):
            return project
        identifier = project or task.project
        if not identifier:
            raise ValidationError("context pack requires a project")
        value = self.projects.get(identifier)
        if isinstance(value, Project):
            return value
        if isinstance(value, dict):
            return Project.from_dict(value)
        raise ValidationError(f"project not found: {identifier}")

    def _manifest(self, project: Project) -> list[str]:
        metadata = project.metadata if isinstance(project.metadata, dict) else {}
        existing = metadata.get("manifest")
        if isinstance(existing, list):
            values = [str(item) for item in existing]
        else:
            values = [item for item in _SAFE_MARKERS if (Path(project.path) / item).exists()]
        if project.id not in values:
            values.insert(0, project.id)
        return values

    def _files(self, project: Project, references: list[str], strict: bool = True) -> list[dict[str, Any]]:
        root = Path(project.path).expanduser().resolve()
        files = []
        for reference in references:
            if not isinstance(reference, str) or not reference.strip():
                raise ValidationError("context references must be non-empty strings")
            candidate = Path(reference)
            if candidate.is_absolute() or ".." in candidate.parts:
                raise ValidationError(f"context reference escapes project: {reference}")
            if candidate.name == ".env" or candidate.name.startswith("."):
                raise ValidationError(f"secret or hidden context reference rejected: {reference}")
            resolved = (root / candidate).resolve()
            try:
                relative = resolved.relative_to(root)
            except ValueError as exc:
                raise ValidationError(f"context reference escapes project: {reference}") from exc
            if not resolved.is_file():
                if not strict:
                    continue
                raise ValidationError(f"context reference does not exist: {reference}")
            if resolved.stat().st_size > _MAX_FILE_BYTES:
                raise ValidationError(f"context reference is too large: {reference}")
            text = resolved.read_text(encoding="utf-8", errors="replace")
            files.append({"path": str(relative), "content": redact(text), "bytes": len(text.encode("utf-8"))})
        return files

    def build(self, task: Task, project: Any = None, write: bool = True) -> ContextPack:
        if not isinstance(task, Task):
            task = Task.from_dict(task)
        target = self._project(task, project)
        strict = project is None or (isinstance(project, str) and project == task.project) or (isinstance(project, Project) and project.id == task.project)
        policy = self._policy()
        content = {
            "version": 1,
            "task": redact(task.to_dict()),
            "project": {
                "id": target.id,
                "name": target.name,
                "path": target.path,
                "status": target.status,
                "repository": target.repository,
                "manifest": self._manifest(target),
                "tags": target.tags,
            },
            "policy": policy,
            "files": self._files(target, task.context_references, strict=strict),
            "executor_instructions": [
                "Use only the supplied project context.",
                "Do not access unrelated projects.",
                "Report uncertainty and missing evidence explicitly.",
            ],
        }
        key = None
        if self.cache is not None:
            key = self.cache.key("context", {"task": content["task"], "project": content["project"], "policy": policy, "files": content["files"]})
            cached = self.cache.get(key)
            if isinstance(cached, dict):
                content = cached
            else:
                self.cache.put(key, content, "context", metadata={"task_id": task.id})
        path = None
        if write:
            target_dir = self.state.state_dir / "contexts"
            target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", task.id).strip(".-") or "task"
            path_value = (target_dir / f"{safe_id}.json").resolve()
            try:
                path_value.relative_to(target_dir.resolve())
            except ValueError as exc:
                raise ValidationError("task id cannot produce a contained context path") from exc
            atomic_write_json(path_value, content)
            path = str(path_value)
        approx_bytes = len(canonical_json(content).encode("utf-8"))
        return ContextPack(task_id=task.id, path=path, content=content, approx_bytes=approx_bytes)
