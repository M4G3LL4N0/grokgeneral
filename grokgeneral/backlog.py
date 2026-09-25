from __future__ import annotations

import hashlib
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


class BacklogInspector:
    def __init__(self, state: StateStore, projects: ProjectRegistry) -> None:
        self.state = state
        self.projects = projects
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

    def _safe_files(self, root: Path, limit: int = 200) -> list[Path]:
        values: list[Path] = []
        try:
            candidates = root.rglob("*")
            for path in candidates:
                if len(values) >= limit:
                    break
                if not path.is_file() or path.is_symlink():
                    continue
                relative_parts = path.relative_to(root).parts
                if any(part.startswith(".") or part in {"node_modules", "dist", "build", "vendor"} for part in relative_parts):
                    continue
                if path.name in _SECRET_NAMES or path.suffix.lower() not in _TEXT_SUFFIXES:
                    continue
                values.append(path)
        except OSError:
            return values
        return values

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
        files = self._safe_files(root)
        for path in files:
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
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
