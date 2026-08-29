# ADR-0003: Execute in one isolated worktree and integrate by guarded patch checkpoint

- Status: `ACCEPTED`
- Date: `2026-08-28`
- Owners: `Commander`
- Related requirements: `REQ-015`, `REQ-016`, `REQ-019`, `REQ-020`, `REQ-026`
- Supersedes: `none`

## Context

Autonomous edits must not overwrite concurrent user work, leak failed attempts into the source checkout, or require AutoDev to create commits. The system also needs artifacts that survive cancellation and crashes.

## Decision

Each run acquires an atomic project lock and uses exactly one isolated Git worktree for its writing Task. The runner fingerprints the source baseline, validates protected and Task-allowed paths, runs checks and review in isolation, and captures a binary-safe patch. After acceptance it rechecks the source fingerprint before applying the patch to the source workspace and recording a checkpoint.

If the source fingerprint changed, AutoDev pauses and preserves the patch; it does not overwrite or auto-merge user changes. A successful or safely failed run cleans its worktree. A crashed run preserves recoverable workspace metadata for `resume`.

## Assumptions

- Supported projects use Git and can create a sibling or managed worktree on the same filesystem.
- Binary-safe Git patch plumbing covers the V2 file classes; tests will verify this assumption.

## Consequences

- Failed attempts are isolated from the user's working tree.
- Integration remains explicit and recoverable without automatic commits.
- Concurrent user edits become a PAUSED outcome requiring deliberate reconciliation.
- V2 serializes writer worktrees, limiting throughput but simplifying ownership and recovery.

## Alternatives considered

- Edit the source checkout directly: rejected because rollback and concurrent-edit safety are inadequate.
- Auto-commit every attempt and merge accepted commits: rejected because commits are outside default authority and can pollute history.
- Maintain parallel writer worktrees: deferred because conflict scheduling and ownership are outside core V2.

## Re-evaluation triggers

- Patch application cannot faithfully preserve required metadata or binary files.
- Serial execution becomes the measured bottleneck after core reliability is proven.
- A user-authorized local-commit mode becomes a stable product requirement.
