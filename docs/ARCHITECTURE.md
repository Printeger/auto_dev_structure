# AutoDev V4 Architecture

## System context — FROZEN

AutoDev has one normal interactive topology and one Commander:

```text
User
  -> current Codex CLI session
  -> explicit $autodev Skill
  -> local stdio AutoDev MCP
  -> AutoDev Core / ActionController
  -> current Codex Commander
  -> one fresh Planner, Worker, Reviewer, or Diagnostic subagent
```

The Commander retains only compact Campaign and Action summaries. It does not edit canonical state and it does not run `autodev start`, `codex exec`, or App Server. `CodexExecEngine`, `AppServerCodexEngine`, Fake engines, and the legacy CLI remain adapters for headless, CI, test, debug, and recovery workflows. They do not create a second user-visible development mode.

There is no Managed/Native distinction, `execution_backend`, or related selector. `CHANGE`, `STAGED`, and `CRITICAL` are development strategies; the persisted V3 `mode` field is a compatibility name for strategy only.

## Public seams — FROZEN

`ActionController` is the Codex-native deep module and exposes only:

```python
get_next_action(campaign_id) -> ActionOutcome
submit_action_result(action_id, result) -> ActionOutcome
```

It composes the retained `ControlPlane`, `CampaignWorkspace`, `QualityRouter`, and a shared internal Attempt lifecycle. The existing `CampaignController`/`RunController` headless surface becomes an adapter over that lifecycle rather than a parallel state machine.

External Actions are exactly:

- `PLAN_PHASE`
- `EXECUTE_TASK`
- `RUN_IMMEDIATE_REVIEW`
- `RUN_DIAGNOSTIC`
- `RUN_PHASE_REVIEW`
- `ASK_HUMAN`
- `PAUSED`
- `TARGET_REACHED`

Validation, diff derivation, path enforcement, evidence production, checkpointing, phase gates, phase advancement, recovery, budgeting, and materialization decisions are deterministic Core operations and are never delegated as Actions.

## Canonical ownership and Action protocol — FROZEN

`.autodev/state.json` indexes Campaign, Phase, Task, Attempt, revision, `current_action_id`, and graceful `pause_requested` state. Campaign Requirement JSON remains canonical. Only Core/ControlPlane performs validated, revision-checked, atomic, evented writes.

Actions are immutable JSON records below `.autodev/actions/`. Each record includes a unique ID, canonical revision, Campaign/Phase/Task identity where applicable, Core-selected `quality_route`, an isolated workspace, bounded input context, and the strict schema expected for its result.

Only one Action may be pending for a Campaign. Repeated `get_next_action` calls return that exact record until it is resolved. Result submission is keyed by Action ID and expected canonical revision:

- an identical already-accepted result returns the recorded outcome without mutation;
- a different duplicate, unknown ID, stale revision, or malformed result is rejected without mutation;
- a crash between durable steps is reconciled from canonical state, Action records, journals, refs, and evidence rather than agent memory.

## State machines and development strategy — FROZEN

- Project: `BOOTSTRAP | IDLE | ACTIVE | PAUSED | BLOCKED | STOPPED | FAILED`. Legacy `COMPLETE` remains schema-readable for migration only.
- Campaign: `PROPOSED | ACTIVE | WAITING_FOR_HUMAN | TARGET_REACHED | ARCHIVED | CANCELLED`.
- Phase: `SCAFFOLD | IMPLEMENT | COMPONENT_VERIFY | INTEGRATE | HARDEN | TARGET_REACHED`.
- Task mainline: `DRAFT/READY -> CLAIMED -> RUNNING -> VALIDATING -> [REVIEWING] -> ACCEPTED`.

`CHANGE` uses one IMPLEMENT phase. `STAGED` and `CRITICAL` retain the full phase sequence and stop at the selected maturity target; only monotonic retargeting is permitted. These are development strategies, including when read from the V3 compatibility field `mode`.

## Attempt lifecycle and trust boundary — FROZEN

Core creates a Campaign worktree and returns the applicable isolated workspace in the Action. A Worker is the only writing specialist, must operate only in that workspace, and at most one Worker may run. Core independently derives the binary diff, changed paths, source/concurrency checks, validations, evidence, and checkpoint transition. Agent-provided changed-file and validation fields are claims, never authority.

Planner, Reviewer, and Diagnostic specialists are fresh and read-only. Core verifies their workspace did not change and rejects a result if it did. Review routing comes only from `QualityRouter`: ordinary LOW/MEDIUM work is `NONE`, high impact is `IMMEDIATE`, cumulative architecture/internal integration is `PHASE`, and repeated identical semantic failure is `DIAGNOSTIC`.

The shared internal Attempt lifecycle owns workspace policy, validation, quality routing, budgets, evidence, private CAS checkpoints, and recovery. Codex-native Actions and headless Engines adapt their transport-specific result into that one lifecycle.

## Campaign and materialization — FROZEN

Campaign approval creates `refs/autodev/campaigns/CAMP-NNN/current` at the approved source commit and records the user-tree fingerprint. Accepted Tasks advance the private ref with journaled compare-and-swap. The user branch/worktree stays unchanged during development.

When the target invariant is reached, Core automatically attempts exactly one safe materialization of the private checkpoint increment. It verifies the source fingerprint and binary apply preconditions, never creates a product commit, and records the materialized checkpoint. A conflict enters `ASK_HUMAN`/BLOCKED without overwriting user changes. `materialize_campaign` is an explicit retry after the conflict is resolved.

Pause is a request, not an interrupt: `pause_requested` takes effect after the current external Action is submitted and all deterministic processing completes. The next outcome is `PAUSED`. Continue reconciles state and returns the existing pending Action or the next derived Action.

## Proposal and human interaction — FROZEN

The current Codex performs the Grill and supplies a strict structured Proposal to `propose_campaign`. Core validates and hashes it but never launches App Server or a second Planner. The user confirms the Proposal and Authority Envelope once through the Skill before `approve_campaign`.

Human interaction after approval occurs only for genuine blockers, Authority Envelope exceptions, CRITICAL gates, environment/credential requirements, materialization conflicts, or exhausted mandatory budgets. Answers are applied through Core; secrets are never persisted.

## MCP boundary — FROZEN

The local stdio server exposes only:

`inspect_project`, `initialize_project`, `propose_campaign`, `approve_campaign`, `campaign_status`, `campaign_continue`, `pause_campaign`, `answer_blocker`, `retarget_campaign`, `materialize_campaign`, `get_next_action`, and `submit_action_result`.

Every tool explicitly accepts `project_root`, publishes strict input/output schemas and accurate read-only/destructive/idempotent/open-world annotations, resolves the root without shell interpretation, and maps Core failures to stable MCP errors. Mutating tools call only Core/ControlPlane APIs. No MCP request launches a Codex subprocess.

## Migration and compatibility — PROVISIONAL

`autodev migrate v3 --check|--apply|--rollback` preserves Campaign private refs, Tasks, Evidence, checkpoints, and compatibility strategy fields. Check is read-only; apply is staged and validated; rollback is guarded and is permanently refused after the first V4 Action is created.

Legacy `autodev start`, `campaign start`, and `resume --campaign` retain stable exit codes 0 success, 1 invalid, 2 not ready/paused, 3 blocked, 4 stopped, and 5 infrastructure failure. Their execution engines use the shared Attempt lifecycle and remain outside the normal README path.
