# Project Contract: AutoDev 3.0 alpha

## Mission

Provide a staged autonomous development control plane that turns an approved goal and Authority Envelope into cumulative, bounded, reviewable, and recoverable Codex work with human-on-blocked escalation.

## Motivation

V2 automated a single-Task control loop, but dependent accepted Tasks were applied to the user tree and every new worktree still started from Git HEAD. V3 introduces a cumulative private Campaign baseline and phase-level autonomous planning without inventing a second agent runtime or treating chat history as project memory.

## Deliverables

- An installable Python 3.11+ `autodev` package and CLI.
- A canonical `.autodev/` project contract with validated state, tasks, policy, evidence, debt, locks, migrations, and run artifacts.
- A single-task runner that executes Codex in an isolated Git worktree through a narrow engine interface and safely checkpoints accepted patches.
- Deterministic quality routing, recovery, stopping, budgeting, stagnation detection, and derived Campaign target completion.
- Read-only checks and recoverable migrations from V1 `.agent/` and V2 `.autodev/` projects.

## Success Criteria

- A fresh target project can be initialized, activated, validated, and driven through an accepted task with the Fake engine.
- Every canonical state transition is schema-valid, atomic, evented, and rejected when its preconditions do not hold.
- Required validation and independent review gates cannot be bypassed by an agent proposal.
- A concurrent source change, stale lock, stop request, timeout, crash, or failed attempt has a deterministic and recoverable outcome.
- V3 target completion is derived only after phase Tasks, validation, mandatory review, debt, checkpoint, and active-run invariants pass; legacy `COMPLETE` is read-only compatibility state.
- The package passes its Fake-engine CI and packaging gates; live Codex/App Server smoke remains separately authorized.

## Non-negotiable Constraints

- `.autodev/` is the only canonical state. Campaign Requirement JSON is canonical; Markdown is a derived or legacy migration view.
- AutoDev owns contracts, transitions, scheduling, locks, budgets, evidence, quality routing, recovery, and completion. Codex performs one bounded work or review execution.
- Only one writer task and one Builder may run at a time. Explorer and Reviewer work is read-only.
- Agent execution has approval set to `never`, workspace-only writes, no network, no MCP servers or hooks, structured output, and enforced timeouts.
- Task validation uses structured `argv`, `cwd`, and timeout fields with `shell=False`; tasks cannot supply shell strings or environment variables.
- AutoDev does not commit, push, publish, deploy, rotate credentials, or mutate remote systems unless the user separately and explicitly authorizes that action.
- User files are never silently overwritten; source changes are fingerprinted before an accepted patch is applied.
- Python 3.11+ and `jsonschema>=4.26,<5` are the only runtime platform and dependency commitments.

## Priority Order

1. State integrity, user-data safety, and recoverability.
2. Contract and acceptance correctness.
3. Deterministic autonomous progress.
4. Clear evidence and operability.
5. Extensibility beyond the single-repository, single-writer V3 core.
