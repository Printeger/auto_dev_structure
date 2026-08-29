# AutoDev V4 Workflow

1. The user explicitly invokes `$autodev` in Codex and states an idea, maturity target, and constraints in natural language.
2. The current Codex grills only on decisions that materially affect scope, architecture, or authority, inspects/initializes through MCP, and creates one strict structured Proposal locally.
3. `propose_campaign` validates and hashes that supplied Proposal. The Skill presents the Proposal and Authority Envelope once; the user confirms once; `approve_campaign` freezes them and creates the Campaign private ref/worktree.
4. The Commander calls `get_next_action`. Core completes any deterministic work first and returns the same pending Action until it receives an admissible result.
5. For `PLAN_PHASE`, `EXECUTE_TASK`, `RUN_IMMEDIATE_REVIEW`, `RUN_DIAGNOSTIC`, or `RUN_PHASE_REVIEW`, the Commander starts one fresh matching subagent. Only a Worker may write, only in the Action workspace, and only one Worker may exist at once.
6. The Commander submits the strict result to `submit_action_result`. Core distrusts claims, derives workspace changes and validation itself, enforces paths and read-only roles, routes quality through the sole `QualityRouter`, records evidence, checkpoints accepted work, and advances state.
7. The Commander repeats from `get_next_action`, retaining only the Campaign/Action summary. It does not run `autodev start`, `codex exec`, App Server, or a second planning loop.
8. `pause_campaign` sets a graceful request. The current Action finishes, deterministic processing completes, and the next outcome is `PAUSED`. A later Codex session uses `$autodev` and `campaign_continue` to reconcile and resume from canonical state.
9. `ASK_HUMAN` is returned only for a genuine blocker or mandatory gate. The Skill submits the answer through `answer_blocker`; it never edits `.autodev/` directly.
10. When target invariants pass, Core attempts safe materialization once. Success returns `TARGET_REACHED`; a concurrent-change or apply conflict returns `ASK_HUMAN` without overwriting files. After the user resolves the conflict, `materialize_campaign` retries explicitly.

`CHANGE`, `STAGED`, and `CRITICAL` select development strategy and quality depth, not execution infrastructure. There is one normal workflow and no backend selector.

The retained headless CLI and Engines adapt to the same Core/Attempt lifecycle for CI, tests, debugging, and recovery. They are compatibility infrastructure, not instructions for ordinary `$autodev` use.
