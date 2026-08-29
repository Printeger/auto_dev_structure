# ADR-0002: Make `.autodev/` the only V2 canonical state

- Status: `ACCEPTED`
- Date: `2026-08-28`
- Owners: `Commander`
- Related requirements: `REQ-003`, `REQ-007`, `REQ-009`, `REQ-012`, `REQ-014`, `REQ-023`, `REQ-027`
- Supersedes: `none`

## Context

V1 persists workflow state under `.agent/`, while V2 needs richer schemas, revisions, events, run evidence, locks, migrations, and completion derivation. Reading both trees as live state would make authority ambiguous and recovery unsafe.

## Decision

`.autodev/` is the sole canonical state tree for V2. Only `ControlPlane` may mutate it, using Draft 2020-12 validation, optimistic revision checks, atomic replacement, and event records. Agents submit structured proposals; the runner records independent artifacts and references them from canonical state.

`.agent/` may be read only during an explicit V1 migration and may then be retained as a frozen backup. It is never consulted as live V2 state. A packaged checksum manifest distinguishes unmodified V1 framework files from user-modified conflicts.

## Assumptions

- A single canonical tree is sufficient for one local writer and can be backed up atomically during migration.
- Existing V1 repositories can be classified by packaged checksums plus schema-aware conversion.

## Consequences

- Every command and recovery path has one authoritative revision.
- Migration must be explicit, inspectable, conflict-aware, and reversible only before V2 progress/run artifacts exist.
- This source repository remains a V1 fixture until TASK-003; TASK-001 does not replace its `.agent/` state.
- Duplicate summaries outside `.autodev/` are projections or documentation, never operational truth.

## Alternatives considered

- Incrementally evolve `.agent/` in place: rejected because V1/V2 interpretation and rollback become ambiguous.
- Keep `.agent/` and `.autodev/` synchronized: rejected because crashes can split authority.
- Store state only in Git commits: rejected because AutoDev does not commit by default and must represent active/recoverable runs.

## Re-evaluation triggers

- Migration evidence shows a material class of V1 state cannot be converted without ongoing dual reads.
- Multi-repository or distributed execution becomes an accepted requirement.
