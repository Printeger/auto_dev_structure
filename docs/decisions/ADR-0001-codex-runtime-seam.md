# ADR-0001: Keep lifecycle authority in AutoDev and use Codex behind one execution seam

- Status: `ACCEPTED`
- Date: `2026-08-28`
- Owners: `Commander`
- Related requirements: `REQ-002`, `REQ-017`, `REQ-018`, `REQ-019`
- Supersedes: `none`

## Context

AutoDev must autonomously select, execute, validate, review, recover, and stop work while retaining its policy and durable project model. Codex already supplies fresh non-interactive runs, structured JSONL/output schemas, sandboxes, resume, and built-in/custom subagents. Rebuilding those capabilities would create competing lifecycle and progress models.

## Decision

AutoDev owns the lifecycle through two deep modules: `ControlPlane.execute(Command) -> CommandResult` and `RunController.run(RunRequest) -> RunOutcome`. A narrow `ExecutionEngine` process boundary exposes live-authorization metadata, a no-model `preflight(...)` admission check, and `execute(AttemptRequest) -> EngineResult`. It has exactly two initial adapters: `CodexExecEngine` and `FakeCodexRunner`.

Codex performs one bounded work or review execution. It cannot author canonical state or determine project completion. Ordinary retries and resume use fresh context; `codex exec resume` is not the default control-loop mechanism. AutoDev will not implement an agent runtime, message bus, or natural-language completion signal.

## Assumptions

- Codex retains non-interactive structured-output and sandbox controls; `doctor` will probe them behaviorally.
- One deterministic Fake adapter can exercise the runner without consuming Codex.

## Consequences

- AutoDev can test policy and recovery independently from model behavior.
- Codex upgrades are contained in one adapter and capability probe.
- Internal runner components do not receive shallow public interfaces without a demonstrated second consumer.
- Long-lived model context is deliberately traded for recoverability and reproducible attempt inputs.

## Alternatives considered

- Embed or fork a Ralph-style runtime: rejected because it duplicates objective/progress state and weakens the existing governance model.
- Drive the entire lifecycle in one persistent Codex session: rejected because chat history is not durable project state and recovery is ambiguous.
- Define an adapter for every internal component: rejected because it exposes implementation detail without adding substitutability.

## Re-evaluation triggers

- Codex no longer exposes the required execution controls.
- A second production execution backend requires a materially different attempt contract.
- Fresh-context execution measurably prevents completion despite adequate persisted context bundles.
