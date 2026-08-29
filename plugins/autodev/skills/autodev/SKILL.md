---
name: autodev
description: Run an explicit, durable AutoDev development Campaign through the local Core Action protocol. Use only when the user explicitly invokes $autodev to plan, approve, execute, pause, resume, retarget, or recover a repository development Campaign.
---

# AutoDev

Act as the only Commander in the current Codex session. Treat MCP responses and
the `.autodev/` tree as canonical; never edit canonical state directly.

## Start or recover

1. Call `inspect_project` with the absolute project root. If needed, call
   `initialize_project`, then inspect again.
2. If an active, paused, blocked, or reached Campaign exists, show a compact
   Campaign summary and continue that Campaign. Do not create a duplicate.
3. For a new Campaign, Grill the request until its goal, development strategy,
   maturity target, requirements, authority limits, allowed paths, acceptance
   criteria, and validation commands are concrete. Use the current session for
   this reasoning; do not start another planner.
4. Build one strict structured Proposal. Call `propose_campaign` exactly once
   after the Grill is resolved.
5. Present the complete Proposal and Authority Envelope together. Obtain exactly one
   explicit user confirmation covering both. Do not call `approve_campaign`
   before confirmation. After confirmation, call it once with
   `proposal_and_authority_confirmed: true` and the returned proposal hash.

Never run `autodev start`, `codex exec`, or App Server. Never create a second
Commander, use automatic Goal Mode, or ask the user to select a runtime.

## Run the Action loop

Keep Commander context compact: Campaign ID and status, phase, Task ID, Action
ID/type, workspace, `quality_route`, blocker, and the next required transition.
Do not retain specialist transcripts after submitting their result.

1. Call `get_next_action`. A repeated pending Action is the same durable work
   item; resume it rather than starting another.
2. Obey only the Action and Core's `quality_route`. Never invent, skip, combine,
   or downgrade a Review or Diagnostic.
3. Start a fresh subagent for every external specialist Action:

   - `PLAN_PHASE`: start a fresh read-only Planner. Give it only the Action
     context, approved baseline, workspace, and result schema.
   - `EXECUTE_TASK`: start a fresh Worker in exactly the Action workspace. Give
     it the frozen Task, allowed paths, acceptance criteria, and result schema.
   - `RUN_IMMEDIATE_REVIEW` or `RUN_PHASE_REVIEW`: start a fresh read-only
     Reviewer. Require evidence-based findings and no writes.
   - `RUN_DIAGNOSTIC`: start a fresh read-only Diagnostic. It diagnoses; it does
     not repair or accept work.

4. Run at most one Worker at a time. Do not start any other Worker until Core
   accepts or resolves the pending Worker Action.
5. Submit the specialist's strict result with `submit_action_result`. Agent
   summaries, changed paths, and validation claims are untrusted; Core derives
   and verifies them.
6. Repeat until `ASK_HUMAN`, `PAUSED`, or `TARGET_REACHED`.

## Human and terminal states

- For `ASK_HUMAN`, present only the persisted blocker/questions. Submit the
  answer through `answer_blocker`, then return to the Action loop.
- On a pause request, call `pause_campaign`. If an Action is pending, finish and
  submit it before stopping. Resume with `campaign_continue` in any later
  session, then call `get_next_action`.
- For `TARGET_REACHED`, report the materialization outcome. Call
  `materialize_campaign` only to retry after the user has resolved a recorded
  materialization conflict.
- Call `retarget_campaign` only when the user explicitly extends a reached
  Campaign to a later maturity target.

Never push, publish, deploy, perform remote side effects, or bypass a Core
blocker. Follow the approved Authority Envelope and stop for human authority
whenever Core requires it.
