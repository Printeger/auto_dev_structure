"""Persistent Codex-native Action orchestration.

The module deliberately exposes one deep workflow seam: callers ask for the
next external Action and submit its strict result.  All state advancement,
workspace inspection, validation, review routing, evidence, and checkpoints
remain Core responsibilities.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from autodev._resources import _read_text
from autodev._workspace import (
    GitWorkspace, PatchPolicyViolation, _write_json_atomic, source_fingerprint,
)
from autodev.attempt_lifecycle import AttemptLifecycle
from autodev.campaign import CampaignController
from autodev.campaign_workspace import CampaignWorkspace, CampaignWorkspaceError
from autodev.control_plane import Command, ControlPlane
from autodev.quality import QualityDecision


_RESULT_SCHEMA = "https://autodev.local/schemas/action-result.schema.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _hash(value: Any) -> str:
    if isinstance(value, bytes):
        content = value
    else:
        content = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()
    return hashlib.sha256(content).hexdigest()


def _workspace_fingerprint(workspace: Path) -> str:
    process = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=workspace, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if process.returncode:
        raise CampaignWorkspaceError(process.stderr.decode(errors="replace").strip())
    diff = subprocess.run(
        ["git", "diff", "--binary", "--full-index", "HEAD", "--"],
        cwd=workspace, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if diff.returncode:
        raise CampaignWorkspaceError(diff.stderr.decode(errors="replace").strip())
    return _hash(process.stdout + b"\0" + diff.stdout)


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    status: str
    message: str
    action: Mapping[str, Any] | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        return {
            "SUCCESS": 0, "INVALID": 1, "NOT_READY": 2, "BLOCKED": 3,
            "STOPPED": 4, "INFRA_FAILURE": 5,
        }[self.status]


class ActionController:
    """Return persistent external Actions and accept untrusted strict results."""

    def __init__(self, project_root: Path) -> None:
        self._root = project_root.resolve()
        self._canonical = self._root / ".autodev"
        self._control = ControlPlane(self._root)
        self._attempts = AttemptLifecycle(self._root)

    def get_next_action(self, campaign_id: str) -> ActionOutcome:
        state = self._read_state()
        campaign = state.get("campaigns", {}).get(campaign_id)
        if campaign is None:
            return ActionOutcome("INVALID", f"unknown Campaign: {campaign_id}")
        pending_id = state.get("current_action_id")
        if pending_id:
            return self._load_pending(str(pending_id), campaign_id)
        recovered = self._recover_unpublished_action(state, campaign_id)
        if recovered is not None:
            return recovered
        if state.get("project_status") == "BLOCKED":
            return self._create_terminal(campaign_id, "ASK_HUMAN")
        if campaign["status"] == "WAITING_FOR_HUMAN":
            return self._create_terminal(campaign_id, "ASK_HUMAN")
        if state.get("project_status") == "PAUSED" or state.get("pause_requested"):
            return self._create_terminal(campaign_id, "PAUSED")
        if campaign["status"] == "TARGET_REACHED":
            return self._create_terminal(campaign_id, "TARGET_REACHED")
        if campaign["status"] != "ACTIVE":
            return ActionOutcome("NOT_READY", f"Campaign {campaign_id} is {campaign['status']}")

        ready = self._ready_tasks(state, campaign_id, campaign["phase"])
        if ready:
            return self._create_execute(campaign_id, campaign["phase"], ready[0])
        phase_tasks = self._phase_tasks(state, campaign_id, campaign["phase"])
        if not phase_tasks:
            return self._create_plan(campaign_id, campaign["phase"])
        if any(record["status"] != "ACCEPTED" for _, record in phase_tasks):
            return ActionOutcome("NOT_READY", "Campaign has no runnable Task")
        phase_flow = self._read_phase_flow(campaign_id, campaign["phase"])
        if phase_flow.get("status") == "REPAIR_PLAN":
            return self._create_plan(campaign_id, campaign["phase"], repair=True)
        if any(self._task_contract(task_id).get("review_scope") == "PHASE" for task_id, _ in phase_tasks):
            return self._create_phase_review(campaign_id, campaign["phase"], phase_tasks)
        progressed = CampaignController(self._root).phase_gate(campaign_id)
        if progressed.status not in {"SUCCESS", "NOT_READY"}:
            return ActionOutcome(progressed.status, progressed.message, data=progressed.data)
        updated = self._read_state()
        updated_campaign = updated["campaigns"][campaign_id]
        if updated_campaign != campaign or updated.get("project_status") != state.get("project_status"):
            return self.get_next_action(campaign_id)
        return ActionOutcome("NOT_READY", progressed.message, data=progressed.data)

    def submit_action_result(
        self, action_id: str, result: Mapping[str, Any],
    ) -> ActionOutcome:
        directory = self._canonical / "actions" / action_id
        try:
            action = json.loads((directory / "action.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ActionOutcome("INVALID", f"unknown Action: {action_id}")
        accepted_path = directory / "result.json"
        if accepted_path.is_file():
            try:
                accepted = json.loads(accepted_path.read_text(encoding="utf-8"))
                recorded = json.loads((directory / "outcome.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                return ActionOutcome("INFRA_FAILURE", f"accepted Action cannot be recovered: {error}")
            if accepted != result:
                return ActionOutcome("INVALID", "conflicting duplicate Action result")
            state = self._read_state()
            if state.get("current_action_id") == action_id:
                reconciled = self._control.execute(Command("action.resolve", {
                    "action_id": action_id, "result": accepted,
                    "result_hash": _hash(accepted), "outcome": recorded,
                }))
                if reconciled.status != "SUCCESS":
                    return ActionOutcome(reconciled.status, reconciled.message, data=reconciled.data)
                if action.get("type") in {"EXECUTE_TASK", "RUN_IMMEDIATE_REVIEW"}:
                    run_id = str(action.get("context", {}).get("run_id", ""))
                    attempt_path = self._canonical / "runs" / run_id / "action-attempt.json"
                    refreshed = self._read_state()
                    task = refreshed.get("tasks", {}).get(action.get("task_id"), {})
                    if (
                        action.get("type") == "EXECUTE_TASK"
                        and task.get("status") == "REVIEWING"
                        and attempt_path.is_file()
                    ):
                        attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
                        final = self._create_immediate_review(
                            action, Path(action["workspace"]), attempt,
                        )
                    else:
                        cleanup = self._safe_cleanup(
                            str(action["campaign_id"]), Path(str(action["workspace"])),
                        )
                        final = cleanup or self.get_next_action(str(action["campaign_id"]))
                    _write_json_atomic(directory / "outcome.json", self._outcome_dict(final))
                    return final
                if action.get("type") == "RUN_PHASE_REVIEW":
                    gated = CampaignController(self._root).phase_gate(str(action["campaign_id"]))
                    if gated.status not in {"SUCCESS", "NOT_READY"}:
                        final = ActionOutcome(gated.status, gated.message, data=gated.data)
                    else:
                        final = self.get_next_action(str(action["campaign_id"]))
                    _write_json_atomic(directory / "outcome.json", self._outcome_dict(final))
                    return final
            return self._outcome_from(recorded)
        errors = self._schema_errors("action-result", result)
        if errors:
            return ActionOutcome("INVALID", "malformed Action result", data={"errors": errors})
        if result.get("action_id") != action_id:
            return ActionOutcome("INVALID", "Action result ID mismatch")
        state = self._read_state()
        if state.get("current_action_id") != action_id:
            return ActionOutcome("INVALID", "Action is not pending")
        action_revision = action.get("canonical_revision")
        state_revision = state.get("revision")
        revision_is_current = state_revision == action_revision
        graceful_pause_only = (
            isinstance(action_revision, int) and isinstance(state_revision, int)
            and state_revision > action_revision and state.get("pause_requested")
            and self._only_pause_events(action_revision, state_revision)
        )
        recoverable_processing = (
            isinstance(action_revision, int) and isinstance(state_revision, int)
            and state_revision > action_revision
            and self._only_processing_events(action, action_revision, state_revision)
        )
        if result.get("canonical_revision") != action_revision or not (
            revision_is_current or graceful_pause_only or recoverable_processing
        ):
            return ActionOutcome("INVALID", "stale Action revision")
        task = state.get("tasks", {}).get(action.get("task_id"), {})
        if (
            task.get("status") == "ACCEPTED"
            and action["type"] in {"EXECUTE_TASK", "RUN_IMMEDIATE_REVIEW"}
        ):
            return self._recover_accepted_action(action, dict(result))
        if action["type"] == "EXECUTE_TASK":
            return self._submit_worker(action, dict(result))
        if action["type"] == "PLAN_PHASE":
            return self._submit_plan(action, dict(result))
        if action["type"] == "RUN_IMMEDIATE_REVIEW":
            return self._submit_review(action, dict(result))
        if action["type"] == "RUN_PHASE_REVIEW":
            recovered_phase = self._recover_passed_phase_review(action, dict(result))
            if recovered_phase is not None:
                return recovered_phase
            return self._submit_phase_review(action, dict(result))
        if action["type"] == "RUN_DIAGNOSTIC":
            return self._submit_diagnostic(action, dict(result))
        return ActionOutcome("INVALID", f"result is not accepted for {action['type']}")

    def _submit_plan(self, action: dict[str, Any], result: dict[str, Any]) -> ActionOutcome:
        data = result["data"]
        workspace = Path(action["workspace"]).resolve()
        if _workspace_fingerprint(workspace) != action["workspace_fingerprint"]:
            return ActionOutcome("INVALID", "read-only Planner modified its workspace")
        if source_fingerprint(self._root).digest != action["context"]["source_fingerprint"]:
            return ActionOutcome("INVALID", "source changed concurrently during planning")
        if result["outcome"] != "PASS":
            return ActionOutcome("INVALID", "Phase Planner must return PASS with a frozen batch")
        if data.get("phase") != action["phase"]:
            return ActionOutcome("INVALID", "Phase Planner returned the wrong phase")
        tasks = data.get("tasks")
        questions = data.get("questions", [])
        if not isinstance(tasks, list) or not tasks:
            return ActionOutcome("INVALID", "Phase Planner returned no Tasks")
        if questions:
            return ActionOutcome("BLOCKED", "Planner questions require human interaction")
        admitted = CampaignController(self._root).admit(str(action["campaign_id"]), tasks)
        if admitted.status != "SUCCESS":
            return ActionOutcome(admitted.status, admitted.message, data=admitted.data)
        if action["context"].get("purpose") == "PHASE_REPAIR":
            flow = self._read_phase_flow(str(action["campaign_id"]), str(action["phase"]))
            flow["status"] = "ACTIVE"
            self._write_phase_flow(str(action["campaign_id"]), str(action["phase"]), flow)
        CampaignWorkspace(self._root, str(action["campaign_id"])).remove_task_workspace(workspace)
        provisional = ActionOutcome("SUCCESS", f"Action {action['id']} accepted")
        resolved = self._resolve(action, result, provisional)
        if resolved.status != "SUCCESS":
            return resolved
        final = self.get_next_action(str(action["campaign_id"]))
        _write_json_atomic(
            self._canonical / "actions" / action["id"] / "outcome.json",
            self._outcome_dict(final),
        )
        return final

    def _read_state(self) -> dict[str, Any]:
        return json.loads((self._canonical / "state.json").read_text(encoding="utf-8"))

    def _load_pending(self, action_id: str, campaign_id: str) -> ActionOutcome:
        try:
            action = json.loads(
                (self._canonical / "actions" / action_id / "action.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as error:
            return ActionOutcome("INFRA_FAILURE", f"pending Action cannot be recovered: {error}")
        errors = self._schema_errors("action", action)
        if errors or action.get("campaign_id") != campaign_id or action.get("id") != action_id:
            return ActionOutcome(
                "INFRA_FAILURE", "pending Action is invalid", data={"errors": errors},
            )
        return ActionOutcome("SUCCESS", f"Action {action_id} is pending", action)

    def _recover_unpublished_action(
        self, state: Mapping[str, Any], campaign_id: str,
    ) -> ActionOutcome | None:
        run_id = state.get("current_run_id")
        task_id = state.get("current_task_id")
        if not isinstance(run_id, str) or not isinstance(task_id, str):
            return None
        context_path = self._canonical / "runs" / run_id / "context.json"
        try:
            context = json.loads(context_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            return ActionOutcome(
                "INFRA_FAILURE", f"claimed run has no recoverable publication context: {error}",
            )
        if context.get("campaign_id") != campaign_id or context.get("task_id") != task_id:
            return ActionOutcome("INFRA_FAILURE", "claimed run publication context is inconsistent")
        task_status = state.get("tasks", {}).get(task_id, {}).get("status")
        if task_status == "REVIEWING":
            try:
                attempt = json.loads(
                    (self._canonical / "runs" / run_id / "action-attempt.json").read_text(encoding="utf-8")
                )
                worker = json.loads(
                    (self._canonical / "actions" / str(attempt["worker_action_id"]) / "action.json").read_text(
                        encoding="utf-8"
                    )
                )
            except (OSError, KeyError, json.JSONDecodeError) as error:
                return ActionOutcome(
                    "INFRA_FAILURE", f"Reviewer handoff cannot be recovered: {error}",
                )
            return self._create_immediate_review(worker, Path(context["workspace"]), attempt)
        action_id = str(context.get("action_id", ""))
        action_path = self._canonical / "actions" / action_id / "action.json"
        if action_path.is_file():
            try:
                action = json.loads(action_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                return ActionOutcome("INFRA_FAILURE", f"orphan Action is invalid: {error}")
        else:
            action = self._action_record(
                action_id=action_id, action_type="EXECUTE_TASK", campaign_id=campaign_id,
                phase=str(context["phase"]), task_id=task_id, role="worker",
                quality_route=str(context["quality_route"]),
                workspace=Path(context["workspace"]),
                context={
                    "run_id": run_id, "contract_ref": context["contract_ref"],
                    "contract_hash": context["contract_hash"],
                    "source_fingerprint": context["source_fingerprint"],
                },
            )
        if action.get("canonical_revision") != state.get("revision", -1) + 1:
            return ActionOutcome("INFRA_FAILURE", "orphan Action revision cannot be reconciled")
        return self._persist_action(action)

    def _create_execute(self, campaign_id: str, phase: str, task_id: str) -> ActionOutcome:
        action_id = f"ACTION-{uuid.uuid4().hex}"
        run_id = f"RUN-ACTION-{uuid.uuid4().hex[:16]}"
        contract = json.loads(
            (self._canonical / "tasks" / task_id / "contract.json").read_text(encoding="utf-8")
        )
        contract_hash = str(self._read_state()["tasks"][task_id]["contract_hash"])
        contract_ref = f"tasks/{task_id}/contract.json"
        policy = json.loads((self._canonical / "policy.json").read_text(encoding="utf-8"))
        attempt_limit = int(policy.get("runner", {}).get("max_work_attempts", 4))
        prior_attempts = 0
        for path in (self._canonical / "runs").glob("*/context.json"):
            try:
                prior_attempts += json.loads(path.read_text(encoding="utf-8")).get("task_id") == task_id
            except (OSError, json.JSONDecodeError):
                continue
        if prior_attempts >= attempt_limit:
            blocked = self._control.execute(Command("project.transition", {
                "to": "BLOCKED",
                "blocker": f"Task {task_id} attempt budget exhausted.",
                "next_action": "Revise the Task or explicitly increase its attempt budget.",
            }))
            if blocked.status != "SUCCESS":
                return ActionOutcome(blocked.status, blocked.message, data=blocked.data)
            return self._create_terminal(campaign_id, "ASK_HUMAN")
        workspace_owner = CampaignWorkspace(self._root, campaign_id)
        try:
            workspace_owner.recover_checkpoints()
            workspace = workspace_owner.create_task_workspace(run_id)
        except (OSError, RuntimeError) as error:
            return ActionOutcome("INFRA_FAILURE", f"cannot create Action workspace: {error}")
        source_digest = source_fingerprint(self._root).digest
        context_record = {
            "run_id": run_id, "task_id": task_id, "campaign_id": campaign_id,
            "phase": phase, "contract_ref": contract_ref, "contract_hash": contract_hash,
            "action_id": action_id, "workspace": str(workspace),
            "quality_route": str(contract.get("review_scope", "NONE")),
            "source_fingerprint": source_digest, "created_at": _now(),
        }
        _write_json_atomic(self._canonical / "runs" / run_id / "context.json", context_record)
        claim = self._control.execute(Command("run.claim", {"task_id": task_id, "run_id": run_id}))
        if claim.status != "SUCCESS":
            workspace_owner.remove_task_workspace(workspace)
            return ActionOutcome(claim.status, claim.message, data=claim.data)
        running = self._control.execute(Command("run.phase", {"run_id": run_id, "to": "RUNNING"}))
        if running.status != "SUCCESS":
            return ActionOutcome(running.status, running.message, data=running.data)
        action = self._action_record(
            action_id=action_id, action_type="EXECUTE_TASK", campaign_id=campaign_id,
            phase=phase, task_id=task_id, role="worker",
            quality_route=str(contract.get("review_scope", "NONE")), workspace=workspace,
            context={
                "run_id": run_id, "contract_ref": contract_ref,
                "contract_hash": contract_hash, "source_fingerprint": source_digest,
            },
        )
        return self._persist_action(action)

    def _create_plan(
        self, campaign_id: str, phase: str, *, repair: bool = False,
    ) -> ActionOutcome:
        contract = json.loads(
            (self._canonical / "campaigns" / campaign_id / "campaign.json").read_text(encoding="utf-8")
        )
        requirements = json.loads(
            (self._canonical / "campaigns" / campaign_id / "requirements.json").read_text(encoding="utf-8")
        )
        run_id = f"PLAN-ACTION-{uuid.uuid4().hex[:16]}"
        try:
            workspace = CampaignWorkspace(self._root, campaign_id).create_task_workspace(run_id)
        except CampaignWorkspaceError as error:
            return ActionOutcome("INFRA_FAILURE", f"cannot create Planner workspace: {error}")
        action = self._action_record(
            action_id=f"ACTION-{uuid.uuid4().hex}", action_type="PLAN_PHASE",
            campaign_id=campaign_id, phase=phase, task_id=None, role="planner",
            quality_route="NONE", workspace=workspace,
            context={
                "run_id": run_id,
                "campaign_ref": f"campaigns/{campaign_id}/campaign.json",
                "requirements_ref": f"campaigns/{campaign_id}/requirements.json",
                "requirements_hash": contract["requirements_hash"],
                "purpose": "PHASE_REPAIR" if repair else "PHASE_PLAN",
                "source_fingerprint": source_fingerprint(self._root).digest,
            },
        )
        return self._persist_action(action)

    def _create_terminal(self, campaign_id: str, action_type: str) -> ActionOutcome:
        state = self._read_state()
        campaign = state["campaigns"][campaign_id]
        role = "human" if action_type == "ASK_HUMAN" else "core"
        action = self._action_record(
            action_id=f"ACTION-{uuid.uuid4().hex}", action_type=action_type,
            campaign_id=campaign_id, phase=campaign["phase"], task_id=None,
            role=role, quality_route="NONE", workspace=None,
            context={"campaign": campaign, "blocker": state.get("blocker"), "next_action": state.get("next_action")},
        )
        return self._persist_action(action)

    def _create_phase_review(
        self, campaign_id: str, phase: str,
        phase_tasks: list[tuple[str, Mapping[str, Any]]],
    ) -> ActionOutcome:
        run_id = f"PHASE-ACTION-{uuid.uuid4().hex[:16]}"
        owner = CampaignWorkspace(self._root, campaign_id)
        try:
            workspace = owner.create_task_workspace(run_id)
        except CampaignWorkspaceError as error:
            return ActionOutcome("INFRA_FAILURE", f"cannot create Phase Review workspace: {error}")
        validations: list[dict[str, Any]] = []
        seen: set[str] = set()
        for task_id, _ in phase_tasks:
            for validation in self._task_contract(task_id)["validation_commands"]:
                key = json.dumps(validation, sort_keys=True)
                if key not in seen:
                    validations.extend(self._validations({"validation_commands": [validation]}, workspace))
                    seen.add(key)
        flow = self._read_phase_flow(campaign_id, phase)
        budget = self._attempts.budget
        phase_context_ref = f"runs/{run_id}/phase-context.json"
        _write_json_atomic(self._canonical / phase_context_ref, {
            "phase_task_ids": [item[0] for item in phase_tasks],
            "validations": validations,
        })
        if any(item["returncode"] != 0 for item in validations):
            fingerprint = _hash([
                {"argv": item["argv"], "returncode": item["returncode"], "timed_out": item["timed_out"]}
                for item in validations if item["returncode"] != 0
            ])
            failure_fingerprints = list(flow.get("failure_fingerprints", []))
            failure_fingerprints.append(fingerprint)
            flow["failure_fingerprints"] = failure_fingerprints[-8:]
            flow["last_failure_ref"] = phase_context_ref
            route = self._attempts.quality.decide(
                self._task_contract(phase_tasks[0][0]),
                failure_fingerprints=flow["failure_fingerprints"],
                diagnostic_used=int(flow.get("diagnostic_attempts", 0)) > 0,
            )
            if route != QualityDecision.DIAGNOSTIC:
                owner.remove_task_workspace(workspace)
                flow["status"] = "REPAIR_PLAN"
                self._write_phase_flow(campaign_id, phase, flow)
                return self._create_plan(campaign_id, phase, repair=True)
            if int(flow.get("diagnostic_attempts", 0)) >= budget.diagnostics_per_task:
                owner.remove_task_workspace(workspace)
                return self._block_phase_budget(campaign_id, phase, "Phase diagnostic budget exhausted")
            action_type, role, route_value = "RUN_DIAGNOSTIC", "diagnostic", "DIAGNOSTIC"
            flow["diagnostic_attempts"] = int(flow.get("diagnostic_attempts", 0)) + 1
            flow["last_failure_fingerprint"] = fingerprint
            flow["status"] = "DIAGNOSTIC_PENDING"
        else:
            review_limit = budget.phase_reviews + budget.phase_rereviews
            if int(flow.get("review_attempts", 0)) >= review_limit:
                owner.remove_task_workspace(workspace)
                return self._block_phase_budget(campaign_id, phase, "Phase Review budget exhausted")
            action_type, role, route_value = "RUN_PHASE_REVIEW", "reviewer", "PHASE"
            flow["review_attempts"] = int(flow.get("review_attempts", 0)) + 1
            flow["status"] = "REVIEW_PENDING"
        self._write_phase_flow(campaign_id, phase, flow)
        action = self._action_record(
            action_id=f"ACTION-{uuid.uuid4().hex}", action_type=action_type,
            campaign_id=campaign_id, phase=phase, task_id=phase_tasks[0][0],
            role=role, quality_route=route_value, workspace=workspace,
            context={
                "run_id": run_id, "phase_context_ref": phase_context_ref,
                "phase_flow_ref": str(self._phase_flow_path(campaign_id, phase).relative_to(self._canonical)),
                "scope": "PHASE",
                "source_fingerprint": source_fingerprint(self._root).digest,
            },
        )
        return self._persist_action(action)

    def _action_record(
        self, *, action_id: str, action_type: str, campaign_id: str,
        phase: str | None, task_id: str | None, role: str, quality_route: str,
        workspace: Path | None, context: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "$schema": "https://autodev.local/schemas/action.schema.json",
            "schema_version": 1,
            "id": action_id,
            "type": action_type,
            "canonical_revision": self._read_state()["revision"] + 1,
            "campaign_id": campaign_id,
            "phase": phase,
            "task_id": task_id,
            "role": role,
            "quality_route": quality_route,
            "workspace": str(workspace) if workspace else None,
            "workspace_fingerprint": _workspace_fingerprint(workspace) if workspace else None,
            "context": context,
            "result_schema": _RESULT_SCHEMA,
            "created_at": _now(),
        }

    def _persist_action(self, action: dict[str, Any]) -> ActionOutcome:
        context_size = len(json.dumps(
            action.get("context", {}), separators=(",", ":"), ensure_ascii=False,
        ).encode())
        if context_size > 32768:
            return ActionOutcome(
                "INFRA_FAILURE", "Core produced an Action context larger than 32768 bytes",
            )
        errors = self._schema_errors("action", action)
        if errors:
            return ActionOutcome("INFRA_FAILURE", "Core produced an invalid Action", data={"errors": errors})
        result = self._control.execute(Command(
            "action.create", {"action": action}, expected_revision=action["canonical_revision"] - 1,
        ))
        if result.status != "SUCCESS":
            return ActionOutcome(result.status, result.message, data=result.data)
        return ActionOutcome("SUCCESS", f"Action {action['id']} is pending", action)

    def _submit_worker(self, action: dict[str, Any], result: dict[str, Any]) -> ActionOutcome:
        workspace = Path(action["workspace"]).resolve()
        run_id = str(action["context"]["run_id"])
        task_id = str(action["task_id"])
        campaign_id = str(action["campaign_id"])
        try:
            contract = self._contract_for(action)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            return ActionOutcome("INFRA_FAILURE", f"frozen Task contract cannot be recovered: {error}")
        actual_source = source_fingerprint(self._root).digest
        expected_source = action["context"].get("source_fingerprint")
        if actual_source != expected_source:
            return ActionOutcome(
                "INVALID",
                f"source changed concurrently: expected {expected_source}, found {actual_source}",
            )
        policy = json.loads((self._canonical / "policy.json").read_text(encoding="utf-8"))
        try:
            patch, changed_paths = self._attempts.derive_workspace(
                run_id=run_id, workspace=workspace, contract=contract,
                protected_paths=policy.get(
                    "protected_paths",
                    (".autodev/**", ".git/**", ".codex/config.toml", "Second version.md"),
                ),
            )
        except (PatchPolicyViolation, OSError, RuntimeError) as error:
            return self._reject_worker_attempt(
                action, result, f"workspace policy rejected result: {error}",
            )
        if result["outcome"] not in {"PASS", "PASS_WITH_DEBT"}:
            return self._finish_nonpassing(action, result, patch, changed_paths)
        if not patch:
            return ActionOutcome("INVALID", "Worker produced no changes")
        validations = self._validations(contract, workspace)
        task_status = self._read_state()["tasks"][task_id]["status"]
        if task_status == "RUNNING":
            phased = self._control.execute(Command("run.phase", {"run_id": run_id, "to": "VALIDATING"}))
            if phased.status != "SUCCESS":
                return ActionOutcome(phased.status, phased.message, data=phased.data)
        elif task_status not in {"VALIDATING", "REVIEWING"}:
            return ActionOutcome("INFRA_FAILURE", f"cannot reconcile Task from {task_status}")
        failed = [item for item in validations if item["returncode"] != 0]
        if failed:
            return self._resolve_validation_failure(
                action, result, patch, changed_paths, validations,
            )
        if action["quality_route"] == "IMMEDIATE":
            reviewing = self._control.execute(Command(
                "run.phase", {"run_id": run_id, "to": "REVIEWING"},
            ))
            if reviewing.status != "SUCCESS":
                return ActionOutcome(reviewing.status, reviewing.message, data=reviewing.data)
            attempt = {
                "worker_action_id": action["id"], "worker_result": result,
                "review_action_id": f"ACTION-{uuid.uuid4().hex}",
                "patch_hash": _hash(patch), "changed_paths": changed_paths,
                "validations": validations,
            }
            _write_json_atomic(
                self._canonical / "runs" / run_id / "action-attempt.json", attempt,
            )
            provisional = ActionOutcome("SUCCESS", f"Action {action['id']} accepted")
            resolved = self._resolve(action, result, provisional)
            if resolved.status != "SUCCESS":
                return resolved
            review = self._create_immediate_review(action, workspace, attempt)
            _write_json_atomic(
                self._canonical / "actions" / action["id"] / "outcome.json",
                self._outcome_dict(review),
            )
            return review
        return self._accept_changes(action, result, patch, changed_paths, validations)

    def _reject_worker_attempt(
        self, action: dict[str, Any], submitted_result: dict[str, Any], message: str,
    ) -> ActionOutcome:
        """Resolve a valid submission rejected by a Core-derived trust gate."""

        run_id = str(action["context"]["run_id"])
        evidence_result = {
            **submitted_result, "outcome": "REWORK", "summary": message,
            "blocker": None, "next_action": None,
        }
        evidence_id = self._write_evidence(
            action, evidence_result, b"", [], [], None,
            action_result=submitted_result,
        )
        finished = self._attempts.finish(
            run_id=run_id, outcome="REWORK", evidence_id=evidence_id,
        )
        if finished.status != "SUCCESS":
            return ActionOutcome(finished.status, finished.message, data=finished.data)
        resolved = self._resolve(
            action, submitted_result, ActionOutcome("NOT_READY", message),
        )
        if resolved.status != "NOT_READY":
            return resolved
        cleanup = self._safe_cleanup(str(action["campaign_id"]), Path(action["workspace"]))
        return cleanup or resolved

    def _resolve_validation_failure(
        self, action: dict[str, Any], result: dict[str, Any], patch: bytes,
        changed_paths: list[str], validations: list[dict[str, Any]],
    ) -> ActionOutcome:
        run_id = str(action["context"]["run_id"])
        fingerprint = _hash({
            "task_id": action["task_id"], "diff_hash": _hash(patch),
            "failed": [
                {"argv": item["argv"], "returncode": item["returncode"], "timed_out": item["timed_out"]}
                for item in validations if item["returncode"] != 0
            ],
        })
        failure = {
            "action_id": action["id"], "task_id": action["task_id"],
            "fingerprint": fingerprint, "validations": validations,
            "diff_hash": _hash(patch), "changed_paths": changed_paths,
        }
        _write_json_atomic(self._canonical / "runs" / run_id / "failure.json", failure)
        evidence_result = {**result, "outcome": "REWORK"}
        evidence_id = self._write_evidence(
            action, evidence_result, patch, changed_paths, validations, None,
        )
        finished = self._attempts.finish(
            run_id=run_id, outcome="REWORK", evidence_id=evidence_id,
        )
        if finished.status != "SUCCESS":
            return ActionOutcome(finished.status, finished.message, data=finished.data)
        resolved = self._resolve(
            action, result,
            ActionOutcome("NOT_READY", "Core validation requires focused rework", data={"validations": validations}),
        )
        if resolved.status not in {"SUCCESS", "NOT_READY"}:
            return resolved
        same_failures = []
        diagnostic_used = False
        for path in sorted((self._canonical / "runs").glob("*/failure.json")):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if item.get("task_id") == action["task_id"]:
                same_failures.append(item.get("fingerprint"))
                diagnostic_used = diagnostic_used or path.with_name("diagnostic.json").is_file()
        if (
            len(same_failures) >= 2
            and same_failures[-1] == same_failures[-2]
            and not diagnostic_used
        ):
            final = self._create_diagnostic(action, Path(action["workspace"]), failure)
            _write_json_atomic(
                self._canonical / "actions" / action["id"] / "outcome.json",
                self._outcome_dict(final),
            )
            return final
        cleanup = self._safe_cleanup(str(action["campaign_id"]), Path(action["workspace"]))
        return cleanup or resolved

    def _create_diagnostic(
        self, worker: Mapping[str, Any], workspace: Path, failure: Mapping[str, Any],
    ) -> ActionOutcome:
        action = self._action_record(
            action_id=f"ACTION-{uuid.uuid4().hex}", action_type="RUN_DIAGNOSTIC",
            campaign_id=str(worker["campaign_id"]), phase=worker["phase"],
            task_id=str(worker["task_id"]), role="diagnostic", quality_route="DIAGNOSTIC",
            workspace=workspace,
            context={
                "run_id": worker["context"]["run_id"],
                "source_fingerprint": worker["context"]["source_fingerprint"],
                "failure_ref": f"runs/{worker['context']['run_id']}/failure.json",
            },
        )
        return self._persist_action(action)

    def _submit_diagnostic(
        self, action: dict[str, Any], result: dict[str, Any],
    ) -> ActionOutcome:
        workspace = Path(action["workspace"]).resolve()
        if _workspace_fingerprint(workspace) != action["workspace_fingerprint"]:
            return ActionOutcome("INVALID", "read-only Diagnostic modified its workspace")
        if source_fingerprint(self._root).digest != action["context"]["source_fingerprint"]:
            return ActionOutcome("INVALID", "source changed concurrently during Diagnostic")
        run_id = str(action["context"]["run_id"])
        _write_json_atomic(self._canonical / "runs" / run_id / "diagnostic.json", result)
        CampaignWorkspace(self._root, str(action["campaign_id"])).remove_task_workspace(workspace)
        phase_scope = action.get("context", {}).get("scope") == "PHASE"
        if result["outcome"] == "BLOCKED":
            blocked = self._control.execute(Command("project.transition", {
                "to": "BLOCKED", "blocker": result["blocker"],
                "next_action": result["next_action"],
            }))
            if blocked.status != "SUCCESS":
                return ActionOutcome(blocked.status, blocked.message, data=blocked.data)
        elif phase_scope:
            flow = self._read_phase_flow(str(action["campaign_id"]), str(action["phase"]))
            flow["status"] = "REPAIR_PLAN"
            self._write_phase_flow(str(action["campaign_id"]), str(action["phase"]), flow)
        resolved = self._resolve(
            action, result,
            ActionOutcome("SUCCESS", f"Diagnostic {action['id']} accepted"),
        )
        if resolved.status != "SUCCESS":
            return resolved
        final = self.get_next_action(str(action["campaign_id"]))
        _write_json_atomic(
            self._canonical / "actions" / action["id"] / "outcome.json",
            self._outcome_dict(final),
        )
        return final

    def _create_immediate_review(
        self, worker: Mapping[str, Any], workspace: Path, attempt: Mapping[str, Any],
    ) -> ActionOutcome:
        action_id = str(attempt.get("review_action_id", ""))
        action_path = self._canonical / "actions" / action_id / "action.json"
        if action_path.is_file():
            try:
                action = json.loads(action_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                return ActionOutcome("INFRA_FAILURE", f"orphan Reviewer Action is invalid: {error}")
        else:
            action = self._action_record(
                action_id=action_id, action_type="RUN_IMMEDIATE_REVIEW",
                campaign_id=str(worker["campaign_id"]), phase=worker["phase"],
                task_id=str(worker["task_id"]), role="reviewer", quality_route="IMMEDIATE",
                workspace=workspace,
                context={
                    "run_id": worker["context"]["run_id"],
                    "contract_ref": worker["context"]["contract_ref"],
                    "contract_hash": worker["context"]["contract_hash"],
                    "source_fingerprint": worker["context"]["source_fingerprint"],
                    "worker_action_id": worker["id"],
                    "patch_hash": attempt["patch_hash"],
                    "attempt_ref": f"runs/{worker['context']['run_id']}/action-attempt.json",
                },
            )
        state = self._read_state()
        if action.get("canonical_revision") != state.get("revision", -1) + 1:
            return ActionOutcome("INFRA_FAILURE", "orphan Reviewer Action revision cannot be reconciled")
        return self._persist_action(action)

    def _submit_review(self, action: dict[str, Any], result: dict[str, Any]) -> ActionOutcome:
        workspace = Path(action["workspace"]).resolve()
        if _workspace_fingerprint(workspace) != action["workspace_fingerprint"]:
            return ActionOutcome("INVALID", "read-only Reviewer modified its workspace")
        if source_fingerprint(self._root).digest != action["context"]["source_fingerprint"]:
            return ActionOutcome("INVALID", "source changed concurrently during Review")
        budget_errors = self._attempts.review_budget_errors(result)
        if budget_errors:
            return ActionOutcome("INVALID", budget_errors[0], data={"errors": budget_errors})
        if result["outcome"] not in {"PASS", "PASS_WITH_DEBT"}:
            return self._finish_nonpassing(action, result)
        run_id = str(action["context"]["run_id"])
        try:
            attempt = json.loads(
                (self._canonical / "runs" / run_id / "action-attempt.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as error:
            return ActionOutcome("INFRA_FAILURE", f"review attempt cannot be recovered: {error}")
        contract = self._contract_for(action)
        policy = json.loads((self._canonical / "policy.json").read_text(encoding="utf-8"))
        try:
            patch, changed_paths = GitWorkspace(
                self._root, run_id, path=workspace,
            ).collect_patch(
                allowed_paths=contract["allowed_paths"],
                protected_paths=policy.get("protected_paths", (".autodev/**", ".git/**")),
            )
        except (PatchPolicyViolation, OSError, RuntimeError) as error:
            return ActionOutcome("INVALID", f"workspace policy rejected Review: {error}")
        if _hash(patch) != attempt.get("patch_hash"):
            return ActionOutcome("INVALID", "reviewed diff no longer matches the Worker result")
        worker_result = dict(attempt["worker_result"])
        worker_result.update(
            outcome=result["outcome"], summary=result["summary"],
            findings=result["findings"], blocker=result["blocker"],
            next_action=result["next_action"],
        )
        worker_result["data"] = {
            "review": result, "debt_items": result["data"].get("debt_items", []),
        }
        return self._accept_changes(
            action, worker_result, patch, changed_paths, list(attempt["validations"]),
            submitted_result=result, review_result=result,
        )

    def _submit_phase_review(
        self, action: dict[str, Any], result: dict[str, Any],
    ) -> ActionOutcome:
        workspace = Path(action["workspace"]).resolve()
        if _workspace_fingerprint(workspace) != action["workspace_fingerprint"]:
            return ActionOutcome("INVALID", "read-only Phase Reviewer modified its workspace")
        if source_fingerprint(self._root).digest != action["context"]["source_fingerprint"]:
            return ActionOutcome("INVALID", "source changed concurrently during Phase Review")
        budget = self._attempts.budget
        debt_items = result["data"].get("debt_items", [])
        budget_errors = self._attempts.review_budget_errors(result)
        if budget_errors:
            return ActionOutcome("INVALID", budget_errors[0], data={"errors": budget_errors})
        campaign_id = str(action["campaign_id"])
        phase = str(action["phase"])
        flow = self._read_phase_flow(campaign_id, phase)
        if result["outcome"] not in {"PASS", "PASS_WITH_DEBT"}:
            CampaignWorkspace(self._root, str(action["campaign_id"])).remove_task_workspace(workspace)
            if result["outcome"] == "REWORK" and int(flow.get("review_attempts", 0)) < (
                budget.phase_reviews + budget.phase_rereviews
            ):
                flow["status"] = "REPAIR_PLAN"
                self._write_phase_flow(campaign_id, phase, flow)
                provisional = ActionOutcome(
                    "SUCCESS", result["summary"], data={"findings": result["findings"]},
                )
                resolved = self._resolve(action, result, provisional)
                if resolved.status != "SUCCESS":
                    return resolved
                final = self.get_next_action(campaign_id)
                _write_json_atomic(
                    self._canonical / "actions" / action["id"] / "outcome.json",
                    self._outcome_dict(final),
                )
                return final
            blocker = result.get("blocker") or "Phase Review budget exhausted."
            next_action = result.get("next_action") or "Revise the Phase plan or explicitly change its quality budget."
            blocked = self._control.execute(Command("project.transition", {
                "to": "BLOCKED", "blocker": blocker, "next_action": next_action,
            }))
            if blocked.status != "SUCCESS":
                return ActionOutcome(blocked.status, blocked.message, data=blocked.data)
            provisional = ActionOutcome("BLOCKED", result["summary"], data={"findings": result["findings"]})
            resolved = self._resolve(action, result, provisional)
            if resolved.status != "BLOCKED":
                return resolved
            final = self.get_next_action(campaign_id)
            _write_json_atomic(
                self._canonical / "actions" / action["id"] / "outcome.json",
                self._outcome_dict(final),
            )
            return final
        if result["outcome"] == "PASS_WITH_DEBT":
            recorded = self._control.execute(Command("debt.record", {
                "campaign_id": campaign_id, "phase": phase, "debt_items": debt_items,
            }))
            if recorded.status != "SUCCESS":
                return ActionOutcome(recorded.status, recorded.message, data=recorded.data)
        state = self._read_state()
        checkpoint = state["campaigns"][campaign_id]["checkpoint"]
        try:
            phase_context = json.loads(
                (self._canonical / str(action["context"]["phase_context_ref"])).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as error:
            return ActionOutcome("INFRA_FAILURE", f"Phase Review context cannot be recovered: {error}")
        _write_json_atomic(
            self._canonical / "campaigns" / campaign_id / f"phase-summary-{phase}.json",
            {
                "campaign_id": campaign_id, "phase": phase, "status": "PASSED",
                "checkpoint": checkpoint, "validations": phase_context["validations"],
                "review": result, "review_attempts": int(flow.get("review_attempts", 0)),
            },
        )
        cleanup = self._safe_cleanup(campaign_id, workspace)
        if cleanup is not None:
            return cleanup
        resolved = self._resolve(
            action, result, ActionOutcome("SUCCESS", f"Phase Review {action['id']} accepted"),
        )
        if resolved.status != "SUCCESS":
            return resolved
        gated = CampaignController(self._root).phase_gate(campaign_id)
        if gated.status not in {"SUCCESS", "NOT_READY"}:
            return ActionOutcome(gated.status, gated.message, data=gated.data)
        final = self.get_next_action(campaign_id)
        _write_json_atomic(
            self._canonical / "actions" / action["id"] / "outcome.json",
            self._outcome_dict(final),
        )
        return final

    def _recover_passed_phase_review(
        self, action: dict[str, Any], result: dict[str, Any],
    ) -> ActionOutcome | None:
        campaign_id = str(action["campaign_id"])
        phase = str(action["phase"])
        summary_path = self._canonical / "campaigns" / campaign_id / f"phase-summary-{phase}.json"
        if not summary_path.is_file():
            return None
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            return ActionOutcome("INFRA_FAILURE", f"passed Phase Review cannot be recovered: {error}")
        state = self._read_state()
        campaign = state.get("campaigns", {}).get(campaign_id, {})
        if (
            summary.get("status") != "PASSED"
            or summary.get("phase") != phase
            or summary.get("checkpoint") != campaign.get("checkpoint")
        ):
            return None
        if summary.get("review") != result:
            return ActionOutcome("INVALID", "Phase Review result conflicts with canonical summary")
        cleanup = self._safe_cleanup(campaign_id, Path(action["workspace"]))
        if cleanup is not None:
            return cleanup
        resolved = self._resolve(
            action, result, ActionOutcome("SUCCESS", f"Phase Review {action['id']} recovered"),
        )
        if resolved.status != "SUCCESS":
            return resolved
        gated = CampaignController(self._root).phase_gate(campaign_id)
        if gated.status not in {"SUCCESS", "NOT_READY"}:
            final = ActionOutcome(gated.status, gated.message, data=gated.data)
        else:
            final = self.get_next_action(campaign_id)
        _write_json_atomic(
            self._canonical / "actions" / action["id"] / "outcome.json",
            self._outcome_dict(final),
        )
        return final

    def _accept_changes(
        self, action: dict[str, Any], result: dict[str, Any], patch: bytes,
        changed_paths: list[str], validations: list[dict[str, Any]],
        *, submitted_result: dict[str, Any] | None = None,
        review_result: Mapping[str, Any] | None = None,
    ) -> ActionOutcome:
        workspace = Path(action["workspace"]).resolve()
        run_id = str(action["context"]["run_id"])
        task_id = str(action["task_id"])
        campaign_id = str(action["campaign_id"])
        try:
            contract = self._contract_for(action)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            return ActionOutcome("INFRA_FAILURE", f"frozen Task contract cannot be recovered: {error}")
        debt_errors = self._attempts.debt_errors(contract, result["outcome"], result["data"])
        if debt_errors:
            return ActionOutcome(
                "INVALID", "debt gate rejected Action result", data={"errors": debt_errors},
            )
        pause_after_action = bool(self._read_state().get("pause_requested"))
        evidence_id: str | None = None

        def write_acceptance_evidence(checkpoint: Any) -> None:
            nonlocal evidence_id
            evidence_id = self._write_evidence(
                action, result, patch, changed_paths, validations, checkpoint.commit,
                action_result=submitted_result or result, review=review_result,
            )

        try:
            checkpoint = self._attempts.recover_or_checkpoint(
                campaign_id=campaign_id, workspace=workspace, task_id=task_id, run_id=run_id,
                before_record=write_acceptance_evidence,
            )
        except CampaignWorkspaceError as error:
            return ActionOutcome("INFRA_FAILURE", f"checkpoint failed: {error}")
        if evidence_id is None:
            return ActionOutcome("INFRA_FAILURE", "checkpoint evidence was not produced")
        finished = self._attempts.finish(
            run_id=run_id, outcome=str(result["outcome"]), evidence_id=evidence_id,
            checkpoint_id=checkpoint.commit, result=result,
        )
        if finished.status != "SUCCESS":
            return ActionOutcome(finished.status, finished.message, data=finished.data)
        cleanup = self._safe_cleanup(campaign_id, workspace)
        if cleanup is not None:
            return cleanup
        provisional = ActionOutcome("SUCCESS", f"Action {action['id']} accepted")
        resolved = self._resolve(action, submitted_result or result, provisional)
        if resolved.status != "SUCCESS":
            return resolved
        if pause_after_action:
            final = self.get_next_action(campaign_id)
        else:
            progressed = CampaignController(self._root).phase_gate(campaign_id)
            if progressed.status not in {"SUCCESS", "NOT_READY"}:
                final = ActionOutcome(progressed.status, progressed.message, data=progressed.data)
            else:
                final = self.get_next_action(campaign_id)
        _write_json_atomic(
            self._canonical / "actions" / action["id"] / "outcome.json",
            self._outcome_dict(final),
        )
        return final

    def _recover_accepted_action(
        self, action: dict[str, Any], result: dict[str, Any],
    ) -> ActionOutcome:
        """Finish any Action publication after crossing durable run acceptance."""

        run_id = str(action["context"]["run_id"])
        try:
            evidence = json.loads(
                (self._canonical / "runs" / run_id / "evidence.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as error:
            return ActionOutcome("INFRA_FAILURE", f"accepted Action evidence cannot be recovered: {error}")
        state = self._read_state()
        task = state.get("tasks", {}).get(action.get("task_id"), {})
        if (
            evidence.get("action_id") != action["id"]
            or evidence.get("action_result_hash", evidence.get("result_hash")) != _hash(result)
            or evidence.get("evidence_id") not in task.get("evidence_ids", [])
        ):
            return ActionOutcome("INVALID", "accepted Action result conflicts with canonical evidence")
        cleanup = self._safe_cleanup(str(action["campaign_id"]), Path(action["workspace"]))
        if cleanup is not None:
            return cleanup
        resolved = self._resolve(
            action, result, ActionOutcome("SUCCESS", f"Action {action['id']} recovered"),
        )
        if resolved.status != "SUCCESS":
            return resolved
        campaign_id = str(action["campaign_id"])
        progressed = CampaignController(self._root).phase_gate(campaign_id)
        if progressed.status not in {"SUCCESS", "NOT_READY"}:
            final = ActionOutcome(progressed.status, progressed.message, data=progressed.data)
        else:
            final = self.get_next_action(campaign_id)
        _write_json_atomic(
            self._canonical / "actions" / action["id"] / "outcome.json",
            self._outcome_dict(final),
        )
        return final

    def _safe_cleanup(self, campaign_id: str, workspace: Path) -> ActionOutcome | None:
        try:
            CampaignWorkspace(self._root, campaign_id).remove_task_workspace(workspace)
        except CampaignWorkspaceError as error:
            return ActionOutcome("INFRA_FAILURE", f"workspace cleanup must be retried: {error}")
        return None

    def _finish_nonpassing(
        self, action: dict[str, Any], result: dict[str, Any],
        patch: bytes | None = None, changed_paths: list[str] | None = None,
    ) -> ActionOutcome:
        run_id = str(action["context"]["run_id"])
        outcome = result["outcome"]
        evidence_id = self._write_evidence(
            action, result, patch or b"", changed_paths or [], [], None,
            review=result if action.get("role") == "reviewer" else None,
        )
        finished = self._attempts.finish(
            run_id=run_id, outcome=outcome, evidence_id=evidence_id, result=result,
        )
        if finished.status != "SUCCESS":
            return ActionOutcome(finished.status, finished.message, data=finished.data)
        CampaignWorkspace(self._root, str(action["campaign_id"])).remove_task_workspace(
            Path(action["workspace"])
        )
        final = ActionOutcome(
            "BLOCKED" if outcome == "BLOCKED" else "NOT_READY",
            result["summary"], data={"findings": result["findings"]},
        )
        return self._resolve(action, result, final)

    def _resolve(
        self, action: Mapping[str, Any], result: Mapping[str, Any], outcome: ActionOutcome,
    ) -> ActionOutcome:
        recorded = self._control.execute(Command("action.resolve", {
            "action_id": action["id"], "result": dict(result),
            "result_hash": _hash(result), "outcome": self._outcome_dict(outcome),
        }))
        if recorded.status != "SUCCESS":
            return ActionOutcome(recorded.status, recorded.message, data=recorded.data)
        return outcome

    def _validations(self, contract: Mapping[str, Any], workspace: Path) -> list[dict[str, Any]]:
        return self._attempts.run_validations(contract, workspace)

    def _write_evidence(
        self, action: Mapping[str, Any], result: Mapping[str, Any], patch: bytes,
        changed_paths: list[str], validations: list[dict[str, Any]], checkpoint: str | None,
        *, action_result: Mapping[str, Any] | None = None,
        review: Mapping[str, Any] | None = None,
    ) -> str:
        run_id = str(action["context"]["run_id"])
        evidence_id, _ = self._attempts.write_evidence(
            run_dir=self._canonical / "runs" / run_id,
            task_id=str(action["task_id"]), run_id=run_id, outcome=str(result["outcome"]),
            contract=self._contract_for(action), patch=patch, changed_paths=changed_paths,
            validations=validations, quality_route=str(action["quality_route"]),
            checkpoint_id=checkpoint, result=result, review=review,
            extra={
                "action_id": action["id"],
                "action_result_hash": _hash(action_result or result),
                "contract_hash": action["context"]["contract_hash"],
            },
        )
        return evidence_id

    @staticmethod
    def _outcome_dict(outcome: ActionOutcome) -> dict[str, Any]:
        return {
            "status": outcome.status, "message": outcome.message,
            "action": dict(outcome.action) if outcome.action is not None else None,
            "data": dict(outcome.data),
        }

    @staticmethod
    def _outcome_from(value: Mapping[str, Any]) -> ActionOutcome:
        return ActionOutcome(value["status"], value["message"], value.get("action"), value.get("data", {}))

    def _phase_tasks(
        self, state: Mapping[str, Any], campaign_id: str, phase: str,
    ) -> list[tuple[str, Mapping[str, Any]]]:
        found: list[tuple[str, Mapping[str, Any]]] = []
        for task_id, record in state["tasks"].items():
            try:
                contract = json.loads(
                    (self._canonical / "tasks" / task_id / "contract.json").read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                continue
            if contract.get("campaign_id") == campaign_id and contract.get("phase") == phase:
                found.append((task_id, record))
        return found

    def _task_contract(self, task_id: str) -> dict[str, Any]:
        return json.loads(
            (self._canonical / "tasks" / task_id / "contract.json").read_text(encoding="utf-8")
        )

    def _contract_for(self, action: Mapping[str, Any]) -> dict[str, Any]:
        return self._attempts.load_contract(
            str(action["context"]["contract_ref"]),
            str(action["context"]["contract_hash"]),
        )

    def _ready_tasks(self, state: Mapping[str, Any], campaign_id: str, phase: str) -> list[str]:
        ready: list[str] = []
        for task_id, record in self._phase_tasks(state, campaign_id, phase):
            if record["status"] != "READY":
                continue
            contract = json.loads(
                (self._canonical / "tasks" / task_id / "contract.json").read_text(encoding="utf-8")
            )
            if all(state["tasks"].get(item, {}).get("status") == "ACCEPTED" for item in contract["dependencies"]):
                ready.append(task_id)
        return sorted(ready)

    @staticmethod
    def _schema_errors(name: str, document: Any) -> list[str]:
        schema = json.loads(_read_text(f"schemas/{name}.schema.json"))
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        return sorted(error.message for error in validator.iter_errors(document))

    def _only_pause_events(self, action_revision: int, state_revision: int) -> bool:
        for revision in range(action_revision + 1, state_revision + 1):
            try:
                event = json.loads(
                    (self._canonical / "events" / f"{revision:020d}.json").read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                return False
            if event.get("command") != "action.pause":
                return False
        return True

    def _only_processing_events(
        self, action: Mapping[str, Any], action_revision: int, state_revision: int,
    ) -> bool:
        allowed = {"run.phase", "campaign.checkpoint", "run.finish", "action.pause", "debt.record"}
        run_id = action.get("context", {}).get("run_id")
        task_id = action.get("task_id")
        for revision in range(action_revision + 1, state_revision + 1):
            try:
                event = json.loads(
                    (self._canonical / "events" / f"{revision:020d}.json").read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                return False
            if event.get("command") not in allowed:
                return False
            payload = event.get("payload", {})
            if event.get("command") in {"run.phase", "run.finish"} and payload.get("run_id") != run_id:
                return False
            if event.get("command") == "campaign.checkpoint" and payload.get("task_id") != task_id:
                return False
        return True

    def _phase_flow_path(self, campaign_id: str, phase: str) -> Path:
        return self._canonical / "campaigns" / campaign_id / f"phase-flow-{phase}.json"

    def _read_phase_flow(self, campaign_id: str, phase: str) -> dict[str, Any]:
        try:
            return json.loads(self._phase_flow_path(campaign_id, phase).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {
                "schema_version": 1, "campaign_id": campaign_id, "phase": phase,
                "status": "ACTIVE", "review_attempts": 0, "diagnostic_attempts": 0,
            }

    def _write_phase_flow(
        self, campaign_id: str, phase: str, flow: Mapping[str, Any],
    ) -> None:
        _write_json_atomic(self._phase_flow_path(campaign_id, phase), dict(flow))

    def _block_phase_budget(
        self, campaign_id: str, phase: str, blocker: str,
    ) -> ActionOutcome:
        flow = self._read_phase_flow(campaign_id, phase)
        flow["status"] = "BLOCKED"
        self._write_phase_flow(campaign_id, phase, flow)
        blocked = self._control.execute(Command("project.transition", {
            "to": "BLOCKED", "blocker": blocker,
            "next_action": "Revise the Phase plan or explicitly change its quality budget.",
        }))
        if blocked.status != "SUCCESS":
            return ActionOutcome(blocked.status, blocked.message, data=blocked.data)
        return self._create_terminal(campaign_id, "ASK_HUMAN")
