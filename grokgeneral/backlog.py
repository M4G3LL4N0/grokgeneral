from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .models import Project
from .projects import ProjectRegistry
from .storage import StateStore, canonical_json
from .timeutil import isoformat, utc_now

_TEXT_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb", ".md", ".toml", ".json", ".yaml", ".yml"}
_SECRET_NAMES = {".env", "credentials", "secrets"}
_PRUNED_DIRS = {"node_modules", "dist", "build", "vendor", ".git", "__pycache__", ".next", "target", ".venv", "venv", "coverage", ".cache", "site-packages", "Pods", ".terraform", "bower_components", "jspm_packages"}


class BacklogInspector:
    MAX_FILES = 200
    MAX_ENTRIES = 20000
    MAX_FILE_BYTES = 512 * 1024

    def __init__(self, state: StateStore, projects: ProjectRegistry) -> None:
        self.state = state
        self.projects = projects
        self.last_scan_stats: dict[str, Any] = {}
        self.state.initialize()

    def _project(self, project: Project | str | None) -> Project:
        if isinstance(project, Project):
            return project
        if project is not None:
            return self.projects.get(project)
        values = self.projects.list()
        if not values:
            raise ValidationError("no projects available for inspection")
        return values[0]

    @staticmethod
    def _is_pruned(name: str) -> bool:
        return name.startswith(".") or name in _PRUNED_DIRS

    def _walk_files(self, root: Path, limit: int = MAX_FILES, max_entries: int = MAX_ENTRIES) -> tuple[list[Path], dict[str, Any]]:
        """Collect candidate text files with a hard bound on filesystem work.

        Directory pruning happens in place during os.walk so excluded trees such as
        node_modules or .git are never descended into, and max_entries caps the number
        of file names examined. Both bounds keep scan cost independent of repo size.
        """
        values: list[Path] = []
        entries_visited = 0
        pruned_dirs = 0
        truncated = False
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                kept_dirs = []
                for name in dirnames:
                    if self._is_pruned(name):
                        pruned_dirs += 1
                    else:
                        kept_dirs.append(name)
                dirnames[:] = kept_dirs
                for name in filenames:
                    if entries_visited >= max_entries or len(values) >= limit:
                        truncated = True
                        break
                    entries_visited += 1
                    if name in _SECRET_NAMES or Path(name).suffix.lower() not in _TEXT_SUFFIXES:
                        continue
                    path = Path(dirpath) / name
                    try:
                        if path.is_symlink():
                            continue
                        values.append(path)
                    except OSError:
                        continue
                if truncated:
                    break
        except OSError:
            pass
        return values, {"entries_visited": entries_visited, "pruned_dirs": pruned_dirs, "truncated": truncated, "files_kept": len(values)}

    def _safe_files(self, root: Path, limit: int = MAX_FILES) -> list[Path]:
        files, _stats = self._walk_files(root, limit=limit)
        return files

    def _finding(self, project: Project, kind: str, severity: str, path: str | None, detail: str, line: int | None = None, marker: str | None = None, source: str = "backlog") -> dict[str, Any]:
        observed_at = isoformat(utc_now())
        material = {"project": project.id, "kind": kind, "path": path or "", "line": line, "marker": marker or "", "detail": detail}
        return {
            "project": project.id,
            "kind": kind,
            "severity": severity,
            "path": path,
            "line": line,
            "marker": marker,
            "detail": detail,
            "message": detail,
            "evidence": detail,
            "observed_at": observed_at,
            "source": source,
            "fingerprint": hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest(),
        }

    def inspect(self, project: Project | str | None = None) -> list[dict[str, Any]]:
        target = self._project(project)
        root = Path(target.path).expanduser().resolve()
        if not root.is_dir():
            self.last_scan_stats = {"entries_visited": 0, "pruned_dirs": 0, "truncated": False, "files_kept": 0, "files_scanned": 0}
            return [self._finding(target, "missing_path", "error", None, "project path is missing", source="filesystem")]
        findings: list[dict[str, Any]] = []
        metadata = target.metadata if isinstance(target.metadata, dict) else {}
        for key, kind in (("build_status", "failing_build"), ("test_status", "failing_tests"), ("ci_status", "ci_failure")):
            value = str(metadata.get(key, "")).lower()
            if value in {"failed", "failing", "broken", "unhealthy", "error"}:
                findings.append(self._finding(target, kind, "high", None, f"recorded {key} is {value}", source="project-metadata"))
        for key, kind in (("dependency_issue", "dependency_cleanup"), ("architecture_review", "architecture_review")):
            if metadata.get(key):
                findings.append(self._finding(target, kind, "medium", None, f"project metadata requests {kind.replace('_', ' ')}", source="project-metadata"))
        files, stats = self._walk_files(root)
        files_scanned = 0
        for path in files:
            try:
                if path.stat().st_size > self.MAX_FILE_BYTES:
                    continue
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            files_scanned += 1
            relative = str(path.relative_to(root))
            for line_number, text in enumerate(lines, 1):
                match = re.search(r"\b(TODO|FIXME|XXX)\b", text, re.I)
                if match:
                    findings.append(self._finding(target, "todo", "low", relative, text.strip()[:240], line_number, match.group(1).upper(), "source-marker"))
        test_files = [path for path in files if any(part in {"test", "tests", "__tests__", "spec"} for part in path.relative_to(root).parts) or re.search(r"(?:^|[._-])(?:test|spec)(?:[._-]|$)", path.stem, re.I)]
        if not test_files and (root / "package.json").exists():
            findings.append(self._finding(target, "missing_tests", "medium", "package.json", "no obvious test files were found", source="repository-structure"))
        if not (root / ".github" / "workflows").is_dir() and not (root / ".gitlab-ci.yml").exists():
            findings.append(self._finding(target, "missing_ci", "low", ".github/workflows", "no obvious CI configuration was found", source="repository-structure"))
        if not any((root / name).exists() for name in ("LICENSE", "LICENSE.md", "LICENSE.txt")):
            findings.append(self._finding(target, "missing_license", "low", "LICENSE", "no license file was found", source="repository-structure"))
        package = root / "package.json"
        if package.is_file():
            try:
                import json
                value = json.loads(package.read_text(encoding="utf-8"))
                if isinstance(value, dict) and "scripts" in value and not any(key in value["scripts"] for key in ("build", "test")):
                    findings.append(self._finding(target, "incomplete_scripts", "medium", "package.json", "package scripts do not expose build and test commands", source="manifest"))
            except (OSError, ValueError, TypeError):
                findings.append(self._finding(target, "malformed_manifest", "warning", "package.json", "package.json could not be parsed", source="manifest"))
        docs = [path for path in files if path.suffix.lower() == ".md"]
        if not docs:
            findings.append(self._finding(target, "stale_docs", "low", "README/docs", "no readable project documentation was found", source="repository-structure"))
        self.last_scan_stats = {**stats, "files_scanned": files_scanned}
        return findings

    def propose(self, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        proposals = []
        for finding in findings:
            kind = str(finding.get("kind", "follow-up"))
            project = finding.get("project")
            capability = "testing" if kind in {"missing_tests", "todo"} else "repo-analysis"
            proposals.append({
                "id": f"proposal-{project}-{kind}",
                "project": project,
                "goal": f"Review and address {kind.replace('_', ' ')} for {project}",
                "task_type": "backlog",
                "priority": 60 if finding.get("severity") in {"medium", "error"} else 40,
                "required_capabilities": [capability],
                "metadata": {"source": "backlog-inspector", "finding": finding},
            })
        return proposals
