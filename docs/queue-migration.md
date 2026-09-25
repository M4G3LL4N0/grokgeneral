# Shared Queue Migration Boundary

GrokGeneral is the authoritative scheduler for every GrokBot project. This
document defines the boundary external queues must respect when their work is
migrated into the shared control plane, and records which primitives already
exist so migration reuses them instead of creating a second scheduler.

## The boundary

A migrated queue stops scheduling itself. Its items become ordinary GrokGeneral
tasks, and the shared scheduler decides what runs, on which resource, with which
approvals. The external system keeps ownership of its own domain state (issue
trackers, chat transcripts, deployment records) and links to the task rather
than duplicating execution state.

Migrating a queue means:

1. Register each item as a task with `project`, `goal`, `required_capabilities`,
   `priority`, and `dependencies`.
2. Record the source in task metadata (`metadata.source`, `metadata.external_id`)
   so the origin stays traceable and re-import is idempotent.
3. Stop the legacy loop. It must not claim work that GrokGeneral now owns.

GrokGeneral does not import third-party trackers, and it does not run a second
scheduler, a daemon, or an HTTP API. Migration is a data movement plus a
shutdown of the old loop.

## Primitives that already exist

| Concept | Where it lives | Notes |
| --- | --- | --- |
| claim | `Scheduler._claim`, `task_claims` table | Transactional; a task has at most one live claim. |
| lease | claim `data.lease_expires_at` | Expired leases are ignored, so an abandoned claim frees the task. |
| retry | `SchedulerConfig.max_attempts`, `retry_failed` | Opt-in, bounded, and never retries non-retryable failures such as approval or safety blocks. |
| cooldown | `SchedulerConfig.cooldown_seconds` | Backoff after a retryable failure; a task inside cooldown is not claimable. Defaults to `0` (disabled). |
| priority | `SchedulerConfig` ordering, `Task.priority` | Higher priority first, then oldest, then id, so ordering is deterministic. |
| dependency | `Task.dependencies` | A task whose dependencies are incomplete is planned but not claimable. |
| owner | claim `data.owner`, `data.owner_id` | Identifies the owning scheduler run for audit and migration checks. |
| expiry | lease, `Task.deadline`, resource expiration | Deadlines and expiring resources remove work from a cycle. |
| mutation isolation | claim `data.mutation`, `project_key` | Mutating claims for one repository are serialized; read-only claims run concurrently. |
| pause/stop | `Scheduler.pause`/`resume`/`stop` | Cooperative control state checked before each claim. |

## Deliberately not added

- **Heartbeat renewal.** A cycle is bounded by `max_seconds` (at most one hour)
  and a dispatched task moves to `running`, so a lease cannot outlive its work in
  a way that permits a second claim. Renewal would add machinery with no case it
  protects against today. If long-running executions later exceed `max_seconds`,
  add renewal here rather than loosening the claim check.
- **A second queue or scheduler.** Duplicate loops in GrokBot Office, AgentOS,
  GrokMax, and Autobuilder stay until their work is migrated, then they are shut
  down. They are not bridged.

## Verifying a migration

- `gg status` shows tasks by status and running work without rescanning projects.
- `gg schedule` plans a cycle without executing; `gg schedule --execute` runs one
  bounded cycle and records claims, leases, and results.
- Claims are queryable in the `task_claims` table with `owner`, `owner_id`,
  `lease_expires_at`, and the resulting `status`.
- A migrated task that fails enters cooldown rather than being retried every
  cycle, so a broken external integration cannot hot-loop the shared scheduler.
