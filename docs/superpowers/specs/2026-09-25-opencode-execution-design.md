# GrokGeneral OpenCode Execution Extension Design

Date: 2026-09-25

## Baseline

The existing stdlib control plane, SQLite/WAL state, 89-test suite, project/resource/task/event/context/cache/usage systems, and temporary Space Bunny resource remain intact. This extension is additive.

The installed provider contract was verified rather than inferred:

- Executable: `/Users/matador/.opencode/bin/opencode`
- Version: `1.18.30`
- Noninteractive command: `opencode run --pure --format json --dir PROJECT --model PROVIDER/MODEL MESSAGE`
- Model listing includes `opencode/space-bunny-free` and `opencode-go/space-bunny-free`.
- A bounded probe exited `0`, emitted JSONL event types `step_start`, `text`, and `step_finish`, reported zero cost, and returned text.
- An invalid model probe exited `1` with a JSON error event.

## Identity and roots

Project records gain canonical identity metadata, aliases, explicit configured roots, kind, and priority. Manual settings are authoritative over scan heuristics. Roots are explicit paths; no scan of all of `$HOME` is introduced. Existing `Project` IDs and paths remain backward-compatible.

## OpenCode execution

The adapter constructs argv arrays and never passes `--auto`. It requires an explicit project directory, model, timeout, and read/modify permission decision. `--format json` output is parsed as JSONL; large raw output is written outside normal state. A compact execution receipt stores execution/task/project/resource/model, timestamps, exit code, context hash, summary, artifacts, validation result, known usage, and redacted errors.

The Space Bunny resource stores the verified model identifier in mutable resource state. The adapter does not hardcode pricing, expiration, or model identity.

## Task lifecycle and validation

Execution moves a task through `queued`, `routed`, `running`, `validating`, and terminal states. A zero provider exit is not success by itself: project-approved validation argv must pass. A repository snapshot records branch, HEAD, status, and existing changes before execution. No stash, discard, reset, commit, push, deploy, or overwrite occurs implicitly.

Permissions are separate for inspect, modify, validate, commit, push, deploy, spend, and external actions. The first real dogfood task is read-only and uses validation only.

## Expiry and rerouting

Resource expiration refresh emits one event, invalidates route/opportunity cache entries, and reroutes queued work when another capable resource exists. Expired resources never remain eligible merely because a prior route was cached.

## Verification

Tests use fake OpenCode executables for argv, timeout, invalid-model, JSONL, and safety behavior. One safe real task is run only after the implementation and receipt/validation tests pass. The final repository receives logical commits and a clean working tree.
