# TASK-005: Enforce quality routing, evidence, and debt gates

- Risk: `HIGH`
- Quality mode: `HARDENING`
- Requirements: `REQ-021`, `REQ-022`, `REQ-023`
- Milestone: `M4-V2-QUALITY`
- Status: `ACCEPTED`

## Objective

Make risk-based review routing, structured validation, evidence provenance, and permitted debt executable and fail-closed.

## In Scope

- Direct restricted Builder routing for BUILD LOW/MEDIUM, mandatory fresh read-only review triggers, proposal validation, evidence hashes, and debt admission.

## Out of Scope

- Live Codex authorization, sandbox compatibility, dashboard/reporting, commits, pushes, and publishing.

## Acceptance Criteria

- BUILD LOW/MEDIUM uses no Reviewer; every mandatory trigger uses a fresh read-only Reviewer without Builder reasoning.
- Accepted evidence independently covers contract, proposal, diff, validations, review when routed, and checkpoint.
- Blocking or incompletely specified debt is rejected.

## Mandatory Tests

- `python3 -m unittest discover -s tests -p 'test_runner.py' -v`
- `python3 -m unittest discover -s tests -p 'test_control_plane.py' -v`

## Do Not

- Do not let an agent mutate canonical state, weaken tests, defer safety/data-loss/interface failures, commit, push, or publish.

## Evidence

- Reviewer matrix, direct Builder, proposal-schema, evidence-hash, and debt-gate tests.

## Output Contract

Report status, files changed, test results, acceptance evidence, risks, and state-update proposal.
