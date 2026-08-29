# AutoDev V3 Workflow

1. `campaign plan` runs a fresh read-only Planner, asks at most three directional questions, and emits a hashed proposal.
2. `campaign approve` freezes Requirement JSON and Authority Envelope and creates the private Campaign ref.
3. ControlPlane atomically admits the first phase Task batch when every Task stays inside the envelope.
4. RunController executes one Task from the current private checkpoint. Accepted work advances only the private ref.
5. When all phase Tasks are accepted, Phase Gate runs cumulative validation. Architecture/internal interface work receives one cumulative Phase Review.
6. A passed gate writes a phase summary, advances phase, and launches a fresh Planner. No intermediate human approval is needed in STAGED mode.
7. Two identical semantic failure fingerprints launch one fresh read-only Diagnostic, followed by repair planning.
8. At the selected target, STAGED writes the full incremental binary patch to the unchanged user worktree. CRITICAL first persists a human gate.
9. The user may raise maturity with `retarget`, derive reports, or archive after all results are materialized.

Human escalation occurs only for envelope exceptions, genuine blockers, CRITICAL gates, target completion, credential/environment needs, write-back conflicts, or exhausted mandatory budgets.

Resume never restores a model conversation. It reconciles journals and canonical evidence, then starts a fresh Planner or specialist execution.

V2 projects use `autodev migrate v2 --check` before applying. Dirty accepted source requires the exact reported fingerprint. Rollback is allowed only before the first V3 state revision.
