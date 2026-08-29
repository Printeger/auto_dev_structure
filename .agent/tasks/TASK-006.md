# TASK-006: Complete reliability and release gates

- Risk: `HIGH`
- Quality mode: `HARDENING`
- Requirements: `REQ-020`, `REQ-024`, `REQ-025`, `REQ-026`, `REQ-027`, `REQ-028`
- Milestone: `M5-V2-RELEASE-GATE`
- Status: `ACCEPTED`

## Objective

Add bounded retries, timeout/STOP escalation, recovery, semantic stagnation, continuous-mode budgets, and derived completion with stable exit codes.

## In Scope

- Idle/hard timeout, process-group cancellation, infrastructure/rework/stagnation budgets, STOP/resume, explicit continuous mode, validation recording, and completion derivation.

## Out of Scope

- A successful real Codex release smoke, PyPI publication, remote mutation, Docker, dashboard, or native Windows semantics.

## Acceptance Criteria

- Recoverable infrastructure, STOP, BLOCKED, and unrecoverable corruption remain distinct and use stable exits.
- Resume is recovery-safe and ordinary model work starts fresh.
- COMPLETE is impossible without accepted blocking Tasks/MUST evidence, no blocker/debt/run/lock, and full validation.

## Mandatory Tests

- `python3 -m unittest discover -s tests -v`
- `python3 scripts/autodev.py validate --ready`
- `PYTHONPATH=src python3 -m autodev version`
- `git diff --check`

## Do Not

- Do not infer completion from an agent proposal, run continuously without an explicit flag, commit, push, publish, or launch live Codex.

## Evidence

- Timeout/STOP/retry/stagnation/completion tests plus wheel build and isolated installation smoke.

## Output Contract

Report status, files changed, test results, acceptance evidence, risks, and state-update proposal.
