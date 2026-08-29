# TASK-007: Harden live Codex launch and prove BUILD LOW smoke

- Risk: `HIGH`
- Quality mode: `HARDENING`
- Requirements: `REQ-018`, `REQ-029`, `REQ-030`
- Milestone: `M6-V2-LIVE-SMOKE`
- Status: `ACCEPTED`

## Objective

Fail closed around real Codex launch, require a clean Git baseline and explicit authorization, and prove a reproducible BUILD LOW live smoke with one Builder and zero Reviewers.

## In Scope

- Correct global/`exec` argv scoping, final-shape/config/login/sandbox probe, three-layer live gate, clean-source admission, strict output schema, Codex permission profiles, greeting fixture, structured runtime diagnostics, and sanitized evidence.

## Out of Scope

- Editing user Codex configuration, selecting bubblewrap/Landlock, automatic danger-full-access fallback, more than one authorized model process, publication, commits, pushes, or release version `2.0.0`.

## Acceptance Criteria

- Missing authorization, Git HEAD, or clean baseline rejects before claim/state/process mutation.
- Linux live smoke performs a no-model sandbox preflight before claim or model; the smoke permits zero infrastructure retries.
- BUILD uses `:workspace`; unsupported legacy Landlock configuration is never persisted or recommended.
- `external-sandbox` requires explicit project policy and `AUTODEV_EXTERNAL_SANDBOX=1`; it is never an automatic fallback.
- Release PASS requires one Builder, zero Reviewers, only `greeting.py` changed, fixed tests passing, Task ACCEPTED, evidence present, and Project COMPLETE.

## Mandatory Tests

- `python3 -m unittest discover -s tests -v`
- Final-shape help/version/login probe plus no-model Linux sandbox preflight.
- Wheel build and isolated install/init/validate smoke.
- One live greeting smoke only when separately authorized.

## Do Not

- Do not retry a failed live call, retain credentials/transcripts in the repository, modify `.codex/config.toml`, commit, push, publish, or claim PASS from BLOCKED evidence.

## Evidence

- Model-free suite, final-shape/login/sandbox probe, wheel/install smoke, and sanitized `examples/build-low-greeting/smoke-result.json`.
- Accepted live smoke: one Builder, zero Reviewers, only `greeting.py` changed, validation passed, Task ACCEPTED, and Project COMPLETE.

## Blocker

No implementation or live-smoke blocker remains. Publication and stable-version policy require a separate release decision.

## Output Contract

Report status, files changed, test results, acceptance evidence, risks, and state-update proposal.
