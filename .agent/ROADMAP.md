# Roadmap

## Completed foundations

- V2 established the installable package, canonical ControlPlane, V1 migration, isolated
  Runner/Engine seam, validation/review/evidence gates, reliability budgets, and live Codex
  hardening.
- V3 established Campaign requirements, phase planning/admission, private CAS checkpoints,
  development strategies, human interaction, cumulative review, materialization, and V2 migration.

## Completed V4 implementation

### M7-V4-CONTRACT — V4 contract and package identity

- Frozen single Codex-native topology and Action Protocol in project contracts and ADR-0007.
- Python package `4.0.0a1`, plugin `4.0.0-alpha.1`, MCP dependency, and `autodev-mcp` entry point.
- Independently reviewable V4 Task contracts. TASK-008 is `ACCEPTED`.

### M8-V4-ACTION-CORE — Action Protocol and V3 migration

- Persistent strict Action/Result protocol, isolated workspaces, Core-derived validation,
  QualityRouter routing, evidence/budgets/checkpoints, recovery, pause, progression, and safe
  materialization.
- Guarded `migrate v3 --check|--apply|--rollback` with first-V4-Action rollback prohibition.
- TASK-009 is `ACCEPTED` by focused fault-injection/migration tests and the MCP-enabled full suite.

### M9-V4-HEADLESS-ADAPTER — Unified compatibility path

- Campaign/RunController and Action flows reuse the Attempt lifecycle.
- Fake/Codex/App Server contracts, live gates, CLI exit codes, evidence, and crash recovery remain
  compatible. TASK-010 is `ACCEPTED` by adapter parity and full regression tests.

### M10-V4-CODEX-PLUGIN — MCP, plugin, and Skill

- Twelve strict local stdio tools, explicit `project_root`, annotations, error mapping, safe root
  resolution, and Core-only canonical mutations.
- Explicit `$autodev` Skill, repository `personal` marketplace, one Proposal confirmation, fresh
  specialists, single-Worker rule, and no hooks/UI. TASK-011 is `ACCEPTED` by MCP, validator,
  package-install, and plugin-layout smokes.

### M11-V4-RELEASE-GATE — Documentation, acceptance, and review

- README V4 normal/recovery/migration/headless guidance, handoff/state, package/plugin evidence,
  and the final acceptance record are complete.
- Independent Standards and `v4.md` Spec reviews used fixed point `v3.0.0a1`. Review-driven
  hardening closed Action publication/finalization, strict-schema, blocker, materialization, and
  retry-recovery findings; both axes ultimately reported PASS.
- Final MCP-enabled regression is `199/199 PASS`; wheel/install/stdio and Plugin/Skill/marketplace
  validation pass. TASK-012 is `ACCEPTED`. Nothing was pushed, published, deployed, or run against
  a real model Campaign.

## Deferred

Dashboard/UI, hooks, automatic Goal Mode, parallel writers, multi-repository execution, Windows
process/lock semantics, and any user-facing runtime implementation choice remain out of
scope.
