from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .backlog import BacklogInspector
from .errors import ValidationError
from .executions import ExecutionRegistry
from .models import Project
from .projects import ProjectRegistry
from .storage import StateStore
from .timeutil import isoformat, parse_time, utc_now
from .validation import RepositorySnapshot

_OPPORTUNITY_DIGEST_ID = "opportunities"
_DIGEST_LIMIT = 10
_FINDING_KINDS = {
    "failing_build": "build_status",
    "failing_tests": "test_status",
    "ci_failure": "build_status",
    "malformed_manifest": "build_status",
    "incomplete_scripts": "build_status",
}
_UNSCANNED = {
    "last_scanned": None,
    "git_health": "unknown",
    "dirty": None,
    "branch": None,
    "head": None,
    "test_status": "unknown",
    "build_status": "unknown",
    "ci_present": None,
    "blockers": [],
    "backlog_count": 0,
    "recent_execution": None,
    "scan_seconds": None,
    "scan_stats": {},
}


def _age_seconds(value: Any) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, (utc_now() - parse_time(value)).total_seconds())
    except ValueError:
        return None


class StatusReadModel:
    """Persisted read models that make status fast and read-only.

    Scanning is explicit: only scan() touches the filesystem or git. Every other
    method reads previously persisted summaries so status never rescans projects.
    """

    MAX_AGE_SECONDS = 3600

    def __init__(
        self,
        state: StateStore,
        projects: ProjectRegistry,
        backlog: BacklogInspector,
        executions: ExecutionRegistry | None = None,
    ) -> None:
        self.state = state
        self.projects = projects
        self.backlog = backlog
        self.executions = executions
        self.state.initialize()

    def _freshness(self, last_scanned: Any, max_age_seconds: int) -> dict[str, Any]:
        age = _age_seconds(last_scanned)
        if age is None:
            return {"freshness": "never", "stale": True, "age_seconds": None, "max_age_seconds": max_age_seconds}
        stale = age > max_age_seconds
        return {"freshness": "stale" if stale else "fresh", "stale": stale, "age_seconds": age, "max_age_seconds": max_age_seconds}

    def _decorate(self, record: dict[str, Any] | None, project_id: str, max_age_seconds: int) -> dict[str, Any]:
        if not record:
            return {"project": project_id, **dict(_UNSCANNED), **self._freshness(None, max_age_seconds)}
        summary = {key: value for key, value in record.items() if key not in {"id", "project_id", "data", "created_at", "updated_at"}}
        summary["project"] = summary.get("project") or project_id
        summary.update(self._freshness(summary.get("last_scanned"), max_age_seconds))
        return summary

    def summary(self, project_id: str, max_age_seconds: int | None = None) -> dict[str, Any]:
        limit = self.MAX_AGE_SECONDS if max_age_seconds is None else int(max_age_seconds)
        return self._decorate(self.state.get_record("project_health", project_id), project_id, limit)

    def summaries(self, project_ids: list[str] | None = None, max_age_seconds: int | None = None) -> dict[str, dict[str, Any]]:
        limit = self.MAX_AGE_SECONDS if max_age_seconds is None else int(max_age_seconds)
        records = {item.get("id"): item for item in self.state.list_records("project_health")}
        identifiers = project_ids if project_ids is not None else [project.id for project in self.projects.list()]
        return {identifier: self._decorate(records.get(identifier), identifier, limit) for identifier in identifiers}

    def health_overview(self, max_age_seconds: int | None = None) -> dict[str, Any]:
        summaries = self.summaries(max_age_seconds=max_age_seconds)
        values = list(summaries.values())
        scanned = [item for item in values if item["last_scanned"]]
        return {
            "projects": len(values),
            "scanned": len(scanned),
            "never_scanned": len([item for item in values if not item["last_scanned"]]),
            "stale": len([item for item in scanned if item["stale"]]),
            "fresh": len([item for item in scanned if not item["stale"]]),
            "dirty": len([item for item in scanned if item["dirty"] is True]),
            "max_age_seconds": self.MAX_AGE_SECONDS if max_age_seconds is None else int(max_age_seconds),
        }

    def _git_state(self, project: Project) -> dict[str, Any]:
        root = Path(project.path).expanduser()
        if not (root / ".git").exists():
            return {"git_health": "not_a_repo", "dirty": None, "branch": None, "head": None}
        try:
            snapshot = RepositorySnapshot.capture(root)
        except ValidationError:
            return {"git_health": "unknown", "dirty": None, "branch": None, "head": None}
        return {
            "git_health": "dirty" if snapshot["dirty"] else "clean",
            "dirty": bool(snapshot["dirty"]),
            "branch": snapshot.get("branch"),
            "head": snapshot.get("head"),
        }

    def _recent_execution(self, project: Project) -> dict[str, Any] | None:
        if self.executions is None:
            return None
        records = self.executions.list(project=project.id)
        if not records:
            return None
        latest = records[-1]
        return {
            "id": latest.get("id"),
            "task_id": latest.get("task_id"),
            "status": latest.get("status"),
            "executor": latest.get("executor"),
            "ended_at": latest.get("ended_at"),
            "validation": (latest.get("validation") or {}).get("status"),
        }

    def _record_project(self, project: Project, started: float) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        findings = self.backlog.inspect(project)
        stats = dict(getattr(self.backlog, "last_scan_stats", {}) or {})
        metadata = project.metadata if isinstance(project.metadata, dict) else {}
        summary: dict[str, Any] = {
            "project": project.id,
            "last_scanned": isoformat(utc_now()),
            "blockers": [str(item) for item in project.blockers],
            "backlog_count": len(findings),
            "recent_execution": self._recent_execution(project),
            "scan_seconds": round(time.perf_counter() - started, 4),
            "scan_stats": stats,
            **self._git_state(project),
        }
        summary["test_status"] = str(metadata.get("test_status") or "unknown")
        summary["build_status"] = str(metadata.get("build_status") or "unknown")
        summary["ci_present"] = (Path(project.path).expanduser() / ".github" / "workflows").is_dir() or (Path(project.path).expanduser() / ".gitlab-ci.yml").exists()
        for finding in findings:
            field = _FINDING_KINDS.get(str(finding.get("kind")))
            if field and summary[field] == "unknown":
                summary[field] = "failing"
        record = {
            "id": project.id,
            "project_id": project.id,
            "status": "blocked" if project.blockers else project.status,
            "health": summary["git_health"],
            "last_activity": summary["last_scanned"],
            "data": summary,
        }
        with self.state.transaction() as connection:
            self.state.put_record(connection, "project_health", record)
        return summary, findings

    def record_opportunity_digest(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        payload = {
            "generated_at": isoformat(utc_now()),
            "count": len(items),
            "items": list(items)[:_DIGEST_LIMIT],
        }
        with self.state.transaction() as connection:
            self.state.put_record(connection, "status_digest", {"id": _OPPORTUNITY_DIGEST_ID, "kind": "opportunities", "data": payload})
        return payload

    def opportunity_digest(self, max_age_seconds: int | None = None) -> dict[str, Any]:
        limit = self.MAX_AGE_SECONDS if max_age_seconds is None else int(max_age_seconds)
        record = self.state.get_record("status_digest", _OPPORTUNITY_DIGEST_ID)
        if not record:
            return {"status": "never_refreshed", "generated_at": None, "count": 0, "items": []}
        data = record.get("data") if isinstance(record.get("data"), dict) else {}
        age = _age_seconds(data.get("generated_at"))
        stale = age is None or age > limit
        return {
            "status": "stale" if stale else "fresh",
            "generated_at": data.get("generated_at"),
            "age_seconds": age,
            "count": int(data.get("count") or 0),
            "items": list(data.get("items") or []),
        }

    def scan(
        self,
        projects: Any = None,
        force: bool = False,
        max_age_seconds: int | None = None,
        include_opportunities: bool = True,
        opportunity_source: Any = None,
    ) -> dict[str, Any]:
        limit = self.MAX_AGE_SECONDS if max_age_seconds is None else int(max_age_seconds)
        selected = self._projects(projects)
        scanned: list[str] = []
        skipped: list[str] = []
        collected: dict[str, list[dict[str, Any]]] = {}
        for project in selected:
            if not force:
                current = self.summary(project.id, max_age_seconds=limit)
                if current["last_scanned"] and not current["stale"]:
                    skipped.append(project.id)
                    continue
            started = time.perf_counter()
            try:
                _summary, findings = self._record_project(project, started)
            except OSError:
                skipped.append(project.id)
                continue
            collected[project.id] = findings
            scanned.append(project.id)
        digest = None
        if include_opportunities and opportunity_source is not None:
            current = self.opportunity_digest(max_age_seconds=limit)
            if force or current["status"] in {"never_refreshed", "stale"}:
                try:
                    items = opportunity_source(limit=_DIGEST_LIMIT, findings=collected or None)
                except TypeError:
                    items = opportunity_source(limit=_DIGEST_LIMIT)
                except Exception:
                    items = []
                self.record_opportunity_digest(items)
            digest = self.opportunity_digest(max_age_seconds=limit)
        overview = self.health_overview(max_age_seconds=limit)
        return {
            "generated_at": isoformat(utc_now()),
            "scanned": len(scanned),
            "skipped_fresh": len(skipped),
            "scanned_projects": scanned,
            "skipped_projects": skipped,
            "health": overview,
            "opportunity_digest": digest if digest is not None else self.opportunity_digest(max_age_seconds=limit),
        }
    def _projects(self, projects: Any = None) -> list[Project]:
        if projects is None:
            values = self.projects.list()
        elif isinstance(projects, (str, Project)):
            values = [projects if isinstance(projects, Project) else self.projects.get(projects)]
        else:
            values = [self.projects.get(item) if isinstance(item, str) else item for item in projects]
        return sorted(values, key=lambda item: (-item.priority_score, item.id))
