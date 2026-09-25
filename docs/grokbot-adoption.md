# GrokGeneral Adoption

GrokGeneral is the authoritative shared control plane for GrokBot projects and workflows.

## Runtime boundary

```text
user
  → GrokBot (intent, interaction, supervision)
  → GrokGeneral (state, priorities, opportunities, routing, approvals, scheduling, receipts, usage)
  → cheapest capable executor
  → validation
  → compact result
  → GrokBot
```

GrokBot is registered once in GrokGeneral as `grokbot-orchestrator`. It is scarce, limited-capacity, high-cost orchestration capacity for user interaction, cross-system decisions, approvals, and coordination. It is not the default coding, testing, repository-inspection, refactoring, or documentation executor.

## Authoritative state

The following shared operational state belongs only in GrokGeneral:

- project identity, roots, priority, kind, and recorded health;
- shared task backlog and lifecycle;
- executor/resource availability, capabilities, cost, expiration, and health;
- global routing policy and route decisions;
- narrow approvals;
- opportunity observations and work deduplication;
- scheduler claims and bounded cycles;
- execution receipts, raw local logs, compact results, and usage.

GrokBot repositories may retain domain-private state and policy. They must not copy the shared task queue, resource table, approvals, receipts, or usage ledger.

## Compact operating loop

1. Read `gg contract status --json` for bounded global status.
2. Read a project-scoped opportunity set with `gg opportunities --project ID --limit N --json`.
3. Submit only the exact task and project identity to GrokGeneral.
4. Route the task and inspect its approval requirements.
5. Request and consume narrow approval IDs.
6. Run one bounded scheduler cycle.
7. Read the compact result and pending approvals; do not consume raw execution transcripts.

Planning and execution remain separate. Do not run broad work across the portfolio, and do not start a daemon or HTTP API.

## Ecosystem classification

| Thing | Classification | Adoption decision |
| --- | --- | --- |
| Host GrokBot | persistent GrokBot | Keep one; UI, intent, supervision, and coordination only. |
| `grokbot-office` | project / synthetic role registry | Keep role and handoff definitions; migrate global routing, tasks, resources, approvals, receipts, and usage to GrokGeneral. |
| `grokbot-concierge` | domain project / domain orchestrator | Keep durable travel-project and fact state; use GrokGeneral for shared backlog, routing, execution, and receipts. |
| `grokbot-society` | domain runtime / synthetic people | Keep durable people, roles, relationships, and social state; it is not a general persistent GrokBot. |
| `agentos` | execution substrate | Keep provider-neutral capabilities and workers; GrokGeneral is the shared scheduler and resource ledger. |
| `grokmax` | workflow / worker | Keep prompt compilation and benchmarks; consume GrokGeneral routing, execution, and receipts. |
| `autobuilder` | workflow / project fleet tooling | Keep project build state; centralize global backlog, priorities, and execution. |
| `grokinstall` | skill / capability broker | Keep capability installation and contracts; centralize project/task/execution state. |
| `gh0st` and startup projects | projects | Registered in GrokGeneral; no duplicate global state. |
| `opsautopilot` | project / workflow cockpit | Keep product-specific workflow UX; move recurring tasks, queues, and approvals to GrokGeneral. |
| `opencode-watchdog` | resource / local safety tool | Keep the circuit breaker as an executor-safety adapter; do not create a persistent bot for it. |
| Autobuilder fleet claims and cooldowns | ephemeral workers / workflow state | Keep per-project build state; migrate global claims, retries, cooldowns, and receipts to GrokGeneral. |

## Consolidation candidates

- Move `grokbot-office` routing, live-role roster, materialization decisions, routine queues, and usage summaries toward GrokGeneral resources, tasks, approvals, and receipts.
- Make `grokmax` and `autobuilder` submit tasks and consume compact results rather than owning parallel global schedulers.
- Keep domain-persistent state in Q-Concierge and GrokBot Society because it is not generic operational state.
- Keep GrokInstall capabilities as skills; do not turn every capability into a persistent bot.
- Keep AgentOS as an execution adapter/substrate; do not duplicate the central resource ledger.
- Keep OpenCode Watchdog as a local circuit breaker, not a persistent bot or scheduler.
- Keep Autobuilder per-project build/portfolio state, but migrate fleet claims, cooldowns, and execution history.
- Keep OpsAutopilot's domain cockpit and reports, but submit its recurring work to GrokGeneral.

No existing system is deleted or restructured by this adoption record.

## Current resource facts

Resource availability and expiration are queried from GrokGeneral, not copied here. As of adoption, the registered resources are:

- `local`: free deterministic shell;
- `space-bunny`: temporary free OpenCode capacity, model `opencode/space-bunny-free`;
- `grokbot-orchestrator`: scarce high-value orchestration capacity.

## Safety

- Do not mirror GrokGeneral state into GrokBot projects.
- Do not use GrokBot for routine implementation work.
- Do not broadcast the same resource fact to every project; update the resource once in GrokGeneral.
- Do not execute dirty repositories or mutate projects without explicit narrow approval.
- Do not place secrets, raw logs, or repository contents in the GrokBot contract.
