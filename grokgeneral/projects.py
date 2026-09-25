from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .cache import Cache
from .errors import NotFoundError, ValidationError
from .models import Project
from .storage import StateStore
from .timeutil import isoformat, utc_now

_SKIP_NAMES = {"archive", "archives", "backups", "cache", "node_modules", "dist", "build", ".git"}
_MARKERS = {
    ".git",
    "README.md",
    "README",
    "package.json",
    "pyproject.toml",
    "go.mod",
    "Cargo.toml",
    "Makefile",
    "docker-compose.yml",
    "docker-compose.yaml",
}
_REMOTE = re.compile(r"\[remote\s+\"origin\"\](?P<body>.*?)(?=\n\[|\Z)", re.S)
_URL = re.compile(r"^\s*url\s*=\s*(.+?)\s*$", re.M)


def slug(value: str) -> str:
    result = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value).strip().lower()).strip("-.")
    return result or "project"


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string")
    return value.strip()


def _path(value: Any) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValidationError("path must be a non-empty path")
    return Path(value).expanduser().resolve()


def _read_text(path: Path, limit: int = 800) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _repository(path: Path) -> str | None:
    config = path / ".git" / "config"
    if not config.is_file():
        return None
    match = _REMOTE.search(_read_text(config, 10000))
    if not match:
        return None
    url_match = _URL.search(match.group("body"))
    if not url_match:
        return None
    value = url_match.group(1).strip()
    if "://" not in value:
        return value
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except ValueError:
        return None


def _manifest(path: Path) -> list[str]:
    names = []
    for marker in sorted(_MARKERS):
        if marker != ".git" and (path / marker).exists():
            names.append(marker)
    for directory in ("src", "app", "apps", "packages", "cmd", "internal"):
        if (path / directory).is_dir():
            names.append(directory)
    return names


def _last_activity(path: Path) -> str:
    values = []
    try:
        values = [item.stat().st_mtime for item in path.iterdir()]
    except OSError:
        values = []
    return isoformat(utc_now()) if not values else isoformat(datetime.fromtimestamp(max(values), tz=timezone.utc))


class ProjectRegistry:
    def __init__(self, state: StateStore, events: Any | None = None, cache: Cache | None = None) -> None:
        self.state = state
        self.events = events
        self.cache = cache
        self.state.initialize()

    def list(self) -> list[Project]:
        return [Project.from_dict(item) for item in self.state.list_records("projects")]

    def get(self, identifier: str) -> Project:
        wanted = str(identifier).strip().lower()
        for item in self.state.list_records("projects"):
            project = Project.from_dict(item)
            if project.id.lower() == wanted or project.name.lower() == wanted:
                return project
        raise NotFoundError(f"project not found: {identifier}")

    def _by_path(self, path: Path) -> Project | None:
        for item in self.state.list_records("projects", "path=?", (str(path),)):
            return Project.from_dict(item)
        return None

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.events is not None:
            self.events.emit(event_type, payload)

    def _save(self, project: Project, action: str) -> Project:
        with self.state.transaction() as connection:
            self.state.put_record(connection, "projects", project.to_record())
        self.state.audit(action, "project", project.id, {"name": project.name, "path": project.path})
        return project

    def add(self, data: dict[str, Any]) -> Project:
        if not isinstance(data, dict):
            raise ValidationError("project data must be an object")
        project_id = _text(data.get("id") or data.get("name"), "project id")
        name = _text(data.get("name") or project_id, "project name")
        path = _path(data.get("path"))
        if self._by_path(path) is not None:
            raise ValidationError(f"project path already registered: {path}")
        project = Project.from_dict({**data, "id": project_id, "name": name, "path": str(path)})
        self._save(project, "project.added")
        self._emit("PROJECT_DISCOVERED", {"project": project.id, "path": project.path})
        return project

    def update(self, identifier: str, changes: dict[str, Any]) -> Project:
        if not isinstance(changes, dict):
            raise ValidationError("project changes must be an object")
        project = self.get(identifier)
        values = project.to_dict()
        values.update(changes)
        if "path" in changes:
            new_path = _path(changes["path"])
            existing = self._by_path(new_path)
            if existing is not None and existing.id != project.id:
                raise ValidationError(f"project path already registered: {new_path}")
            values["path"] = str(new_path)
        values["id"] = project.id
        updated = Project.from_dict(values)
        self._save(updated, "project.updated")
        if updated.blockers:
            self._emit("PROJECT_BLOCKED", {"project": updated.id, "blockers": updated.blockers})
        if "dependencies" in changes:
            self._emit("DEPENDENCY_CHANGED", {"project": updated.id, "dependencies": updated.dependencies})
        return updated

    def _is_candidate(self, path: Path, include_all: bool) -> bool:
        if path.is_symlink() or not path.is_dir() or path.name.startswith(".") or path.name.lower() in _SKIP_NAMES:
            return False
        if include_all:
            return True
        return any((path / marker).exists() for marker in _MARKERS) or any((path / item).is_dir() for item in ("src", "app", "apps", "packages", "cmd", "internal"))

    def scan(self, root: Path | str, include_all: bool = False) -> list[Project]:
        root_path = _path(root)
        if not root_path.is_dir():
            raise ValidationError(f"startup root does not exist: {root_path}")
        found: list[Project] = []
        try:
            entries = sorted(root_path.iterdir(), key=lambda item: item.name.lower())
        except OSError as exc:
            raise ValidationError(f"unable to scan startup root: {root_path}") from exc
        cache_key = None
        if self.cache is not None:
            fingerprint = []
            for entry in entries:
                try:
                    fingerprint.append({"name": entry.name, "mtime_ns": entry.stat().st_mtime_ns, "kind": "dir" if entry.is_dir() else "file"})
                except OSError:
                    fingerprint.append({"name": entry.name, "kind": "unknown"})
            cache_key = self.cache.key("project-scan", {"root": str(root_path), "include_all": include_all, "entries": fingerprint})
            cached = self.cache.get(cache_key)
            if isinstance(cached, list):
                values = []
                for item in cached:
                    identifier = item.get("id") if isinstance(item, dict) else None
                    current = self._find_by_id(identifier) if identifier else None
                    values.append(current or Project.from_dict(item))
                return values
        for entry in entries:
            if not self._is_candidate(entry, include_all):
                continue
            existing = self._by_path(entry)
            description = _read_text(entry / "README.md") if (entry / "README.md").is_file() else ""
            metadata = {
                "manifest": _manifest(entry),
                "repository_detected": bool(_repository(entry)),
            }
            if existing is None:
                base_id = slug(entry.name)
                existing_id = base_id
                counter = 2
                while self._find_by_id(existing_id) is not None:
                    existing_id = f"{base_id}-{counter}"
                    counter += 1
                project = Project(
                    id=existing_id,
                    name=entry.name,
                    path=str(entry),
                    repository=_repository(entry),
                    description=description,
                    status="unknown",
                    priority=50,
                    metadata=metadata,
                    discovered=True,
                    last_activity=_last_activity(entry),
                )
                self._save(project, "project.discovered")
                self._emit("PROJECT_DISCOVERED", {"project": project.id, "path": project.path})
            else:
                values = existing.to_dict()
                values["discovered"] = True
                values["last_activity"] = _last_activity(entry)
                values["metadata"] = {**values.get("metadata", {}), **metadata}
                if not values.get("description") and description:
                    values["description"] = description
                project = Project.from_dict(values)
                self._save(project, "project.refreshed")
            found.append(project)
        if self.cache is not None and cache_key is not None:
            self.cache.put(cache_key, [item.to_dict() for item in found], "project-scan", ttl_seconds=300, metadata={"root": str(root_path)})
        return found

    def _find_by_id(self, identifier: str) -> Project | None:
        try:
            return self.get(identifier)
        except NotFoundError:
            return None

    def backlog_candidates(self) -> list[Project]:
        return [project for project in self.list() if project.discovered or project.status not in {"archived", "inactive"}]
