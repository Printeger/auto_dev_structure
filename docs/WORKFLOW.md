# Workflow Guide

## Operating model

The Commander is persistent only for the current planning horizon. Project memory lives in versioned contracts, `STATE.json`, Task files, ADRs, evidence, and Git—not in chat transcripts. Explorer, Builder, and Reviewer are short-lived specialists.

Only Builder writes implementation, and only one Builder runs at a time. Explorer and Reviewer are read-only. The workflow never commits, pushes, deploys, or recursively invokes Codex by default.

## Bootstrap

1. Fill `PROJECT.md`, `docs/REQUIREMENTS.md`, `docs/ARCHITECTURE.md`, and `.agent/ROADMAP.md`.
2. Replace required `{{...}}` markers with concrete text.
3. Set `project_status` to `ACTIVE`, choose a milestone, and set a concrete next action/owner.
4. Run `python3 scripts/autodev.py validate --ready`.

## Task loop

1. Commander selects a vertical slice and creates a Task contract with `new-task`.
2. Explorer may map relevant code and constraints without editing.
3. Commander marks the Task current and sets phase to `IMPLEMENT`.
4. One Builder implements, runs mandatory tests, self-reviews, and returns the Task output contract.
5. Commander checks the diff and evidence. Use Reviewer when required by risk or quality mode.
6. Record `PASS`, `PASS_WITH_DEBT`, `REWORK`, or `BLOCKED`; update state counters and next action.
7. On acceptance, integrate deliberately, update durable documents, then select the next Task.

Run `validate` after every manual state transition. Counters are per current Task and must reset deliberately when a new Task becomes current.

## Review routing

| Condition | Required gate |
| --- | --- |
| LOW/MEDIUM in BUILD | Builder self-review + Commander incremental checks |
| HIGH risk | Independent Reviewer |
| Architecture/shared data change | ADR + independent Reviewer |
| Milestone integration | INTEGRATION checks + Reviewer |
| HARDENING | Full checks + Reviewer |

BUILD favors momentum while protecting required behavior and core correctness. INTEGRATION expands checks across interfaces and modules. HARDENING closes boundary, performance, security, operations, documentation, and debt gaps.

## Checkpoints and rotation

Checkpoint at milestone completion or major architecture change. Update `HANDOFF.md` with current goal, accepted behavior, active risks, exact validation commands/results, relevant ADRs, and next action. Start a fresh Commander session when context is noisy; use `autodev.py prompt` to reconstruct the working set.

## Human escalation

Use BLOCKED only when progress requires a human decision/authority, a missing external dependency, resolution of contradictory contracts, or additional budget. `blocker`, `next_action`, and `next_owner=HUMAN` must be explicit. Ordinary test failures remain REWORK.

## Optional automation

`hooks.example.json` is inert until copied to `hooks.json` and trusted through `/hooks`. Its Stop hook validates state once and does not edit files. Non-interactive `codex exec --json` is intentionally outside V1 orchestration; add it only with explicit authentication, sandboxing, budgets, structured outputs, and human escalation controls.
