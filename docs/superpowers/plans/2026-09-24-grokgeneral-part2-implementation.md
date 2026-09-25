# GrokGeneral Part 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add evidence-backed opportunities, durable narrow approvals, a bounded scheduler, compact results, unit-aware usage, a useful status dashboard, and a GrokBot contract around the existing GrokGeneral execution infrastructure.

**Architecture:** Keep `GrokGeneral`, SQLite `StateStore`, router, OpenCode adapter, Executor, validation, and receipts as the execution foundation. Add focused registries and service methods for approvals, opportunity V2, scheduler claims, results, dashboard, and compact contracts. Defer the optional localhost API.

**Tech Stack:** Python 3.11+, standard library only, SQLite through existing `StateStore`, `unittest`, existing OpenCode adapter and policy engine.

## Global Constraints

- Do not rewrite the working Part 1 execution infrastructure.
- Use explicit project roots; never scan all of `$HOME`.
- Do not add a background daemon or localhost HTTP API in this plan.
- Do not automatically execute every project or every opportunity.
- Planning and execution remain separate by default; optimize execution queues work only.
- OpenCode execution never receives `--auto` and never performs Git commit, push, deploy, stash, reset, discard, or spending actions.
- Sensitive actions require narrow task/attempt/project/action approval; no permanent broad approval.
- Usage values use only `measured`, `reported`, `estimated`, or `unknown`; never infer missing values.
- Raw provider output remains local and redacted; GrokBot and scheduler responses are compact and bounded.
- Every production behavior change is preceded by a failing `unittest` test and followed by a focused green run.
- Every logical commit must pass the complete test suite.

---

### Task 1: Harden execution safety and persistence

**Files:**
- Modify: `grokgeneral/storage.py`
- Modify: `grokgeneral/errors.py`
- Modify: `grokgeneral/events.py`
- Modify: `grokgeneral/policies.py`
- Modify: `grokgeneral/tasks.py`
- Modify: `grokgeneral/validation.py`
- Modify: `grokgeneral/executor.py`
- Modify: `grokgeneral/service.py`
- Test: `tests/test_execution_safety.py`
- Test: `tests/test_executor.py`

**Interfaces:**
- Produces `StateStore` tables for `approvals`, `scheduler_runs`, and `task_claims` through `_TABLES`, schema creation, generic record mapping, and migration-safe initialization.
- Produces task status `awaiting_approval` and `TaskRegistry` transitions that can persist it.
- Produces action classification covering all applicable actions rather than the first match.
- Produces an exception-safe `Executor.run()` terminal receipt for provider, policy, timeout, and validation exceptions.
- Produces pre/post repository snapshot comparison with no automatic revert.

- [ ] **Step 1: Write failing safety tests**

Add tests that:

```python
def test_provider_exception_finishes_receipt_and_task(self):
    service = self.service_with_fake_adapter(error=RuntimeError("provider failed"))
    result = service.executor_run(self.task.id, allow_execution=True, approvals={"validate"})
    self.assertEqual(result["receipt"]["status"], "failed")
    self.assertEqual(service.tasks.get(self.task.id).status, "failed")

def test_validation_is_not_silently_approved(self):
    service = self.service_with_validation()
    with self.assertRaises(SafetyBlockedError):
        service.executor_run(self.task.id, allow_execution=True)

def test_modify_requires_explicit_permission_and_post_snapshot_is_recorded(self):
    service = self.service_with_mutating_fake_adapter()
    result = service.executor_run(self.task.id, allow_execution=True, approvals={"validate"})
    self.assertEqual(result["receipt"]["status"], "failed")
    self.assertIn("snapshot", result["receipt"])
```

Use a fake adapter that raises, times out, mutates a temporary Git repository, and returns normally. Keep the tests deterministic and use temporary state directories.

- [ ] **Step 2: Run the focused tests and verify red**

Run:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_execution_safety tests.test_executor -v
```

Expected: failures for the new receipt terminalization, validation approval, post-snapshot, and action behavior.

- [ ] **Step 3: Add the minimal schema and task/action support**

Add `approvals`, `scheduler_runs`, and `task_claims` to `_TABLES`. Add `CREATE TABLE IF NOT EXISTS` definitions with existing common columns and JSON `data`; add indexes for task/claim status. Extend task status handling with `awaiting_approval`. Make action classification return a sorted list of all matching actions.

- [ ] **Step 4: Harden Executor terminalization and snapshots**

Wrap provider and validation stages in `try/except/finally` logic that:

1. Starts the receipt only after explicit execution approval.
2. Converts adapter exceptions/timeouts into a failed receipt and failed task.
3. Captures a post-provider repository snapshot.
4. Marks unexpected changes as a safety failure and never calls Git mutation commands.
5. Requires explicit `validate` approval when validation commands exist.
6. Emits a policy-block event when execution is denied.

Keep the existing raw log and receipt path behavior.

- [ ] **Step 5: Run focused and full tests**

Run:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_execution_safety tests.test_executor -q
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit the safety milestone**

```sh
git add grokgeneral/storage.py grokgeneral/events.py grokgeneral/policies.py grokgeneral/tasks.py grokgeneral/validation.py grokgeneral/executor.py grokgeneral/service.py tests/test_execution_safety.py tests/test_executor.py
git commit -m "feat: harden execution safety and approval boundaries"
```

---

### Task 2: Add durable narrow approvals

**Files:**
- Create: `grokgeneral/approvals.py`
- Modify: `grokgeneral/storage.py`
- Modify: `grokgeneral/service.py`
- Modify: `grokgeneral/cli.py`
- Test: `tests/test_approvals.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- `ApprovalRegistry.request(task, attempt, actions, payload_hash=None, project_id=None, work_key=None, expires_at=None) -> dict`
- `ApprovalRegistry.list(status=None, task_id=None) -> list[dict]`
- `ApprovalRegistry.show(approval_id) -> dict`
- `ApprovalRegistry.approve(approval_id, actor="cli") -> dict`
- `ApprovalRegistry.reject(approval_id, reason=None, actor="cli") -> dict`
- `ApprovalRegistry.consume(approval_ids, task, attempt, actions, payload_hash=None) -> set[str]`
- `ApprovalRegistry.expire() -> list[dict]`

- [ ] **Step 1: Write failing approval tests**

Cover:

```python
def test_approval_is_scoped_to_task_attempt_actions_and_payload(self):
    approval = self.approvals.request(self.task, 1, {"modify", "validate"}, "hash-a")
    self.approvals.approve(approval["id"])
    with self.assertRaises(ApprovalError):
        self.approvals.consume([approval["id"]], self.other_task, 1, {"modify", "validate"}, "hash-a")
    with self.assertRaises(ApprovalError):
        self.approvals.consume([approval["id"]], self.task, 2, {"modify", "validate"}, "hash-a")
    with self.assertRaises(ApprovalError):
        self.approvals.consume([approval["id"]], self.task, 1, {"push"}, "hash-a")

def test_approved_request_is_single_use(self):
    approval = self.approvals.request(self.task, 1, {"validate"}, "hash-a")
    self.approvals.approve(approval["id"])
    self.assertEqual(self.approvals.consume([approval["id"]], self.task, 1, {"validate"}, "hash-a"), {"validate"})
    with self.assertRaises(ApprovalError):
        self.approvals.consume([approval["id"]], self.task, 1, {"validate"}, "hash-a")

def test_rejected_and_expired_requests_are_not_usable(self):
    approval = self.approvals.request(self.task, 1, {"push"})
    self.approvals.reject(approval["id"], "not authorized")
    self.assertEqual(self.approvals.list(status="rejected")[0]["status"], "rejected")
```

- [ ] **Step 2: Run the tests and verify red**

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_approvals -v
```

Expected: import or API failures because the registry and CLI commands do not exist.

- [ ] **Step 3: Implement validation, persistence, and atomic consumption**

Validate task IDs, positive attempts, normalized action names, object payloads, and non-empty reason limits. Store all request metadata in the `approvals.data` record. Use a `BEGIN IMMEDIATE` transaction to verify scope/status/expiry and mark an approval consumed atomically. Reject reuse, wrong project/task/attempt/action/payload, and expired requests.

- [ ] **Step 4: Wire service and CLI commands**

Add service wrappers and parser commands:

```text
gg approvals [--status STATUS] [--task TASK]
gg approval show ID
gg approval approve ID [--actor ACTOR]
gg approval reject ID [--reason REASON] [--actor ACTOR]
```

JSON output must be the approval record or a bounded list. Human output must show ID, task, actions, status, project, and age without secrets.

- [ ] **Step 5: Add integration tests and run all tests**

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_approvals tests.test_cli -q
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -q
```

- [ ] **Step 6: Commit**

```sh
git add grokgeneral/approvals.py grokgeneral/storage.py grokgeneral/service.py grokgeneral/cli.py tests/test_approvals.py tests/test_cli.py
git commit -m "feat: add durable task approvals"
```

---

### Task 3: Build evidence-backed Opportunity Engine V2

**Files:**
- Create: `grokgeneral/opportunity_v2.py`
- Modify: `grokgeneral/backlog.py`
- Modify: `grokgeneral/service.py`
- Modify: `grokgeneral/cli.py`
- Modify: `grokgeneral/storage.py`
- Test: `tests/test_opportunity_v2.py`
- Modify: `tests/test_backlog.py`

**Interfaces:**
- `OpportunityEngineV2.list(projects=None, resource=None, limit=20) -> list[dict]`
- `OpportunityEngineV2.optimize(max_tasks=4, queue=False) -> dict`
- `OpportunityEngineV2.work_key(project, evidence) -> str`
- `OpportunityEngineV2.normalize_evidence(finding) -> dict`
- Backlog findings include `kind`, `path`, `line`, `marker`, `detail`, `observed_at`, `source`, and `fingerprint`.

- [ ] **Step 1: Write failing opportunity tests**

Cover evidence requirements, concrete kinds, work keys, dedup, blockers, health, validation, and global ordering:

```python
def test_no_evidence_returns_no_generic_opportunity(self):
    self.project.update(self.project.id, {"metadata": {}})
    self.assertEqual(self.engine.list(), [])

def test_duplicate_evidence_is_one_work_item(self):
    first = self.engine.normalize_evidence(self.finding)
    second = dict(first, marker="same marker")
    self.assertEqual(self.engine.work_key(self.project, first), self.engine.work_key(self.project, second))

def test_previous_completed_execution_suppresses_same_work(self):
    opportunity = self.engine.list()[0]
    self.executions.complete(self.execution["id"], {"status": "completed", "validation": {"status": "passed"}})
    self.assertNotIn(opportunity["work_key"], [item["work_key"] for item in self.engine.list()])

def test_optimize_orders_all_work_globally_and_queues_only(self):
    result = self.engine.optimize(max_tasks=1, queue=True)
    self.assertEqual(len(result["items"]), 1)
    self.assertEqual(result["items"][0]["approval_needed"], [])
    self.assertEqual(result["executed"], False)
```

- [ ] **Step 2: Run the tests and verify red**

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_opportunity_v2 -v
```

Expected: API/import failures.

- [ ] **Step 3: Extend backlog evidence without running commands**

Add safe, bounded checks for recorded build/test/CI metadata, TODO/blocker markers, missing test/config evidence, dependency metadata, and packaging markers. Return only relative paths and short bounded details. Never execute project commands during inspection.

- [ ] **Step 4: Implement V2 candidates and scoring**

Build candidates only from evidence or active tasks. Compute `work_key`, evidence confidence, project health, priority, task value/deadline, capability fit, cost, expiry/reset urgency, validation readiness, blockers, duplicate status, and prior execution history. Reject blocked/expired/invalid work before selection. Return a `reason` and component breakdown.

- [ ] **Step 5: Implement global optimize and queue-only behavior**

Deduplicate before sorting, sort by score and stable ID, cap output, and attach `task`, `project`, `executor`, `reason`, `cost_class`, and `approval_needed`. Queue only when explicitly requested and never invoke providers.

- [ ] **Step 6: Wire service/CLI and run tests**

Make `gg opportunities` and `gg optimize` use V2 while preserving optional resource/project filters and JSON output. Add `--project`, `--limit`, and `--queue` without removing existing flags.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_opportunity_v2 tests.test_backlog tests.test_opportunities tests.test_cli -q
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -q
```

- [ ] **Step 7: Commit**

```sh
git add grokgeneral/opportunity_v2.py grokgeneral/backlog.py grokgeneral/service.py grokgeneral/cli.py grokgeneral/storage.py tests/test_opportunity_v2.py tests/test_backlog.py tests/test_opportunities.py
git commit -m "feat: add evidence-backed opportunity engine"
```

---

### Task 4: Add bounded scheduler with claims and repository locks

**Files:**
- Modify: `grokgeneral/scheduler.py`
- Modify: `grokgeneral/storage.py`
- Modify: `grokgeneral/tasks.py`
- Modify: `grokgeneral/service.py`
- Modify: `grokgeneral/cli.py`
- Test: `tests/test_scheduler.py`
- Create: `tests/test_scheduler_concurrency.py`

**Interfaces:**
- `SchedulerConfig(concurrency=2, max_tasks=4, max_seconds=300, max_attempts=2, retry_failed=False)`.
- `Scheduler.plan() -> list[dict]` remains a compatibility read.
- `Scheduler.run_cycle(config=None, approval_ids=None, execute=False) -> dict`.
- `Scheduler.pause()`, `Scheduler.resume()`, `Scheduler.stop()`, `Scheduler.control_status()`.
- Claims use `task_claims` records and mutation repository keys.

- [ ] **Step 1: Write failing scheduler tests**

Cover bounds, atomic claims, same-repo mutation serialization, read-only concurrency, pause/stop, health gates, retries, project cwd, and graceful errors:

```python
def test_default_cycle_is_bounded_to_two_workers(self):
    result = self.scheduler.run_cycle(execute=True)
    self.assertLessEqual(result["concurrency"], 2)
    self.assertLessEqual(result["selected"], 4)

def test_mutation_claims_for_one_repository_are_serialized(self):
    first = self.tasks.add(self.mutation_task("one"))
    second = self.tasks.add(self.mutation_task("two"))
    result = self.scheduler.run_cycle(execute=True)
    self.assertEqual(result["same_repo_conflicts"], 1)

def test_read_only_claims_for_one_repository_can_run_together(self):
    result = self.scheduler.run_cycle(execute=True)
    self.assertEqual(result["completed"] + result["failed"], result["selected"])

def test_pause_stops_new_claims(self):
    self.scheduler.pause()
    result = self.scheduler.run_cycle(execute=True)
    self.assertEqual(result["status"], "paused")
    self.assertEqual(result["selected"], 0)
```

- [ ] **Step 2: Run the tests and verify red**

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_scheduler tests.test_scheduler_concurrency -v
```

Expected: failures for missing bounds, claims, controls, and scheduler result fields.

- [ ] **Step 3: Add atomic claim storage and control state**

Implement claim acquisition/recovery in one `BEGIN IMMEDIATE` transaction. Reject duplicate live claims, serialize mutation claims by normalized project path, and expire stale leases. Persist scheduler control state in `meta` with keys `scheduler.control` and `scheduler.stop`.

- [ ] **Step 4: Implement bounded execution**

Use a small `ThreadPoolExecutor` bounded by `config.concurrency`. Keep cycle deadlines monotonic, check pause/stop before each claim, and never start more than `max_tasks`. Resolve project paths through the service. Recheck resource expiration/health immediately before dispatch. Use the existing task runner for local argv commands and the existing Executor for OpenCode tasks with approval IDs.

- [ ] **Step 5: Add retries and compact scheduler results**

Retry only explicitly enabled, retryable failures under `max_attempts`; never retry safety, approval, dirty-repo, deadline, or unhealthy-resource failures. Return selected/completed/failed/skipped/awaiting-approval counts and compact task results. Persist `scheduler_runs`.

- [ ] **Step 6: Wire CLI and service**

Add `gg schedule --concurrency --max-tasks --max-seconds --max-attempts --retry-failed --approval-id` and subcommands/flags for `pause`, `resume`, `stop`, and `status`. Planning remains default; `--execute` is required to dispatch.

- [ ] **Step 7: Run tests and commit**

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_scheduler tests.test_scheduler_concurrency tests.test_tasks tests.test_integration -q
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -q
git add grokgeneral/scheduler.py grokgeneral/storage.py grokgeneral/tasks.py grokgeneral/service.py grokgeneral/cli.py tests/test_scheduler.py tests/test_scheduler_concurrency.py
git commit -m "feat: add bounded scheduler and repository locks"
```

---

### Task 5: Add compact results and unit-aware usage

**Files:**
- Create: `grokgeneral/results.py`
- Modify: `grokgeneral/executions.py`
- Modify: `grokgeneral/executor.py`
- Modify: `grokgeneral/tasks.py`
- Modify: `grokgeneral/usage.py`
- Modify: `grokgeneral/service.py`
- Modify: `grokgeneral/cli.py`
- Create: `tests/test_results.py`
- Modify: `tests/test_usage.py`
- Modify: `tests/test_executor.py`

**Interfaces:**
- `compact_result(receipt, before_snapshot=None, after_snapshot=None, task=None) -> dict`
- `UsageLedger.record(..., execution_id=None, provider=None, model=None, unit=None, duration_seconds=None, input_tokens=None, output_tokens=None, total_tokens=None, cost_class=None, status=None, idempotency_key=None)`.
- `UsageLedger.summary()` groups by source, unit, resource, provider, model, and project without mixing units.
- `GrokGeneral.compact_result(execution_id) -> dict`.
- `gg execution result ID --json` returns the compact envelope.

- [ ] **Step 1: Write failing result and usage tests**

```python
def test_compact_result_has_stable_shape_and_no_raw_paths(self):
    result = compact_result(self.receipt, self.before, self.after)
    self.assertEqual(result["status"], "success")
    self.assertIn("files_changed", result)
    self.assertIn("tests", result)
    self.assertNotIn("raw_log_path", result)
    self.assertLessEqual(len(canonical_json(result)), 16384)

def test_usage_does_not_sum_tokens_and_seconds(self):
    self.usage.record(source="reported", units=10, unit="tokens", resource="space")
    self.usage.record(source="measured", units=2, unit="seconds", resource="local")
    summary = self.usage.summary()
    self.assertEqual(summary["by_unit"]["tokens"]["units"], 10)
    self.assertEqual(summary["by_unit"]["seconds"]["units"], 2)

def test_execution_usage_is_idempotent(self):
    first = self.usage.record(source="reported", execution_id="exec-1", unit="tokens", units=4)
    second = self.usage.record(source="reported", execution_id="exec-1", unit="tokens", units=4)
    self.assertEqual(first["id"], second["id"])
```

- [ ] **Step 2: Run the tests and verify red**

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_results tests.test_usage -v
```

Expected: missing-module/API failures.

- [ ] **Step 3: Implement compact result derivation**

Derive files changed from sanitized Git snapshot file lists, tests from validation exit/result metadata, commit always `null` for this executor, blockers from task/project state, and next action from status/error. Truncate summary and allowlist artifact metadata. Do not include raw events, commands, context, or absolute paths.

- [ ] **Step 4: Extend usage persistence and grouping**

Preserve legacy `units` and `source` fields. Add optional execution/provider/model/unit/duration/token/cost/status fields in `data`. Use a deterministic idempotency key derived from execution ID and attempt. Group known values by unit and source; return `None` totals when any member of a group is unknown.

- [ ] **Step 5: Connect Executor and local task usage**

Record provider-reported token/cost/duration values exactly once. Record local execution duration as `seconds` with source `measured` when the process duration is measured. Record failed and timed-out attempts with status and `unknown` for unavailable values. Never infer cost from cost class.

- [ ] **Step 6: Wire CLI/service and run tests**

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_results tests.test_usage tests.test_executor -q
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -q
```

- [ ] **Step 7: Commit**

```sh
git add grokgeneral/results.py grokgeneral/executions.py grokgeneral/executor.py grokgeneral/tasks.py grokgeneral/usage.py grokgeneral/service.py grokgeneral/cli.py tests/test_results.py tests/test_usage.py tests/test_executor.py
git commit -m "feat: add compact results and usage accounting"
```

---

### Task 6: Add dashboard, status CLI, and GrokBot contract

**Files:**
- Create: `grokgeneral/dashboard.py`
- Create: `grokgeneral/contracts.py`
- Modify: `grokgeneral/service.py`
- Modify: `grokgeneral/cli.py`
- Modify: `README.md`
- Create: `docs/grokbot-integration.md`
- Create: `tests/test_dashboard.py`
- Create: `tests/test_contract.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- `GrokGeneral.status_snapshot(full=False) -> dict` is side-effect-free.
- `GrokGeneral.contract_status() -> dict`.
- `GrokGeneral.contract_submit_task(payload) -> dict`.
- `GrokGeneral.contract_route_task(task_id) -> dict`.
- `GrokGeneral.contract_request_execution(task_id, approval_ids, options) -> dict`.
- `GrokGeneral.contract_result(task_id=None, execution_id=None) -> dict`.
- `GrokGeneral.contract_pending_approvals(limit=20) -> dict`.
- `GrokGeneral.contract_global_changes(limit=20, cursor=None) -> dict`.
- `gg status --full` and `gg contract status` expose compact service/contract views.

- [ ] **Step 1: Write failing dashboard and contract tests**

```python
def test_status_snapshot_is_read_only_and_bounded(self):
    before = self.service.status_snapshot()
    after = self.service.status_snapshot()
    self.assertEqual(before["projects"], after["projects"])
    self.assertEqual(before["tasks_by_status"], after["tasks_by_status"])
    self.assertLessEqual(len(before["projects"]["items"]), 10)
    self.assertNotIn("raw_log_path", str(before))

def test_contract_result_uses_compact_envelope(self):
    response = self.service.contract_result(execution_id=self.execution["id"])
    self.assertTrue(response["ok"])
    self.assertEqual(response["schema_version"], "1")
    self.assertLessEqual(len(canonical_json(response)), 8192)
    self.assertNotIn("/Users/", str(response))

def test_contract_execution_requires_approval_id(self):
    response = self.service.contract_request_execution(self.task.id, [], {"allow_execution": True})
    self.assertFalse(response["ok"])
    self.assertEqual(response["error"]["code"], "APPROVAL_REQUIRED")
```

- [ ] **Step 2: Run the tests and verify red**

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_dashboard tests.test_contract -v
```

Expected: missing dashboard/contract methods and CLI options.

- [ ] **Step 3: Implement side-effect-free dashboard assembly**

Read projects, tasks, resources, approvals, executions, opportunities, and usage without refresh/reroute/provider calls. Default project list is bounded to core/active and ten items; `--full` adds bounded details. Include health, blockers, running/validating tasks, pending approvals, free/expiring resources, recent completions, top opportunities, and next actions.

- [ ] **Step 4: Implement compact contract methods**

Validate payloads, call existing service methods, and wrap results in `{schema_version, ok, result, warnings}`. Never return context, raw logs, commands, source, secrets, or absolute paths. Bound each response at 8 KiB with explicit warnings when truncated.

- [ ] **Step 5: Wire CLI and documentation**

Add `gg contract` and `gg contract status`, `gg status --full`, and update README commands/safety. Create `docs/grokbot-integration.md` with the seven operations, request/response examples, approval semantics, and architecture boundary. State explicitly that GrokBot does not implement work.

- [ ] **Step 6: Run tests and commit**

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_dashboard tests.test_contract tests.test_cli -q
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -q
git add grokgeneral/dashboard.py grokgeneral/contracts.py grokgeneral/service.py grokgeneral/cli.py README.md docs/grokbot-integration.md tests/test_dashboard.py tests/test_contract.py tests/test_cli.py
git commit -m "feat: add dashboard and grokbot contract"
```

---

### Task 7: Integrated dogfood, smoke tests, and final report

**Files:**
- Modify: `README.md` only if smoke results reveal inaccurate commands.
- Create: `docs/superpowers/dogfood/2026-09-24-part2-batch.json` only if a compact machine-readable dogfood record is useful; otherwise do not add a generated file.
- Test: all existing and Part 2 tests.

**Interfaces:**
- Uses the public CLI/service contract and verified `opencode/space-bunny-free` adapter.
- Produces receipts, usage records, approvals, scheduler run records, compact results, and a final clean Git tree.

- [ ] **Step 1: Build an isolated dogfood state and two small temporary Git projects**

Use only temporary directories. Initialize each project with a README, one explicit test/build/health fixture, and a clean Git commit. Register only those projects through `gg project add`; do not register or scan the 154 default projects.

- [ ] **Step 2: Inspect and generate opportunities**

Run `gg projects`, `gg backlog`, `gg opportunities`, and `gg optimize --json` against the temporary state. Select at most two concrete evidence-backed tasks with validation configured. Confirm duplicate scans do not increase the work count.

- [ ] **Step 3: Route and approve the batch**

Run `gg task route` and `gg optimize --queue`; request required approvals. Approve only the exact task/action requests. Confirm the compact optimize plan reports task, project, executor, reason, cost class, and approval needs.

- [ ] **Step 4: Execute through the bounded scheduler/Executor**

Run one scheduler cycle with concurrency `2` and max tasks `2`, using a read-only OpenCode prompt and explicit execution approval. Validate each task with its argv command. Do not use `--auto`; do not touch neighboring repositories.

- [ ] **Step 5: Verify results, usage, and status**

Run `gg usage`, `gg usage --records`, `gg status --json`, `gg status --full --json`, `gg approvals`, `gg executions`, and compact result commands. Confirm terminal receipts, validation status, source labels, no path/raw-log leakage, and free-resource conservation.

- [ ] **Step 6: Run final verification**

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -q
PYTHONDONTWRITEBYTECODE=1 python3 -m compileall -q grokgeneral tests
git diff --check
test -z "$(git status --short)"
```

- [ ] **Step 7: Commit final documentation and report evidence**

```sh
git add README.md docs/grokbot-integration.md
 git commit -m "docs: record part 2 dogfood workflow"
```

If no documentation changed, do not create an empty commit. Return the requested Part 2 completion template with actual commit hashes, test count, control-plane state, usage evidence, dogfood counts, limitations, and next actions.
