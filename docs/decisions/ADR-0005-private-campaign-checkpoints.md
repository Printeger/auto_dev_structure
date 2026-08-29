# ADR-0005: Accumulate Campaign work on a private Git ref

- Status: `ACCEPTED`
- Date: `2026-08-29`

## Decision

Each Campaign owns `refs/autodev/campaigns/CAMP-NNN/current`. Task worktrees start from that ref. Accepted work uses `write-tree`, `commit-tree`, and compare-and-swap `update-ref`; a two-phase journal bridges the Git-ref/canonical-state crash window. The user branch is never moved and hooks are not run.

At target, AutoDev applies one binary delta from the last materialized checkpoint after source fingerprint and `git apply --check` validation. Conflict blocks without overwriting user changes.

## Consequences

Dependent Tasks see accepted predecessors across processes and phases. Retarget continues from the same ref. Recovery must prove the ref, journal, and acceptance evidence agree; otherwise it fails closed. Archive may delete the ref only after materialization and child-dependency checks.
