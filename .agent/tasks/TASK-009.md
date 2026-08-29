# TASK-009: Implement the Action Protocol and V3 migration

- Risk: `HIGH`
- Quality mode: `INTEGRATION`
- Requirements: `REQ-044`, `REQ-045`, `REQ-046`, `REQ-048`, `REQ-049`, `REQ-050`, `REQ-055`, `REQ-056`
- Milestone: `M8-V4-ACTION-CORE`
- Status: `ACCEPTED`

## Objective

Implement persistent, recovery-safe Action orchestration at the confirmed `ActionController` seam and a lossless guarded V3-to-V4 migration.

## In Scope

- Strict Action/Result schemas, pending-Action persistence, isolated workspace preparation, submission/idempotency/revision rejection, independent diff/path/validation checks, QualityRouter Action routing, evidence/budgets/checkpoint, graceful pause, phase/target progression, safe materialization, and crash recovery.
- State schema additions including `current_action_id` and `pause_requested`.
- `migrate v3 --check|--apply|--rollback` preservation and progress guard.

## Out of Scope

- Refactoring headless adapters, MCP transport/tools, plugin/Skill files, README release positioning, or live model runs.

## Acceptance Criteria

- One full Fake Campaign advances from `PLAN_PHASE` to one-time materialized `TARGET_REACHED` through only the two public methods.
- Pending reads and identical retries are stable; unknown/stale/conflicting/malformed submissions produce zero mutation.
- Core derives actual changes/validation and rejects path, concurrency, and read-only-role writes.
- Pause completes the current Action, restart preserves identity, and migration/rollback preserve V3 assets and honor the first-Action guard.

## Mandatory Tests

- Focused Action/schema/migration tests after each red-green seam.
- `python3 -m unittest discover -s tests -v`
- `python3 scripts/autodev.py validate`
- `git diff --check`

## Do Not

- Do not trust agent diff/test claims, bypass QualityRouter, expose deterministic internal steps as Actions, add a backend selector, run live Codex, or mutate remotes.

## Evidence

- Fault-injection and snapshot tests, full Fake Campaign trace, workspace/review concurrency evidence, migration preservation hashes, and full regression output.
- Accepted in the MCP-enabled `171/171` V4 regression on 2026-08-30; implementation and recovery fixes are local commits `27e6826` through `ef60002`.

## Output Contract

Return exactly these headings: `STATUS`, `FILES_CHANGED`, `TEST_RESULTS`, `ACCEPTANCE_EVIDENCE`, `RISKS`, `STATE_UPDATE_PROPOSAL`.
