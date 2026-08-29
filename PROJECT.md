# Project Contract: AutoDev 2.0

## Mission

Provide a policy-first autonomous development control plane that turns versioned project contracts into bounded, reviewable, and recoverable Codex work.

## Motivation

The V1 repository proves the governance model but still depends on a human Commander to select tasks, invoke specialists, validate evidence, and maintain state. AutoDev 2.0 automates that control loop without inventing a second agent runtime or treating chat history as project memory.

## Deliverables

- An installable Python 3.11+ `autodev` package and CLI.
- A canonical `.autodev/` project contract with validated state, tasks, policy, evidence, debt, locks, migrations, and run artifacts.
- A single-task runner that executes Codex in an isolated Git worktree through a narrow engine interface and safely checkpoints accepted patches.
- Deterministic quality routing, recovery, stopping, budgeting, stagnation detection, and derived project completion.
- A read-only, checksum-aware migration path from V1 `.agent/` projects.

## Success Criteria

- A fresh target project can be initialized, activated, validated, and driven through an accepted task with the Fake engine.
- Every canonical state transition is schema-valid, atomic, evented, and rejected when its preconditions do not hold.
- Required validation and independent review gates cannot be bypassed by an agent proposal.
- A concurrent source change, stale lock, stop request, timeout, crash, or failed attempt has a deterministic and recoverable outcome.
- Project `COMPLETE` is derived only after all MUST requirements, blocking tasks, debt, project validation, locks, and active-run invariants pass.
- The package passes its Fake-engine CI and packaging gates; version `2.0.0` is reserved for the explicitly authorized live-Codex release gate.

## Non-negotiable Constraints

- `.autodev/` is the only V2 canonical state. `.agent/` is migration input or a frozen migration backup, never a second source of truth.
- AutoDev owns contracts, transitions, scheduling, locks, budgets, evidence, quality routing, recovery, and completion. Codex performs one bounded work or review execution.
- Only one writer task and one Builder may run at a time. Explorer and Reviewer work is read-only.
- Agent execution has approval set to `never`, workspace-only writes, no network, no MCP servers or hooks, structured output, and enforced timeouts.
- Task validation uses structured `argv`, `cwd`, and timeout fields with `shell=False`; tasks cannot supply shell strings or environment variables.
- AutoDev does not commit, push, publish, deploy, rotate credentials, or mutate remote systems unless the user separately and explicitly authorizes that action.
- User files are never silently overwritten; source changes are fingerprinted before an accepted patch is applied.
- Python 3.11+ and `jsonschema>=4.26,<5` are the only V2 runtime platform and dependency commitments.

## Priority Order

1. State integrity, user-data safety, and recoverability.
2. Contract and acceptance correctness.
3. Deterministic autonomous progress.
4. Clear evidence and operability.
5. Extensibility beyond the single-writer V2 core.
