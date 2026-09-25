# GrokGeneral

GrokGeneral is the local kernel and control plane for the GrokBot ecosystem. It indexes projects and resources, normalizes work, chooses the cheapest capable execution path, packages narrow context, records decisions and usage, and surfaces temporary capacity opportunities.

It is not a general-purpose autonomous agent. GrokGeneral performs little expensive reasoning itself. Work is delegated to registered resources or the local shell only after capability, policy, safety, and approval checks.

## Design

- **Kernel:** `GrokGeneral` is the public service facade used by the CLI and available to future local services.
- **Registries:** projects, resources, tasks, policies, usage, events, and cache metadata.
- **Policy and routing:** deterministic, capability-aware, cost-aware, explainable decisions with fallbacks.
- **Opportunity engine:** interpretable project importance × suitability × expiration urgency ÷ effective cost scoring. Scores are planning aids, not precision claims.
- **Context builder:** task-scoped packs with compact policy, one project, safe manifest/status data, and explicitly referenced files only.
- **Adapters:** local shell is functional; OpenCode, Cursor, ChatGPT, GrokBot, and GitHub are optional capability/health boundaries.
- **Persistence:** SQLite with WAL, transactions, a busy timeout, private permissions, and atomic JSON policy/cache/context writes.

No AI provider or API key is required. The default path works offline.

## Requirements

- Python 3.11 or newer.
- No third-party runtime dependencies.

## Run

From this directory:

```sh
./gg --help
python3 -m grokgeneral --help
```

Install the `gg` console script in an isolated environment if desired:

```sh
python3 -m pip install -e .
```

The repository also includes an executable `./gg` launcher; it does not modify the user's shell `PATH`.

## State

Default state is `~/.grokgeneral/`. Override it with `GG_STATE_DIR` or `--state-dir`:

```sh
GG_STATE_DIR=/tmp/gg-state ./gg status --json
./gg --state-dir /tmp/gg-state status --json
```

The state directory contains `state.db`, `policies.json`, private cache artifacts, and context packs. The directory is created with user-only permissions. SQLite is the source of truth for normalized records and event/audit history. JSON files are replaced atomically.

The policy objective and safety rules are inspectable at:

```sh
./gg policy show --json
```

The default policy prefers existing results, deterministic local work, free and expiring capacity, cheap capable resources, and premium/GrokBot capacity only when justified. Network, spending, push, posting, and destructive actions require explicit approval.

## Quick start

```sh
./gg init --scan
./gg projects --json
./gg resources --json
./gg opportunities --json
./gg optimize --json
```

`init --scan` safely scans immediate children of `~/startups/` (or `GG_STARTUPS_ROOT`) and records only filesystem-visible facts. It never executes project setup scripts. Use `--all` to include every non-hidden immediate directory.

The first initialization also seeds a local shell resource named `local`. It is deterministic, free, and used as a safe fallback. The first initialization seeds an ordinary temporary resource named `space-bunny`:

- provider: `opencode`
- executor/model: `Space Bunny`
- cost: free
- availability: unlimited
- capabilities: coding, repository analysis, refactoring, testing
- expiration: seven days after the first seed

It is normal mutable state, not a permanent source-code special case. Update or expire it with:

```sh
./gg resource update space-bunny --set expires_at=2026-10-01T00:00:00Z
./gg resource expire space-bunny
./gg event emit RESOURCE_AVAILABLE --resource space-bunny --expires 7d --json
```

A resource event immediately recalculates relevant opportunities. Expiration and exhaustion are detected during status/opportunity refresh and recorded once as events.

## Commands

All important commands accept `--json` before or after the command.

### Projects

```sh
./gg projects
./gg projects scan --json
./gg project show gh0st
./gg project add --id gh0st --name gh0st --path "$HOME/startups/gh0st" --priority 80
./gg project update gh0st --set priority=90
```

### Resources

```sh
./gg resources
./gg resource show space-bunny
./gg resource add --id cursor --name cursor --provider cursor \
  --cost-class cheap --capabilities coding,architecture-review --availability limited
./gg resource update cursor --set remaining_capacity=100
./gg resource expire cursor
```

### Tasks and routing

```sh
./gg tasks
./gg task add "audit tests" --project gh0st --capabilities repo-analysis,testing --json
./gg task show TASK_ID
./gg task route TASK_ID --json
./gg task run TASK_ID
./gg task complete TASK_ID --output '[{"result":"ok"}]'
./gg task fail TASK_ID "reason"
```

Tasks have explicit lifecycle states: `proposed`, `queued`, `running`, `completed`, `failed`, `blocked`, and `cancelled`. A task with a command runs only an explicit argv list. It never invokes a shell string. A missing command routes/queues the task; it does not call a model automatically.

Routing output contains the selected executor, provider, model, rationale, fallbacks, cost class, policy matches, and candidate scores:

```sh
./gg route "audit all startup repos" --json
./gg route --project gh0st "fix failing build" --json
```

### Opportunities and scheduling

```sh
./gg opportunities
./gg opportunities --resource space-bunny
./gg optimize
./gg optimize --execute --max-tasks 5
./gg schedule
./gg schedule --execute
```

`optimize` and `schedule` are planning operations by default. `--execute` only queues proposal work or runs explicitly local task commands; it does not silently call paid providers or spend credits. The lightweight scheduler is an on-demand plan, not a background daemon.

### Events

```sh
./gg events
./gg events --event-type RESOURCE_AVAILABLE
./gg event emit PROJECT_DISCOVERED --project gh0st --json
./gg event subscribe RESOURCE_AVAILABLE opportunity-watcher
./gg event deliver EVENT_ID
```

Events are durable SQLite records with local subscriptions and idempotent delivery tracking. A broker is not required for v1.

### Context and cache

```sh
./gg context TASK_ID
./gg context TASK_ID --json
./gg cache status
./gg cache inspect
./gg cache prune
```

Context references are constrained to the selected project. Traversal, hidden files, `.env` files, oversized files, and unrelated-project references are rejected. Cache keys are deterministic, values are redacted, and TTL/invalidation/pruning are supported.

### Usage and deterministic questions

```sh
./gg usage
./gg usage --project gh0st
./gg usage --resource space-bunny
./gg usage --record --source measured --units 12 --project gh0st
./gg ask "Which projects need testing?"
./gg ask "What should Space Bunny work on this week?"
./gg ask "Which repos have unfinished builds?"
./gg ask "Where can Cursor credits create the most value?"
```

Usage always identifies its source as `measured`, `user-entered`, `estimated`, or `unknown`. Unknown totals remain unknown; GrokGeneral does not fabricate cost precision. `ask` is deterministic by default and does not call an AI provider.

### Health, export, and adapters

```sh
./gg status
./gg doctor
./gg adapters
./gg export backup.json
./gg import backup.json
```

`doctor` checks SQLite integrity, policy validity, project paths, duplicate identities, stale resources, event delivery state, and optional adapter health. Missing provider binaries are reported as degraded optional adapters, not startup failures.

## Safety model

- Read-only inspection, planning, and local testing are allowed by default.
- Local commands require explicit argv and a bounded timeout.
- Network, spending, push, posting, privacy-sensitive, and destructive operations require explicit approval flags or policy approval.
- Provider adapters do not contain credentials and do not read or persist secrets.
- Secret-shaped keys and common secret assignments are redacted before state, cache, event, context, and output persistence.
- No network operation is required for tests or normal operation.

## Tests and validation

Run the full offline suite:

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q grokgeneral tests
```

The tests cover persistence, atomic writes, concurrent writers, project/resource registries, expiration, policies, routing, task lifecycle, adapters, opportunities, events, context isolation, cache, usage, malformed input, offline behavior, CLI JSON, and service integration.

## Deliberate v1 limitations

- There is no persistent daemon, distributed broker, or HTTP server. The service facade is the boundary for a future local API/dashboard.
- Optional provider integrations expose capability and health boundaries; they do not guarantee vendor-specific workflows.
- Backlog inspection proposes findings and tasks but never changes neighboring repositories.
- Build status is evidence-based. GrokGeneral does not claim a build is broken without recorded evidence or an explicitly run local command.
- The scheduler is on-demand; no background process consumes resources while the CLI is closed.
- The repository contains no marketing website; a future site belongs in `~/startups/grokgeneral-website/`.
