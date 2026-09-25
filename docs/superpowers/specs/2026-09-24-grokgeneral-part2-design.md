# GrokGeneral Part 2: Scheduler and Resource Optimizer Design

**Date:** 2026-09-24

## Goal

Turn the existing GrokGeneral v1 control plane into the central scheduler and resource optimizer for GrokBot-related projects without replacing the working SQLite state store, router, OpenCode adapter, executor, validation boundary, or receipts.

Part 2 is CLI- and service-first. The optional localhost API is explicitly deferred until the control plane and GrokBot contract are stable.

## Non-goals

- No automatic execution across every registered project.
- No permanent broad approval grants.
- No GrokBot implementation work, source edits, or raw log transport.
- No background daemon in Part 2.
- No new third-party runtime dependency.
- No claim that provider usage is measured when the provider only reports estimates.

## Architecture

```text
read-only project/task evidence
        ↓
Opportunity Engine V2 → stable work_key → deduped global plan
        ↓
approval request → narrow task/attempt approval
        ↓
bounded scheduler → atomic claim → resource health check
        ↓
existing route/executor → validation → receipt + raw local log
        ↓
usage ledger + compact result + side-effect-free dashboard
        ↓
GrokBot contract (small structured responses only)
```

The existing `GrokGeneral` facade remains the composition root. New components are additive and receive existing registries rather than reimplementing routing, policy, context, validation, or adapter behavior.

### New focused modules

- `grokgeneral/opportunity_v2.py`: evidence normalization, work fingerprints, scoring, deduplication, and global optimization.
- `grokgeneral/approvals.py`: durable task/attempt/action-scoped approval requests and decisions.
- `grokgeneral/results.py`: bounded compact result envelopes and local-only raw log references.
- `grokgeneral/dashboard.py`: side-effect-free daily dashboard assembly.
- `grokgeneral/contracts.py`: GrokBot-facing methods and compact DTO construction.
- Existing `scheduler.py` is extended with atomic claims, bounded cycles, retries, pause/stop state, and repository mutation locks.
- Existing `usage.py` is extended with execution-linked, unit-aware fields while preserving the legacy `record()` API.

## Safety foundation before scheduling

The existing executor is retained but made exception-safe and policy-complete:

1. Every started provider execution reaches a terminal receipt state, including timeout, adapter exception, policy failure, and unexpected validation failure.
2. Repository snapshots are compared before and after provider execution. Unexpected changes require explicit modify permission and are recorded in the receipt; no automatic revert is performed.
3. Validation is never silently granted. It requires an explicit `validate` approval or a durable task-scoped approval request.
4. Approval-sensitive routes are reevaluated against current policy/resource health instead of being returned from an approval-insensitive cache.
5. Every applicable action is classified (`modify`, `commit`, `push`, `deploy`, `spend`, `external`, `dirty-repo`, `validate`) rather than only the first matching action.
6. Scheduler stops before launching work when pause/stop state, cycle deadline, retry limit, or resource health makes execution unsafe.

No existing execution path is rewritten; these are narrow guards around the current adapter and receipt lifecycle.

## Persistence additions

Use additive SQLite tables through the existing `StateStore` generic record mechanism. Keep schema version migration compatible with existing state.

### `approvals`

Fields stored in the existing record `data` JSON where necessary:

- `id`
- `task_id`
- `attempt`
- `work_key`
- `project_id`
- `required_actions`
- `status`: `pending`, `approved`, `rejected`, `expired`, `consumed`
- `payload_hash`
- `actor`
- `created_at`, `decided_at`, `expires_at`, `consumed_at`
- `reason`

A request is bound to the exact task, attempt, work key, project, normalized action set, and payload hash. An approval is single-use and may be rejected or expired. Existing CLI boolean flags remain compatibility conveniences, but scheduler/GrokBot execution consumes explicit approval IDs.

### `scheduler_runs`

- `id`, `started_at`, `ended_at`
- `concurrency`, `max_tasks`, `max_seconds`, `max_attempts`
- `status`, `selected`, `completed`, `failed`, `skipped`, `paused`, `stopped`
- `data`: compact item summaries and warnings

### `task_claims`

- `id`, `task_id`, `project_key`, `scheduler_run_id`
- `attempt`, `status`, `lease_expires_at`
- `mutation`: boolean
- `created_at`, `updated_at`, `completed_at`

A claim is acquired in one `BEGIN IMMEDIATE` transaction. A task is not claimable if another unexpired claim exists. Mutation claims for the same normalized repository path are serialized; read-only claims may run concurrently. Expired claims are recoverable and audited.

### Opportunity records

The existing `opportunities` table stores observations and work metadata in its JSON `data` field. The record includes `work_key`, evidence, score components, validation readiness, approval requirements, and prior execution references. A work key is independent of executor so the same work is not duplicated across free, local, or paid resources.

## Opportunity Engine V2

### Evidence-first discovery

Generated opportunities require evidence. The engine consumes:

- active task records and task value metadata;
- backlog findings with kind, relative path, optional line/marker, detail, observation time, and source;
- recorded project build/test/CI health;
- project blockers and dependencies;
- project priority and capability needs;
- configured validation commands;
- prior execution receipts linked by `work_key`.

If no actionable evidence exists, the engine returns no generic proposal. It will not select the first projects merely to produce work.

Concrete evidence families are:

- failing build or recorded build failure;
- failing or missing test evidence;
- CI/configuration defect;
- documented TODO/blocker with a useful action;
- dependency cleanup or packaging/repo hygiene issue;
- architecture review only when project metadata or backlog evidence justifies it.

Discovery is read-only and bounded to explicitly selected projects/roots or a caller-provided project limit. It never runs builds, tests, provider calls, or validation commands.

### Stable work identity

Use a deterministic fingerprint over canonical project ID, evidence kind, normalized relative path, optional line/marker, and normalized action intent:

```text
work_key = sha256(canonical_project + "|" + kind + "|" + locator + "|" + intent)
```

Task-backed work uses the task ID in its metadata as the source identity, while retaining the same fingerprint fields. A completed or validated prior execution suppresses the same work. A prior failed execution remains visible as history and receives a retry penalty rather than being silently discarded.

Deduplicate before scoring and before applying `--max-tasks`. Exact work keys are primary; same canonical project plus same kind plus overlapping locator is a conservative secondary duplicate. The selected record explains which duplicate it supersedes.

### Eligibility and score

For each candidate, evaluate:

- project priority/tier;
- observed project health and build/test/CI state;
- evidence strength and age;
- task value/priority/deadline;
- required capability fit;
- resource effective health and availability;
- resource cost class and known marginal cost;
- expiration/reset urgency;
- validation availability;
- blockers/dependencies;
- duplicate state;
- prior execution outcome and attempts.

The score is explainable and bounded. Unknown cost is not treated as free. Missing validation is visible and lowers execution readiness; it is not silently considered a pass. Blocked or expired-deadline work is not eligible for execution. The output includes a reason and component breakdown rather than a precision-looking unexplained number.

### Global optimize

`gg opportunities` returns a compact V2 list. `gg optimize` sorts unique feasible work globally across all eligible resources, not resource-by-resource. Each selected item contains at least:

```json
{
  "task": "task-id",
  "project": "project-id",
  "executor": "Space Bunny",
  "reason": "free capable resource; validation configured",
  "cost_class": "free",
  "approval_needed": []
}
```

Planning is the default. `--execute` means queue/materialize only, matching the existing safety boundary; it does not invoke a provider. A one-time resource update changes the resource registry globally and invalidates opportunity/route cache entries so all projects observe it.

## Durable approvals

Add:

```sh
gg approvals
gg approval show ID
gg approval approve ID
gg approval reject ID
```

`ApprovalRegistry` exposes request, list, show, approve, reject, consume, and expire methods. Requests include all applicable actions and a stable payload hash. Decisions are audited and scoped; an approval for one task/attempt/project/action set cannot authorize another.

Sensitive executor actions remain blocked unless a matching approval ID is consumed. A dirty worktree may be inspected but cannot be modified under a generic execution approval. CLI flags may continue to create compatibility grants for direct operator use, but broad permanent shortcuts are not introduced.

## Bounded scheduler

### Configuration

Expose conservative defaults:

- concurrency: `2`
- maximum tasks per cycle: `4`
- maximum cycle duration: `300` seconds
- maximum attempts: `2`
- retry failed tasks: opt-in only
- mutation repository lock: enabled
- resource health refresh: before dispatch

Configuration may be overridden per invocation, but bounded values are validated and never unbounded by default.

### Cycle algorithm

1. Load a read-only plan of queued tasks, project state, routes, validation readiness, approvals, and resource health.
2. Sort deterministically by value, priority, deadline, and task ID.
3. Atomically claim eligible tasks. Never claim tasks for expired deadlines, incomplete dependencies, blocked projects, unhealthy/expired resources, or missing required approvals.
4. Claim at most the configured concurrency and cycle budget.
5. Serialize mutation claims by normalized repository path. Allow read-only work to run concurrently.
6. Dispatch local tasks through the existing project-aware service path. Dispatch OpenCode tasks through the existing Executor with approval IDs and explicit execution permission.
7. Stop launching new work when pause, stop, deadline, or cycle budget is reached. Running work may finish gracefully.
8. Persist a scheduler run summary and compact result per claimed task.
9. Retry only retryable failures, only when explicitly enabled, only below the attempt limit, and only after rechecking health/approvals/dirty state.

Scheduler actions are distinguishably reported as `executed`, `route_only`, `awaiting_approval`, `skipped_blocked`, `skipped_deadline`, `skipped_retry_limit`, `skipped_health`, `paused`, or `stopped`.

### Pause and stop

Persist scheduler control state in the scheduler/meta store:

- `gg schedule pause`
- `gg schedule resume`
- `gg schedule stop`
- `gg schedule status`

Pause prevents new claims while allowing running work to finish. Stop prevents new claims and marks the cycle stopped; it does not kill a provider process or discard receipts.

## Compact results and usage

### Compact result

`results.py` creates a bounded envelope, with a hard target of 16 KiB and a summary cap of 2 KiB:

```json
{
  "status": "success",
  "project": "gh0st",
  "executor": "opencode/space-bunny",
  "files_changed": 4,
  "tests": "27/27",
  "commit": null,
  "blockers": [],
  "next_action": null
}
```

Only allowlisted artifact metadata is included. Raw stdout, stderr, JSONL events, context content, commands, and absolute log paths remain local. `gg execution show` may retain its detailed local view; GrokBot and scheduler responses use the compact envelope.

### Usage accounting

Extend usage records with:

- `execution_id` or deterministic idempotency key;
- provider, model, project, task, and executor;
- unit (`tokens`, `credits`, `seconds`, or `unknown`);
- duration;
- input/output/total tokens when reported;
- cost/currency when reported;
- execution status;
- source: `measured`, `reported`, `estimated`, or `unknown`.

The existing source set remains compatible; no usage value is inferred from text or cost class. Record once per execution attempt, including failure/timeout. Summaries group by unit, resource, provider/model, project, and source. `gg usage` makes free-resource conservation and expensive-resource use visible without summing seconds and tokens together.

## Daily status

Add a side-effect-free `status_snapshot()` for CLI/GrokBot reads. It must not refresh expiration, reroute tasks, run providers, or run validation merely because it was read.

`gg status` and `gg status --json` show compactly:

- health;
- core/active project counts and selected projects;
- blockers;
- running/validating tasks;
- pending approvals;
- free and expiring resources;
- recent completions;
- top opportunities;
- highest-value next actions;
- usage/resource conservation summary.

`gg status --full` adds bounded project and recent execution details. The default never dumps all registered projects.

## GrokBot integration contract

Create `docs/grokbot-integration.md` and a service/contract layer with these minimal operations:

- `status()` → compact dashboard;
- `submit_task(payload)` → bounded task ID and validation summary;
- `route_task(task_id)` → compact route rationale and approval requirements;
- `request_execution(task_id, approval_ids, options)` → execution/receipt ID or pending approval;
- `get_result(task_id or execution_id)` → compact result;
- `get_pending_approvals()` → bounded pending approval list;
- `get_global_changes(limit)` → resource/opportunity/scheduler changes since the supplied cursor or latest snapshot.

All contract responses use a small envelope:

```json
{"schema_version":"1","ok":true,"result":{},"warnings":[]}
```

Errors use `ok:false` with a stable code and short message. Contract responses contain no secrets, raw logs, source snippets, context packs, or absolute local paths. A hard 8 KiB default response limit is enforced and truncation is explicit.

The contract documents the architecture:

```text
GrokBot → GrokGeneral → cheapest capable executor
```

GrokBot may submit, inspect, route, request approval, and consume results; it does not inspect repositories or implement work itself.

## Optional API

No localhost API is implemented in Part 2. The service facade and contract methods are designed so a future loopback API can be added without changing CLI behavior. A future API must be disabled by default, bind to loopback, protect mutations with authentication/authorization, bound request/response sizes, use idempotency keys, and return the same compact envelopes.

## Testing strategy

Add focused tests before implementation behavior changes:

- opportunity evidence required, concrete categories, scoring components, exact/overlap deduplication, and prior-execution suppression;
- global optimize ordering, correct per-item scores, bounded max tasks, and queue-only behavior;
- scheduler concurrency bounds, atomic claims, same-repository mutation lock, read-only parallelism, pause/stop, health gates, retries, and graceful failures;
- approval scoping, approve/reject/expiry/consume, action coverage, and no broad grants;
- compact result shape, size limits, redaction, files/tests extraction, and raw-log separation;
- usage source labels, unit grouping, provider/model linkage, failure/timeout recording, and idempotency;
- side-effect-free status, bounded default output, full output, approvals/resources/completions/opportunities;
- GrokBot contract method shapes, response limits, and no path/raw-log leakage;
- existing execution, validation, adapter, CLI, and storage tests remain green.

Run the full suite, compile check, CLI smoke tests, and one small real OpenCode dogfood batch in clean temporary projects. Dogfood must not touch neighboring repositories or run against all registered projects.

## Delivery and commits

Logical commits:

1. `feat: harden execution safety and approval boundaries`
2. `feat: add evidence-backed opportunity engine`
3. `feat: add durable approvals and bounded scheduler`
4. `feat: add compact results usage and dashboard`
5. `docs: add grokbot contract and dogfood evidence`

Every commit leaves the full suite green. The final commit is followed by a clean-tree check and the requested Part 2 completion report.
