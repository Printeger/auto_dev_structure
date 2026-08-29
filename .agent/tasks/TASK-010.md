# TASK-010: Unify the headless adapters on the Attempt lifecycle

- Risk: `HIGH`
- Quality mode: `INTEGRATION`
- Requirements: `REQ-017`, `REQ-020`, `REQ-021`, `REQ-023`, `REQ-026`, `REQ-028`, `REQ-047`
- Milestone: `M9-V4-HEADLESS-ADAPTER`
- Status: `ACCEPTED`

## Objective

Make Campaign/RunController and Fake/Codex/App Server paths reuse the V4 shared Attempt lifecycle without changing their compatibility behavior or presenting a second product workflow.

## In Scope

- Remove duplicate lifecycle/state/quality logic from headless controllers, add transport adapters, and preserve path, validation, evidence, budgets, checkpoint/recovery, engine preflight, and stable exit behavior.

## Out of Scope

- New Action semantics, MCP tools, plugin/Skill, UI, execution-mode options, or real model Campaigns.

## Acceptance Criteria

- Equivalent Fake Action/headless attempts produce the same canonical result, evidence, route, checkpoint, and recovery behavior.
- Existing Fake/Codex/App Server contract tests and all CLI exit-code tests pass unchanged in public behavior.
- No Managed/Native/backend selector exists.

## Mandatory Tests

- Focused Runner/Campaign/Engine parity tests.
- `python3 -m unittest discover -s tests -v`
- `git diff --check`

## Do Not

- Do not fork a second state machine, weaken live gates, expose adapter choice as a development strategy, launch live Codex, or mutate remotes.

## Evidence

- Adapter parity matrix, removed duplication diff, existing regression suite, and stable CLI outcome evidence.
- Accepted in the MCP-enabled `171/171` V4 regression on 2026-08-30; shared lifecycle and parity fixes are local commits `4ed19ff` and `e92ff85`.

## Output Contract

Return exactly these headings: `STATUS`, `FILES_CHANGED`, `TEST_RESULTS`, `ACCEPTANCE_EVIDENCE`, `RISKS`, `STATE_UPDATE_PROPOSAL`.
