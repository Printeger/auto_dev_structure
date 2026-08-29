# AutoDev 2.0 Workflow Guide

## Operating model

AutoDev is the durable Commander/control plane. It owns selection, state, policy, locks, budgets, evidence, review routing, recovery, checkpoints, and completion.

Codex performs one fresh, bounded work or review execution. Chat history is never project memory.

Only one writing Task and one Builder may run at a time. For BUILD + LOW/MEDIUM, the primary Codex process is the restricted Builder and AutoDev supplies the Commander gate.

The packaged `autodev-builder` remains available to interactive workflows. Explorer is Codex's built-in read-only explorer. Reviewers start only when policy requires a fresh read-only session.

AutoDev does not commit, push, publish, deploy, rotate credentials, or mutate remote systems unless the user separately authorizes the action.

This repository retains V1 `.agent/` data as a migration fixture. The V2 command surface below is implemented by the installable `autodev` package; target projects use only `.autodev/` as live state.

## Canonical project lifecycle

1. `autodev init TARGET --name NAME` installs project contracts, `.autodev/` templates, and the Builder agent definition. `--merge` never overwrites existing paths.
2. The user completes requirements and any OPEN product decisions, then `autodev activate` validates the project and enters ACTIVE.
3. Task contracts are created in DRAFT, checked with stable requirement IDs, and frozen by `autodev task ready`.
4. `autodev run` processes one deterministic READY Task and stops. Continuous scheduling requires the explicit `--until complete-or-blocked` option.
5. `autodev resume` recovers canonical artifacts and normally starts a fresh attempt. `--recover-stale` is required for eligible abandoned locks/workspaces.
6. `autodev complete` evaluates, but cannot bypass, the derived completion gate.

Project outcomes remain distinct. PAUSED means recoverable infrastructure or workspace intervention. BLOCKED means product decision, authority, contract resolution, or exhausted budget.

STOPPED is a user stop. FAILED is reserved for unrecoverable canonical corruption.

## Task contract and selection

`contract.json` is the sole Task contract. It contains scope, requirement IDs, dependencies, priority, risk, quality mode, allowed paths, acceptance criteria, validation, and prohibited actions.

`contract.md` is generated for people and agents.

DRAFT is editable. READY stores the contract hash; a mismatch stops claim or execution.

`task reopen --reason` is the only supported way to change a frozen contract and invalidates old claims and evidence.

Selection is deterministic by priority, satisfied dependencies, creation time, then Task ID.

Validation commands are data, never shell: `argv[]`, project-relative `cwd`, and timeout. Policy checks executables and working directories, and subprocess execution uses `shell=False`. Tasks cannot provide environment variables.

## Single-Task run

1. Require `AUTODEV_LIVE_CODEX=1`; validate canonical state, Git HEAD, and a clean source baseline outside `.autodev/**`.
2. Run no-model preflight for the Builder `:workspace` and Reviewer `:read-only` profiles before lock, claim, artifacts, or model execution.
3. Acquire the project lock, then deterministically select or confirm one READY Task.
4. Create the run context, atomically claim the Task at the expected revision, and create one isolated Git worktree.
5. Launch a fresh restricted Builder, stream JSONL events, and enforce schema, timeout, STOP, and retry rules.
6. Independently collect the diff, enforce protected/allowed paths, and run structured validation commands.
7. Start a fresh read-only Reviewer only when the routing matrix requires one.
8. On acceptance, write evidence and a binary-safe checkpoint, recheck the source fingerprint, and apply the patch.
9. Clean the worktree when safe. Preserve checkpoints and recovery instructions after interruption or concurrent source changes.

Agents return structured proposals only. The runner creates diff/test/review evidence; only `ControlPlane` writes canonical state.

## Runtime admission and failure boundaries

AutoDev expresses permission intent with Codex profiles. BUILD defaults to `:workspace`; Reviewer defaults to `:read-only`. Codex chooses the Linux sandbox backend.

On Codex CLI 0.144.5, the Linux no-model probe is `codex sandbox -- /bin/true`. The command must pass before a live model process can start.

AutoDev never selects legacy Landlock, persists `features.use_legacy_landlock`, or falls back automatically to danger-full-access.

| Classification | Meaning | Run effect |
| --- | --- | --- |
| `legacy_landlock_incompatibility` | Legacy mode cannot enforce the active permission profile | Infrastructure failure before model |
| `bubblewrap_bootstrap_failure` | Bubblewrap, user namespace, or AppArmor setup failed | Infrastructure failure before model |
| `nested_sandbox_restriction` | An outer container or namespace blocks inner sandbox setup | Infrastructure failure before model |
| `codex_configuration_error` | CLI options, TOML, executable, or profile resolution is invalid | Infrastructure failure before model |
| `environment_runtime_failure` | Other runtime setup failure | Infrastructure failure before model |
| `agent_task_failure` | A started Agent cannot satisfy the Task or returns invalid task output | Task/run outcome after model |

The classifier is an initial routing hint, not a substitute for host diagnostics. The same kernel error can have different roots; for example, `RTM_NEWADDR` can also be caused by Ubuntu AppArmor restrictions.

The explicit `external-sandbox` mode is reserved for trusted Docker/devcontainer environments. It requires project policy plus exact `AUTODEV_EXTERNAL_SANDBOX=1` and is never selected as fallback.

## Review routing

| Condition | Required gate |
| --- | --- |
| LOW/MEDIUM BUILD with no special change class | Builder self-review, deterministic validation, Commander gate |
| HIGH risk | Independent Reviewer |
| Architecture, public interface, security, migration, shared schema/data structure | Independent Reviewer |
| Milestone integration or threshold rework | Integration checks and independent Reviewer |
| HARDENING | Full checks and independent Reviewer |

Reviewer context contains the frozen Task, relevant interfaces, diff, and validation evidence. It excludes Builder reasoning history.

Reviewer results are `PASS`, `PASS_WITH_DEBT`, `REWORK`, or `BLOCKED`, with ordered findings and missing evidence.

`PASS_WITH_DEBT` is limited to eligible non-blocking LOW/MEDIUM findings. Each item identifies source Task, reason, severity, module, and fix-before milestone.

Safety, data loss, failed acceptance criteria, public-interface breakage, and unrecorded debt are never eligible.

## Budgets, stagnation, stopping, and recovery

Defaults are 30 iterations/four hours per run; four work attempts, two reworks, and two consecutive stagnations per Task; 600 seconds idle, 2400 seconds hard timeout, and one infrastructure retry per Attempt.

Progress is semantic. Its fingerprint includes Task/phase, acceptance evidence, failure signatures, allowed-path diff hash, and blocking review findings. Logs, narrative summaries, and weakened tests do not reset stagnation.

`autodev stop` writes the managed stop request. The runner interrupts the process group, waits ten seconds, terminates, then force-kills after the final grace period.

The runner records recoverable state. A normal resume starts fresh rather than importing old model context.

A same-host stale lock is recoverable only after its PID is proven dead. A live lock is never stolen.

## Migration

`autodev migrate --check` is read-only and reports conversion and conflicts.

`--apply` stages the V2 tree in a temporary location, validates it, installs it atomically, and freezes the V1 backup.

Packaged checksums identify unmodified framework copies. Modified files stop automatic migration for explicit resolution.

`--rollback MIGRATION_ID` is allowed only before any V2 run or meaningful state advance.

After migration, `.autodev/` is canonical and `.agent/` is never consulted as live state.

## Completion, checkpoints, and session rotation

Completion requires accepted blocking Tasks, accepted evidence for every MUST requirement, no blocking debt, successful full project validation, and no current Task/run/lock/blocker. An agent proposal cannot set COMPLETE.

At milestone completion or a major architecture change, update state, roadmap, ADRs, test evidence, and `.agent/HANDOFF.md`. A fresh Commander session reconstructs its context from those files, not the previous conversation.

The accepted BUILD + LOW smoke demonstrates the full path: one Builder, zero Reviewers, one allowed source change, passing validation, immutable evidence, Task ACCEPTED, and Project COMPLETE.

## Stable command and exit-code contract

The implemented V2 command surface is recorded in `docs/ARCHITECTURE.md` and `README.md`.

Exit codes are `0` success, `1` invalid, `2` not ready/paused, `3` blocked, `4` stopped, and `5` infrastructure failure.

Tests and CI inject `FakeCodexRunner`. Real Codex execution remains a separate, explicitly authorized live smoke.
