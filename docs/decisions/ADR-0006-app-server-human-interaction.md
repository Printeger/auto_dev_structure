# ADR-0006: Isolate experimental App Server input behind HumanInteraction

- Status: `ACCEPTED`
- Date: `2026-08-29`

## Decision

Planner sessions prefer Codex App Server with experimental API opt-in and handle `item/tool/requestUserInput`. AutoDev translates the protocol to `HumanRequest → HumanResponse | Pending`. TTY, persistent, timeout-default, and Fake adapters share the same business seam; fresh `codex exec` is the Planner fallback.

Secrets are never persisted. App Server startup or protocol changes do not alter Campaign business state and cannot remove the terminal/persistent recovery path.

## Consequences

Native option UI remains available without making an experimental wire contract canonical. Doctor reports `native` or `fallback`. Headless answers can be persisted and resumed with a fresh Planner rather than an old conversation.
