# ADR-0007: Use one Codex-native workflow and a Core-owned Action Protocol

- Status: `ACCEPTED`
- Date: `2026-08-30`
- Owners: `AutoDev maintainers`
- Related requirements: `REQ-043` through `REQ-056`
- Supersedes: `ADR-0001` as the normal interactive topology; `ADR-0006` as the normal planning/input transport

## Context

V3 made AutoDev durable and cumulative, but its normal CLI starts an App Server Planner and fresh `codex exec` attempts. Invoking that path from an already-running Codex session would create two Commanders, duplicate planning, expand context and token cost, and risk nested sandbox failures. Exposing Managed/Native or backend selection would also turn an infrastructure transition into permanent product complexity.

The desired experience is one explicit `$autodev` invocation in Codex, one approval of the Campaign Proposal and Authority Envelope, then autonomous progress using current Codex native subagents while Core retains canonical state authority.

## Decision

AutoDev V4 has one normal workflow:

```text
$autodev -> local stdio MCP -> AutoDev Core -> current Codex Commander -> fresh subagents
```

The current Codex session is the only Commander. It keeps compact Campaign/Action summaries and starts a fresh Planner, Worker, Reviewer, or Diagnostic for each external Action. At most one Worker runs, and only Workers may write.

Core exposes the deep `ActionController` seam `get_next_action(campaign_id)` and `submit_action_result(action_id, result)`. It persists one immutable pending Action per Campaign with canonical revision, identity, quality route, isolated workspace, bounded context, and strict result schema. Pending reads are stable; identical submissions are idempotent; stale, unknown, malformed, or conflicting submissions are rejected without mutation.

Only planning, execution, required review, diagnostic, human wait, pause, and target terminal states cross the Action boundary. Core performs deterministic validation, diff/path checks, evidence, budgets, checkpoint, recovery, phase advancement, and materialization internally. `QualityRouter` alone selects Review/Diagnostic Actions.

The Skill supplies the structured Proposal; MCP/Core must not launch a second Planner, `codex exec`, or App Server. Existing Engines and CLI are retained as headless/CI/test/debug/recovery adapters over the shared Attempt lifecycle. They are not a second development mode. `CHANGE`, `STAGED`, and `CRITICAL` remain development strategies; V3 `mode` is compatibility terminology only. No `execution_backend` field is introduced.

## Assumptions

- Codex can invoke a local stdio MCP server and start fresh role-specific subagents; plugin and MCP integration tests verify both boundaries.
- A new Commander session can reconstruct all necessary context from canonical Campaign/Action state; crash/restart tests verify this without restored chat history.
- Core can bind each write Action to an isolated Campaign workspace; path, source-fingerprint, and concurrent-change tests verify the trust boundary.

## Consequences

- Users learn one entry point and never choose an execution backend.
- The plugin path avoids recursive Codex processes and duplicate planning.
- Action persistence and shared Attempt extraction are substantial migration work and require independent architecture review.
- Commander sessions are disposable; continuity comes from Core, not conversation history.
- Headless compatibility remains testable without defining a competing product workflow.

## Alternatives considered

- Expose Managed and Native modes: rejected because it creates two Commanders or a permanent backend choice that does not express user intent.
- Make `$autodev` shell out to `autodev start`: rejected because it starts a second model runtime and risks nested sandbox failures.
- Move canonical state into the Skill or Commander context: rejected because prompts and conversations cannot provide atomicity, revision checks, recovery, or trustworthy evidence.
- Delete headless Engines immediately: rejected because they remain valuable for CI, Fake tests, live smoke, and recovery while the shared lifecycle is introduced.

## Re-evaluation triggers

- Codex removes local MCP or fresh subagent support with no equivalent replacement.
- Campaign workspaces cannot be safely bound to native Workers across supported Codex environments.
- Evidence shows the Action boundary cannot preserve V3 checkpoint/recovery invariants without delegating deterministic authority.
