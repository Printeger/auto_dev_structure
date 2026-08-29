# Commander Handoff

## Current objective

AutoDev 2.0 alpha core, runtime hardening, and the authorized release smoke are complete. Linux live runs preflight before lock, Task claim, or model invocation. BUILD uses `:workspace`; backend selection belongs to Codex.

The next decision is release policy: keep `2.0.0a1`, define stable compatibility promises, or authorize packaging/publication. No release or remote mutation has been performed.

## Accepted behavior

- Installable Python 3.11+ package, console entry point, version `2.0.0a1`, sole runtime dependency `jsonschema>=4.26,<5`.
- `.autodev/` canonical state, Draft 2020-12 validation, optimistic revisions, atomic state/events, frozen Task contracts and derived completion.
- V2 init plus read-only/checksum-aware V1 migration, staged apply and guarded rollback.
- Atomic lock/heartbeat/stale recovery, isolated Git worktree, binary patch checkpoint, source fingerprint concurrency protection.
- Codex/FakeCodexRunner seam, corrected Codex 0.144.5 option scoping, strict structured output, approval never, MCP/hooks disabled, config/login probing, no-model sandbox preflight, timeout/STOP escalation, and structured runtime diagnostics. Sandbox startup and compatibility failures remain fail-closed.
- Three-layer `AUTODEV_LIVE_CODEX=1` authorization; Git HEAD and clean-source admission before claim; single-Task and explicit continuous Runner, deterministic selection, structured validations, risk-based review routing, evidence/debt gates, budgets and semantic stagnation.
- BUILD + LOW/MEDIUM uses one fresh restricted Builder with no Reviewer; AutoDev's state/path/test/evidence checks are the Commander gate.

## Evidence

- `pytest -q` and `python3 -m unittest discover -s tests -q` — 85/85 PASS; ordinary tests use `FakeCodexRunner` or fake local executables and make no model call.
- `python3 scripts/autodev.py validate --ready`, `PYTHONPATH=src python3 -m autodev version`, and `git diff --check` — PASS.
- Wheel `autodev-2.0.0a1-py3-none-any.whl` built with SHA-256 `e42516f3c0cfc0958fa944b753143f663ffcf097ff16160095360bf80c724ac4`; isolated-venv install with declared dependencies, `autodev version`, fresh `init`, and `validate --json` — PASS.
- `python3 scripts/autodev.py validate --ready` — PASS for the retained V1 migration fixture.
- `PYTHONPATH=src python3 -m autodev version` — `2.0.0a1`.
- Offline wheel build and isolated-venv install smoke PASS; wheel included schemas, templates, checksum manifest and console entry point.
- Accepted real Codex smoke: one Builder, zero Reviewers, only `greeting.py` changed, validation return code 0, Task ACCEPTED, Project COMPLETE, and no completion-time attempt.
- Sanitized evidence is in `examples/build-low-greeting/smoke-result.json`; no model transcript or credentials are stored. PyPI publish: NOT RUN.

## Preserved user state

- `.codex/config.toml` remains the user's pre-existing modification.
- `Second version.md` remains untracked and untouched.
- No commit, push, deploy or remote mutation was performed.

## Next action

Keep the alpha version unless a separate release decision defines compatibility, packaging, publication, and rollback expectations. Future live runs still require doctor readiness and explicit authorization.

For a trusted Docker/devcontainer outer boundary, configure `external-sandbox` plus `AUTODEV_EXTERNAL_SANDBOX=1`; never use it as an automatic fallback.
