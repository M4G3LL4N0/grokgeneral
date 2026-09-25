from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .errors import GrokGeneralError, ValidationError
from .events import EVENT_TYPES
from .service import GrokGeneral
from .timeutil import isoformat, parse_duration, parse_time, utc_now


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(value.to_dict() if hasattr(value, "to_dict") else asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _emit(value: Any, json_output: bool, human: str | None = None) -> None:
    if json_output:
        print(json.dumps(_jsonable(value), sort_keys=True, ensure_ascii=False, default=str))
        return
    if human is not None:
        print(human)
        return
    if isinstance(value, list):
        if not value:
            print("none")
            return
        for item in value:
            if isinstance(item, dict):
                if "code" in item and "severity" in item:
                    print(f"{item['severity']}: {item['code']}: {item.get('message', '')}")
                    continue
                identifier = item.get("id") or item.get("name") or item.get("resource") or "item"
                detail = item.get("path") or item.get("goal") or item.get("provider") or item.get("status") or item.get("answer") or ""
                print(f"{identifier}: {detail}")
            else:
                print(item)
        return
    if isinstance(value, dict):
        if {"projects", "tasks", "resources", "health"}.issubset(value):
            print(f"Projects: {value['projects']}  Tasks: {value['tasks']}  Resources: {value['resources']}")
            print(f"Expiring: {len(value.get('expiring_resources', []))}  Opportunities: {len(value.get('opportunities', []))}  Healthy: {value['health'].get('ok')}")
            for action in value.get("next_actions", [])[:3]:
                print(f"Next: {action.get('action', '')}")
            return
        if "executor" in value and "provider" in value:
            print(f"{value['executor']} via {value['provider']} ({value.get('estimated_cost_class', 'unknown')}): {value.get('reason', '')}")
            return
        for key, item in value.items():
            if isinstance(item, list):
                print(f"{key}: {len(item)} item(s)")
            elif isinstance(item, dict):
                print(f"{key}: {json.dumps(_jsonable(item), sort_keys=True, default=str)}")
            else:
                print(f"{key}: {item}")
        return
    print(value)


def _csv(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _parse_json(value: str | None, field: str) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{field} must be valid JSON") from exc


def _sets(args: argparse.Namespace) -> dict[str, Any]:
    result = {}
    for item in getattr(args, "set", None) or []:
        if "=" not in item:
            raise ValidationError(f"--set requires key=value: {item}")
        key, value = item.split("=", 1)
        try:
            result[key] = json.loads(value)
        except json.JSONDecodeError:
            result[key] = value
    return result


def _extract_globals(argv: list[str]) -> tuple[list[str], bool, bool, str | None, str | None, bool]:
    remaining = []
    json_output = False
    offline = False
    state_dir = None
    startups_root = None
    version = False
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--json":
            json_output = True
        elif item == "--offline":
            offline = True
        elif item == "--version":
            version = True
        elif item == "--state-dir":
            index += 1
            if index >= len(argv):
                raise ValidationError("--state-dir requires a path")
            state_dir = argv[index]
        elif item.startswith("--state-dir="):
            state_dir = item.split("=", 1)[1]
        elif item == "--startups-root":
            index += 1
            if index >= len(argv):
                raise ValidationError("--startups-root requires a path")
            startups_root = argv[index]
        elif item.startswith("--startups-root="):
            startups_root = item.split("=", 1)[1]
        else:
            remaining.append(item)
        index += 1
    return remaining, json_output, offline, state_dir, startups_root, version


def _add_list_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--status")
    parser.add_argument("--project")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gg", description="GrokGeneral local control plane")
    commands = parser.add_subparsers(dest="command")

    init = commands.add_parser("init", help="initialize local state")
    init.add_argument("--scan", action="store_true")
    init.add_argument("--root")
    init.add_argument("--all", action="store_true")

    commands.add_parser("status")
    commands.add_parser("doctor")

    projects = commands.add_parser("projects")
    projects.add_argument("--priority")
    projects.add_argument("--kind")
    project_commands = projects.add_subparsers(dest="action")
    project_scan = project_commands.add_parser("scan")
    project_scan.add_argument("--root")
    project_scan.add_argument("--all", action="store_true")
    project_commands.add_parser("list")
    project_show = project_commands.add_parser("show")
    project_show.add_argument("name")
    project_add = project_commands.add_parser("add")
    _project_fields(project_add)
    project_update = project_commands.add_parser("update")
    project_update.add_argument("name")
    project_update.add_argument("--set", action="append")

    project = commands.add_parser("project")
    project_sub = project.add_subparsers(dest="action", required=True)
    project_sub_show = project_sub.add_parser("show")
    project_sub_show.add_argument("name")
    project_sub_add = project_sub.add_parser("add")
    _project_fields(project_sub_add)
    project_sub_update = project_sub.add_parser("update")
    project_sub_update.add_argument("name")
    project_sub_update.add_argument("--set", action="append")
    project_alias = project_sub.add_parser("alias")
    project_alias_sub = project_alias.add_subparsers(dest="alias_action", required=True)
    project_alias_add = project_alias_sub.add_parser("add")
    project_alias_add.add_argument("project")
    project_alias_add.add_argument("alias")
    project_priority = project_sub.add_parser("priority")
    project_priority.add_argument("project")
    project_priority.add_argument("priority")
    project_kind = project_sub.add_parser("kind")
    project_kind.add_argument("project")
    project_kind.add_argument("kind")
    project_validation = project_sub.add_parser("validation")
    project_validation_sub = project_validation.add_subparsers(dest="validation_action", required=True)
    project_validation_add = project_validation_sub.add_parser("add")
    project_validation_add.add_argument("project")
    project_validation_add.add_argument("commands")
    project_validation_list = project_validation_sub.add_parser("list")
    project_validation_list.add_argument("project")

    roots = commands.add_parser("roots")
    root = commands.add_parser("root")
    root_sub = root.add_subparsers(dest="action", required=True)
    root_add = root_sub.add_parser("add")
    root_add.add_argument("path")
    root_add.add_argument("--name")
    root_remove = root_sub.add_parser("remove")
    root_remove.add_argument("id")

    resources = commands.add_parser("resources")
    resources.add_argument("--expiring", action="store_true")
    resources.add_argument("--expired", action="store_true")
    resource_commands = resources.add_subparsers(dest="action")
    resource_commands.add_parser("list")
    resource_show = resource_commands.add_parser("show")
    resource_show.add_argument("name")
    resource_add = resource_commands.add_parser("add")
    _resource_fields(resource_add)
    resource_update = resource_commands.add_parser("update")
    resource_update.add_argument("name")
    resource_update.add_argument("--set", action="append")
    resource_expire = resource_commands.add_parser("expire")
    resource_expire.add_argument("name")
    resource_expire.add_argument("--at")
    resource_expire.add_argument("--expires")

    resource = commands.add_parser("resource")
    resource_sub = resource.add_subparsers(dest="action", required=True)
    resource_sub_show = resource_sub.add_parser("show")
    resource_sub_show.add_argument("name")
    resource_sub_add = resource_sub.add_parser("add")
    _resource_fields(resource_sub_add)
    resource_sub_update = resource_sub.add_parser("update")
    resource_sub_update.add_argument("name")
    resource_sub_update.add_argument("--set", action="append")
    resource_sub_expire = resource_sub.add_parser("expire")
    resource_sub_expire.add_argument("name")
    resource_sub_expire.add_argument("--at")
    resource_sub_expire.add_argument("--expires")

    tasks = commands.add_parser("tasks")
    _add_list_options(tasks)
    task = commands.add_parser("task")
    task_sub = task.add_subparsers(dest="action", required=True)
    task_list = task_sub.add_parser("list")
    _add_list_options(task_list)
    task_show = task_sub.add_parser("show")
    task_show.add_argument("id")
    task_add = task_sub.add_parser("add")
    _task_fields(task_add)
    task_route = task_sub.add_parser("route")
    task_route.add_argument("id")
    task_run = task_sub.add_parser("run")
    task_run.add_argument("id")
    task_run.add_argument("--allow-network", action="store_true")
    task_run.add_argument("--allow-push", action="store_true")
    task_run.add_argument("--allow-destructive", action="store_true")
    task_run.add_argument("--allow-spend", action="store_true")
    task_run.add_argument("--allow-post", action="store_true")
    task_complete = task_sub.add_parser("complete")
    task_complete.add_argument("id")
    task_complete.add_argument("--output", default="[]")
    task_fail = task_sub.add_parser("fail")
    task_fail.add_argument("id")
    task_fail.add_argument("error")

    approvals = commands.add_parser("approvals")
    approvals.add_argument("--status")
    approvals.add_argument("--task")
    approval = commands.add_parser("approval")
    approval_sub = approval.add_subparsers(dest="action", required=True)
    approval_request = approval_sub.add_parser("request")
    approval_request.add_argument("task_id")
    approval_request.add_argument("--actions", required=True)
    approval_request.add_argument("--attempt", type=int)
    approval_request.add_argument("--payload-hash")
    approval_request.add_argument("--work-key")
    approval_request.add_argument("--expires")
    approval_show = approval_sub.add_parser("show")
    approval_show.add_argument("id")
    approval_approve = approval_sub.add_parser("approve")
    approval_approve.add_argument("id")
    approval_approve.add_argument("--actor", default="cli")
    approval_reject = approval_sub.add_parser("reject")
    approval_reject.add_argument("id")
    approval_reject.add_argument("--reason")
    approval_reject.add_argument("--actor", default="cli")

    route = commands.add_parser("route")
    route.add_argument("goal", nargs="?")
    route.add_argument("--project")

    opportunities = commands.add_parser("opportunities")
    opportunities.add_argument("--resource")
    optimize = commands.add_parser("optimize")
    optimize.add_argument("--max-tasks", type=int, default=20)
    optimize.add_argument("--execute", action="store_true")

    events = commands.add_parser("events")
    events.add_argument("--limit", type=int)
    events.add_argument("--event-type")
    event = commands.add_parser("event")
    event_sub = event.add_subparsers(dest="action", required=True)
    event_emit = event_sub.add_parser("emit")
    event_emit.add_argument("event_type")
    event_emit.add_argument("--resource")
    event_emit.add_argument("--project")
    event_emit.add_argument("--expires")
    event_emit.add_argument("--payload")
    event_subscribe = event_sub.add_parser("subscribe")
    event_subscribe.add_argument("event_type")
    event_subscribe.add_argument("name")
    event_subscribe.add_argument("--action", dest="subscription_action", default="record")
    event_deliver = event_sub.add_parser("deliver")
    event_deliver.add_argument("id")

    usage = commands.add_parser("usage")
    usage.add_argument("--project")
    usage.add_argument("--resource")
    usage.add_argument("--record", action="store_true")
    usage.add_argument("--source", choices=["measured", "user-entered", "estimated", "unknown"])
    usage.add_argument("--units", type=float)
    usage.add_argument("--cost", type=float)
    usage.add_argument("--task-id")
    usage.add_argument("--metadata")
    usage.add_argument("--records", action="store_true")

    executor = commands.add_parser("executor")
    executor_sub = executor.add_subparsers(dest="action", required=True)
    executor_run = executor_sub.add_parser("run")
    executor_run.add_argument("task_id")
    executor_run.add_argument("--dry-run", action="store_true")
    executor_run.add_argument("--allow-execution", action="store_true")
    executor_run.add_argument("--allow-modify", action="store_true")
    executor_run.add_argument("--allow-validate", action="store_true")
    executor_run.add_argument("--allow-network", action="store_true")
    executor_run.add_argument("--allow-push", action="store_true")
    executor_run.add_argument("--allow-destructive", action="store_true")
    executor_run.add_argument("--allow-spend", action="store_true")
    executor_run.add_argument("--approval-id", action="append")
    executor_run.add_argument("--model")
    executor_run.add_argument("--timeout", type=int, default=120)

    executions = commands.add_parser("executions")
    executions.add_argument("--task")
    executions.add_argument("--project")
    execution = commands.add_parser("execution")
    execution_sub = execution.add_subparsers(dest="action", required=True)
    execution_show = execution_sub.add_parser("show")
    execution_show.add_argument("id")
    task_executions = task_sub.add_parser("executions")
    task_executions.add_argument("id")

    context = commands.add_parser("context")
    context.add_argument("task_id")
    cache = commands.add_parser("cache")
    cache.add_argument("action", choices=["status", "inspect", "prune", "invalidate"])
    cache.add_argument("--kind")
    cache.add_argument("--key")
    ask = commands.add_parser("ask")
    ask.add_argument("question")
    backlog = commands.add_parser("backlog")
    backlog.add_argument("--project")
    adapters = commands.add_parser("adapters")
    adapter = commands.add_parser("adapter")
    adapter_sub = adapter.add_subparsers(dest="action", required=True)
    opencode = adapter_sub.add_parser("opencode")
    opencode_sub = opencode.add_subparsers(dest="opencode_action", required=True)
    opencode_sub.add_parser("health")
    opencode_models = opencode_sub.add_parser("models")
    opencode_models.add_argument("--provider")
    opencode_models.add_argument("--refresh", action="store_true")
    opencode_run = opencode_sub.add_parser("run")
    opencode_run.add_argument("message")
    opencode_run.add_argument("--project", required=True)
    opencode_run.add_argument("--model", required=True)
    opencode_run.add_argument("--timeout", type=float, default=120)
    opencode_run.add_argument("--dry-run", action="store_true")
    opencode_run.add_argument("--allow-execution", action="store_true")
    policy = commands.add_parser("policy")
    policy.add_argument("action", choices=["show", "path"])
    export = commands.add_parser("export")
    export.add_argument("path")
    import_parser = commands.add_parser("import")
    import_parser.add_argument("path")
    schedule = commands.add_parser("schedule")
    schedule.add_argument("--execute", action="store_true")
    schedule.add_argument("--allow-network", action="store_true")
    schedule.add_argument("--allow-push", action="store_true")
    schedule.add_argument("--allow-destructive", action="store_true")
    schedule.add_argument("--allow-spend", action="store_true")
    schedule.add_argument("--allow-post", action="store_true")
    commands.add_parser("close")
    return parser


def _project_fields(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--id")
    parser.add_argument("--canonical-id")
    parser.add_argument("--aliases")
    parser.add_argument("--kind")
    parser.add_argument("--root")
    parser.add_argument("--name")
    parser.add_argument("--path")
    parser.add_argument("--repository")
    parser.add_argument("--description")
    parser.add_argument("--tags")
    parser.add_argument("--domains")
    parser.add_argument("--status", default="unknown")
    parser.add_argument("--priority", default="50")
    parser.add_argument("--capabilities")
    parser.add_argument("--preferred")
    parser.add_argument("--fallback")


def _resource_fields(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--id")
    parser.add_argument("--name")
    parser.add_argument("--provider")
    parser.add_argument("--executor")
    parser.add_argument("--model")
    parser.add_argument("--cost-class")
    parser.add_argument("--marginal-cost", type=float)
    parser.add_argument("--availability")
    parser.add_argument("--expires")
    parser.add_argument("--capabilities")
    parser.add_argument("--context-limit", type=int)
    parser.add_argument("--health")
    parser.add_argument("--remaining-capacity")


def _task_fields(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("goal")
    parser.add_argument("--id")
    parser.add_argument("--project")
    parser.add_argument("--type", dest="task_type", default="general")
    parser.add_argument("--priority", type=int, default=50)
    parser.add_argument("--capabilities")
    parser.add_argument("--deadline")
    parser.add_argument("--context", action="append", dest="context_references")
    parser.add_argument("--depends", action="append", dest="dependencies")
    parser.add_argument("--command", dest="task_command")
    parser.add_argument("--metadata")


def _project_data(args: argparse.Namespace) -> dict[str, Any]:
    data = {key: value for key, value in vars(args).items() if value is not None and key not in {"command", "action", "set", "all"}}
    if getattr(args, "aliases", None):
        data["aliases"] = _csv(args.aliases)
    if getattr(args, "priority", None) is not None:
        value = str(args.priority)
        data["priority"] = int(value) if value.isdigit() else value
    if getattr(args, "tags", None):
        data["tags"] = _csv(args.tags)
    if getattr(args, "domains", None):
        data["domains"] = _csv(args.domains)
    if getattr(args, "capabilities", None):
        data["capabilities_needed"] = _csv(args.capabilities)
    if getattr(args, "preferred", None):
        data["preferred_executors"] = _csv(args.preferred)
    if getattr(args, "fallback", None):
        data["fallback_executors"] = _csv(args.fallback)
    data.pop("task_type", None)
    return data


def _resource_data(args: argparse.Namespace) -> dict[str, Any]:
    data = {key: value for key, value in vars(args).items() if value is not None and key not in {"command", "action", "set"}}
    if getattr(args, "capabilities", None):
        data["capabilities"] = _csv(args.capabilities)
    mapping = {"cost_class": "cost_class", "marginal_cost": "marginal_cost", "context_limit": "context_limit", "remaining_capacity": "remaining_capacity"}
    for source, target in mapping.items():
        if source in data and data[source] is not None:
            data[target] = data.pop(source)
    return data


def _task_data(args: argparse.Namespace) -> dict[str, Any]:
    data: dict[str, Any] = {"goal": args.goal, "task_type": args.task_type, "priority": args.priority}
    for name in ("id", "project", "deadline"):
        value = getattr(args, name, None)
        if value is not None:
            data[name] = value
    if args.capabilities:
        data["required_capabilities"] = _csv(args.capabilities)
    if args.context_references:
        data["context_references"] = args.context_references
    if args.dependencies:
        data["dependencies"] = args.dependencies
    metadata = _parse_json(args.metadata, "--metadata") if args.metadata else {}
    if args.task_command:
        metadata["command"] = _parse_json(args.task_command, "--command") if args.task_command.strip().startswith("[") else args.task_command.split()
    return {**data, "metadata": metadata}


def _service(args: argparse.Namespace, json_output: bool, offline: bool, state_dir: str | None, startups_root: str | None) -> GrokGeneral:
    if startups_root:
        import os
        os.environ["GG_STARTUPS_ROOT"] = startups_root
    service = GrokGeneral(state_dir, offline=offline)
    service.initialize()
    return service


def _handle(args: argparse.Namespace, json_output: bool, offline: bool, state_dir: str | None, startups_root: str | None) -> Any:
    command = args.command
    if command == "close":
        return {"closed": True}
    service = _service(args, json_output, offline, state_dir, startups_root)
    try:
        if command == "init":
            if args.scan:
                projects = service.scan_projects(args.root, include_all=args.all)
                return {"initialized": True, "state_dir": str(service.state_dir), "projects": [item.to_dict() for item in projects]}
            return service.initialize()
        if command == "status":
            return service.status()
        if command == "doctor":
            return service.doctor()
        if command == "roots":
            return service.roots.list()
        if command == "root":
            if args.action == "add":
                return service.roots.add(args.path, args.name)
            return {"removed": service.roots.remove(args.id)}
        if command in {"projects", "project"}:
            action = getattr(args, "action", None) or "list"
            if action == "scan":
                return [item.to_dict() for item in service.scan_projects(getattr(args, "root", None), include_all=getattr(args, "all", False))]
            if action == "list":
                return [item.to_dict() for item in service.projects.list(priority=getattr(args, "priority", None), kind=getattr(args, "kind", None))]
            if action == "show":
                return service.projects.get(args.name).to_dict()
            if action == "add":
                return service.add_project(_project_data(args)).to_dict()
            if action == "update":
                return service.update_project(args.name, _sets(args)).to_dict()
            if action == "alias" and getattr(args, "alias_action", None) == "add":
                return service.add_project_alias(args.project, args.alias).to_dict()
            if action == "priority":
                return service.set_project_priority(args.project, args.priority).to_dict()
            if action == "kind":
                return service.projects.set_kind(args.project, args.kind).to_dict()
            if action == "validation" and args.validation_action == "add":
                commands = _parse_json(args.commands, "validation commands")
                return service.set_project_validation(args.project, commands).to_dict()
            if action == "validation" and args.validation_action == "list":
                return {"project": args.project, "commands": service.project_validation(args.project)}
        if command in {"resources", "resource"}:
            action = getattr(args, "action", None) or "list"
            if action == "list":
                return [item.to_dict() for item in service.resources.list(expiring=getattr(args, "expiring", False), expired=getattr(args, "expired", False))]
            if action == "show":
                resource = service.resources.get(args.name)
                value = resource.to_dict()
                value["effective"] = service.resources.effective(resource)
                return value
            if action == "add":
                data = _resource_data(args)
                if data.get("expires"):
                    value = data["expires"]
                    data["expires_at"] = isoformat(utc_now() + parse_duration(value)) if not value.startswith(("20", "-")) else isoformat(parse_time(value))
                return service.add_resource(data).to_dict()
            if action == "update":
                return service.update_resource(args.name, _sets(args)).to_dict()
            if action == "expire":
                return service.expire_resource(args.name, args.at or args.expires).to_dict()
        if command in {"tasks", "task"}:
            action = getattr(args, "action", None) or "list"
            if action == "list":
                return [item.to_dict() for item in service.tasks.list(status=args.status, project=args.project)]
            if action == "show":
                return service.tasks.get(args.id).to_dict()
            if action == "add":
                return service.add_task(_task_data(args)).to_dict()
            if action == "route":
                return service.route_task(args.id)
            if action == "run":
                approvals = {name.removeprefix("allow-") for name in ("allow-network", "allow-push", "allow-destructive", "allow-spend", "allow-post") if getattr(args, name, False)}
                return service.run_task(args.id, approvals).to_dict()
            if action == "complete":
                outputs = _parse_json(args.output, "--output")
                return service.tasks.complete(args.id, outputs if isinstance(outputs, list) else [outputs]).to_dict()
            if action == "fail":
                return service.tasks.fail(args.id, args.error).to_dict()
            if action == "executions":
                return service.executions_list(task_id=args.id)
        if command == "approvals":
            return service.approvals_list(status=args.status, task_id=args.task)
        if command == "approval":
            if args.action == "request":
                return service.request_approval(args.task_id, _csv(args.actions), attempt=args.attempt, payload_hash=args.payload_hash, work_key=args.work_key, expires_at=args.expires)
            if args.action == "show":
                return service.approval_show(args.id)
            if args.action == "approve":
                return service.approval_approve(args.id, args.actor)
            return service.approval_reject(args.id, args.reason, args.actor)
        if command == "route":
            if not args.goal:
                raise ValidationError("route requires a goal")
            return service.route_goal(args.goal, args.project)
        if command == "opportunities":
            return service.opportunities(args.resource)
        if command == "optimize":
            return service.optimize(args.max_tasks, args.execute)
        if command == "events":
            return service.events.list(limit=args.limit, event_type=args.event_type)
        if command == "event":
            if args.action == "subscribe":
                return service.events.subscribe(args.event_type, args.name, args.subscription_action)
            if args.action == "deliver":
                return service.events.deliver(args.id)
            payload = _parse_json(args.payload, "--payload") if args.payload else {}
            if not isinstance(payload, dict):
                raise ValidationError("--payload must be a JSON object")
            if args.project:
                payload["project"] = args.project
            if args.resource:
                payload["resource"] = args.resource
                resource = service.resources.get(args.resource)
                if args.expires:
                    expiration = args.expires
                    value = isoformat(utc_now() + parse_duration(expiration)) if not expiration.startswith(("20", "-")) else isoformat(parse_time(expiration))
                    service.update_resource(resource.id, {"expires_at": value})
                    payload["expires_at"] = value
            event = service.events.emit(args.event_type, payload)
            if args.event_type in {"RESOURCE_AVAILABLE", "RESOURCE_RESET", "RESOURCE_EXPIRING", "RESOURCE_EXHAUSTED"}:
                event["opportunities"] = service.opportunities(args.resource)
            return event
        if command == "usage":
            if args.record:
                return service.usage.record(args.source or "unknown", units=args.units, cost=args.cost, project=args.project, resource=args.resource, task_id=args.task_id, metadata=_parse_json(args.metadata, "--metadata") or {})
            if args.records:
                return service.usage.list(args.project, args.resource, args.task_id)
            return service.usage.summary(args.project, args.resource, args.task_id)
        if command == "executor":
            approvals = {name.removeprefix("allow-") for name in ("allow-modify", "allow-validate", "allow-network", "allow-push", "allow-destructive", "allow-spend") if getattr(args, name, False)}
            return service.executor_run(args.task_id, dry_run=args.dry_run, approvals=approvals, approval_ids=args.approval_id, allow_execution=args.allow_execution, model=args.model, timeout=args.timeout)
        if command == "executions":
            return service.executions_list(task_id=args.task, project=args.project)
        if command == "execution":
            return service.execution_show(args.id)
        if command == "context":
            return service.context(args.task_id)
        if command == "schedule":
            approvals = {name.removeprefix("allow-") for name in ("allow-network", "allow-push", "allow-destructive", "allow-spend", "allow-post") if getattr(args, name, False)}
            return service.schedule(execute=args.execute, approvals=approvals)
        if command == "cache":
            if args.action == "status":
                return service.cache.status()
            if args.action == "inspect":
                return service.cache.inspect()
            if args.action == "prune":
                return {"pruned": service.cache.prune()}
            return {"invalidated": service.cache.invalidate(args.kind, args.key)}
        if command == "ask":
            return service.ask(args.question)
        if command == "backlog":
            findings = service.backlog.inspect(args.project) if args.project else [item for project in service.projects.list() for item in service.backlog.inspect(project)]
            return {"findings": findings, "proposals": service.backlog.propose(findings)}
        if command == "adapter":
            if args.opencode_action == "health":
                return service.adapters.get("opencode").health()
            if args.opencode_action == "models":
                return service.adapters.models("opencode", args.provider, args.refresh)
            return service.adapters.run("opencode", args.message, cwd=args.project, model=args.model, timeout=args.timeout, dry_run=args.dry_run, allow_execution=args.allow_execution)
        if command == "adapters":
            return service.adapters.health()
        if command == "policy":
            return {"path": str(service.policies.policy_path), "policy": service.policies.load()} if args.action == "show" else {"path": str(service.policies.policy_path)}
        if command == "export":
            snapshot = service.export_snapshot(args.path)
            return {"exported": args.path, "tables": sorted(snapshot.get("tables", {}))}
        if command == "import":
            path = Path(args.path)
            service.import_snapshot(json.loads(path.read_text(encoding="utf-8")))
            return {"imported": str(path)}
        raise ValidationError(f"unknown command: {command}")
    finally:
        service.close()


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    json_output = "--json" in raw
    try:
        remaining, json_output, offline, state_dir, startups_root, version = _extract_globals(raw)
        if version:
            print(__version__)
            return 0
        parser = _build_parser()
        try:
            args = parser.parse_args(remaining)
        except SystemExit as exc:
            return int(exc.code)
        if not args.command:
            parser.print_help()
            return 0
        result = _handle(args, json_output, offline, state_dir, startups_root)
        _emit(result, json_output)
        return 0
    except GrokGeneralError as exc:
        if json_output:
            print(json.dumps({"error": str(exc), "type": exc.__class__.__name__}))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        if json_output:
            print(json.dumps({"error": str(exc), "type": exc.__class__.__name__}))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
