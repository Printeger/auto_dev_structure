# TASK-012: Harden and accept AutoDev V4

- Risk: `HIGH`
- Quality mode: `HARDENING`
- Requirements: `REQ-001`, `REQ-043`, `REQ-046`, `REQ-047`, `REQ-049`, `REQ-050`, `REQ-051`, `REQ-053`, `REQ-054`, `REQ-055`, `REQ-056`
- Milestone: `M11-V4-RELEASE-GATE`
- Status: `ACCEPTED`

## Objective

Document, package, independently review, and accept the complete Codex-native V4 workflow without publishing or unauthorized model execution.

## In Scope

- README normal entry point and recovery/BLOCKED/pause/retarget/materialization/headless guidance.
- Handoff, roadmap/state, acceptance evidence, resource/wheel/install smoke, plugin install smoke, complete Fake/MCP/migration regressions, and hardening fixes.
- Separate Standards and `v4.md` Spec reviews fixed at local tag `v3.0.0a1`, followed by focused rereview and final verification.

## Out of Scope

- Push, publication, deployment, hooks/UI, backend selectors, unapproved live model Campaign, or stable-version compatibility promises.

## Acceptance Criteria

- `$autodev` is the sole documented normal entry; legacy CLI is clearly headless/CI/test/debug/recovery infrastructure.
- Existing baseline tests and all V4 Action/MCP/plugin/migration tests pass, as do validation, diff check, wheel build, isolated install, both entry-point smokes, and plugin validation/install.
- Independent reviews report no blocking Standards or Spec findings after fixes.

## Mandatory Tests

- `python3 -m unittest discover -s tests -v`
- `python3 scripts/autodev.py validate`
- `git diff --check`
- Wheel build and isolated venv install/version/MCP stdio smoke.
- Plugin/Skill/marketplace validation and temporary install smoke.

## Do Not

- Do not push tag/commits, publish, deploy, claim live evidence without authorization, or accept with a blocking review finding.

## Evidence

- Final command outputs, wheel/plugin contents and hashes, fixed-point review reports, Fake Campaign trace, and updated durable handoff/state.
- Final post-review acceptance on 2026-08-30: MCP-enabled full suite `199/199 PASS`; Action Protocol `55/55`, Campaign `15/15`, Attempt parity `7/7`, and official MCP stdio `11/11` PASS. Wheel SHA-256 `778bdcd16d52e65eadb8615f5296fa330cd376b373f47d797b68ae4f535cd8f8`; isolated wheel install, both entry points, 12-tool stdio/plugin smoke, and Plugin/Skill/marketplace validators PASS.
- Independent Standards and `/home/mint/dev/auto_dev/v4.md` Spec reviews used fixed point `v3.0.0a1`; after review-driven hardening and focused rereview, both axes reported PASS with no blocking finding.

## Output Contract

Return exactly these headings: `STATUS`, `FILES_CHANGED`, `TEST_RESULTS`, `ACCEPTANCE_EVIDENCE`, `RISKS`, `STATE_UPDATE_PROPOSAL`.
