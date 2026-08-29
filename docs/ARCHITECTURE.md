# AutoDev V3 Architecture

## System context — FROZEN

AutoDev is the durable authority for one repository and one writer. Codex supplies fresh, bounded Planner, Builder, Reviewer, and Diagnostic executions. Chat history is never canonical state.

```text
CLI
 └─ CampaignController
     ├─ HumanInteraction ─ App Server / TTY / persistent queue / Fake
     ├─ ControlPlane ─ schema, frozen contracts, transitions, events
     ├─ CampaignWorkspace ─ private ref, CAS checkpoint, journal, materialize
     ├─ RunController ─ one Task attempt
     └─ QualityRouter ─ NONE / IMMEDIATE / PHASE / DIAGNOSTIC
```

Public seams are `CampaignController`, `CampaignRequest/Outcome`, `HumanInteraction`, `CampaignWorkspace`, `AppServerCodexEngine`, `QualityDecision`, and the retained V2-compatible `ControlPlane` and `RunController`.

## Canonical ownership — FROZEN

`.autodev/state.json` indexes Campaign and Task state. A Campaign freezes idea, mode, target, autonomy, Requirement Baseline hash, Authority Envelope, phase sequence, proposal hash, source checkpoint, and approval time.

`.autodev/campaigns/CAMP-NNN/requirements.json` is requirement truth. `report` creates read-only Markdown views. Planner proposals cannot directly mutate or authorize canonical state; only ControlPlane can atomically admit a batch as READY.

Legacy V2 Markdown requirements remain readable during the compatibility period and are imported explicitly by `migrate v2`.

## State machines — FROZEN

- Project: `BOOTSTRAP | IDLE | ACTIVE | PAUSED | BLOCKED | STOPPED | FAILED`. Legacy `COMPLETE` remains schema-readable for migration only.
- Campaign: `PROPOSED | ACTIVE | WAITING_FOR_HUMAN | TARGET_REACHED | ARCHIVED | CANCELLED`.
- Phase: `SCAFFOLD | IMPLEMENT | COMPONENT_VERIFY | INTEGRATE | HARDEN | TARGET_REACHED`.
- Task mainline remains `DRAFT/READY → CLAIMED → RUNNING → VALIDATING → [REVIEWING] → ACCEPTED`.

CHANGE has one IMPLEMENT phase. STAGED and CRITICAL retain the full phase sequence and stop at their selected target. Retarget only advances maturity.

## Private checkpoint protocol — FROZEN

Campaign approval creates `refs/autodev/campaigns/CAMP-NNN/current` at the approved source commit and records the user-tree fingerprint. Each Task worktree starts at the current private ref.

Acceptance stages the worktree, creates a tree and parented commit with Git plumbing, writes a PREPARED journal, compare-and-swaps the ref, updates canonical state, then marks the journal COMMITTED. Recovery completes only states provable from the journal, ref, and acceptance evidence; divergence fails closed.

The user branch and worktree do not move during development. At target, AutoDev computes one binary diff from the last materialized checkpoint, verifies source fingerprint and `git apply --check`, applies it, and records the new materialized checkpoint. No product commit is created.

## Planner and admission — FROZEN

Every phase uses a fresh Planner session. The default envelope permits up to MEDIUM risk, implementation/test/documentation/architecture/internal-interface/shared-internal-data, and existing dependencies only. Public API, security, migration, permission expansion, and remote side effects require human authority; commit/push/publish/deploy remain forbidden.

Batch admission checks identity, active phase, requirement references, risk, change classes, protected paths, validation argv/cwd policy, prohibited actions, known dependencies, and DAG acyclicity. One violation rejects the whole batch and creates the smallest human decision.

## Human interaction — PROVISIONAL

The Planner prefers Codex App Server JSONL with `experimentalApi=true`. AutoDev handles server-initiated `item/tool/requestUserInput`, 1–3 questions, 2–3 options, free-form `isOther`, and `autoResolutionMs`. The experimental transport is isolated behind `HumanInteraction`.

Protocol/startup failure falls back to fresh `codex exec` plus TTY or persisted questions. Headless requests enter a recoverable waiting path. Secret questions are sanitized and never store answers; credentials must arrive through a controlled environment.

## Quality and evidence — FROZEN

`QualityRouter.decide` is the only routing interface. Immediate Review handles high-impact changes. Architecture and internal integration are reviewed once per cumulative phase. Repeated identical semantic failure routes to one read-only Diagnostic; Diagnostic proposes cause and repair direction but never accepts work.

Default budgets are one Review plus rereview per immediate Task, one Review plus rereview per Phase, one Diagnostic per Task, and at most five blocking plus five debt findings. Mandatory budget exhaustion blocks.

Run logs, events, journals, and phase summaries are operational evidence, ignored from product Git history by default. Governance Markdown is not a Task output.

## CLI — FROZEN

The V3 surface is `start`, `campaign plan|approve|status|answer|retarget|start|materialize|archive`, `resume --campaign --until target-or-blocked`, `report phase|requirements|release`, and `migrate v2 --check|--apply|--rollback`.

Exit codes remain 0 success, 1 invalid, 2 not ready/paused, 3 blocked, 4 stopped, and 5 infrastructure failure. Live model execution requires exact `AUTODEV_LIVE_CODEX=1`.
