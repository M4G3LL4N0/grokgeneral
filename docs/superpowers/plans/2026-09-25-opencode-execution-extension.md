# GrokGeneral OpenCode Execution Extension Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the verified GrokGeneral baseline with explicit project identity, real OpenCode execution, receipts, validation, safety gates, expiry rerouting, and one safe dogfood run.

**Architecture:** Preserve the existing `GrokGeneral` service and SQLite state. Add focused identity, execution, receipt, and safety modules; expose them through the existing service/CLI. OpenCode is invoked only through an argv-based adapter using the verified `opencode run --format json --dir … --model …` contract. Large logs remain outside the database while compact receipts are durable.

**Tech Stack:** Python 3.11+, stdlib only, SQLite/WAL, `unittest`, OpenCode CLI 1.18.30.

## Global Constraints

- Do not rebuild or replace the existing control plane.
- Preserve the 89-test baseline and all existing CLI behavior.
- Use only explicit configured roots; never scan all of `$HOME`.
- Never pass OpenCode `--auto`; no implicit tool approval.
- Use `opencode/space-bunny-free` only as verified mutable resource state, not adapter pricing/expiration logic.
- No stash, discard, reset, commit, push, deploy, overwrite, or external side effect without explicit permission.
- Provider exit code zero is not task success until validation passes.
- Raw logs and secrets never enter normal state or normal output.
- Use logical commits and leave the final repository clean.

---

### Task 0: Verify and commit the baseline

**Files:**
- Modify: `.gitignore`
- Create: `docs/superpowers/specs/2026-09-25-opencode-execution-design.md`
- Create: `docs/superpowers/plans/2026-09-25-opencode-execution-extension.md`

- [ ] Run `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -q` and record 89 passing tests.
- [ ] Run isolated `./gg init --scan`, `projects`, `resources`, `route`, `opportunities`, `doctor`, and `export` smoke commands with `GG_STATE_DIR` set to a temporary directory.
- [ ] Verify `.gitignore` excludes `.grokgeneral/`, state/runtime/backup directories, database files, logs, `.env` files, and secret-shaped credential files while retaining `.env.example`.
- [ ] Inspect `git status --short`; ensure no runtime state, caches, provider output, or credentials are staged.
- [ ] Commit baseline as `chore: establish verified grokgeneral baseline`.

### Task 1: Add canonical project identity and explicit roots

**Files:**
- Modify: `grokgeneral/models.py`, `grokgeneral/projects.py`, `grokgeneral/storage.py`, `grokgeneral/service.py`, `grokgeneral/cli.py`
- Create: `tests/test_identity.py`

**Interfaces:**
- Extend `Project` with `canonical_id`, `aliases`, `kind`, `priority`, and `root`.
- Add `RootRegistry(state)`: `list()`, `add(path)`, `remove(identifier)`, `scan_projects()`.
- Add `ProjectRegistry.reconcile(root, aliases=None, manual=None) -> list[Project]`.
- Add `ProjectRegistry.add_alias(identifier, alias)` and `set_priority(identifier, priority)`.

- [ ] Write failing tests for canonical IDs, aliases, explicit roots, valid kinds, priority values, manual override precedence, duplicate paths, and no `$HOME` scanning.
- [ ] Add a `roots` SQLite table with path, label, enabled, created/updated timestamps; migrate schema version without deleting existing rows.
- [ ] Extend project records with backward-compatible defaults and preserve unknown existing metadata.
- [ ] Implement safe root validation: configured paths only, resolved absolute paths, no implicit home expansion during scans.
- [ ] Add CLI `gg roots`, `gg root add PATH`, `gg root remove ID`, `gg project alias add PROJECT ALIAS`, `gg project priority PROJECT core`, and `gg projects --priority core`.
- [ ] Make scan heuristics assign `kind` and numeric `priority` only when no manual value exists; manual values win.
- [ ] Reconcile requested important projects by explicit path and leave missing paths absent.
- [ ] Run `python3 -m unittest tests.test_identity tests.test_projects tests.test_cli -v` and commit as `feat: add project identity and roots`.

### Task 2: Upgrade the OpenCode adapter using the verified CLI contract

**Files:**
- Modify: `grokgeneral/adapters.py`, `grokgeneral/resources.py`, `grokgeneral/service.py`, `grokgeneral/cli.py`
- Create: `tests/test_opencode_adapter.py`

**Interfaces:**
- `OpenCodeAdapter.health() -> dict` includes executable, version, model-listing status, and invocation format.
- `OpenCodeAdapter.models(provider=None, refresh=False) -> list[dict]`.
- `OpenCodeAdapter.run(request, cwd, model, timeout, dry_run=False, approvals=None) -> dict` returns argv, exit code, parsed JSONL events, compact text summary, timestamps, and redacted output references.
- `AdapterRegistry` exposes `health`, `models`, and `run` without embedding model pricing or expiration.

- [ ] Write failing tests with a fake executable for exact argv, `--pure`, `--format json`, `--dir`, `--model`, timeout, exit 0/1, JSONL parsing, invalid model error, and dry-run.
- [ ] Implement executable/version probing with `shutil.which` and `subprocess.run` using bounded timeout and minimal environment.
- [ ] Implement `models()` by invoking the documented `opencode models [provider] --verbose` surface, parsing output without credentials.
- [ ] Implement `run()` with explicit argv, no shell, no `--auto`, explicit cwd, model, timeout, captured stdout/stderr, and redacted compact output.
- [ ] Parse JSONL events defensively; retain event type, session ID, text, token/cost metadata, and error message only.
- [ ] Update the `space-bunny` resource in mutable state to model `opencode/space-bunny-free`, with verification timestamp/source metadata; do not hardcode it in adapter selection.
- [ ] Add `gg adapter opencode health`, `gg adapter opencode models`, and `gg adapter opencode run --dry-run …` commands with JSON output.
- [ ] Run adapter tests and commit as `feat: add verified opencode adapter`.

### Task 3: Persist compact execution receipts and raw-log separation

**Files:**
- Modify: `grokgeneral/storage.py`, `grokgeneral/models.py`, `grokgeneral/service.py`, `grokgeneral/cli.py`
- Create: `grokgeneral/executions.py`, `tests/test_executions.py`

**Interfaces:**
- `ExecutionRegistry(state)`: `start(data)`, `complete(execution_id, result)`, `fail(execution_id, error)`, `get(execution_id)`, `list(task_id=None, project=None)`.
- Receipt fields: execution ID, task/project, executor/provider/model, start/end, status, exit code, context hash, summary, artifacts, validation result, known usage, redacted error.
- Large output path is `<state>/execution-logs/<execution-id>.jsonl`; normal database rows contain only compact metadata.

- [ ] Write failing tests for receipt lifecycle, task/project filters, redacted errors, raw-log containment, and context hash stability.
- [ ] Add an `executions` table and migration with indexed task/project/status/timestamps.
- [ ] Implement atomic raw-log writes outside normal state and compact receipt persistence.
- [ ] Add `gg executions`, `gg execution show ID`, and `gg task executions TASK_ID`.
- [ ] Run receipt tests and commit as `feat: persist execution receipts`.

### Task 4: Add repository snapshots, validation, permissions, and lifecycle states

**Files:**
- Modify: `grokgeneral/models.py`, `grokgeneral/projects.py`, `grokgeneral/tasks.py`, `grokgeneral/service.py`, `grokgeneral/policies.py`, `grokgeneral/cli.py`
- Create: `grokgeneral/validation.py`, `tests/test_execution_safety.py`

**Interfaces:**
- `RepositorySnapshot.capture(path) -> dict` records branch, HEAD, porcelain status, and existing-change paths.
- `ValidationRunner.run(project, commands, approvals) -> dict` accepts only argv arrays and never mutates repository state.
- `PermissionPolicy.authorize(permission, approvals) -> PolicyDecision` for inspect, modify, validate, commit, push, deploy, spend, external.
- Task statuses include `routed` and `validating`; terminal success requires validation result `passed`.

- [ ] Write failing tests for dirty-repo snapshots, branch/HEAD capture, argv-only validation, timeout, permission separation, no stash/reset/discard behavior, and zero-exit-without-validation failure.
- [ ] Implement read-only git inspection using argv subprocesses; never invoke cleanup or mutation commands.
- [ ] Add project validation command arrays with manual-only configuration and safe serialization.
- [ ] Extend lifecycle transitions and execution pipeline: route → context → safety → provider → receipt → validation → completed/failed.
- [ ] Require explicit modify/commit/push/deploy/spend/external approvals; inspect and validate remain separately controlled.
- [ ] Add `gg executor run TASK_ID --dry-run` and `gg executor run TASK_ID` with permission flags and JSON receipts.
- [ ] Run safety/lifecycle tests and commit as `feat: add safe execution lifecycle`.

### Task 5: Make resource expiry invalidate routing and reroute queued work

**Files:**
- Modify: `grokgeneral/resources.py`, `grokgeneral/router.py`, `grokgeneral/opportunities.py`, `grokgeneral/tasks.py`, `grokgeneral/service.py`, `grokgeneral/cli.py`
- Create: `tests/test_expiry_reroute.py`

**Interfaces:**
- `ResourceRegistry.refresh_expirations()` emits once, invalidates route/opportunity cache entries, and returns changed resource IDs.
- `Router.route()` never returns an expired resource even when a cached decision exists.
- `TaskRegistry.reroute_queued(changed_resources=None) -> list[Task]`.
- CLI supports `gg resources --expiring` and `gg resources --expired`.

- [ ] Write failing tests for expiration event-once behavior, cache invalidation, expired-resource exclusion, queued reroute, and no reroute when no capable fallback exists.
- [ ] Make resource status refresh authoritative at routing time.
- [ ] Invalidate only affected cache kinds and reroute queued tasks with dependency checks.
- [ ] Emit resource and task events for each reroute decision.
- [ ] Run expiry tests and commit as `feat: reroute work on resource expiry`.

### Task 6: Run tests, one safe dogfood task, and finalize documentation

**Files:**
- Modify: `README.md`, `docs/superpowers/specs/2026-09-25-opencode-execution-design.md`
- Create: `tests/test_dogfood.py` only if a deterministic fixture dogfood is useful.

- [ ] Run the complete suite with `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -q`.
- [ ] Run all new adapter/receipt/safety/expiry tests and verify no network or paid API is required.
- [ ] Run one real read-only GrokGeneral task against a clean temporary project with `opencode/space-bunny-free`, explicit `--dir`, bounded timeout, no `--auto`, and no repository mutation.
- [ ] Run the project's approved validation argv and confirm the receipt, context hash, timestamps, exit code, and validation result.
- [ ] Verify the resulting repository has no secrets, raw logs, state DBs, temporary files, or unrelated changes.
- [ ] Update README with only verified commands and limitations.
- [ ] Commit as `feat: complete verified opencode execution path` and leave `git status --short` empty.

## Verification Matrix

| Requirement | Evidence |
| --- | --- |
| Baseline preserved | Existing 89 tests plus baseline smoke |
| Identity/roots | `tests/test_identity.py` and CLI commands |
| OpenCode contract | Fake adapter tests plus installed CLI health/model output |
| Space Bunny mapping | Mutable resource record and verified model listing |
| Receipts | `tests/test_executions.py`, CLI receipt commands |
| Safety/lifecycle | `tests/test_execution_safety.py` |
| Expiry/reroute | `tests/test_expiry_reroute.py` |
| Dogfood | One real read-only task with validation and receipt |
| Clean repository | Final `git status --short` and secret scan |
