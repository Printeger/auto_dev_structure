# ADR-0004: Route independent review by risk and change class

- Status: `ACCEPTED`
- Date: `2026-08-28`
- Owners: `Commander`
- Related requirements: `REQ-021`, `REQ-022`, `REQ-023`, `REQ-027`
- Supersedes: `none`

## Context

Reviewing every small BUILD task independently is costly, but Builder self-review is insufficient for high-impact or integration-sensitive changes. Reviewer independence is also lost if the review inherits Builder reasoning rather than evaluating contracts and evidence.

## Decision

LOW/MEDIUM BUILD work may pass with Builder self-review, deterministic validation, and the Commander gate. A fresh read-only Reviewer is mandatory for HIGH risk, HARDENING, architecture, public-interface, security, migration, shared schema/data structures, milestone integration, or threshold rework.

Reviewer input is limited to the Task contract, relevant requirements/interfaces, diff, and validation evidence. It excludes Builder reasoning history. BUILD blocks only missing required behavior, core test failure, non-negotiable violations, clear regressions, security/correctness defects, and architectural dead ends. Lesser accepted findings become fully recorded debt when eligible.

## Assumptions

- Risk and change classes are explicit and validated in Task contracts.
- Fresh read-only review provides meaningful independence when its evidence bundle is complete.

## Consequences

- Review cost follows impact rather than being uniform.
- High-impact acceptance always has evidence from an independent context.
- `PASS_WITH_DEBT` needs a mechanical eligibility gate and durable debt records.
- Incorrect task classification is a policy risk and must itself be reviewable evidence.

## Alternatives considered

- Require independent review for every Task: rejected for excessive latency in low-risk BUILD work.
- Let the Builder decide whether review is needed: rejected because the gate must be deterministic and outside the attempt.
- Reuse the Builder conversation for review: rejected because it biases the reviewer and couples review to ephemeral context.

## Re-evaluation triggers

- Defect data shows mandatory triggers miss a material category of blocking issue.
- Review volume or false-positive rates prevent useful progress.
- A different evidence-isolation mechanism proves equally independent at lower cost.
