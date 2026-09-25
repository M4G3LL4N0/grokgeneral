from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .cache import Cache
from .errors import NotFoundError, ValidationError
from .models import PROJECT_KINDS, PROJECT_PRIORITIES, Project
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


class RootRegistry:
    def __init__(self, state: StateStore) -> None:
        self.state = state
        self.state.initialize()

    def list(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.state.list_records("roots") if item.get("enabled", True)]

    def get(self, identifier: str) -> dict[str, Any]:
        wanted = str(identifier).strip().lower()
        for item in self.state.list_records("roots"):
            if str(item.get("id", "")).lower() == wanted or str(item.get("name", "")).lower() == wanted or str(item.get("path", "")).lower() == wanted:
                return dict(item)
        raise NotFoundError(f"root not found: {identifier}")

    def add(self, path: Path | str, name: str | None = None) -> dict[str, Any]:
        root_path = _path(path)
        if not root_path.is_dir():
            raise ValidationError(f"root does not exist or is not a directory: {root_path}")
        root_id = f"root-{hashlib.sha256(str(root_path).encode('utf-8')).hexdigest()[:16]}"
        existing = self.state.get_record("roots", root_id)
        record = {
            "id": root_id,
            "name": name or root_path.name,
            "path": str(root_path),
            "enabled": True,
            "data": {"root_path": str(root_path)},
            "created_at": (existing or {}).get("created_at"),
        }
        with self.state.transaction() as connection:
            self.state.put_record(connection, "roots", record)
        self.state.audit("root.added", "root", root_id, {"path": str(root_path)})
        return self.get(root_id)

    def remove(self, identifier: str) -> bool:
        record = self.get(identifier)
        self.state.delete_record("roots", record["id"])
        self.state.audit("root.removed", "root", record["id"], {"path": record.get("path")})
        return True

    def scan_projects(self, projects: ProjectRegistry) -> list[Project]:
        found: list[Project] = []
        for root in self.list():
            found.extend(projects.scan(root["path"]))
        return found


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

    def list(self, priority: str | None = None, kind: str | None = None) -> list[Project]:
        projects = [Project.from_dict(item) for item in self.state.list_records("projects")]
        if priority is not None:
            projects = [project for project in projects if project.priority == priority or project.priority_tier == priority]
        if kind is not None:
            projects = [project for project in projects if project.kind == kind]
        return projects

    def get(self, identifier: str) -> Project:
        wanted = str(identifier).strip().lower()
        for item in self.state.list_records("projects"):
            project = Project.from_dict(item)
            if project.id.lower() == wanted or project.name.lower() == wanted or (project.canonical_id or "").lower() == wanted or any(alias.lower() == wanted for alias in project.aliases):
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

    def add_alias(self, identifier: str, alias: str) -> Project:
        project = self.get(identifier)
        value = _text(alias, "alias")
        aliases = list(dict.fromkeys([*project.aliases, value]))
        return self.update(project.id, {"aliases": aliases})

    def set_priority(self, identifier: str, priority: str) -> Project:
        if priority not in PROJECT_PRIORITIES:
            raise ValidationError(f"priority must be one of {sorted(PROJECT_PRIORITIES)}")
        return self.update(identifier, {"priority": priority, "priority_tier": priority})

    def set_kind(self, identifier: str, kind: str) -> Project:
        if kind not in PROJECT_KINDS:
            raise ValidationError(f"kind must be one of {sorted(PROJECT_KINDS)}")
        return self.update(identifier, {"kind": kind})

    def _is_candidate(self, path: Path, include_all: bool) -> bool:
        if path.is_symlink() or not path.is_dir() or path.name.startswith(".") or path.name.lower() in _SKIP_NAMES:
            return False
        if include_all:
            return True
        return any((path / marker).exists() for marker in _MARKERS) or any((path / item).is_dir() for item in ("src", "app", "apps", "packages", "cmd", "internal"))

    def _infer_kind(self, path: Path) -> str:
        name = path.name.lower()
        if any(token in name for token in ("website", "web", "site")):
            return "website"
        if any(token in name for token in ("archive", "backup", "legacy")):
            return "archive"
        if any((path / marker).exists() for marker in ("package.json", "pyproject.toml", "go.mod", "Cargo.toml")):
            return "tool"
        return "unknown"

    def _infer_priority(self, path: Path) -> tuple[int, str]:
        try:
            age_days = max(0, (utc_now() - datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)).total_seconds() / 86400)
        except OSError:
            age_days = 3650
        if (path / ".git").exists() and age_days <= 30:
            return 80, "active"
        if (path / ".git").exists() or any((path / marker).exists() for marker in _MARKERS):
            return 60, "maintained"
        return 30, "dormant"

    def scan(self, root: Path | str, include_all: bool = False, root_label: str | None = None, manual: dict[str, Any] | None = None) -> list[Project]:
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
            cache_key = self.cache.key("project-scan", {"root": str(root_path), "root_label": root_label, "include_all": include_all, "manual": manual or {}, "entries": fingerprint})
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
                score, tier = self._infer_priority(entry)
                project = Project(
                    id=existing_id,
                    name=entry.name,
                    path=str(entry),
                    canonical_id=existing_id,
                    kind=self._infer_kind(entry),
                    root=str(root_path),
                    repository=_repository(entry),
                    description=description,
                    status="unknown",
                    priority=score,
                    priority_tier=tier,
                    metadata=metadata,
                    discovered=True,
                    last_activity=_last_activity(entry),
                )
                if manual:
                    project = Project.from_dict({**project.to_dict(), **{key: manual[key] for key in manual if key in {"canonical_id", "aliases", "kind", "priority", "priority_tier", "root", "status"}}})
                self._save(project, "project.discovered")
                self._emit("PROJECT_DISCOVERED", {"project": project.id, "path": project.path})
            else:
                values = existing.to_dict()
                values["discovered"] = True
                values["last_activity"] = _last_activity(entry)
                values["metadata"] = {**values.get("metadata", {}), **metadata}
                if not values.get("description") and description:
                    values["description"] = description
                if not values.get("canonical_id"):
                    values["canonical_id"] = values.get("id", entry.name)
                if values.get("kind") in {None, "unknown"}:
                    values["kind"] = self._infer_kind(entry)
                if values.get("priority_tier") is None and values.get("priority") in {50, "50"}:
                    score, tier = self._infer_priority(entry)
                    values["priority"] = score
                    values["priority_tier"] = tier
                if not values.get("root"):
                    values["root"] = str(root_path)
                if manual:
                    values.update({key: manual[key] for key in manual if key in {"canonical_id", "aliases", "kind", "priority", "priority_tier", "root", "status"}})
                project = Project.from_dict(values)
                self._save(project, "project.refreshed")
            found.append(project)
        if self.cache is not None and cache_key is not None:
            self.cache.put(cache_key, [item.to_dict() for item in found], "project-scan", ttl_seconds=300, metadata={"root": str(root_path)})
        return found

    def reconcile(self, root: Path | str, aliases: dict[str, list[str]] | None = None, manual: dict[str, dict[str, Any]] | None = None) -> list[Project]:
        values = self.scan(root, manual=None)
        alias_map = aliases or {}
        manual_map = manual or {}
        for project in values:
            changes = dict(manual_map.get(project.id, {}))
            if project.id in alias_map:
                changes["aliases"] = list(dict.fromkeys([*project.aliases, *alias_map[project.id]]))
            if changes:
                self.update(project.id, changes)
        return self.list()

    def _find_by_id(self, identifier: str) -> Project | None:
        try:
            return self.get(identifier)
        except NotFoundError:
            return None

    def backlog_candidates(self) -> list[Project]:
        return [project for project in self.list() if project.discovered or project.status not in {"archived", "inactive"}]
