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
import time
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
from autodev.campaign import CampaignController, TARGET_PHASE
from autodev.campaign_workspace import CampaignWorkspace, CampaignWorkspaceError
from autodev.control_plane import Command, ControlPlane


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

    def get_next_action(self, campaign_id: str) -> ActionOutcome:
        state = self._read_state()
        campaign = state.get("campaigns", {}).get(campaign_id)
        if campaign is None:
            return ActionOutcome("INVALID", f"unknown Campaign: {campaign_id}")
        pending_id = state.get("current_action_id")
        if pending_id:
            return self._load_pending(str(pending_id), campaign_id)
        if state.get("project_status") == "BLOCKED":
            return self._create_terminal(campaign_id, "ASK_HUMAN")
        if state.get("project_status") == "PAUSED" or state.get("pause_requested"):
            return self._create_terminal(campaign_id, "PAUSED")
        if campaign["status"] == "TARGET_REACHED":
            return self._create_terminal(campaign_id, "TARGET_REACHED")
        if campaign["status"] == "WAITING_FOR_HUMAN":
            return self._create_terminal(campaign_id, "ASK_HUMAN")
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
                if action.get("type") == "EXECUTE_TASK":
                    run_id = str(action.get("context", {}).get("run_id", ""))
                    attempt_path = self._canonical / "runs" / run_id / "action-attempt.json"
                    refreshed = self._read_state()
                    task = refreshed.get("tasks", {}).get(action.get("task_id"), {})
                    if task.get("status") == "REVIEWING" and attempt_path.is_file():
                        attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
                        final = self._create_immediate_review(
                            action, Path(action["workspace"]), attempt,
                        )
                    else:
                        if Path(str(action.get("workspace", ""))).exists():
                            CampaignWorkspace(
                                self._root, str(action["campaign_id"]),
                            ).remove_task_workspace(Path(action["workspace"]))
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
        if result.get("canonical_revision") != action_revision or not (
            revision_is_current or graceful_pause_only
        ):
            return ActionOutcome("INVALID", "stale Action revision")
        if action["type"] == "EXECUTE_TASK":
            return self._submit_worker(action, dict(result))
        if action["type"] == "PLAN_PHASE":
            return self._submit_plan(action, dict(result))
        if action["type"] == "RUN_IMMEDIATE_REVIEW":
            return self._submit_review(action, dict(result))
        if action["type"] == "RUN_PHASE_REVIEW":
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

    def _create_execute(self, campaign_id: str, phase: str, task_id: str) -> ActionOutcome:
        action_id = f"ACTION-{uuid.uuid4().hex}"
        run_id = f"RUN-ACTION-{uuid.uuid4().hex[:16]}"
        contract = json.loads(
            (self._canonical / "tasks" / task_id / "contract.json").read_text(encoding="utf-8")
        )
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
        claim = self._control.execute(Command("run.claim", {"task_id": task_id, "run_id": run_id}))
        if claim.status != "SUCCESS":
            workspace_owner.remove_task_workspace(workspace)
            return ActionOutcome(claim.status, claim.message, data=claim.data)
        running = self._control.execute(Command("run.phase", {"run_id": run_id, "to": "RUNNING"}))
        if running.status != "SUCCESS":
            return ActionOutcome(running.status, running.message, data=running.data)
        _write_json_atomic(self._canonical / "runs" / run_id / "context.json", {
            "run_id": run_id, "task_id": task_id, "campaign_id": campaign_id,
            "phase": phase, "contract_hash": self._read_state()["tasks"][task_id]["contract_hash"],
            "created_at": _now(),
        })
        action = self._action_record(
            action_id=action_id, action_type="EXECUTE_TASK", campaign_id=campaign_id,
            phase=phase, task_id=task_id, role="worker",
            quality_route=str(contract.get("review_scope", "NONE")), workspace=workspace,
            context={
                "run_id": run_id, "contract": contract,
                "source_fingerprint": source_fingerprint(self._root).digest,
            },
        )
        return self._persist_action(action)

    def _create_plan(self, campaign_id: str, phase: str) -> ActionOutcome:
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
                "run_id": run_id, "campaign": contract,
                "requirements": requirements["requirements"],
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
        if any(item["returncode"] != 0 for item in validations):
            action_type, role, route = "RUN_DIAGNOSTIC", "diagnostic", "DIAGNOSTIC"
        else:
            action_type, role, route = "RUN_PHASE_REVIEW", "reviewer", "PHASE"
        action = self._action_record(
            action_id=f"ACTION-{uuid.uuid4().hex}", action_type=action_type,
            campaign_id=campaign_id, phase=phase, task_id=phase_tasks[0][0],
            role=role, quality_route=route, workspace=workspace,
            context={
                "run_id": run_id, "phase_task_ids": [item[0] for item in phase_tasks],
                "validations": validations,
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
        contract = dict(action["context"]["contract"])
        actual_source = source_fingerprint(self._root).digest
        expected_source = action["context"].get("source_fingerprint")
        if actual_source != expected_source:
            return ActionOutcome(
                "INVALID",
                f"source changed concurrently: expected {expected_source}, found {actual_source}",
            )
        if result["outcome"] not in {"PASS", "PASS_WITH_DEBT"}:
            return self._finish_nonpassing(action, result)
        policy = json.loads((self._canonical / "policy.json").read_text(encoding="utf-8"))
        git_workspace = GitWorkspace(self._root, run_id, path=workspace)
        try:
            patch, changed_paths = git_workspace.collect_patch(
                allowed_paths=contract["allowed_paths"],
                protected_paths=policy.get(
                    "protected_paths",
                    (".autodev/**", ".git/**", ".codex/config.toml", "Second version.md"),
                ),
            )
        except (PatchPolicyViolation, OSError, RuntimeError) as error:
            return ActionOutcome("INVALID", f"workspace policy rejected result: {error}")
        if not patch:
            return ActionOutcome("INVALID", "Worker produced no changes")
        validations = self._validations(contract, workspace)
        phased = self._control.execute(Command("run.phase", {"run_id": run_id, "to": "VALIDATING"}))
        if phased.status != "SUCCESS":
            return ActionOutcome(phased.status, phased.message, data=phased.data)
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
        self._write_evidence(action, result, patch, changed_paths, validations, None)
        finished = self._control.execute(Command("run.finish", {
            "run_id": run_id, "outcome": "REWORK",
        }))
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
        else:
            CampaignWorkspace(self._root, str(action["campaign_id"])).remove_task_workspace(
                Path(action["workspace"])
            )
            final = self.get_next_action(str(action["campaign_id"]))
        _write_json_atomic(
            self._canonical / "actions" / action["id"] / "outcome.json",
            self._outcome_dict(final),
        )
        return final

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
                "failure": dict(failure),
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
        if result["outcome"] == "BLOCKED":
            blocked = self._control.execute(Command("project.transition", {
                "to": "BLOCKED", "blocker": result["blocker"],
                "next_action": result["next_action"],
            }))
            if blocked.status != "SUCCESS":
                return ActionOutcome(blocked.status, blocked.message, data=blocked.data)
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
        action = self._action_record(
            action_id=f"ACTION-{uuid.uuid4().hex}", action_type="RUN_IMMEDIATE_REVIEW",
            campaign_id=str(worker["campaign_id"]), phase=worker["phase"],
            task_id=str(worker["task_id"]), role="reviewer", quality_route="IMMEDIATE",
            workspace=workspace,
            context={
                "run_id": worker["context"]["run_id"],
                "contract": worker["context"]["contract"],
                "source_fingerprint": worker["context"]["source_fingerprint"],
                "worker_action_id": worker["id"],
                "patch_hash": attempt["patch_hash"],
                "validations": attempt["validations"],
            },
        )
        return self._persist_action(action)

    def _submit_review(self, action: dict[str, Any], result: dict[str, Any]) -> ActionOutcome:
        workspace = Path(action["workspace"]).resolve()
        if _workspace_fingerprint(workspace) != action["workspace_fingerprint"]:
            return ActionOutcome("INVALID", "read-only Reviewer modified its workspace")
        if source_fingerprint(self._root).digest != action["context"]["source_fingerprint"]:
            return ActionOutcome("INVALID", "source changed concurrently during Review")
        if result["outcome"] not in {"PASS", "PASS_WITH_DEBT"}:
            return self._finish_nonpassing(action, result)
        run_id = str(action["context"]["run_id"])
        try:
            attempt = json.loads(
                (self._canonical / "runs" / run_id / "action-attempt.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as error:
            return ActionOutcome("INFRA_FAILURE", f"review attempt cannot be recovered: {error}")
        contract = dict(action["context"]["contract"])
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
        worker_result["data"] = {**worker_result.get("data", {}), "review": result}
        return self._accept_changes(
            action, worker_result, patch, changed_paths, list(attempt["validations"]),
            submitted_result=result,
        )

    def _submit_phase_review(
        self, action: dict[str, Any], result: dict[str, Any],
    ) -> ActionOutcome:
        workspace = Path(action["workspace"]).resolve()
        if _workspace_fingerprint(workspace) != action["workspace_fingerprint"]:
            return ActionOutcome("INVALID", "read-only Phase Reviewer modified its workspace")
        if source_fingerprint(self._root).digest != action["context"]["source_fingerprint"]:
            return ActionOutcome("INVALID", "source changed concurrently during Phase Review")
        if result["outcome"] not in {"PASS", "PASS_WITH_DEBT"}:
            CampaignWorkspace(self._root, str(action["campaign_id"])).remove_task_workspace(workspace)
            return self._resolve(
                action, result,
                ActionOutcome(
                    "BLOCKED" if result["outcome"] == "BLOCKED" else "NOT_READY",
                    result["summary"], data={"findings": result["findings"]},
                ),
            )
        campaign_id = str(action["campaign_id"])
        phase = str(action["phase"])
        state = self._read_state()
        checkpoint = state["campaigns"][campaign_id]["checkpoint"]
        _write_json_atomic(
            self._canonical / "campaigns" / campaign_id / f"phase-summary-{phase}.json",
            {
                "campaign_id": campaign_id, "phase": phase, "status": "PASSED",
                "checkpoint": checkpoint, "validations": action["context"]["validations"],
                "review": result, "review_attempts": 1,
            },
        )
        CampaignWorkspace(self._root, campaign_id).remove_task_workspace(workspace)
        resolved = self._resolve(
            action, result, ActionOutcome("SUCCESS", f"Phase Review {action['id']} accepted"),
        )
        if resolved.status != "SUCCESS":
            return resolved
        campaign = self._read_state()["campaigns"][campaign_id]
        if phase == TARGET_PHASE[campaign["target"]]:
            reached = self._control.execute(Command("campaign.transition", {
                "id": campaign_id, "status": "TARGET_REACHED", "phase": "TARGET_REACHED",
            }))
            if reached.status != "SUCCESS":
                return ActionOutcome(reached.status, reached.message, data=reached.data)
            materialized = CampaignController(self._root).materialize(campaign_id)
            if materialized.status != "SUCCESS":
                final = self.get_next_action(campaign_id)
            else:
                final = self.get_next_action(campaign_id)
        else:
            contract = json.loads(
                (self._canonical / "campaigns" / campaign_id / "campaign.json").read_text(encoding="utf-8")
            )
            phases = contract["phases"]
            next_phase = phases[phases.index(phase) + 1]
            advanced = self._control.execute(Command(
                "campaign.transition", {"id": campaign_id, "phase": next_phase},
            ))
            if advanced.status != "SUCCESS":
                return ActionOutcome(advanced.status, advanced.message, data=advanced.data)
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
    ) -> ActionOutcome:
        workspace = Path(action["workspace"]).resolve()
        run_id = str(action["context"]["run_id"])
        task_id = str(action["task_id"])
        campaign_id = str(action["campaign_id"])
        pause_after_action = bool(self._read_state().get("pause_requested"))
        campaign_workspace = CampaignWorkspace(self._root, campaign_id)
        try:
            checkpoint = campaign_workspace.checkpoint(workspace, task_id=task_id, run_id=run_id)
        except CampaignWorkspaceError as error:
            return ActionOutcome("INFRA_FAILURE", f"checkpoint failed: {error}")
        checkpointed = self._control.execute(Command("campaign.checkpoint", {
            "id": campaign_id, "checkpoint": checkpoint.commit, "task_id": task_id,
        }))
        if checkpointed.status != "SUCCESS":
            return ActionOutcome(checkpointed.status, checkpointed.message, data=checkpointed.data)
        campaign_workspace.finalize_checkpoint(
            checkpoint, canonical_revision=checkpointed.revision or 0,
        )
        evidence_id = self._write_evidence(
            action, result, patch, changed_paths, validations, checkpoint.commit,
        )
        finished = self._control.execute(Command("run.finish", {
            "run_id": run_id, "outcome": result["outcome"], "evidence_id": evidence_id,
            "checkpoint_id": checkpoint.commit,
            "debt_items": result["data"].get("debt_items", []),
        }))
        if finished.status != "SUCCESS":
            return ActionOutcome(finished.status, finished.message, data=finished.data)
        campaign_workspace.remove_task_workspace(workspace)
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

    def _finish_nonpassing(
        self, action: dict[str, Any], result: dict[str, Any],
    ) -> ActionOutcome:
        run_id = str(action["context"]["run_id"])
        outcome = result["outcome"]
        arguments: dict[str, Any] = {"run_id": run_id, "outcome": outcome}
        if outcome == "BLOCKED":
            arguments.update(blocker=result["blocker"], next_action=result["next_action"])
        finished = self._control.execute(Command("run.finish", arguments))
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
        results: list[dict[str, Any]] = []
        for validation in contract["validation_commands"]:
            started = time.monotonic()
            try:
                process = subprocess.run(
                    validation["argv"], cwd=workspace / validation["cwd"], shell=False,
                    capture_output=True, text=True, timeout=validation["timeout"], check=False,
                )
                item = {
                    "argv": validation["argv"], "cwd": validation["cwd"],
                    "returncode": process.returncode, "timed_out": False,
                    "stdout": process.stdout, "stderr": process.stderr,
                    "duration_seconds": time.monotonic() - started,
                }
            except subprocess.TimeoutExpired as error:
                item = {
                    "argv": validation["argv"], "cwd": validation["cwd"],
                    "returncode": None, "timed_out": True,
                    "stdout": str(error.stdout or ""), "stderr": str(error.stderr or ""),
                    "duration_seconds": time.monotonic() - started,
                }
            results.append(item)
        return results

    def _write_evidence(
        self, action: Mapping[str, Any], result: Mapping[str, Any], patch: bytes,
        changed_paths: list[str], validations: list[dict[str, Any]], checkpoint: str | None,
    ) -> str:
        run_id = str(action["context"]["run_id"])
        evidence = {
            "task_id": action["task_id"], "run_id": run_id,
            "outcome": result["outcome"], "created_at": _now(),
            "action_id": action["id"], "result_hash": _hash(result),
            "diff_hash": _hash(patch), "changed_paths": changed_paths,
            "validations": [
                {"argv": item["argv"], "returncode": item["returncode"],
                 "timed_out": item["timed_out"], "log_hash": _hash(item)}
                for item in validations
            ],
            "checkpoint_id": checkpoint,
        }
        evidence_id = f"EVIDENCE-{_hash(evidence)}"
        evidence["evidence_id"] = evidence_id
        _write_json_atomic(self._canonical / "runs" / run_id / "evidence.json", evidence)
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
