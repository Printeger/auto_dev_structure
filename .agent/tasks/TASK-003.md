# TASK-003: Install V2 projects and migrate V1 state

- Risk: `HIGH`
- Quality mode: `INTEGRATION`
- Requirements: `REQ-013`, `REQ-014`
- Milestone: `M2-V2-MIGRATION`
- Status: `ACCEPTED`

## Objective

Install the minimal V2 project surface and provide checksum-aware, staged V1 check/apply/rollback migration without overwriting user-owned files.

## In Scope

- Fresh and merge initialization of canonical contracts, state, requirements, and the Builder template.
- Read-only migration checks, staged apply, checksum classification, frozen `.agent/` backup, and guarded rollback.

## Out of Scope

- Runner execution, worktrees, Codex attempts, quality routing, or publishing.

## Acceptance Criteria

- Init preflights every conflict and never overwrites an existing file.
- Migration apply validates a staged V2 tree before installation and blocks on modified framework copies.
- Rollback is refused after V2 progress and remains recovery-safe at write boundaries.

## Mandatory Tests

- `python3 -m unittest discover -s tests -p 'test_project_v2.py' -v`
- `python3 -m unittest discover -s tests -p 'test_package.py' -v`

## Do Not

- Do not delete user-modified V1 files, mutate `.codex/config.toml`, commit, push, or publish.

## Evidence

- Init/migration fixtures, conflict and rollback tests, packaged checksum manifest, and installed-wheel init smoke.

## Output Contract

Report status, files changed, test results, acceptance evidence, risks, and state-update proposal.
