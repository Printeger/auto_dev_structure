# TASK-NNN: Task title

- Risk: `LOW | MEDIUM | HIGH`
- Quality mode: `BUILD | INTEGRATION | HARDENING`
- Requirements: `REQ-NNN`
- Milestone: `MILESTONE-ID`
- Status: `DRAFT | READY | IN_PROGRESS | REVIEW | ACCEPTED | BLOCKED`

## Objective

One measurable outcome.

## Requirements

- `REQ-NNN`: relevant behavior only.

## Inputs

- Files, interfaces, decisions, fixtures, or commands that must be read.

## In Scope

- Explicitly allowed behavior and modules.

## Out of Scope

- Adjacent behavior that must not be added.

## Acceptance Criteria

- AC1: observable result.

## Mandatory Tests

- Exact command and expected signal.

## Do Not

- Prohibited files, behavior, remote actions, commits, or scope expansion.

## Evidence

- Diff, test output, reproduction, benchmark, or other artifact required for acceptance.

## Output Contract

Return exactly these headings:

- `STATUS`
- `FILES_CHANGED`
- `TEST_RESULTS`
- `ACCEPTANCE_EVIDENCE`
- `RISKS`
- `STATE_UPDATE_PROPOSAL`
