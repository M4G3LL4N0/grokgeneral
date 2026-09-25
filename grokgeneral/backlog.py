from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .models import Project
from .projects import ProjectRegistry
from .storage import StateStore

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

    def inspect(self, project: Project | str | None = None) -> list[dict[str, Any]]:
        target = self._project(project)
        root = Path(target.path).expanduser().resolve()
        findings: list[dict[str, Any]] = []
        if not root.is_dir():
            return [{"project": target.id, "kind": "missing_path", "severity": "error", "evidence": str(root), "message": "project path is missing"}]
        files = self._safe_files(root)
        todo_count = 0
        for path in files:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            todo_count += len(re.findall(r"\b(?:TODO|FIXME|XXX)\b", text, re.I))
        if todo_count:
            findings.append({"project": target.id, "kind": "todo", "severity": "low", "count": todo_count, "evidence": "source markers", "message": f"{todo_count} TODO/FIXME markers found"})
        test_files = [path for path in files if any(part in {"test", "tests", "__tests__", "spec"} for part in path.relative_to(root).parts) or re.search(r"(?:^|[._-])(?:test|spec)(?:[._-]|$)", path.stem, re.I)]
        if not test_files and (root / "package.json").exists():
            findings.append({"project": target.id, "kind": "missing_tests", "severity": "medium", "evidence": "package.json", "message": "no obvious test files were found"})
        if not (root / ".github" / "workflows").is_dir() and not (root / ".gitlab-ci.yml").exists():
            findings.append({"project": target.id, "kind": "missing_ci", "severity": "low", "evidence": ".github/workflows", "message": "no obvious CI configuration was found"})
        if not any((root / name).exists() for name in ("LICENSE", "LICENSE.md", "LICENSE.txt")):
            findings.append({"project": target.id, "kind": "missing_license", "severity": "low", "evidence": "LICENSE", "message": "no license file was found"})
        package = root / "package.json"
        if package.is_file():
            try:
                import json
                value = json.loads(package.read_text(encoding="utf-8"))
                if isinstance(value, dict) and "scripts" in value and not any(key in value["scripts"] for key in ("build", "test")):
                    findings.append({"project": target.id, "kind": "incomplete_scripts", "severity": "medium", "evidence": "package.json scripts", "message": "package scripts do not expose build and test commands"})
            except (OSError, ValueError, TypeError):
                findings.append({"project": target.id, "kind": "malformed_manifest", "severity": "warning", "evidence": "package.json", "message": "package.json could not be parsed"})
        docs = [path for path in files if path.suffix.lower() == ".md"]
        if not docs:
            findings.append({"project": target.id, "kind": "stale_docs", "severity": "low", "evidence": "README/docs", "message": "no readable project documentation was found"})
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
