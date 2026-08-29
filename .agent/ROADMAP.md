# Roadmap

## Completed foundations

- V2 established the installable package, canonical ControlPlane, V1 migration, isolated Runner/Engine seam, validation/review/evidence gates, reliability budgets, and live Codex hardening.
- V3 established Campaign requirements, phase planning/admission, private CAS checkpoints, development strategies, human interaction, cumulative review, materialization, and V2 migration.

## M7-V4-CONTRACT — V4 contract and package identity

- Freeze the single Codex-native topology and Action Protocol in project contracts and ADR-0007.
- Set Python package version `4.0.0a1`, plugin version contract `4.0.0-alpha.1`, MCP dependency, and `autodev-mcp` entry point.
- Add independently reviewable V4 Task contracts.

## M8-V4-ACTION-CORE — Action Protocol and V3 migration

- Implement strict persistent Action/Result schemas and `ActionController` with recovery-safe idempotency and revision conflict rejection.
- Extract a shared Attempt lifecycle for workspace preparation, validation, Review/Diagnostic routing, evidence, budgets, checkpoint, progression, graceful pause, and safe materialization.
- Add guarded `migrate v3 --check|--apply|--rollback`; forbid rollback after the first V4 Action.

## M9-V4-HEADLESS-ADAPTER — Unified compatibility path

- Move Campaign/RunController and Fake/Codex/App Server Engines onto the shared Attempt lifecycle.
- Preserve existing headless commands, Fake behavior, live gates, and CLI exit codes without exposing an execution-mode selector.

## M10-V4-CODEX-PLUGIN — MCP, plugin, and Skill

- Implement the twelve-tool local stdio MCP boundary with strict schemas, accurate annotations, safe project-root resolution, and no Codex subprocesses.
- Ship the explicit `$autodev` Skill and local plugin, with one Proposal confirmation, Action loop, compact Commander context, fresh subagents, and no hooks/UI.

## M11-V4-RELEASE-GATE — Documentation, acceptance, and review

- Make `$autodev` the README entry point and document recovery, blockers, pause/resume, retarget, materialization, and headless positioning.
- Pass existing and new Fake/MCP/plugin/migration/package tests, validation, wheel/install smoke, and fixed-point Standards plus Spec reviews from `v3.0.0a1`.
- Fix blocking review findings and record final handoff/state/evidence. Do not push, publish, deploy, or run an unauthorized live model Campaign.

## Deferred

Dashboard/UI, hooks, automatic Goal Mode, parallel writers, multi-repository execution, native Windows process/lock semantics, and any user-facing backend choice remain out of scope.
