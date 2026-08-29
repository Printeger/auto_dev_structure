# Commander Handoff

## Current objective

AutoDev 4.0 alpha implementation, headless parity, local stdio MCP, and repository plugin are
implemented. The release documentation and local acceptance slice is complete. TASK-012 remains in
`REVIEW`: the required independent fixed-point Standards and `v4.md` Spec reviews from
`v3.0.0a1` have not yet been run and must not be inferred from the test evidence below.

The next owner is a fresh Reviewer. Run the two review axes independently, fix any blocking
findings, rerun focused review and final verification, then decide whether TASK-012 can move from
`REVIEW` to `ACCEPTED`.

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
- MCP-enabled isolated environment:
  `python3 -m unittest discover -s tests -v` — `171/171 PASS` in 29.448 seconds. The suite includes
  official Python MCP stdio initialization/tool/call tests; it uses Fake/local processes and made no
  live model call.
- Focused package tests — `8/8 PASS`; focused V4 migration tests — `4/4 PASS`.
- Wheel `dist/autodev-4.0.0a1-py3-none-any.whl` built successfully with SHA-256
  `9fd615468488800fa3674cdefdceada0c3d844797edeba9a788301c21bcc3fa6`.
- A second isolated venv installed that wheel. `autodev version` and `autodev-mcp --version` both
  returned `4.0.0a1`; installed plugin layout plus 12-tool stdio valid/invalid call smoke passed.
- Plugin validator — PASS; Skill validator — PASS; repository `personal` marketplace structure and
  local source resolution — PASS.
- `python3 scripts/autodev.py validate` and `git diff --check` — PASS for the completed
  documentation/state slice. Run them again after any review-driven fix.
- Push, publication, deployment, remote mutation, real model Campaign, and fixed-point review:
  NOT RUN.

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

- Work began from a clean worktree at local implementation commit `43ea3fb`; no unrelated user
  files were modified.
- No tag or commit was pushed. No package/plugin was published or deployed.

## Next action

Use `v3.0.0a1` as the fixed point and run separate Standards and `v4.md` Spec reviews. Do not mark
TASK-012 accepted while either axis has a blocking finding or while final validation is missing.
