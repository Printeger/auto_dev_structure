# Commander Handoff

## Current objective

AutoDev 4.0 alpha is locally accepted. The Codex-native Action workflow, shared headless lifecycle,
stdio MCP, repository plugin, migration, documentation, and release gates are complete. TASK-012 is
`ACCEPTED`; there is no pending owner or required implementation work.

Independent Standards and `v4.md` Spec reviews used the annotated `v3.0.0a1` fixed point. Their
blocking findings drove additional strict-schema, read-only identity, human-question,
materialization, and crash-finalization hardening. Both axes ultimately reported PASS.

## Accepted V4 behavior

- Installable Python 3.11+ package `4.0.0a1` with `autodev` and `autodev-mcp` entry points,
  `jsonschema>=4.26,<5`, and `mcp>=2.1,<3`.
- Codex plugin `4.0.0-alpha.1`: explicit `$autodev` Skill plus local stdio MCP only; no hooks or UI.
- One normal interactive path: `$autodev -> local stdio MCP -> AutoDev Core -> current Codex
  Commander -> fresh subagents`.
- One Proposal and Authority Envelope confirmation after the current Commander grills material
  decisions. The MCP path does not launch another Planner, Codex subprocess, or App Server.
- Persistent `ActionController` seam with only `get_next_action(campaign_id)` and
  `submit_action_result(action_id, result)` as workflow methods. Pending reads, result retries,
  revision checks, isolated workspace enforcement, Core-derived changes/validation, QualityRouter,
  evidence, checkpoint, progression, graceful pause, terminal recovery, and safe materialization are
  covered by Fake/fault-injection tests.
- Shared Attempt lifecycle parity for Action and retained headless adapters.
- Guarded V3-to-V4 check/apply/rollback preserving Campaign refs, Tasks, Evidence, checkpoints, and
  dirty user source. Creating the first V4 Action permanently forbids rollback.
- `CHANGE`, `STAGED`, and `CRITICAL` are development strategies. The persisted compatibility field
  `mode` stores that strategy only.

## Local acceptance evidence

- Fixed point: annotated tag `v3.0.0a1^{}` resolves to
  `ad0e586aab8d38477eec7b3f7c5f3f23795ea2ca`.
- MCP-enabled isolated environment: `199/199 PASS`. The suite includes official Python MCP stdio
  initialization/tool/call tests; it uses Fake/local processes and made no live model call.
- Focused final gates: Action Protocol `55/55 PASS`, Campaign `15/15 PASS`, Attempt parity `7/7
  PASS`, official MCP stdio `11/11 PASS`, package `8/8 PASS`, and V4 migration `4/4 PASS`.
- Wheel `dist/autodev-4.0.0a1-py3-none-any.whl` built successfully with SHA-256
  `778bdcd16d52e65eadb8615f5296fa330cd376b373f47d797b68ae4f535cd8f8`.
- A second isolated venv installed that wheel. `autodev version` and `autodev-mcp --version` both
  returned `4.0.0a1`; installed plugin layout plus 12-tool stdio valid/invalid call smoke passed.
- Plugin validator — PASS; Skill validator — PASS; repository `personal` marketplace structure and
  local source resolution — PASS.
- `python3 scripts/autodev.py validate`, compile checks, and `git diff --check` — PASS after the
  final review-driven fix.
- Fixed-point Standards review — PASS; fixed-point `v4.md` Spec review — PASS.
- Push, publication, deployment, remote mutation, and real model Campaign: NOT RUN.

## Recovery and release notes

- The README now documents `$autodev` as the sole normal workflow, explicit invocation, one
  confirmation, Action-loop recovery, BLOCKED answers, graceful pause/resume in a new Commander,
  retarget, conflict-safe target materialization/retry, V3 migration, and headless positioning.
- Target materialization is attempted safely once. A source fingerprint or patch conflict becomes a
  human blocker; explicit materialization is only a post-resolution retry.
- Legacy commands and Codex exec/App Server/Fake engines remain headless, CI, test, debug, and
  recovery infrastructure. They are not a second user workflow.
- Wheel build emits setuptools deprecation warnings for the legacy license table/classifier. It does
  not fail this alpha gate, but should be cleaned up before setuptools' 2027 enforcement date.

## Preserved user state

- Work began from clean implementation commit `ad0e586`; no unrelated user files were modified.
- No tag or commit was pushed. No package/plugin was published or deployed.

## Next action

No required action. Keep the alpha local unless a separate decision authorizes publication or a
new Campaign defines further product work.
