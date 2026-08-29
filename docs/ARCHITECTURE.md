# Architecture

Every material design section declares one status: `FROZEN`, `PROVISIONAL`, or `OPEN`. FROZEN sections are binding and change only through an ADR and required independent review.

## System Context — FROZEN

AutoDev 2.0 is a local, policy-first control plane around one Git project and the Codex CLI.

AutoDev owns durable contracts, state, scheduling, locks, budgets, evidence, quality routing, recovery, and completion.

Codex performs one bounded implementation or review attempt. It is an execution dependency, not the lifecycle authority.

The source repository retains V1 `.agent/` data as a migration fixture. V2 target projects use `.autodev/` as their sole canonical state.

A target may retain `.agent/` only as migration input or a frozen backup. Git supplies isolation and patch handling.

AutoDev never commits, pushes, publishes, deploys, rotates credentials, or mutates remotes by default.

## Public Module Seams — FROZEN

The package has two deep public modules and one deliberately narrow adapter seam:

```text
CLI -> ControlPlane.execute(Command) -> CommandResult
    -> RunController.run(RunRequest) -> RunOutcome
        -> ExecutionEngine.preflight(...) / execute(AttemptRequest) -> EngineResult
           |- CodexExecEngine
           `- FakeCodexRunner
```

- `ControlPlane` hides schema validation, legal transitions, optimistic revision checks, atomic writes, event recording, contract freezing, and derived completion.
- `RunController` hides deterministic selection, claim/run orchestration, isolated workspace lifecycle, budgets, retries, validation, review, stagnation, stopping, checkpoint application, and recovery.
- `ExecutionEngine` declares whether live authorization is required, performs a no-model runtime preflight, and crosses the process boundary for exactly one attempt. V2 initially has only a real Codex adapter and a deterministic test fake.
- The CLI parses public commands and renders `CommandResult`; it does not contain business transitions or runner policy.

Internal helpers remain implementation details until evidence demonstrates a second meaningful consumer. AutoDev does not implement an agent runtime, message bus, or natural-language completion protocol. See ADR-0001.

## Package and Target Layout — FROZEN

```text
src/autodev/
  cli.py
  control_plane.py
  run_controller.py
  engines/
  resources/
    schemas/
    templates/

target project:
.autodev/
  manifest.json  config.json  policy.json  state.json
  tasks/  runs/  debt.json  locks/  workspaces/  migrations/
.codex/agents/
  autodev-builder.toml
```

Python 3.11+ is required. `jsonschema>=4.26,<5` is the sole runtime dependency. Draft 2020-12 plus the standard format checker validates V2 contracts.

Packaged resources load through `importlib.resources`, not assumed filesystem-relative paths.

V2 initialization installs contracts, state templates, and the Builder definition only. It does not copy AutoDev source or framework tests.

Codex's built-in explorer replaces a custom template. Reviewer execution is a fresh read-only Codex session, not a reusable project template.

## CLI Contract — FROZEN

```text
autodev version
autodev init TARGET --name NAME [--merge]
autodev doctor [--json]
autodev validate [--ready] [--json]
autodev activate

autodev migrate --check
autodev migrate --apply
autodev migrate --rollback MIGRATION_ID

autodev task create --id ID --title TITLE --risk LEVEL \
  --quality-mode MODE --requirements IDS
autodev task ready|show|defer|block|unblock TASK_ID
autodev task reopen TASK_ID --reason REASON

autodev run [--task TASK_ID]
autodev run --until complete-or-blocked
autodev resume [--recover-stale]
autodev stop
autodev complete

autodev checkpoint adopt-existing
autodev status [--json]
autodev logs --run RUN_ID
autodev evidence TASK_ID
```

Stable exit codes are `0` success, `1` invalid, `2` not ready/paused, `3` blocked, `4` stopped, and `5` infrastructure failure.

The command surface sits behind `ControlPlane` and `RunController`. The CLI does not embed business transitions.

## Canonical Data and Ownership — FROZEN

`.autodev/` is the single source of V2 operational truth. It stores versions, revision, Project state, current milestone/Task/run, last outcome/checkpoint, blocker, owner, and next action.

Only `ControlPlane` writes canonical state. Agents return structured proposals; the runner independently creates evidence.

Each Task uses editable `contract.json` in DRAFT and generated `contract.md` for people and agents. READY freezes the JSON hash; a mismatch stops execution.

`task reopen --reason` is the sole mutation path for a frozen contract and invalidates older claims and evidence.

`docs/REQUIREMENTS.md` remains requirement truth. The parser extracts ID, priority, status, and acceptance signal without copying prose into canonical state.

Evidence and run artifacts are immutable, content-addressed where practical, and referenced from canonical indexes. See ADR-0002.

## State Machines — FROZEN

- Project: `BOOTSTRAP | ACTIVE | PAUSED | BLOCKED | COMPLETE | FAILED | STOPPED`.
- Task mainline: `DRAFT -> READY -> CLAIMED -> RUNNING -> VALIDATING -> [REVIEWING] -> ACCEPTED`.
- Task side states: `DEFERRED | BLOCKED | CANCELLED`; explicit commands govern return paths such as unblock and reopen.
- Attempt outcomes: `PASS | PASS_WITH_DEBT | REWORK | NO_PROGRESS | INFRA_FAILURE | BLOCKED | STOPPED`.

Illegal transitions fail before mutation. Recoverable infrastructure outcomes map to PAUSED; a user stop maps to STOPPED.

Product decisions, authority, contract conflicts, and exhausted budgets map to BLOCKED. Only unrecoverable canonical corruption maps to FAILED.

Exact transition tables and invariants are executable in `ControlPlane`.

## Runner and Checkpoint Flow — FROZEN

One run validates canonical readiness, live authorization, Codex capabilities, Git HEAD, and a clean source baseline before lock or Task claim.

Only `.autodev/**` runtime changes are excluded from the cleanliness gate.

The runner then selects one Task, writes minimal context, claims atomically, and creates one isolated Git worktree. Ordinary resume starts fresh instead of restoring model context.

The runner validates protected/allowed paths, runs structured commands, records evidence, and starts a fresh read-only Reviewer when policy requires it.

After acceptance, it captures a binary-safe patch, rechecks the source fingerprint, applies safely, and records a checkpoint.

A fingerprint mismatch pauses the project and preserves the patch. Crashed worktrees remain recoverable. See ADR-0003.

Selection orders priority, satisfied dependencies, creation time, and Task ID. Default `run` stops after one Task.

Only `--until complete-or-blocked` requests continued scheduling. V2 never has more than one writing Task/worktree.

The detailed sequence is:

```text
authorize + validate + baseline + runtime preflight
    -> project lock
    -> deterministic selection
    -> run context + atomic Task claim
    -> isolated worktree + fresh Builder
    -> path checks + deterministic validation
    -> optional fresh Reviewer
    -> evidence + checkpoint
    -> source fingerprint recheck + patch apply
    -> ACCEPTED + derived completion
```

Preflight covers each unique live Engine/profile pair. Builder uses `:workspace`; Reviewer uses `:read-only`. A Reviewer profile failure therefore stops the run before the Builder model starts.

## Trust and Mutation Boundaries — FROZEN

The human owns requirements, Task scope, live authorization, external credentials, and product decisions.

`ControlPlane` exclusively owns canonical state mutation. `RunController` owns orchestration and evidence. Agents can propose outcomes but cannot write top-level state or declare completion.

Git owns the source baseline, isolated worktree substrate, binary patch, and concurrent-change signal. AutoDev never overwrites a source tree whose fingerprint changed during a run.

Codex owns model execution and sandbox backend selection. AutoDev supplies permission profiles and fail-closed admission, but does not expose bubblewrap or Landlock as product policy.

## Execution Safety and Recovery — PROVISIONAL

`CodexExecEngine` puts global approval/config options before `exec`, then applies exec-local ephemeral, user-config/rules isolation, JSONL, strict schema, and working directory options.

Approval is `never`. BUILD uses `:workspace`; review uses `:read-only`. MCP servers and hooks are cleared.

AutoDev neither exposes nor selects bubblewrap or Landlock. `features.use_legacy_landlock` is unsupported and never generated, persisted, or recommended.

Before any Linux live model process, the Runner executes a no-model sandbox preflight. Codex CLI 0.144.5 uses `codex sandbox -- /bin/true`.

A failed preflight is an environment/runtime failure before lock or Task claim. AutoDev never falls back automatically to danger-full-access.

Diagnostics distinguish legacy/profile incompatibility, bubblewrap bootstrap, nested sandbox/container restrictions, Codex configuration errors, generic environment failures, and genuine Agent/Task failures.

Classifications are routing hints. Host diagnosis remains necessary because one kernel symptom may have several roots; Ubuntu AppArmor can produce the same `RTM_NEWADDR` symptom as a restricted container.

`external-sandbox` is explicit and limited to trusted Docker/devcontainer environments whose outer container is the security boundary.

It requires project policy plus exact `AUTODEV_EXTERNAL_SANDBOX=1`; it is not a fallback.

`doctor` checks command shape, config, login, sandbox, authorization, runtime policy, Git HEAD, clean baseline, and canonical state.

Agents never receive permission to mutate canonical state.

`AUTODEV_LIVE_CODEX=1` is mandatory at the CLI, Runner, and Engine boundaries. Without it, `run` and `resume` return NOT_READY before STOP cleanup, lock acquisition, Task claim, run artifacts, or process creation.

Validation uses `argv[]`, project-relative `cwd`, and timeout with `shell=False`. Policy allowlists executables and working directories. Task-defined shell strings and environment variables are invalid.

Defaults are 30 iterations/four hours per run; four work attempts, two reworks, and two consecutive no-progress outcomes per Task.

Each Attempt has 600 seconds idle, 2400 seconds hard timeout, and one infrastructure retry.

STOP interrupts the process group, terminates after ten seconds, then force-kills after the final grace period while preserving recovery evidence.

## Quality Routing and Evidence — FROZEN

For LOW/MEDIUM BUILD, the fresh primary Codex execution is the sole restricted Builder.

State, path enforcement, deterministic validation, checkpoint, and evidence form the Commander gate; no Reviewer starts.

Independent review is mandatory for HIGH, HARDENING, architecture, public interfaces, security, migration, shared schemas/data, milestone integration, and threshold rework.

Reviewer context contains only the Task, diff, validation evidence, and relevant interfaces—not Builder reasoning. See ADR-0004.

`PASS_WITH_DEBT` is limited to non-blocking LOW/MEDIUM debt. Security, data loss, failed acceptance criteria, and public-interface breaks cannot be deferred.

Every accepted debt item records identity, source Task, reason, severity, module, and fix-before milestone.

The stagnation fingerprint covers Task/phase, acceptance evidence, failing-check signatures, allowed-path diff hash, and blocking Reviewer findings. Log churn, narrative summaries, or weakened tests do not count as progress.

## Completion and Release — FROZEN

`COMPLETE` is derived, never accepted from an agent proposal.

It requires accepted blocking Tasks, evidence for every MUST requirement, no blocking debt, successful full validation, and no current Task/run/lock/blocker.

Failed prerequisites leave the project non-complete with an actionable outcome.

CI and ordinary tests use `FakeCodexRunner` and are model-free. A real Codex smoke is a separate opt-in test requiring exact `AUTODEV_LIVE_CODEX=1` and user authorization.

The accepted disposable BUILD + LOW smoke ran one Builder and zero Reviewers, changed only `greeting.py`, passed validation, accepted the Task, and derived Project COMPLETE.

The implementation and current acceptance gates are complete. The version remains `2.0.0a1` because stable API commitment and PyPI publication require a separate release decision.

## Open Extensions — OPEN

AutoDev-owned Docker provisioning, dashboard, notifications, multi-repository operation, parallel writers/worktrees, local commits, a Codex SDK adapter, and native Windows process semantics remain outside core V2.

The explicit `external-sandbox` runtime mode does not provision isolation. It only records that a trusted Docker/devcontainer already supplies the outer security boundary.

## Re-evaluation Triggers

- Codex removes or materially changes required structured-output, sandbox, subagent, or non-interactive capabilities.
- Git patches cannot preserve a required file class or source fingerprints cause unacceptable false conflicts.
- A second execution backend demonstrates that the `ExecutionEngine` contract is Codex-specific.
- Real project evidence shows serialized writing cannot meet the target throughput.
- `jsonschema` or Python support policy conflicts with a supported platform or security requirement.
- Quality metrics show the review matrix systematically misses blocking defects or routes excessive low-risk work to review.
