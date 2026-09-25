from __future__ import annotations

import json
from typing import Any

from .errors import ProviderUnavailableError, SafetyBlockedError, ValidationError
from .models import Task
from .validation import PermissionPolicy, RepositorySnapshot, ValidationRunner


class Executor:
    def __init__(self, service: Any) -> None:
        self.service = service
        self.permissions = PermissionPolicy(service.policies)
        self.validation = ValidationRunner(service.policies, self.permissions)

    def _project(self, task: Task):
        if not task.project:
            raise ValidationError("OpenCode execution requires a project")
        return self.service.projects.get(task.project)

    def _resource(self, decision: dict[str, Any]):
        candidates = decision.get("candidates", [])
        selected = next((item for item in candidates if item.get("executor") == decision.get("executor") and item.get("provider") == decision.get("provider")), None)
        if selected:
            try:
                return self.service.resources.get(selected["id"])
            except Exception:
                pass
        for resource in self.service.resources.list():
            if resource.provider == decision.get("provider") and (resource.executor == decision.get("executor") or resource.name == decision.get("executor")):
                return resource
        raise ProviderUnavailableError("routed resource is not registered")

    def _transition_to_running(self, task: Task) -> Task:
        current = task
        if current.status == "proposed":
            current = self.service.tasks.update(current.id, {"status": "queued"})
        if current.status == "queued":
            current = self.service.tasks.update(current.id, {"status": "routed"})
        if current.status == "routed":
            current = self.service.tasks.update(current.id, {"status": "running", "attempts": current.attempts + 1})
        elif current.status not in {"running"}:
            raise ValidationError(f"task cannot execute from status {current.status}")
        return current

    def _record_usage(self, task: Task, resource: Any, result: dict[str, Any]) -> None:
        usage = result.get("usage")
        if isinstance(usage, dict) and usage.get("total") is not None:
            self.service.usage.record(source="measured", units=usage.get("total"), cost=usage.get("cost"), project=task.project, resource=resource.id if resource else None, task_id=task.id, metadata={"kind": "opencode"})
        else:
            self.service.usage.record(source="unknown", project=task.project, resource=resource.id if resource else None, task_id=task.id, metadata={"kind": "opencode"})

    def run(self, task_id: str, dry_run: bool = False, approvals: Any = None, allow_execution: bool = False, model: str | None = None, timeout: int = 120) -> dict[str, Any]:
        task = self.service.tasks.get(task_id)
        if not all(self.service.tasks.get(dependency).status == "completed" for dependency in task.dependencies):
            raise ValidationError("task dependencies are not complete")
        project = self._project(task)
        decision = self.service.router.route(task, project=project)
        resource = self._resource(decision.to_dict())
        if resource.provider != "opencode":
            raise ProviderUnavailableError("selected resource is not an OpenCode execution path")
        if not self.service.resources.effective(resource)["available"]:
            raise ProviderUnavailableError("selected OpenCode resource is unavailable")
        model_name = model or resource.model
        if not isinstance(model_name, str) or "/" not in model_name:
            raise ValidationError("OpenCode resource has no provider/model identifier")
        self.permissions.require("inspect", approvals)
        context = self.service.context_builder.build(task, project=project, write=not dry_run)
        prompt = json.dumps({"task": task.to_dict(), "context": context.content}, sort_keys=True, ensure_ascii=False)
        prompt = prompt[-16000:]
        if dry_run:
            adapter_result = self.service.adapters.run("opencode", prompt, cwd=project.path, model=model_name, timeout=timeout, dry_run=True)
            return {"dry_run": True, "task": task.to_dict(), "route": decision.to_dict(), "context": context.to_dict(), "adapter": adapter_result, "validation_commands": self.service.project_validation(project.id)}
        self.permissions.require("modify", approvals) if task.metadata.get("modify") else None
        snapshot = RepositorySnapshot.capture(project.path)
        if task.metadata.get("modify") and snapshot["dirty"]:
            raise SafetyBlockedError("project has existing changes; modify execution requires a clean worktree")
        if not allow_execution:
            raise SafetyBlockedError("OpenCode execution requires explicit execution approval")
        running = self._transition_to_running(task)
        receipt = self.service.executions.start({"task_id": running.id, "project": project.id, "resource": resource.id, "executor": resource.executor, "provider": resource.provider, "model": model_name, "context": context.content})
        adapter_result = self.service.adapters.run("opencode", prompt, cwd=project.path, model=model_name, timeout=timeout, allow_execution=True)
        self._record_usage(running, resource, adapter_result)
        if adapter_result.get("status") != "completed":
            receipt = self.service.executions.complete(receipt["id"], {"status": "failed", "exit_code": adapter_result.get("exit_code"), "summary": adapter_result.get("summary", ""), "error": adapter_result.get("error") or "OpenCode execution failed", "raw_events": adapter_result.get("events", []), "usage": adapter_result.get("usage")})
            failed = self.service.tasks.fail(running.id, receipt.get("error") or "OpenCode execution failed", increment_attempt=False)
            return {"task": failed.to_dict(), "receipt": receipt, "route": decision.to_dict(), "context": context.to_dict(), "snapshot": snapshot, "adapter": adapter_result}
        validating = self.service.tasks.update(running.id, {"status": "validating"})
        self.service.events.emit("TASK_VALIDATING", {"task_id": validating.id, "execution_id": receipt["id"]})
        commands = self.service.project_validation(project.id)
        validation_result = self.validation.run(project.path, commands, approvals=set(approvals or set()) | {"validate"}, timeout=timeout) if commands else {"status": "pending", "changed": False, "exit_code": None, "commands": []}
        final_status = "completed" if validation_result.get("status") == "passed" and not validation_result.get("changed") else "failed"
        receipt = self.service.executions.complete(receipt["id"], {"status": final_status, "exit_code": adapter_result.get("exit_code"), "summary": adapter_result.get("summary", ""), "validation": validation_result, "raw_events": adapter_result.get("events", []), "usage": adapter_result.get("usage"), "error": None if final_status == "completed" else "validation did not pass"})
        if final_status == "completed":
            completed = self.service.tasks.complete(validating.id, [{"execution_id": receipt["id"], "summary": adapter_result.get("summary", "")}])
        else:
            completed = self.service.tasks.fail(validating.id, "validation did not pass", increment_attempt=False)
        return {"task": completed.to_dict(), "receipt": receipt, "route": decision.to_dict(), "context": context.to_dict(), "snapshot": snapshot, "adapter": adapter_result, "validation": validation_result}
