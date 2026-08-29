# TASK-011: Ship the stdio MCP and explicit AutoDev plugin

- Risk: `HIGH`
- Quality mode: `INTEGRATION`
- Requirements: `REQ-043`, `REQ-048`, `REQ-051`, `REQ-052`, `REQ-053`, `REQ-054`
- Milestone: `M10-V4-CODEX-PLUGIN`
- Status: `DRAFT`

## Objective

Make `$autodev` the working Codex-native control surface using a strict local stdio MCP server and a minimal Skill-plus-MCP plugin.

## In Scope

- The exact twelve MCP tools, explicit `project_root`, strict schemas/annotations/error mapping, safe root handling, official-client stdio tests, and Core-only writes.
- `plugins/autodev` manifest at `4.0.0-alpha.1`, `.mcp.json`, explicit `$autodev` Skill, and repository personal marketplace entry.
- Grill, one Proposal/Authority confirmation, compact Commander summaries, Action loop, fresh specialist instructions, and single-Worker rule.

## Out of Scope

- Hooks, UI, `.app.json`, automatic Goal Mode, backend selectors, Core state-machine duplication, README release evidence, or live model Campaigns.

## Acceptance Criteria

- Official Python MCP client initializes stdio, lists exactly the contracted tools, drives valid/invalid calls, and observes accurate annotations/errors without a Codex subprocess.
- Plugin/Skill/marketplace validators and temporary install smoke pass; structure proves no hooks/UI/backend selector.
- Fake plugin flow covers initialize, Proposal/approval, BLOCKED answer, pause/resume, Action progression, and target completion.

## Mandatory Tests

- Focused MCP protocol/security tests.
- Plugin, Skill, and marketplace validators plus temporary installation smoke.
- `python3 -m unittest discover -s tests -v`
- `git diff --check`

## Do Not

- Do not run `autodev start`, `codex exec`, App Server, hidden Skill invocation, hooks, or remote mutation.

## Evidence

- Tool-list/schema/annotation snapshots, injection/process tests, validator output, installed plugin layout, and Fake end-to-end trace.

## Output Contract

Return exactly these headings: `STATUS`, `FILES_CHANGED`, `TEST_RESULTS`, `ACCEPTANCE_EVIDENCE`, `RISKS`, `STATE_UPDATE_PROPOSAL`.
