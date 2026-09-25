# GrokGeneral v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a working, offline-first GrokGeneral control plane with registries, deterministic routing, opportunity planning, safety gates, context packs, cache, usage accounting, adapters, and a tested `gg` CLI.

**Architecture:** Use a stdlib-only Python 3.11+ package. SQLite is the transactional source of truth for normalized records and audit/event history; inspectable JSON policy and cache files use atomic replacement. A `GrokGeneral` service facade coordinates focused modules, while the CLI is a thin JSON/human presentation layer. Provider adapters are optional capability boundaries and never required for startup or tests.

**Tech Stack:** Python 3.11+, `argparse`, `sqlite3`, `dataclasses`, `datetime`, `hashlib`, `json`, `pathlib`, `subprocess`, `unittest`.

## Global Constraints

- Work only in `/Users/matador/startups/grokgeneral/`; do not modify neighboring startup projects.
- Use no third-party runtime dependencies and no paid or network-required tests.
- Default state is `~/.grokgeneral/`; `GG_STATE_DIR` and `--state-dir` override it.
- Use SQLite transactions, restrictive permissions, and atomic JSON replacement for durable writes.
- Never store or print secrets; redact secret-shaped keys in persisted values and diagnostics.
- Read-only inspection and local testing are allowed by default; network, spending, push, posting, and destructive actions require explicit approval.
- Routing is deterministic and explainable; no automatic AI/API calls are permitted.
- `optimize` is planning by default; execution requires `--execute` and never silently spends resources.
- Do not commit changes unless the user explicitly requests a commit.

## File Map

- Create `pyproject.toml`: package metadata, Python floor, and `gg` console script.
- Create `grokgeneral/__init__.py`: version and public exports.
- Create `grokgeneral/errors.py`: typed user-facing errors and exit-code mapping.
- Create `grokgeneral/timeutil.py`: UTC timestamps, ISO parsing, duration parsing, and expiration helpers.
- Create `grokgeneral/models.py`: normalized project/resource/task/policy/usage/event/cache dataclasses.
- Create `grokgeneral/storage.py`: JSON redaction, atomic writes, SQLite schema, transactions, export/import, and audit helpers.
- Create `grokgeneral/policies.py`: default inspectable policies, policy loading, and safety evaluation.
- Create `grokgeneral/projects.py`: read-only startup discovery and project CRUD.
- Create `grokgeneral/resources.py`: resource CRUD, effective availability, expiration, and Space Bunny seeding.
- Create `grokgeneral/tasks.py`: normalized task CRUD and lifecycle transitions.
- Create `grokgeneral/events.py`: persistent event bus, event types, and local subscriptions.
- Create `grokgeneral/cache.py`: deterministic content-addressed cache with invalidation and pruning.
- Create `grokgeneral/usage.py`: measured/user-entered/estimated/unknown usage records and aggregates.
- Create `grokgeneral/router.py`: capability filtering, policy-aware ranking, fallbacks, and route persistence.
- Create `grokgeneral/context.py`: task-scoped context packs with byte estimates and path safety.
- Create `grokgeneral/opportunities.py`: resource arbitrage scoring, proposed backlog work, and optimization plans.
- Create `grokgeneral/backlog.py`: read-only project inspection findings converted to proposals.
- Create `grokgeneral/adapters.py`: local shell plus optional provider/GitHub boundary adapters and health probes.
- Create `grokgeneral/doctor.py`: prioritized health checks and remediation output.
- Create `grokgeneral/service.py`: public `GrokGeneral` facade, status, deterministic ask, export/import, and orchestration.
- Create `grokgeneral/cli.py`: argparse command tree, JSON/human rendering, and exit codes.
- Create `grokgeneral/__main__.py`: `python -m grokgeneral` entry point.
- Create `gg`: executable local launcher for `./gg` smoke use.
- Create `tests/test_*.py`: focused unit, integration, CLI, persistence, and concurrency tests.
- Create `README.md`: truthful architecture, command, state, safety, and limitation documentation.

### Task 1: Foundation and durable state

**Files:**
- Create `pyproject.toml`, `grokgeneral/__init__.py`, `grokgeneral/errors.py`, `grokgeneral/timeutil.py`, `grokgeneral/models.py`, `grokgeneral/storage.py`.
- Test `tests/test_storage.py`, `tests/test_models.py`.

**Interfaces:**
- `utc_now() -> datetime`, `parse_time(value: str | None) -> datetime | None`, `parse_duration(value: str) -> timedelta`.
- `GrokGeneralError`, `ValidationError`, `NotFoundError`, `SafetyBlockedError`, `ProviderUnavailableError`.
- `atomic_write_json(path: Path, value: object) -> None`, `redact(value: object) -> object`.
- `StateStore(state_dir: Path | str | None = None)`: `initialize()`, `connect()`, `transaction()`, `export_snapshot()`, `import_snapshot()`, `audit()`.

- [ ] Write tests for UTC/duration parsing, JSON redaction, schema initialization, persistence across reopened stores, export/import, and concurrent transactions.
- [ ] Run `python3 -m unittest tests.test_storage tests.test_models -v`; expect import failures before implementation.
- [ ] Implement JSON helpers with same-directory temporary files, `fsync`, `os.replace`, and mode `0600`.
- [ ] Implement a SQLite schema with `projects`, `resources`, `tasks`, `policies`, `events`, `subscriptions`, `usage`, `cache_entries`, `opportunities`, `audit_log`, and `meta` tables. Store flexible record fields as canonical JSON text and indexed scalar columns for IDs, status, timestamps, and paths.
- [ ] Enable foreign keys, WAL, busy timeout, and `BEGIN IMMEDIATE` for write transactions. Ensure parent directory permissions are `0700` and database/cache files are private.
- [ ] Implement row conversion and validation helpers for all normalized records, rejecting malformed JSON with actionable errors.
- [ ] Run the focused tests; expect all to pass and no files outside the target state directory to be touched.

### Task 2: Policies and safety boundaries

**Files:**
- Create `grokgeneral/policies.py`.
- Test `tests/test_policies.py`.

**Interfaces:**
- `PolicyEngine(state: StateStore)`: `load()`, `evaluate(task: Task, project: Project | None, resource: Resource, approvals: set[str]) -> PolicyDecision`, `authorize(action: str, approvals: set[str], policy: dict) -> PolicyDecision`.
- `PolicyDecision` fields: `allowed: bool`, `requires_approval: bool`, `matches: list[str]`, `reasons: list[str]`.

- [ ] Write tests for default conservation ordering, capability requirements, cost ceilings, context limits, privacy/network rules, destructive/push/spend/post approvals, and malformed policy data.
- [ ] Implement a default policy document containing the global objective, preferred cost order, GrokBot conservation rule, capability mapping, fallback, retry, concurrency, and safety rules.
- [ ] Persist the policy document outside code at `<state>/policies.json` using atomic writes; load and validate it on every service operation.
- [ ] Implement deterministic policy matching with stable rule identifiers and no network calls.
- [ ] Run `python3 -m unittest tests.test_policies -v`; expect all policy tests to pass.

### Task 3: Project and resource registries

**Files:**
- Create `grokgeneral/projects.py`, `grokgeneral/resources.py`.
- Test `tests/test_projects.py`, `tests/test_resources.py`.

**Interfaces:**
- `ProjectRegistry(state)`: `list()`, `get(identifier)`, `add(data)`, `update(identifier, changes)`, `scan(root, include_all=False)`, `backlog_candidates()`.
- `ResourceRegistry(state, events)`: `list()`, `get(name)`, `add(data)`, `update(name, changes)`, `expire(name, at=None)`, `effective(resource)`, `seed_defaults()`.

- [ ] Write tests for safe immediate-child scanning, manifest/repository detection, duplicate identities, unknown-field preservation, project updates, resource CRUD, expiration, exhaustion, health filtering, and idempotent Space Bunny seeding.
- [ ] Implement scanner rules: skip hidden/archive/cache directories, never execute project code, preserve existing user fields, and derive only facts visible in the filesystem.
- [ ] Seed `space-bunny` with provider `opencode`, executor/model `Space Bunny`, free cost, unlimited availability, coding/repository-analysis/refactoring/testing capabilities, and `expires_at = seed time + 7 days`. Do not reset an existing expiration.
- [ ] Emit `PROJECT_DISCOVERED`, `RESOURCE_AVAILABLE`, `RESOURCE_EXPIRING`, and `RESOURCE_EXHAUSTED` events through the event boundary.
- [ ] Run registry tests and verify a temporary state directory can be removed after close.

### Task 4: Tasks and deterministic router

**Files:**
- Create `grokgeneral/tasks.py`, `grokgeneral/router.py`.
- Test `tests/test_tasks.py`, `tests/test_router.py`.

**Interfaces:**
- `TaskRegistry(state, events, policies, resources)`: `list(filters)`, `get(task_id)`, `add(data)`, `update(task_id, changes)`, `route(task_id)`, `run(task_id, approvals)`, `complete(task_id, outputs)`, `fail(task_id, error)`.
- `Router(state, policies, resources, cache)`: `route(task, project=None, approvals=None) -> RouteDecision`, `route_goal(goal, project=None) -> RouteDecision`.
- `RouteDecision` serializes `executor`, `provider`, `model`, `reason`, `fallbacks`, `context_pack`, `estimated_cost_class`, `policy_matches`, `candidates`, and `cache_hit`.

- [ ] Write tests for capability filtering, expired/exhausted/unhealthy exclusion, free-before-paid ordering, expiring-resource preference, project preferences/fallbacks, context limits, policy denials, stable rationale, and fallback lists.
- [ ] Implement task validation and legal state transitions: proposed, queued, running, completed, failed, blocked, cancelled. Reject invalid transitions and missing dependencies.
- [ ] Implement deterministic candidate scoring components and retain each component in the decision for explainability. Include a cache reuse check before provider selection.
- [ ] Make `gg route` goals ephemeral by default; `gg task route` persists the rationale and assignment.
- [ ] Make `run` execute only an explicit local command from task metadata after safety evaluation; otherwise leave the task queued with a clear reason.
- [ ] Run task and router tests; verify no subprocess or network call occurs during ordinary routing.

### Task 5: Events, cache, usage, and context

**Files:**
- Create `grokgeneral/events.py`, `grokgeneral/cache.py`, `grokgeneral/usage.py`, `grokgeneral/context.py`.
- Test `tests/test_events.py`, `tests/test_cache.py`, `tests/test_usage.py`, `tests/test_context.py`.

**Interfaces:**
- `EventBus(state)`: `emit(event_type, payload)`, `list(limit)`, `subscribe(event_type, name, action)`, `deliver(event_id)`.
- `Cache(state)`: `key(kind, payload)`, `get(key)`, `put(key, value, kind, ttl_seconds=None, metadata=None)`, `invalidate(kind=None, key=None)`, `status()`, `inspect()`, `prune()`.
- `UsageLedger(state)`: `record(...)`, `list(project=None, resource=None)`, `summary(...)`.
- `ContextBuilder(state, projects, cache)`: `build(task, project=None, write=True) -> ContextPack`.

- [ ] Write tests for event ordering, local subscriber delivery, no broker requirement, deterministic cache keys, secret redaction, TTL/invalidation/pruning, usage source labels, context path traversal rejection, project isolation, and byte estimates.
- [ ] Implement event constants for all requested event types and durable delivery records. Keep subscribers local and idempotent by event ID.
- [ ] Implement canonical SHA-256 cache keys over redacted, sorted JSON input. Store only non-secret values and enforce artifact path containment.
- [ ] Implement usage records with explicit `source` values `measured`, `user-entered`, `estimated`, and `unknown`; never infer a monetary total from unknown values.
- [ ] Build context from only the target project, compact policy, task, manifest/status, and explicitly referenced files. Cap snippets and record `approx_bytes`.
- [ ] Run the four focused test modules; expect all to pass offline.

### Task 6: Opportunities, backlog, ask, status, and doctor

**Files:**
- Create `grokgeneral/opportunities.py`, `grokgeneral/backlog.py`, `grokgeneral/doctor.py`.
- Modify `grokgeneral/service.py` after the service facade is created in Task 7.
- Test `tests/test_opportunities.py`, `tests/test_backlog.py`, `tests/test_doctor.py`.

**Interfaces:**
- `OpportunityEngine(state, projects, resources, tasks, router)`: `list(resource=None)`, `optimize(max_tasks=20, execute=False)`.
- `BacklogInspector(state, projects)`: `inspect(project=None)`, `propose(findings)`.
- `Doctor(state, projects, resources, events)`: `run() -> list[Diagnostic]`.
- `GrokGeneral.ask(question) -> dict`, `GrokGeneral.status() -> dict`.

- [ ] Write tests for zero-cost expiring coding capacity, high-priority project weighting, deadline urgency, no-task suggestions, optimization non-execution, backlog proposal-only behavior, unfinished-build queries, duplicate paths, stale resources, and doctor remediation priority.
- [ ] Implement interpretable opportunity components without claiming false precision: value, suitability, project importance, expiration urgency, effective cost, and explanation.
- [ ] Implement project inspection using only manifests, CI files, TODOs, and local metadata. Convert findings into proposed tasks without modifying repositories.
- [ ] Implement deterministic answers for “which projects need testing?”, “what should Space Bunny work on?”, “which repos have unfinished builds?”, and “where can Cursor credits create value?”. Unknown facts remain unknown.
- [ ] Implement doctor checks for state, policies, paths, duplicate project paths, malformed rows, missing dependencies, provider binaries, adapter health, stale/expired resources, and event delivery backlog.
- [ ] Run focused tests and verify `space-bunny` appears in opportunities while expired when its stored expiration passes.

### Task 7: Adapters, service facade, and CLI

**Files:**
- Create `grokgeneral/adapters.py`, `grokgeneral/service.py`, `grokgeneral/cli.py`, `grokgeneral/__main__.py`, `gg`.
- Test `tests/test_adapters.py`, `tests/test_cli.py`.

**Interfaces:**
- `Adapter` protocol: `name`, `capabilities()`, `health()`, `run(request)`.
- `AdapterRegistry(state)`: `get(name)`, `all()`, `health()`.
- `GrokGeneral(state_dir=None, offline=False)`: public methods for every CLI operation.
- `main(argv: list[str] | None = None) -> int`.

- [ ] Write tests for local shell argument-list execution, timeout/failure capture, missing provider binaries, offline mode, no embedded credentials, CLI help, JSON validity, meaningful exit codes, route syntax, and all required top-level command families.
- [ ] Implement local shell with explicit argv, project working directory, timeout, captured output, and write/network/spend approval checks. Implement OpenCode, Cursor, ChatGPT, GrokBot, and GitHub as capability/health boundaries; only local probes run automatically.
- [ ] Implement the service facade as the sole orchestration entry point. It should initialize state, wire modules, scan/seed safely, route, build context, record usage, and produce status/doctor output.
- [ ] Implement argparse commands and aliases exactly as requested, with global options accepted before or after subcommands. JSON output must contain no human formatting or diagnostic noise.
- [ ] Add `pyproject.toml` console entry point and executable `gg` launcher. Do not alter the user shell PATH.
- [ ] Run CLI tests and `chmod +x gg`; verify `./gg --help` and `python3 -m grokgeneral --help`.

### Task 8: Documentation, integration tests, and smoke validation

**Files:**
- Create `README.md`.
- Create or modify `tests/test_integration.py`.
- Create `.gitignore`.

**Interfaces:**
- Public behavior is exercised through `GrokGeneral` and `gg` only; tests use temporary state directories and fixture project trees.

- [ ] Write integration tests for the complete offline flow: initialize, scan fixture projects, seed Space Bunny, add task, route, context, opportunity, event, cache, usage, complete, export, import, and doctor.
- [ ] Run the full suite with `python3 -m unittest discover -s tests -v`; require zero failures and zero errors.
- [ ] Run syntax validation with `python3 -m compileall -q grokgeneral tests`.
- [ ] Run available lint/type commands only if installed; otherwise record that no project linter/type checker is configured and use compileall plus unittest.
- [ ] Run isolated smoke commands using `GG_STATE_DIR=$(mktemp -d)`: `./gg init`, `./gg projects scan --json`, `./gg resources --json`, `./gg opportunities --json`, `./gg task add ... --json`, `./gg task route ... --json`, `./gg context ... --json`, `./gg optimize --json`, `./gg doctor --json`, and `./gg export ... --json`.
- [ ] Review `git status --short` inside the new repository only, inspect the diff for secrets and unrelated changes, and remove temporary test artifacts.
- [ ] Update README to describe only commands and guarantees verified by tests, including the SQLite decision, offline behavior, safety boundaries, optional adapters, and known limitations.

## Verification Matrix

| Requirement | Primary evidence |
| --- | --- |
| Project/resource registries | `tests/test_projects.py`, `tests/test_resources.py`, CLI smoke |
| Policies/router/expiry | `tests/test_policies.py`, `tests/test_router.py`, `tests/test_resources.py` |
| Task lifecycle and offline execution | `tests/test_tasks.py`, `tests/test_adapters.py` |
| Opportunities/backlog/ask | `tests/test_opportunities.py`, `tests/test_backlog.py` |
| Events/context/cache/usage | focused modules plus `tests/test_integration.py` |
| Persistence/atomicity/concurrency | `tests/test_storage.py`, `tests/test_integration.py` |
| Doctor and JSON CLI | `tests/test_doctor.py`, `tests/test_cli.py` |
| Documentation accuracy | README review against smoke output |
