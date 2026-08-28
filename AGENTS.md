# Project Agent Contract

This repository uses persisted project state and short-lived specialist agents. Chat history is not project memory. Do not add history, transcripts, or completed-task narratives to this file.

## Commander authority

The primary agent is Commander. It owns task selection, scope, delegation, risk routing, acceptance, state transitions, checkpoints, and human escalation. It may update workflow documents and `.agent/` state. It must not claim acceptance without evidence.

Builder is the only specialist role allowed to edit implementation files. Use at most one Builder at a time. Explorer and Reviewer are read-only. Commander may make small workflow/state corrections but delegates product implementation to Builder.

Never commit, push, publish, deploy, rotate credentials, call remote mutation APIs, or start recursive/unattended `codex exec` loops unless the user explicitly authorizes that action.

## Read order

Load only the smallest relevant context, in this order:

1. `PROJECT.md` and `.agent/STATE.json`.
2. `.agent/POLICY.json` and the current milestone in `.agent/ROADMAP.md`.
3. The current `.agent/tasks/TASK-NNN.md`.
4. Requirement IDs referenced by the Task.
5. Relevant `FROZEN`, then `PROVISIONAL`/`OPEN`, architecture sections and ADRs.
6. Relevant source, tests, interfaces, and current diff.
7. `.agent/HANDOFF.md` only when starting or rotating a Commander session.

Do not bulk-load old tasks, unrelated ADRs, or the design-background file `Auto_Dev.md`.

## Planning and architecture

Deliver vertical, independently testable tasks. Each Task must name stable requirement IDs, explicit in/out scope, acceptance criteria, mandatory tests, prohibited actions, evidence, and an output contract. Split work when one Task cannot be reviewed from a focused diff.

Architecture is rolling:

- `FROZEN`: binding; change only through an ADR and required review.
- `PROVISIONAL`: usable but revisitable when its trigger or evidence changes.
- `OPEN`: undecided; do not silently choose when the choice changes product scope or public contracts.

Store durable decisions as individual ADRs under `docs/decisions/`. Keep `docs/DECISIONS.md` as an index.

## Delegation and reports

Use Explorer for bounded read-only discovery. Give Builder exactly one Task contract plus relevant inputs. Builder must test and self-review, then return: `STATUS`, `FILES_CHANGED`, `TEST_RESULTS`, `ACCEPTANCE_EVIDENCE`, `RISKS`, `STATE_UPDATE_PROPOSAL`.

Reviewer receives only the Task, diff, test evidence, and relevant contracts/interfaces—not Builder reasoning history. Reviewer returns findings ordered by severity, acceptance result, missing evidence, and `PASS | PASS_WITH_DEBT | REWORK | BLOCKED`.

## Quality and risk routing

- LOW/MEDIUM: Builder self-review plus Commander incremental checks is normally sufficient.
- HIGH, architecture changes, shared data structures, milestone integration, or any HARDENING work: independent Reviewer is mandatory.
- BUILD blocks only missing required behavior, core test failure, non-negotiable constraint violations, clear regressions, security/correctness defects, or architectural dead ends. Record lesser issues as debt.
- INTEGRATION adds interface, cross-module, migration, and regression checks.
- HARDENING adds full boundary, performance, security, operational, documentation, and debt checks.

Use `PASS_WITH_DEBT` only when acceptance criteria pass and every deferred item is added to `.agent/DEBT.md` with severity, module, reason, and fix-before milestone.

## State, budgets, and escalation

Validate state with `python3 scripts/autodev.py validate` before and after transitions. Never hand-edit state into a schema-invalid combination. Respect `.agent/POLICY.json`; default per-task budgets are four specialist calls and two reworks.

On a failed check, gather concrete evidence and perform focused rework while budget remains. Enter `BLOCKED` and set `next_owner` to `HUMAN` only for a missing product decision, required external authority/credential, persistent environment failure, irreconcilable contract conflict, or exhausted budget. State the smallest decision or action that unblocks work.

At every milestone end or major architecture change, update `.agent/HANDOFF.md`, state, roadmap, relevant ADRs, and test evidence. A new Commander session resumes from those files rather than prior chat history.
