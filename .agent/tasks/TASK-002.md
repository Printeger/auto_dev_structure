# TASK-002: Implement the executable V2 ControlPlane

- Risk: `HIGH`
- Quality mode: `BUILD`
- Requirements: `REQ-006`, `REQ-007`, `REQ-008`, `REQ-009`, `REQ-010`, `REQ-011`, `REQ-012`, `REQ-027`, `REQ-028`
- Milestone: `M2-V2-CONTROL`
- Status: `ACCEPTED`

## Objective

Provide an executable, schema-validated `ControlPlane.execute(Command) -> CommandResult` that is the sole writer of `.autodev/` canonical state and supports V2 activation, validation/status, structured Task creation/freezing/reopening, legal transitions, revision checks, event records, and conservative derived-completion checks.

## Requirements

- `REQ-006`: Draft 2020-12 schemas are validated through `jsonschema` and the standard format checker.
- `REQ-007`: Canonical writes are validated, revision-checked, atomic, and evented; only `ControlPlane` owns them.
- `REQ-008`: Published Project/Task/Attempt states and legal transitions are executable; illegal edges do not mutate state.
- `REQ-009`: DRAFT `contract.json` becomes hash-frozen at READY, has a generated `contract.md`, and only reopen-with-reason permits later edits while invalidating old claim/evidence.
- `REQ-010`: Task contracts contain every required structured field and identified acceptance criterion.
- `REQ-011`: Validation commands are `argv[] + cwd + timeout`, never shell/environment strings, and are checked against policy executable/cwd constraints.
- `REQ-012`: Requirements parsing extracts only ID, priority, status, and acceptance signal.
- `REQ-027`: Completion is derived conservatively; a proposal cannot force it.
- `REQ-028`: CLI exit codes keep the frozen 0/1/2/3/4/5 meanings.

## Inputs

- `docs/ARCHITECTURE.md` FROZEN public seams, canonical ownership, state machines, CLI contract, and completion section.
- `docs/REQUIREMENTS.md` rows REQ-006 through REQ-012, REQ-027, REQ-028.
- `docs/decisions/ADR-0001-codex-runtime-seam.md` and `ADR-0002-canonical-state.md`.
- TASK-001 package/resource foundation and current V1 compatibility suite.

## In Scope

- Add public `Command`, `CommandResult`, and `ControlPlane` types without exposing shallow helper APIs.
- Add packaged Draft 2020-12 schemas for Project state, Task contract, policy/config/manifest as required by this slice, attempt outcome, event, and result/proposal data.
- Add atomic JSON replacement with fsync/rename discipline, monotonic revision checks, and per-revision immutable event records with recovery-safe ordering.
- Add a requirements-table parser that emits only ID/priority/status/acceptance signal and rejects malformed/duplicate IDs.
- Add ControlPlane commands for project `validate`, `activate`, `status`, conservative `complete`, and Task `create`, `ready`, `show`, and `reopen`; expose the matching frozen CLI forms implemented in this Task.
- Model the full legal Project/Task/Attempt enums and transition tables even when later runner commands will consume some edges.
- Store Task operational status/hash/generation in canonical state and the editable contract under `.autodev/tasks/TASK-ID/contract.json`; generate `contract.md` on READY.
- Add tests for schemas, transitions, atomic/revision behavior, frozen contracts, safe validation-command policy, requirements parsing, completion refusal/success foundations, CLI JSON/output and exit codes.

## Out of Scope

- Installing `.autodev/` into targets and any V1 migration/checksum/rollback logic (TASK-003). Test fixtures may create a minimal canonical tree directly.
- Locks, worktrees, patch checkpoints, Runner scheduling, Engines, Codex processes, Reviewer/debt enforcement, run evidence production, stop/resume, or stagnation (TASK-004+).
- Full completion evidence generation; this slice only derives from already-recorded canonical accepted requirement IDs/task/debt/full-validation facts and must fail closed.

## Acceptance Criteria

- `AC-001`: All packaged schemas declare Draft 2020-12 and valid/invalid/format-invalid fixtures are correctly accepted/rejected through `jsonschema.Draft202012Validator` plus the standard format checker.
- `AC-002`: Two writers using the same expected revision cannot both mutate; the winner increments revision once and records a matching immutable event, while the loser returns INVALID without state change.
- `AC-003`: Fault injection around temp write and `os.replace` never exposes invalid/partial JSON; orphan pre-state event/projection files are safely ignored or deterministically reconciled.
- `AC-004`: Every documented legal Project/Task edge is encoded and representative illegal edges return INVALID without mutation.
- `AC-005`: `task ready` validates the full contract and policy, writes a deterministic Markdown projection, freezes a SHA-256 hash, and later hash mismatch makes validate/ready fail closed.
- `AC-006`: `task reopen --reason` is the sole frozen-contract edit path, returns the Task to DRAFT, increments generation, and invalidates old claim/evidence references; empty reasons and invalid source states are rejected.
- `AC-007`: Shell strings, Task environment fields, empty argv, disallowed executables, absolute/escaping cwd, and out-of-policy cwd are rejected.
- `AC-008`: Requirements parsing returns exactly ID/priority/status/acceptance signal and rejects malformed priority/status/duplicates without copying Requirement prose.
- `AC-009`: `complete` rejects missing MUST evidence, unaccepted blocking Tasks, blocking debt, failed full validation, or active task/run/lock/blocker, and only transitions to COMPLETE when all represented prerequisites pass.
- `AC-010`: Implemented CLI forms render stable JSON/human results and use exit codes 0 success, 1 invalid contract/transition, 2 not-ready/paused, 3 blocked, 4 stopped, 5 unrecoverable infrastructure failure.
- `AC-011`: TASK-001 behavior and all pre-existing V1 tests remain passing.

## Mandatory Tests

- `python3 -m unittest discover -s tests` — all V1, package, and new ControlPlane tests pass.
- `PYTHONPATH=src python3 -m autodev version` — prints `2.0.0a1`.
- `python3 scripts/autodev.py validate --ready` — V1 workflow metadata remains ready during TASK-002.
- `git diff --check` — no whitespace errors.

## Do Not

- Do not mutate `.agent/` state or TASK contracts; Builder only proposes state changes.
- Do not modify `.codex/config.toml`, `Second version.md`, or the pre-existing test cache.
- Do not read or package `Auto_Dev.md`.
- Do not implement init/migrate/Runner/Engine/lock/worktree/review/debt/reliability behavior.
- Do not use a custom JSON-schema validator, shell command execution, Task-provided environment, or non-atomic canonical writes.
- Do not commit, push, publish, deploy, mutate remotes, or invoke unattended/recursive Codex.

## Evidence

- Focused diff for public types, internal atomic/state/schema helpers, packaged schemas, CLI routing, and tests.
- Test names/output covering every AC, including concurrent expected-revision mutation and injected write failures.
- State/event/contract projection examples with revision and SHA-256 evidence.

### Recorded Acceptance Evidence

- Fail-closed ControlPlane rework closed the frozen-hash, projection crash-ordering, and malformed-state defects reproduced during review.
- Final repository suite: 65 tests passing, including schema instances, atomicity/concurrency, init/migration, locks/worktrees, Engines, Runner, policy/evidence/debt, recovery/budgets, CLI, and packaging foundations.
- Installed-wheel smoke: `autodev version`, target `init`, and target `validate --json` passed from an isolated venv.

## Output Contract

Return exactly these headings:

- `STATUS`
- `FILES_CHANGED`
- `TEST_RESULTS`
- `ACCEPTANCE_EVIDENCE`
- `RISKS`
- `STATE_UPDATE_PROPOSAL`
