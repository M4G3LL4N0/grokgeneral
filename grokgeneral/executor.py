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

    def _record_usage(self, task: Task, resource: Any, result: dict[str, Any], execution_id: str | None = None) -> None:
        usage = result.get("usage") if isinstance(result, dict) else None
        if isinstance(usage, dict) and usage.get("total") is not None:
            self.service.usage.record(source="reported", units=usage.get("total"), unit="tokens", cost=usage.get("cost"), project=task.project, resource=resource.id if resource else None, task_id=task.id, execution_id=execution_id, provider=resource.provider if resource else None, model=resource.model if resource else None, input_tokens=usage.get("input"), output_tokens=usage.get("output"), total_tokens=usage.get("total"), cost_class=resource.cost_class if resource else None, status="reported", metadata={"kind": "opencode"})
        else:
            self.service.usage.record(source="unknown", project=task.project, resource=resource.id if resource else None, task_id=task.id, execution_id=execution_id, provider=resource.provider if resource else None, model=resource.model if resource else None, status="unknown", metadata={"kind": "opencode"})

    @staticmethod
    def _snapshot_changed(before: dict[str, Any], after: dict[str, Any] | None) -> bool:
        if after is None:
            return False
        return before.get("head") != after.get("head") or before.get("changes") != after.get("changes")

    def _fail_execution(self, task: Task, receipt_id: str, error: str, adapter_result: dict[str, Any] | None = None, before: dict[str, Any] | None = None, after: dict[str, Any] | None = None, status: str = "failed") -> tuple[dict[str, Any], dict[str, Any]]:
        adapter_result = adapter_result or {}
        snapshot = {"before": before, "after": after, "changed": self._snapshot_changed(before or {}, after)}
        receipt = self.service.executions.complete(receipt_id, {
            "status": status,
            "exit_code": adapter_result.get("exit_code"),
            "summary": adapter_result.get("summary", ""),
            "error": error,
            "raw_events": adapter_result.get("events", []),
            "usage": adapter_result.get("usage"),
            "snapshot": snapshot,
        })
        failed = self.service.tasks.fail(task.id, error, increment_attempt=False)
        return receipt, failed.to_dict()

    def _required_approval_actions(self, task: Task, commands: list[list[str]]) -> set[str]:
        metadata = task.metadata if isinstance(task.metadata, dict) else {}
        actions: set[str] = set()
        if metadata.get("modify") or metadata.get("requires_modify"):
            actions.add("modify")
        command = metadata.get("command")
        if isinstance(command, (list, tuple)) and command and all(isinstance(item, str) for item in command):
            actions.update(self.service.tasks.classify_command_actions(list(command)))
        for key in ("network", "push", "destructive", "spend", "commit", "deploy", "external", "privacy", "dirty-repo"):
            if metadata.get(key) or metadata.get(f"requires_{key.replace('-', '_')}"):
                actions.add(key)
        if commands:
            actions.add("validate")
        return actions

    def run(self, task_id: str, dry_run: bool = False, approvals: Any = None, approval_ids: list[str] | None = None, allow_execution: bool = False, model: str | None = None, timeout: int = 120) -> dict[str, Any]:
        task = self.service.tasks.get(task_id)
        if not all(self.service.tasks.get(dependency).status == "completed" for dependency in task.dependencies):
            raise ValidationError("task dependencies are not complete")
        project = self._project(task)
        commands = self.service.project_validation(project.id)
        supplied = set(approvals or [])
        if approval_ids and not dry_run:
            required = self._required_approval_actions(task, commands)
            if required:
                supplied.update(self.service.approvals.consume(approval_ids, task, task.attempts + 1, required))
        approvals = supplied
        decision = self.service.router.route(task, project=project, approvals=approvals)
        resource = self._resource(decision.to_dict())
        if resource.provider != "opencode":
            raise ProviderUnavailableError("selected resource is not an OpenCode execution path")
        if not self.service.resources.effective(resource)["available"]:
            raise ProviderUnavailableError("selected OpenCode resource is unavailable")
        model_name = model or resource.model
        if not isinstance(model_name, str) or "/" not in model_name:
            raise ValidationError("OpenCode resource has no provider/model identifier")
        try:
            self.permissions.require("inspect", approvals)
        except SafetyBlockedError:
            self.service.events.emit("POLICY_BLOCKED", {"task_id": task.id, "action": "inspect"})
            raise
        context = self.service.context_builder.build(task, project=project, write=not dry_run)
        prompt = json.dumps({"task": task.to_dict(), "context": context.content}, sort_keys=True, ensure_ascii=False)
        prompt = prompt[-16000:]
        if dry_run:
            adapter_result = self.service.adapters.run("opencode", prompt, cwd=project.path, model=model_name, timeout=timeout, dry_run=True)
            return {"dry_run": True, "task": task.to_dict(), "route": decision.to_dict(), "context": context.to_dict(), "adapter": adapter_result, "validation_commands": self.service.project_validation(project.id)}
        if task.metadata.get("modify"):
            try:
                self.permissions.require("modify", approvals)
            except SafetyBlockedError:
                self.service.events.emit("POLICY_BLOCKED", {"task_id": task.id, "action": "modify"})
                raise
        if commands:
            try:
                self.permissions.require("validate", approvals)
            except SafetyBlockedError:
                self.service.events.emit("POLICY_BLOCKED", {"task_id": task.id, "action": "validate"})
                raise
        if not allow_execution:
            raise SafetyBlockedError("OpenCode execution requires explicit execution approval")
        before = RepositorySnapshot.capture(project.path)
        if task.metadata.get("modify") and before["dirty"]:
            raise SafetyBlockedError("project has existing changes; modify execution requires a clean worktree")
        running = self._transition_to_running(task)
        receipt = self.service.executions.start({"task_id": running.id, "project": project.id, "resource": resource.id, "executor": resource.executor, "provider": resource.provider, "model": model_name, "work_key": task.metadata.get("work_key") if isinstance(task.metadata, dict) else None, "context": context.content})
        try:
            adapter_result = self.service.adapters.run("opencode", prompt, cwd=project.path, model=model_name, timeout=timeout, allow_execution=True)
        except Exception as exc:
            self._record_usage(running, resource, {"usage": None}, receipt["id"])
            after = RepositorySnapshot.capture(project.path)
            receipt, failed = self._fail_execution(running, receipt["id"], str(exc), before=before, after=after)
            return {"task": failed, "receipt": receipt, "route": decision.to_dict(), "context": context.to_dict(), "snapshot": {"before": before, "after": after, "changed": self._snapshot_changed(before, after)}, "adapter": {"status": "failed", "error": str(exc), "events": []}}
        self._record_usage(running, resource, adapter_result, receipt["id"])
        after = RepositorySnapshot.capture(project.path)
        snapshot = {"before": before, "after": after, "changed": self._snapshot_changed(before, after)}
        if snapshot["changed"] and not task.metadata.get("modify"):
            receipt, failed = self._fail_execution(running, receipt["id"], "provider changed the repository without modify approval", adapter_result, before, after)
            return {"task": failed, "receipt": receipt, "route": decision.to_dict(), "context": context.to_dict(), "snapshot": snapshot, "adapter": adapter_result}
        if adapter_result.get("status") != "completed":
            receipt, failed = self._fail_execution(running, receipt["id"], adapter_result.get("error") or "OpenCode execution failed", adapter_result, before, after)
            return {"task": failed, "receipt": receipt, "route": decision.to_dict(), "context": context.to_dict(), "snapshot": snapshot, "adapter": adapter_result}
        validating = self.service.tasks.update(running.id, {"status": "validating"})
        self.service.events.emit("TASK_VALIDATING", {"task_id": validating.id, "execution_id": receipt["id"]})
        try:
            validation_result = self.validation.run(project.path, commands, approvals=approvals, timeout=timeout) if commands else {"status": "pending", "changed": False, "exit_code": None, "commands": []}
        except Exception as exc:
            receipt, failed = self._fail_execution(validating, receipt["id"], f"validation failed: {exc}", adapter_result, before, RepositorySnapshot.capture(project.path))
            return {"task": failed, "receipt": receipt, "route": decision.to_dict(), "context": context.to_dict(), "snapshot": snapshot, "adapter": adapter_result}
        final_status = "completed" if validation_result.get("status") == "passed" and not validation_result.get("changed") else "failed"
        receipt = self.service.executions.complete(receipt["id"], {"status": final_status, "exit_code": adapter_result.get("exit_code"), "summary": adapter_result.get("summary", ""), "validation": validation_result, "raw_events": adapter_result.get("events", []), "usage": adapter_result.get("usage"), "snapshot": snapshot, "error": None if final_status == "completed" else "validation did not pass"})
        if final_status == "completed":
            completed = self.service.tasks.complete(validating.id, [{"execution_id": receipt["id"], "summary": adapter_result.get("summary", "")}])
        else:
            completed = self.service.tasks.fail(validating.id, "validation did not pass", increment_attempt=False)
        return {"task": completed.to_dict(), "receipt": receipt, "route": decision.to_dict(), "context": context.to_dict(), "snapshot": snapshot, "adapter": adapter_result, "validation": validation_result}
