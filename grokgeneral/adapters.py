from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol

from .errors import ProviderUnavailableError, SafetyBlockedError, ValidationError
from .policies import PolicyEngine
from .storage import StateStore, redact
from .timeutil import isoformat, utc_now


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

    def __init__(self, executable: str | None = None, timeout: int = 120) -> None:
        super().__init__(executable=executable)
        self.timeout = timeout
        self._version: str | None = None

    def _run_process(self, command: list[str], timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        resolved = self._resolved()
        if not resolved:
            raise ProviderUnavailableError("opencode executable is unavailable")
        environment = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
        try:
            return subprocess.run([resolved, *command], env=environment, capture_output=True, text=True, timeout=timeout or self.timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(str(exc)) from exc
        except OSError as exc:
            raise ProviderUnavailableError(f"opencode command failed to start: {exc}") from exc

    def health(self) -> dict[str, Any]:
        resolved = self._resolved()
        if not resolved:
            return {"available": False, "executable": self.executable, "version": None, "models_command": "opencode models [provider]", "capabilities": self.capabilities()}
        try:
            result = self._run_process(["--version"], timeout=min(self.timeout, 15))
            version = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else None
            self._version = version
        except (ProviderUnavailableError, TimeoutError):
            version = None
        return {"available": True, "executable": resolved, "version": version, "models_command": "opencode models [provider]", "run_command": "opencode run --pure --format json --dir PROJECT --model PROVIDER/MODEL MESSAGE", "capabilities": self.capabilities()}

    def models(self, provider: str | None = None, refresh: bool = False) -> list[dict[str, str]]:
        command = ["models"]
        if provider:
            command.append(provider)
        if refresh:
            command.append("--refresh")
        result = self._run_process(command, timeout=min(self.timeout, 30))
        if result.returncode != 0:
            raise ProviderUnavailableError(f"opencode models exited with {result.returncode}")
        models = []
        for line in result.stdout.splitlines():
            value = line.strip()
            if not value or " " in value or not re.match(r"^[^/\s]+/[^/\s]+$", value):
                continue
            provider_name, model_name = value.split("/", 1)
            models.append({"id": value, "provider": provider_name, "model": model_name})
        return models

    def _parse_events(self, output: str) -> tuple[list[dict[str, Any]], str, str | None, dict[str, Any] | None]:
        events: list[dict[str, Any]] = []
        text_parts: list[str] = []
        error_message: str | None = None
        usage: dict[str, Any] | None = None
        for line in output.splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(value, dict):
                continue
            event_type = value.get("type")
            part = value.get("part") if isinstance(value.get("part"), dict) else {}
            text = value.get("text") if isinstance(value.get("text"), str) else part.get("text")
            if isinstance(text, str):
                text_parts.append(text)
            error = value.get("error")
            if isinstance(error, dict):
                data = error.get("data") if isinstance(error.get("data"), dict) else {}
                error_message = str(data.get("message") or error.get("name") or "OpenCode error")
            if isinstance(part.get("tokens"), dict):
                usage = dict(part["tokens"])
            if isinstance(part.get("cost"), (int, float)):
                usage = {**(usage or {}), "cost": part["cost"]}
            events.append({
                "type": str(event_type or "unknown"),
                "session_id": value.get("sessionID"),
                "timestamp": value.get("timestamp"),
                "text": text,
                "tokens": part.get("tokens") if isinstance(part.get("tokens"), dict) else None,
                "cost": part.get("cost") if isinstance(part.get("cost"), (int, float)) else None,
            })
        return events, "".join(text_parts).strip(), error_message, usage

    def run(self, request: Any, cwd: str | Path, model: str, timeout: int | float | None = None, dry_run: bool = False, allow_execution: bool = False, **_: Any) -> dict[str, Any]:
        if not isinstance(request, str) or not request.strip():
            raise ValidationError("OpenCode request must be a non-empty message")
        if not isinstance(model, str) or "/" not in model:
            raise ValidationError("OpenCode model must use provider/model format")
        directory = Path(cwd).expanduser().resolve()
        if not directory.is_dir():
            raise ValidationError(f"OpenCode working directory does not exist: {directory}")
        command = [self._resolved() or self.executable, "run", "--pure", "--format", "json", "--dir", str(directory), "--model", model, request]
        started_at = isoformat(utc_now())
        started = time.perf_counter()
        if dry_run:
            return {"adapter": self.name, "status": "dry-run", "dry_run": True, "exit_code": None, "command": redact(command), "model": model, "cwd": str(directory), "started_at": started_at, "ended_at": started_at, "stdout": "", "stderr": "", "events": [], "summary": "", "error": None, "usage": None}
        if not allow_execution:
            raise SafetyBlockedError("OpenCode execution requires explicit adapter approval")
        try:
            result = self._run_process(command[1:], timeout=timeout or self.timeout)
        except TimeoutError as exc:
            ended_at = isoformat(utc_now())
            return {"adapter": self.name, "status": "timeout", "exit_code": None, "command": redact(command), "model": model, "cwd": str(directory), "started_at": started_at, "ended_at": ended_at, "duration_seconds": time.perf_counter() - started, "stdout": "", "stderr": "", "events": [], "summary": "", "error": str(exc), "usage": None}
        events, summary, error_message, usage = self._parse_events(result.stdout)
        status = "completed" if result.returncode == 0 and error_message is None else "failed"
        return {
            "adapter": self.name,
            "status": status,
            "dry_run": False,
            "exit_code": result.returncode,
            "command": redact(command),
            "model": model,
            "cwd": str(directory),
            "started_at": started_at,
            "ended_at": isoformat(utc_now()),
            "duration_seconds": time.perf_counter() - started,
            "stdout": redact(result.stdout)[-100000:],
            "stderr": redact(result.stderr)[-100000:],
            "events": events,
            "summary": summary,
            "error": error_message,
            "usage": usage,
        }


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

    def models(self, name: str = "opencode", provider: str | None = None, refresh: bool = False) -> list[dict[str, str]]:
        adapter = self.get(name)
        if not hasattr(adapter, "models"):
            raise ProviderUnavailableError(f"adapter does not expose models: {name}")
        return adapter.models(provider=provider, refresh=refresh)

    def run(self, name: str, request: Any, **kwargs: Any) -> dict[str, Any]:
        adapter = self.get(name)
        if not hasattr(adapter, "run"):
            raise ProviderUnavailableError(f"adapter does not expose execution: {name}")
        return adapter.run(request, **kwargs)
