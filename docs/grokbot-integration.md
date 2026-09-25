# GrokBot Integration Contract

GrokBot is an orchestration client. It submits and coordinates work through GrokGeneral; it does not inspect repositories, choose implementation details, or execute providers itself.

```text
GrokBot → GrokGeneral → cheapest capable executor
```

## Transport

Part 2 exposes the contract through the service facade and `gg contract` CLI. A localhost HTTP API is intentionally deferred. A future API must use the same methods and compact envelopes.

Every response has this shape:

```json
{
  "schema_version": "1",
  "ok": true,
  "result": {},
  "warnings": []
}
```

Errors use:

```json
{
  "schema_version": "1",
  "ok": false,
  "result": null,
  "warnings": [],
  "error": {"code": "APPROVAL_REQUIRED", "message": "an explicit approval ID is required"}
}
```

Responses are bounded to 8 KiB. They do not contain secrets, raw provider logs, source snippets, context packs, commands, or absolute local paths.

## Operations

### Status

Service: `GrokGeneral.contract_status()`
CLI: `gg contract status --json`

Returns the daily dashboard: health, project counts, blockers, running tasks, pending approvals, free/expiring resources, recent completions, top opportunities, next actions, and grouped usage.

### Submit task

Service:

```python
service.contract_submit_task({
    "goal": "repair the failing test",
    "project": "gh0st",
    "required_capabilities": ["testing"]
})
```

Returns a task ID, project, and lifecycle status. Submission does not execute work.

### Route task

Service: `GrokGeneral.contract_route_task(task_id)`
CLI: `gg task route TASK_ID --json`

Returns the selected executor/provider/model, routing reason, and approval requirements. Routing is planning; it does not dispatch a provider.

### Request approved execution

Service:

```python
service.contract_request_execution(
    task_id,
    ["approval-id"],
    {"allow_execution": True, "timeout": 120}
)
```

The approval ID is required and must match the exact task, attempt, project, payload, and action set. Approvals are single-use and narrow:

- `modify`
- `commit`
- `push`
- `deploy`
- `spend`
- `external`
- `network`
- `destructive`
- `validate`
- `dirty-repo`

An approval for one task or action cannot authorize another. A dirty repository is never silently modified. OpenCode execution never receives `--auto` and GrokGeneral never commits, pushes, deploys, or spends credits.

### Get result

Service: `GrokGeneral.contract_result(task_id=..., execution_id=...)`
CLI: `gg contract result --execution EXECUTION_ID --json` or `gg execution result EXECUTION_ID --json`

Returns a compact result:

```json
{
  "status": "success",
  "project": "gh0st",
  "executor": "opencode/space-bunny-free",
  "files_changed": 0,
  "tests": "passed",
  "commit": null,
  "blockers": [],
  "next_action": null
}
```

Detailed receipts and raw logs remain available to local operators through `gg execution show EXECUTION_ID`.

### Pending approvals

Service: `GrokGeneral.contract_pending_approvals(limit=20)`
CLI: `gg approvals --json` or `gg contract approvals --json`

Returns only pending approval IDs, task/project scope, actions, and status. Approval decisions use:

```sh
gg approval show ID
gg approval approve ID
gg approval reject ID --reason "not authorized"
```

### Important global changes

Service: `GrokGeneral.contract_global_changes(limit=20, cursor=None)`
CLI: `gg contract changes --limit 20 --json`

Returns recent resource, opportunity, scheduler, task, and approval events without payloads that could leak source or secrets. The cursor is an opaque event timestamp for bounded polling.

## Usage and conservation

Provider-reported token/cost values use `source=reported`; locally measured durations use `source=measured`; estimates use `source=estimated`; unavailable values remain `source=unknown`. GrokGeneral does not infer usage from text or cost class. `gg usage` groups tokens, credits, and seconds separately and shows free/paid resource use so GrokBot can conserve scarce capacity.

## Safety and failure behavior

- Missing or mismatched approval: `APPROVAL_REQUIRED` or `APPROVAL_REJECTED`.
- Dirty repository or unsafe action: `SAFETY_BLOCKED`.
- Missing task/execution: `NOT_FOUND`.
- Provider/validation failure: terminal `failed`, `timeout`, or `blocked` result with a short next action.
- Scheduler pause/stop prevents new claims; running work is allowed to finish gracefully.
- The contract never bypasses the CLI/service policy engine.
