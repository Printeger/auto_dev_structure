# Architecture Decision Records

Durable decisions live as individual files under `docs/decisions/`. Add one index row for each ADR; do not turn this file into a chronological development log.

| ADR | Decision | Status | Date |
| --- | --- | --- | --- |
| [ADR-0001](decisions/ADR-0001-codex-runtime-seam.md) | Keep lifecycle authority in AutoDev and use Codex behind one execution seam | ACCEPTED | 2026-08-28 |
| [ADR-0002](decisions/ADR-0002-canonical-state.md) | Make `.autodev/` the only V2 canonical state | ACCEPTED | 2026-08-28 |
| [ADR-0003](decisions/ADR-0003-isolated-patch-checkpoint.md) | Execute in one isolated worktree and integrate by guarded patch checkpoint | ACCEPTED | 2026-08-28 |
| [ADR-0004](decisions/ADR-0004-quality-routing.md) | Route independent review by risk and change class | ACCEPTED | 2026-08-28 |
| [ADR-0005](decisions/ADR-0005-private-campaign-checkpoints.md) | Accumulate Campaign work on a private Git ref with journaled CAS | ACCEPTED | 2026-08-29 |
| [ADR-0006](decisions/ADR-0006-app-server-human-interaction.md) | Isolate experimental App Server input behind HumanInteraction | ACCEPTED | 2026-08-29 |
