# TASK-004: Build the isolated Runner and Engine seam

- Risk: `HIGH`
- Quality mode: `INTEGRATION`
- Requirements: `REQ-015`, `REQ-016`, `REQ-017`, `REQ-019`
- Milestone: `M3-V2-RUNNER`
- Status: `ACCEPTED`

## Objective

Run one deterministic Task through a locked isolated Git worktree, an interchangeable Engine attempt, validated path policy, and a concurrency-safe binary patch checkpoint.

## In Scope

- Atomic lock and stale-owner recovery, Git HEAD/clean admission, source fingerprints, isolated worktree lifecycle, Codex/Fake Engine seam, scheduling, and patch application.

## Out of Scope

- Reviewer/debt policy, long-running recovery budgets, live model authorization, commits, pushes, and publishing.

## Acceptance Criteria

- Exactly one owner and writing worktree exist; missing HEAD, dirty source, path violations, and concurrent changes fail before overwrite.
- FakeCodexRunner drives deterministic success/failure outcomes through the same public seam as Codex.
- Binary patches are captured and reapplied only when the source fingerprint matches.

## Mandatory Tests

- `python3 -m unittest discover -s tests -p 'test_workspace.py' -v`
- `python3 -m unittest discover -s tests -p 'test_runner.py' -v`
- `python3 -m unittest discover -s tests -p 'test_engine.py' -v`

## Do Not

- Do not write outside allowed paths, overwrite concurrent user changes, commit, push, publish, or invoke a live model in tests.

## Evidence

- Lock contention, stale recovery, missing/dirty baseline, binary patch, protected-path, and FakeCodexRunner routing test output.

## Output Contract

Report status, files changed, test results, acceptance evidence, risks, and state-update proposal.
