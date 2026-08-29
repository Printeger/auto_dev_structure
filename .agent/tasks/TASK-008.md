# TASK-008: Freeze the V4 Codex-native contracts and package identity

- Risk: `HIGH`
- Quality mode: `BUILD`
- Requirements: `REQ-001`, `REQ-043`, `REQ-044`, `REQ-045`, `REQ-047`, `REQ-051`, `REQ-052`, `REQ-053`, `REQ-054`, `REQ-056`
- Milestone: `M7-V4-CONTRACT`
- Status: `ACCEPTED`

## Objective

Replace the V3 product topology with one binding Codex-native workflow and Action Protocol contract, and establish the V4 package/MCP entry-point identity without implementing the ActionController or MCP server.

## Inputs

- Approved AutoDev V4 Codex-native development plan and `v4.md` Q2 refinement.
- `PROJECT.md`, requirements, architecture, workflow, roadmap, ADR index/template, package metadata/resources, and package tests.
- Clean `ad0e586` baseline and local annotated tag `v3.0.0a1` at that commit.

## In Scope

- Freeze `$autodev -> MCP -> Core -> current Commander -> fresh subagents`, Action types, authority, persistence/idempotency, workspace trust, shared lifecycle, migration, MCP, plugin, pause, and materialization contracts.
- Add ADR-0007 and the independently reviewable TASK-009 through TASK-012 slices.
- Set package `4.0.0a1`, plugin contract `4.0.0-alpha.1`, dependencies `jsonschema>=4.26,<5` and `mcp>=2.1,<3`, and both console-script declarations.
- Provide only a fail-closed placeholder `autodev-mcp` module sufficient for packaging/entry-point resolution before TASK-011.

## Out of Scope

- Action/Result schemas, ActionController, shared Attempt refactor, V3 migration implementation, working MCP transport/tools, plugin scaffolding, README/handoff/state release evidence, or real model execution.

## Acceptance Criteria

- Contracts contain no user-visible Managed/Native or backend selector and define V3 `mode` only as development-strategy compatibility.
- The normal path has exactly one Commander and no recursive Codex/App Server launch.
- Public Action types and the two-method ActionController seam match the approved plan; deterministic work remains inside Core.
- Package metadata/resource version/tests agree on `4.0.0a1`, declare MCP 2.x, and expose a resolvable, fail-closed `autodev-mcp` entry point.
- Existing tests, project validation, and whitespace checks pass.

## Mandatory Tests

- `python3 -m unittest discover -s tests -p 'test_package.py' -v`
- `python3 -m unittest discover -s tests -v`
- `python3 scripts/autodev.py validate`
- `PYTHONPATH=src python3 -m autodev version`
- `git diff --check`

## Do Not

- Do not implement TASK-009 or later, add hooks/UI/Goal automation, start Codex/App Server, modify remotes, push, publish, deploy, or run a real model Campaign.

## Evidence

- Focused contract/metadata diff, annotated-tag verification, package tests, full Fake suite, validation output, version output, and diff check.

### Recorded Acceptance Evidence

- `v3.0.0a1^{}` and `HEAD` both resolved to `ad0e586aab8d38477eec7b3f7c5f3f23795ea2ca` before the slice; the baseline worktree was clean.
- Package-focused tests passed 8/8; the full model-free suite passed 120/120.
- `python3 scripts/autodev.py validate`, `PYTHONPATH=src python3 -m autodev version`, and `git diff --check` passed; the version output was `4.0.0a1`.
- Offline wheel build passed. Metadata declared both required dependencies and both console scripts; the wheel contained `mcp_server.py` and the versioned resource manifest.
- A temporary system-site-packages venv installed the wheel. `autodev version` and `autodev-mcp --version` returned `4.0.0a1`; the intentionally incomplete `--stdio` path failed closed with exit 2.
- No Codex/App Server/model Campaign, push, publish, deploy, or remote mutation ran.

## Output Contract

Return exactly these headings: `STATUS`, `FILES_CHANGED`, `TEST_RESULTS`, `ACCEPTANCE_EVIDENCE`, `RISKS`, `STATE_UPDATE_PROPOSAL`.
