# GrokGeneral v1 Design

Date: 2026-09-24

## Objective

GrokGeneral is the local kernel and control plane for the GrokBot ecosystem. It indexes projects and resources, normalizes work, chooses the cheapest capable execution path, packages narrow context, records decisions and usage, and surfaces temporary capacity opportunities. It does not require an AI provider and does not perform hidden model calls.

## Approach

The v1 uses Python's standard library only. SQLite provides transactional, concurrent, queryable state for projects, resources, tasks, events, usage, opportunities, audit records, and cache metadata. Policies and exported/imported snapshots are JSON so policy changes remain inspectable and portable. JSON files are replaced atomically; SQLite uses transactions, WAL mode, a busy timeout, and restrictive local permissions.

The package is split into a service facade and focused modules. The CLI only translates arguments and formats output. Routing, safety, and execution decisions live in the core so future API, daemon, or dashboard integrations can reuse them.

## Boundaries

- Project scanning is read-only and preserves user-maintained project fields.
- Resource state is ordinary mutable state. The initial Space Bunny record is seeded with a relative seven-day expiration and can expire or be updated.
- Routing filters unavailable, expired, exhausted, unhealthy, and incapable resources before ranking capable candidates.
- Local shell execution accepts explicit argument lists only. Network, spending, pushing, posting, and destructive actions require explicit approval flags or policy.
- Provider adapters expose capability and health probes but remain optional. Missing provider binaries produce degraded health, not startup failure.
- Cached values are scrubbed for secret-shaped keys and are never populated from environment secrets.
- Opportunity output is a plan. `optimize --execute` may create queued/proposed work only; it does not silently spend resources or call an AI provider.

## Data flow

1. `StateStore` opens or initializes the local database and policy file.
2. Registries load and validate rows, emitting persistent audit events for changes.
3. A task is normalized and checked against safety policies.
4. The policy engine filters resources; the router ranks them with capability, cost, health, project preference, capacity, and expiration urgency.
5. A route decision, context-pack reference, and usage record are persisted.
6. An optional adapter may execute an explicitly authorized task.
7. Events, cache entries, usage records, and audit records provide the durable decision trail.

## CLI

`gg` exposes status/doctor, project and resource registries, task lifecycle, route, opportunities/optimize, events, context, cache, usage, ask, backlog, and export/import. Every command supports JSON output where practical. Exit codes distinguish usage, missing records, safety blocks, provider unavailability, and internal errors.

## Verification

The test suite uses `unittest` and temporary state directories. It covers persistence, atomic writes, registry validation, expiration, policy filtering, routing, opportunities, events, context isolation, cache invalidation, task transitions, adapters, offline behavior, CLI JSON, malformed input, export/import, and concurrent writers. Smoke tests run against an isolated state directory so existing startup projects and user state are not modified.

## Deliberate v1 limitations

There is no daemon, distributed broker, automatic background scheduler, model API client, or automatic repository mutation. Backlog findings are proposals. Provider integrations are capability boundaries and local probes rather than guaranteed vendor workflows. A future service can call the same `GrokGeneral` methods over HTTP without changing core policy or routing semantics.
