from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .errors import GrokGeneralError
from .policies import PolicyEngine
from .projects import ProjectRegistry
from .resources import ResourceRegistry
from .storage import StateStore


_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2, "ok": 3}


class Doctor:
    def __init__(self, state: StateStore, projects: ProjectRegistry | None = None, resources: ResourceRegistry | None = None, events: Any | None = None, adapters: Any | None = None, status_model: Any | None = None) -> None:
        self.state = state
        self.projects = projects
        self.resources = resources
        self.events = events
        self.adapters = adapters
        self.status_model = status_model
        self.state.initialize()

    def _diagnostic(self, code: str, severity: str, message: str, remediation: str, **details: Any) -> dict[str, Any]:
        return {"code": code, "severity": severity, "message": message, "remediation": remediation, **details}

    def run(self) -> list[dict[str, Any]]:
        diagnostics: list[dict[str, Any]] = []
        health = self.state.health()
        if not health.get("ok"):
            diagnostics.append(self._diagnostic("state.integrity", "error", "SQLite integrity check failed", "Restore from backup or inspect the state database.", details=health))
        else:
            diagnostics.append(self._diagnostic("state.healthy", "ok", "SQLite state is readable", "No action required.", details=health))
        try:
            policy = PolicyEngine(self.state).load()
            if not policy.get("global_objective"):
                raise ValueError("missing objective")
            diagnostics.append(self._diagnostic("policy.healthy", "ok", "Policy is valid", "No action required."))
        except Exception as exc:
            diagnostics.append(self._diagnostic("policy.invalid", "error", f"Policy could not be loaded: {exc}", "Repair or replace policies.json."))
        try:
            projects = self.projects.list() if self.projects is not None else []
        except Exception as exc:
            projects = []
            diagnostics.append(self._diagnostic("state.malformed_data", "error", f"Project registry data is malformed: {exc}", "Repair or restore the affected project row."))
        paths: dict[str, list[str]] = {}
        for project in projects:
            paths.setdefault(project.path, []).append(project.id)
            if not Path(project.path).exists():
                diagnostics.append(self._diagnostic("project.path_missing", "error", f"Project path is missing: {project.path}", "Update the project path or remove the stale registry entry.", project=project.id))
            elif not Path(project.path).is_dir():
                diagnostics.append(self._diagnostic("project.path_invalid", "error", f"Project path is not a directory: {project.path}", "Update the project path.", project=project.id))
        for path, ids in paths.items():
            if len(ids) > 1:
                diagnostics.append(self._diagnostic("project.duplicate_path", "warning", f"Multiple projects share path {path}", "Assign distinct paths or archive duplicate entries.", projects=ids))
        if self.resources is not None:
            try:
                resources = self.resources.list()
            except Exception as exc:
                resources = []
                diagnostics.append(self._diagnostic("state.malformed_data", "error", f"Resource registry data is malformed: {exc}", "Repair or restore the affected resource row."))
            for resource in resources:
                effective = self.resources.effective(resource)
                if effective["expired"]:
                    diagnostics.append(self._diagnostic("resource.expired", "warning", f"Resource is expired: {resource.id}", "Update its expiration or remove it from active routing.", resource=resource.id, expires_at=resource.expires_at))
                elif effective["exhausted"]:
                    diagnostics.append(self._diagnostic("resource.exhausted", "warning", f"Resource is exhausted: {resource.id}", "Add capacity or mark the resource unavailable.", resource=resource.id))
                if resource.health in {"unhealthy", "offline", "failed"}:
                    diagnostics.append(self._diagnostic("resource.unhealthy", "warning", f"Resource health is {resource.health}: {resource.id}", "Run adapter health checks or update the resource.", resource=resource.id))
        if self.events is not None:
            try:
                subscriptions = self.events.subscriptions()
                pending = [event for event in self.events.list() if event.get("delivered_at") is None] if subscriptions else []
            except Exception as exc:
                subscriptions = []
                diagnostics.append(self._diagnostic("state.malformed_data", "error", f"Event data is malformed: {exc}", "Repair or restore the affected event row."))
            if subscriptions and pending:
                diagnostics.append(self._diagnostic("events.pending", "info", f"{len(pending)} events await local delivery", "Run event delivery through the service facade.", count=len(pending)))
        if self.adapters is not None:
            try:
                adapter_health = self.adapters.health()
                for name, value in adapter_health.items():
                    if not isinstance(value, dict):
                        continue
                    if value.get("available") is False:
                        extra: dict[str, Any] = {}
                        searched = value.get("searched")
                        if searched:
                            extra["searched"] = searched
                        diagnostics.append(self._diagnostic("adapter.unavailable", "info", f"Adapter unavailable: {name}", f"Run 'gg adapter {name} configure PATH' to pin an executable, or install the optional provider. Local fallbacks stay available.", adapter=name, **extra))
                    elif value.get("resolved_via"):
                        diagnostics.append(self._diagnostic("adapter.resolved", "ok", f"Adapter {name} resolved via {value['resolved_via']}", "No action required.", adapter=name, resolved_via=value["resolved_via"]))
            except Exception as exc:
                diagnostics.append(self._diagnostic("adapter.health_failed", "warning", f"Adapter health check failed: {exc}", "Review adapter configuration."))
        if self.status_model is not None:
            try:
                overview = self.status_model.health_overview()
            except Exception as exc:
                overview = None
                diagnostics.append(self._diagnostic("status.read_failed", "warning", f"Project health summaries could not be read: {exc}", "Run 'gg backlog scan' to rebuild them."))
            if isinstance(overview, dict) and overview.get("projects"):
                never_scanned = int(overview.get("never_scanned") or 0)
                stale = int(overview.get("stale") or 0)
                if never_scanned:
                    diagnostics.append(self._diagnostic("status.never_scanned", "info", f"{never_scanned} of {overview['projects']} projects have never been scanned", "Run 'gg backlog scan' to record project health summaries.", count=never_scanned, projects=overview["projects"]))
                if stale:
                    diagnostics.append(self._diagnostic("status.stale", "info", f"{stale} project health summaries are stale", "Run 'gg backlog scan' to refresh them.", count=stale, max_age_seconds=overview.get("max_age_seconds")))
        diagnostics.sort(key=lambda item: (_SEVERITY_ORDER.get(item["severity"], 9), item["code"], item.get("project", ""), item.get("resource", "")))
        return diagnostics
