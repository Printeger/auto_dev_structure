# TASK-001: Activate the repository and establish the V2 architecture

- Risk: `HIGH`
- Quality mode: `BUILD`
- Requirements: `REQ-001`, `REQ-002`, `REQ-003`, `REQ-004`, `REQ-005`
- Milestone: `M1-V2-FOUNDATION`
- Status: `ACCEPTED`

## Objective

Replace the bootstrap placeholders with the binding AutoDev 2.0 project contracts and add an installable Python package skeleton at version `2.0.0a1` without regressing the V1 public behavior.

## Requirements

- `REQ-001`: AutoDev is an installable Python 3.11+ package with an `autodev` console entry point and packaged schemas/templates.
- `REQ-002`: The architecture assigns contracts/state transitions to `ControlPlane`, run lifecycle to `RunController`, and execution to Codex/Fake engine adapters.
- `REQ-003`: `.autodev/` is the only V2 canonical state; `.agent/` is migration input or frozen backup only.
- `REQ-004`: Durable V2 safety, quality-routing, migration, runner, and completion behavior is captured by stable requirements and architecture decisions.
- `REQ-005`: Existing V1 tests remain passing during the foundation slice.

## Inputs

- User-approved “AutoDev 2.0 核心闭环开发方案” in the current request.
- `PROJECT.md`, `docs/REQUIREMENTS.md`, `docs/ARCHITECTURE.md`, `docs/DECISIONS.md`, `.agent/ROADMAP.md`.
- Existing `scripts/autodev.py`, `tests/test_autodev.py`, and template resources.
- Existing `.codex/config.toml` and `Second version.md` are user-owned changes.

## In Scope

- Complete `PROJECT.md`, requirements, architecture, roadmap, workflow/handoff, and ADR index for the V2 milestone sequence.
- Add separate ADRs for the Codex runtime seam, canonical state, isolated patch checkpoint, and quality routing.
- Add `pyproject.toml`, `src/autodev/`, the `autodev` console entry point, version `2.0.0a1`, and resource-loading/package-data foundations.
- Declare Python `>=3.11` and the sole runtime dependency `jsonschema>=4.26,<5`.
- Preserve the V1 script behavior and its existing tests.
- Rename the packaged Builder template to `autodev-builder`; record that Codex's built-in explorer and an independent read-only reviewer session are used in V2. Do not remove legacy templates until migration work has a checksum-backed path.

## Out of Scope

- Executable V2 state machine or task commands (TASK-002).
- V1 migration behavior (TASK-003).
- Locks, worktrees, patch application, Codex execution, reviewer/debt gates, or reliability loop (TASK-004 through TASK-007).
- PyPI publishing or version `2.0.0`.

## Acceptance Criteria

- `AC-001`: No required bootstrap placeholder remains in the root project contracts, and the V1 readiness validator reports ready after the state is activated by Commander.
- `AC-002`: Four indexed ADR files record the runtime seam, canonical state, isolated patch checkpoint, and quality-routing decisions with clear status and consequences.
- `AC-003`: `python -m build` (when the build frontend is available) produces an installable wheel whose metadata requires Python 3.11+, declares only `jsonschema>=4.26,<5` at runtime, exposes `autodev`, and contains required package resources.
- `AC-004`: `autodev version` (directly from source or an installed wheel) prints `2.0.0a1` and exits 0.
- `AC-005`: All existing 14 V1 tests pass unchanged or with only compatibility-oriented updates that retain their asserted public behavior.
- `AC-006`: `.codex/config.toml` and `Second version.md` are not modified, deleted, copied into package resources, or treated as run artifacts.

## Mandatory Tests

- `python3 scripts/autodev.py validate` — exits 0 before Commander activation.
- `python3 -m unittest discover -s tests` — all existing tests pass.
- `PYTHONPATH=src python3 -m autodev version` — prints `2.0.0a1` and exits 0.
- `python3 -m build` — run if the `build` module is available; otherwise report the missing local build frontend as evidence debt, while validating `pyproject.toml` and package discovery directly.

## Do Not

- Do not modify `.codex/config.toml` or `Second version.md`.
- Do not read or package `Auto_Dev.md`.
- Do not implement TASK-002 or later behavior.
- Do not commit, push, publish, deploy, mutate remotes, or invoke unattended/recursive Codex.
- Do not replace V1 `.agent/` state with `.autodev/` yet; this repository remains the V1 source migration fixture until TASK-003.

## Evidence

- Focused diff showing concrete contracts, ADRs, package metadata, package resources, and entry point.
- Exact output of every mandatory test and a list of wheel contents if a wheel is built.
- Confirmation from `git diff -- .codex/config.toml -- 'Second version.md'` that no task-authored changes touched the user-owned inputs.

### Recorded Acceptance Evidence

- `python3 scripts/autodev.py validate`: PASS before activation.
- `python3 -m unittest discover -s tests`: PASS, 18/18 total and 14/14 pre-existing V1 tests.
- `PYTHONPATH=src python3 -m autodev version`: PASS, `2.0.0a1`.
- Offline `pip wheel --no-build-isolation --no-deps`: PASS; wheel metadata and contents include the console entry point, Python requirement, sole runtime dependency, license, schema, manifest, and `autodev-builder` template.
- `python3 -m build`: unavailable locally (`No module named build`); the contract's permitted offline setuptools fallback passed.
- Independent Spec review: PASS with no findings.
- Independent Standards review: initial REWORK resolved; focused re-review PASS with no findings or debt.
- User-owned `.codex/config.toml`, `Second version.md`, and the pre-existing test cache were preserved.

## Output Contract

Return exactly these headings:

- `STATUS`
- `FILES_CHANGED`
- `TEST_RESULTS`
- `ACCEPTANCE_EVIDENCE`
- `RISKS`
- `STATE_UPDATE_PROPOSAL`
