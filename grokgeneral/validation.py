from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .errors import SafetyBlockedError, ValidationError
from .models import PolicyDecision
from .policies import PolicyEngine
from .storage import redact
from .timeutil import isoformat, utc_now


class RepositorySnapshot:
    @staticmethod
    def _git(path: Path, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        executable = shutil.which("git")
        if not executable:
            raise ValidationError("git executable is unavailable")
        environment = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
        try:
            return subprocess.run([executable, "-C", str(path), *arguments], env=environment, capture_output=True, text=True, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValidationError(f"git inspection failed: {exc}") from exc

    @classmethod
    def capture(cls, path: str | Path) -> dict[str, Any]:
        root = Path(path).expanduser().resolve()
        if not root.is_dir():
            raise ValidationError(f"repository path does not exist: {root}")
        if not (root / ".git").exists():
            raise ValidationError(f"path is not a git repository: {root}")
        branch_result = cls._git(root, ["branch", "--show-current"])
        head_result = cls._git(root, ["rev-parse", "HEAD"])
        status_result = cls._git(root, ["status", "--porcelain=v1"])
        if status_result.returncode != 0:
            raise ValidationError("git status inspection failed")
        changes = [line for line in status_result.stdout.splitlines() if line.strip()]
        return {
            "path": str(root),
            "branch": branch_result.stdout.strip() or None,
            "head": head_result.stdout.strip() if head_result.returncode == 0 else None,
            "dirty": bool(changes),
            "changes": changes,
            "captured_at": isoformat(utc_now()),
        }


class PermissionPolicy:
    def __init__(self, policies: PolicyEngine) -> None:
        self.policies = policies

    def authorize(self, permission: str, approvals: Any) -> PolicyDecision:
        return self.policies.authorize(permission, approvals, self.policies.load())

    def require(self, permission: str, approvals: Any) -> PolicyDecision:
        decision = self.authorize(permission, approvals)
        if not decision.allowed:
            raise SafetyBlockedError(f"permission {permission} is required: {'; '.join(decision.reasons)}")
        return decision


class ValidationRunner:
    def __init__(self, policies: PolicyEngine, permissions: PermissionPolicy | None = None) -> None:
        self.policies = policies
        self.permissions = permissions or PermissionPolicy(policies)

    def run(self, project: str | Path, commands: list[list[str]], approvals: Any = None, timeout: int = 120) -> dict[str, Any]:
        self.permissions.require("validate", approvals)
        if not isinstance(commands, list) or not commands:
            raise ValidationError("validation requires at least one argv command")
        root = Path(project).expanduser().resolve()
        if not root.is_dir():
            raise ValidationError(f"validation project path does not exist: {root}")
        before = RepositorySnapshot.capture(root)
        results = []
        changed = False
        status = "passed"
        exit_code = 0
        started_at = isoformat(utc_now())
        started = time.perf_counter()
        for command in commands:
            if isinstance(command, str) or not isinstance(command, (list, tuple)) or not command or not all(isinstance(item, str) and item for item in command):
                raise ValidationError("validation commands must be argv arrays")
            argv = list(command)
            try:
                result = subprocess.run(argv, cwd=root, env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}, capture_output=True, text=True, timeout=timeout, check=False)
            except subprocess.TimeoutExpired as exc:
                results.append({"command": redact(argv), "status": "timeout", "exit_code": None, "stdout": "", "stderr": str(exc)})
                status = "timeout"
                exit_code = None
                break
            except OSError as exc:
                results.append({"command": redact(argv), "status": "failed", "exit_code": None, "stdout": "", "stderr": str(exc)})
                status = "failed"
                exit_code = None
                break
            after = RepositorySnapshot.capture(root)
            command_changed = after["head"] != before["head"] or after["changes"] != before["changes"]
            changed = changed or command_changed
            results.append({"command": redact(argv), "status": "completed" if result.returncode == 0 else "failed", "exit_code": result.returncode, "stdout": redact(result.stdout)[-100000:], "stderr": redact(result.stderr)[-100000:]})
            if result.returncode != 0:
                status = "failed"
                exit_code = result.returncode
                break
        if changed and status == "passed":
            status = "failed"
        return {
            "status": status,
            "changed": changed,
            "exit_code": exit_code,
            "before": before,
            "commands": results,
            "started_at": started_at,
            "ended_at": isoformat(utc_now()),
            "duration_seconds": time.perf_counter() - started,
        }
