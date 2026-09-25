from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Protocol

from .errors import ProviderUnavailableError, SafetyBlockedError, ValidationError
from .policies import PolicyEngine
from .storage import StateStore, redact


class Adapter(Protocol):
    name: str

    def capabilities(self) -> list[str]:
        ...

    def health(self) -> dict[str, Any]:
        ...

    def run(self, request: Any, **kwargs: Any) -> dict[str, Any]:
        ...


class LocalShellAdapter:
    name = "local-shell"

    def __init__(self, policies: PolicyEngine | None = None) -> None:
        self.policies = policies

    def capabilities(self) -> list[str]:
        return ["local-shell", "read", "testing", "build", "coding", "repo-analysis"]

    def health(self) -> dict[str, Any]:
        return {"available": True, "executable": shutil.which("sh") or "/bin/sh", "capabilities": self.capabilities()}

    def _action(self, command: list[str]) -> str | None:
        text = " ".join(command).lower()
        if any(item in text for item in ("git push", "push ")):
            return "push"
        if any(item in text for item in ("curl", "wget", "http://", "https://", "git fetch", "git pull", "git clone", "ssh ", "scp ", "npm publish", "pip install", "docker pull", "gh ")):
            return "network"
        if any(item in text for item in ("rm ", "rmdir", "unlink", "reset --hard", "delete ", "mv ", "cp ", "touch ", "mkdir ", "tee ")) or re.search(r"\b(?:rmtree|unlink|remove|rm)\s*\(", text) or re.search(r"open\s*\([^\n]*['\"](?:w|a|x)", text):
            return "destructive"
        if any(item in text for item in ("stripe", "payment", "purchase", "pay ")):
            return "spend"
        return None

    def run(self, request: Any, cwd: str | Path | None = None, timeout: int = 120, approvals: Any = None, **_: Any) -> dict[str, Any]:
        if isinstance(request, str) or not isinstance(request, (list, tuple)) or not request or not all(isinstance(item, str) and item for item in request):
            raise ValidationError("shell adapter requires a non-empty argv list")
        command = list(request)
        action = self._action(command)
        if action:
            if self.policies is None:
                raise SafetyBlockedError(f"shell command requires {action} approval")
            decision = self.policies.authorize(action, approvals or set(), self.policies.load())
            if not decision.allowed:
                raise SafetyBlockedError(f"shell command requires {action} approval")
        directory = Path(cwd).expanduser().resolve() if cwd else None
        if directory is not None and not directory.is_dir():
            raise ValidationError(f"working directory does not exist: {directory}")
        environment = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
        try:
            result = subprocess.run(command, cwd=directory, env=environment, capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProviderUnavailableError(f"local command failed to start: {exc}") from exc
        return {"adapter": self.name, "returncode": result.returncode, "stdout": redact(result.stdout), "stderr": redact(result.stderr), "command": command}


class OptionalProviderAdapter:
    name = "provider"
    capability_names: list[str] = []

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or self.name

    def capabilities(self) -> list[str]:
        return list(self.capability_names)

    def _resolved(self) -> str | None:
        return shutil.which(self.executable)

    def health(self) -> dict[str, Any]:
        resolved = self._resolved()
        return {"available": bool(resolved), "executable": resolved or self.executable, "capabilities": self.capabilities()}

    def run(self, request: Any, **kwargs: Any) -> dict[str, Any]:
        if not self.health()["available"]:
            raise ProviderUnavailableError(f"{self.name} executable is unavailable")
        if not isinstance(request, (list, tuple)) or not request or not all(isinstance(item, str) and item for item in request):
            raise ValidationError(f"{self.name} requires an explicit argv list")
        if not kwargs.get("allow_execution", False):
            raise SafetyBlockedError(f"{self.name} execution requires explicit adapter approval")
        command = [self._resolved() or self.executable, *request]
        cwd = kwargs.get("cwd")
        directory = Path(cwd).expanduser().resolve() if cwd else None
        if directory is not None and not directory.is_dir():
            raise ValidationError(f"working directory does not exist: {directory}")
        timeout = kwargs.get("timeout", 120)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValidationError("timeout must be a positive number")
        environment = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
        try:
            result = subprocess.run(command, cwd=directory, env=environment, capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProviderUnavailableError(f"{self.name} command failed to start: {exc}") from exc
        return {"adapter": self.name, "returncode": result.returncode, "stdout": redact(result.stdout), "stderr": redact(result.stderr), "command": command}


class OpenCodeAdapter(OptionalProviderAdapter):
    name = "opencode"
    capability_names = ["coding", "repo-analysis", "refactoring", "testing"]


class CursorAdapter(OptionalProviderAdapter):
    name = "cursor"
    capability_names = ["coding", "architecture-review"]


class ChatGPTAdapter(OptionalProviderAdapter):
    name = "chatgpt"
    capability_names = ["research", "strategy", "compression"]


class GrokBotAdapter(OptionalProviderAdapter):
    name = "grokbot"
    capability_names = ["orchestration", "research"]


class GitHubAdapter(OptionalProviderAdapter):
    name = "github"
    capability_names = ["read", "repository-metadata"]

    def run(self, request: Any, **kwargs: Any) -> dict[str, Any]:
        if not kwargs.get("allow_network", False):
            raise SafetyBlockedError("GitHub network reads require network approval")
        if not kwargs.get("allow_execution", False):
            raise SafetyBlockedError("GitHub command execution requires explicit adapter approval")
        return super().run(request, **kwargs)


class AdapterRegistry:
    def __init__(self, state: StateStore, policies: PolicyEngine | None = None) -> None:
        self.state = state
        self.local = LocalShellAdapter(policies)
        self.adapters: dict[str, Adapter] = {
            "local-shell": self.local,
            "opencode": OpenCodeAdapter(),
            "cursor": CursorAdapter(),
            "chatgpt": ChatGPTAdapter(),
            "grokbot": GrokBotAdapter(),
            "github": GitHubAdapter(),
        }

    def all(self) -> list[Adapter]:
        return list(self.adapters.values())

    def get(self, name: str) -> Adapter:
        key = str(name).strip().lower()
        if key not in self.adapters:
            raise ProviderUnavailableError(f"unknown adapter: {name}")
        return self.adapters[key]

    def health(self) -> dict[str, dict[str, Any]]:
        return {name: adapter.health() for name, adapter in self.adapters.items()}
