# Roadmap

## Completed: AutoDev 2.0 alpha core

- M1 Foundation: concrete project/requirement/architecture contracts, four ADRs, installable `2.0.0a1` package and resources.
- M2 Control and migration: Draft 2020-12 schemas, fail-closed `ControlPlane`, frozen Task contracts, init, checksum-aware V1 check/apply/rollback.
- M3 Runner: atomic directory lock, heartbeat/stale recovery, source fingerprint, one isolated worktree, binary patch checkpoint, Codex/Fake Engines, one-Task run loop.
- M4 Quality: structured validation, independent read-only Reviewer routing, evidence hashes, protected/allowed paths and debt gate.
- M5 Reliability/release gate: explicit continuous mode, STOP/resume, stale workspace patch recovery, semantic stagnation, budgets, derived completion, wheel/install smoke and documentation.
- M6 Live-smoke hardening: corrected Codex 0.144.5 argv scoping, config/probe/login checks, strict output schema, three-layer live authorization, clean Git admission, direct BUILD + LOW/MEDIUM Builder routing, a reproducible greeting fixture, Codex permission profiles, and a no-model Linux sandbox preflight.

## Release status

- Version remains `2.0.0a1`.
- PyPI publishing is not part of this work.
- The accepted BUILD + LOW smoke ran one Builder and zero Reviewers, changed only `greeting.py`, passed validation, accepted the Task, and completed the disposable project.
- Core workflow implementation and the current acceptance gates are complete. Stable API commitment and package publication remain separate release decisions.
- AutoDev stops on a failed no-model sandbox preflight before Task claim or model invocation. It never selects a backend or automatically falls back to danger-full-access.
- A trusted Docker/devcontainer may use explicit `external-sandbox` mode with a second environment confirmation. Token-consuming execution still requires separate live authorization.
- Deferred: Docker, dashboard, notifications, multi-repository execution, parallel writers/worktrees, local-commit mode, Codex SDK adapter, and native Windows process/lock semantics.
