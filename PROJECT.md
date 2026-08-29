# Project Contract: AutoDev 4.0 alpha

## Mission

Provide one Codex-native autonomous development workflow that turns an approved goal and Authority Envelope into cumulative, bounded, reviewable, and recoverable work. The current Codex session is the Commander; AutoDev Core remains the durable authority.

## Product workflow

The only normal interactive path is:

```text
$autodev -> local stdio MCP -> AutoDev Core -> current Codex Commander -> fresh subagents
```

The Skill grills only on decisions that materially change the result, presents one structured Campaign Proposal and Authority Envelope for confirmation, then drives the Action Protocol until `TARGET_REACHED`, `PAUSED`, or `ASK_HUMAN`. Planner, Worker, Reviewer, and Diagnostic executions are fresh subagents. At most one writing Worker exists at a time.

`CHANGE`, `STAGED`, and `CRITICAL` are development strategies. The persisted V3 field named `mode` remains a compatibility spelling for that strategy; it never selects an execution backend. AutoDev has no Managed/Native mode, `execution_backend`, or equivalent user choice.

## Deliverables

- An installable Python 3.11+ `autodev` package at `4.0.0a1`, with `autodev` and `autodev-mcp` console entry points and declared `jsonschema` and MCP dependencies.
- A Codex plugin at `4.0.0-alpha.1` containing the explicit `$autodev` Skill and a local stdio MCP configuration, with no hooks or UI.
- An `ActionController` deep module exposing `get_next_action(campaign_id)` and `submit_action_result(action_id, result)` while keeping deterministic transitions inside Core.
- Strict, persistent Action and Action Result contracts with revision, workspace, quality route, context, recovery, and idempotency guarantees.
- One shared Attempt lifecycle used by the Codex-native Action path and retained headless adapters.
- A guarded V3-to-V4 migration preserving Campaign private refs, Tasks, Evidence, and checkpoints.

## Success criteria

- A user can initialize, propose, approve, continue, pause, answer, retarget, materialize, and complete a Campaign from `$autodev` without remembering the legacy CLI.
- AutoDev Core is the only canonical writer and independently derives diffs, changed paths, validation results, review routing, evidence, checkpoints, and state transitions.
- Re-reading a pending Action returns the same Action; equal result submission is retry-safe; conflicting, unknown, or stale submissions cause no mutation.
- Workers modify only the Action workspace. Reviewer and Diagnostic Actions are fresh and read-only; any write is rejected.
- A new Codex Commander session can resume the same canonical Campaign and pending Action without restoring model conversation history.
- Existing Fake, Codex exec, App Server, CLI exit-code, packaging, and migration behavior remains available for headless, CI, test, debug, and recovery use.

## Non-negotiable constraints

- `.autodev/` is the only canonical state. Only Core/ControlPlane may write it; the Skill and agents never edit canonical files directly.
- The normal MCP path never starts `codex exec`, App Server, a second Planner, or a second Commander.
- External Action types are limited to `PLAN_PHASE`, `EXECUTE_TASK`, `RUN_IMMEDIATE_REVIEW`, `RUN_DIAGNOSTIC`, `RUN_PHASE_REVIEW`, `ASK_HUMAN`, `PAUSED`, and `TARGET_REACHED`. Validation, checkpoint, phase advancement, materialization decisions, and other deterministic work stay internal.
- Core creates and identifies the Campaign worktree. Workers may write only inside the returned isolated workspace. Agent claims about diffs, tests, or completion are untrusted input.
- `QualityRouter` alone chooses Review and Diagnostic routes. Ordinary LOW/MEDIUM work does not gain a Reviewer because it entered through the plugin.
- Pause is graceful after the current Action. Safe target materialization is attempted once; conflicts become a recoverable human blocker and explicit materialization retries only after resolution.
- Task validation remains structured (`argv`, project-relative `cwd`, timeout) and uses `shell=False`; project-root handling must not permit shell injection or path escape.
- AutoDev does not commit product changes, push, publish, deploy, rotate credentials, or mutate remote systems without separate explicit authorization. V4 implementation may use authorized local incremental development commits only.

## Compatibility infrastructure

`CodexExecEngine`, `AppServerCodexEngine`, Fake engines, `autodev start`, `campaign start`, and `resume --campaign` remain supported infrastructure. They are not a second product workflow or an execution-mode selector, and the README must not present them as the normal entry point.

## Priority order

1. State integrity, user-data safety, and recoverability.
2. One unambiguous Codex-native workflow and canonical authority.
3. Contract and acceptance correctness.
4. Deterministic progress with minimal review and human interruption.
5. Headless compatibility, evidence, and operability.
