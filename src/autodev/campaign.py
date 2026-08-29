"""Campaign planning, approval, admission, phase progression, and write-back."""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from autodev._resources import _read_text
from autodev._workspace import _write_json_atomic
from autodev.attempt_lifecycle import AttemptLifecycle
from autodev.campaign_workspace import CampaignWorkspace, CampaignWorkspaceError
from autodev.control_plane import Command, ControlPlane
from autodev.engines import AttemptRequest, ExecutionEngine


TARGET_PHASE = {
    "CHANGE_COMPLETE": "IMPLEMENT",
    "ARCHITECTURE_BASELINE": "SCAFFOLD",
    "WORKING_MVP": "COMPONENT_VERIFY",
    "INTEGRATED_SYSTEM": "INTEGRATE",
    "RELEASE_CANDIDATE": "HARDEN",
}
ALL_PHASES = ["SCAFFOLD", "IMPLEMENT", "COMPONENT_VERIFY", "INTEGRATE", "HARDEN"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def default_authority_envelope() -> dict[str, Any]:
    return {
        "max_task_risk": "MEDIUM",
        "allowed_change_classes": [
            "implementation", "test", "documentation", "architecture",
            "internal-interface", "shared-internal-data",
        ],
        "dependency_policy": "existing-only",
        "public_api_changes": "require-human",
        "security_changes": "require-human",
        "data_migration": "require-human",
        "permission_expansion": "require-human",
        "remote_actions": "require-human",
        "commit": "forbidden", "push": "forbidden", "publish": "forbidden", "deploy": "forbidden",
    }


@dataclass(frozen=True, slots=True)
class CampaignRequest:
    idea: str
    mode: str = "STAGED"
    target: str = "WORKING_MVP"
    autonomy: str = "HUMAN_ON_BLOCKED"
    parent_campaign_id: str | None = None
    source_checkpoint: str | None = None


@dataclass(frozen=True, slots=True)
class CampaignOutcome:
    status: str
    message: str
    campaign_id: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        return {"SUCCESS": 0, "INVALID": 1, "NOT_READY": 2, "BLOCKED": 3,
                "STOPPED": 4, "INFRA_FAILURE": 5}[self.status]


@dataclass(frozen=True, slots=True)
class PlannerRequest:
    campaign_id: str
    idea: str
    mode: str
    target: str
    phase: str
    repository: Path
    requirements: tuple[Mapping[str, Any], ...] = ()
    answers: Mapping[str, str] = field(default_factory=dict)


class Planner(Protocol):
    def plan(self, request: PlannerRequest) -> Mapping[str, Any]: ...


class FakePlanner:
    """Deterministic protocol fake; each call is a fresh logical session."""

    def __init__(self, proposals: Sequence[Mapping[str, Any]]) -> None:
        self._proposals = list(proposals)
        self.requests: list[PlannerRequest] = []

    def plan(self, request: PlannerRequest) -> Mapping[str, Any]:
        self.requests.append(request)
        if not self._proposals:
            raise RuntimeError("FakePlanner has no proposal")
        return dict(self._proposals.pop(0))


class CampaignController:
    """The deep module that hides campaign lifecycle and phase orchestration."""

    def __init__(
        self, project_root: Path, planner: Planner | None = None,
        interaction: Any | None = None,
    ) -> None:
        self.root = project_root.resolve()
        self.canonical = self.root / ".autodev"
        self.control = ControlPlane(self.root)
        self.planner = planner
        self.interaction = interaction
        self.attempts = AttemptLifecycle(self.root)

    def _state(self) -> dict[str, Any]:
        return json.loads((self.canonical / "state.json").read_text(encoding="utf-8"))

    def _reconcile_terminal(self, campaign_id: str, *terminal_types: str) -> CampaignOutcome | None:
        state = self._state()
        pending_id = state.get("current_action_id")
        if pending_id is None:
            return None
        try:
            action = json.loads(
                (self.canonical / "actions" / str(pending_id) / "action.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as error:
            return CampaignOutcome("INFRA_FAILURE", f"cannot reconcile terminal Action: {error}", campaign_id)
        if action.get("campaign_id") != campaign_id or action.get("type") not in terminal_types:
            return CampaignOutcome("NOT_READY", "a non-terminal Action is still pending", campaign_id)
        reconciled = self.control.execute(Command("action.reconcile", {
            "campaign_id": campaign_id, "terminal_types": list(terminal_types),
        }))
        if reconciled.status != "SUCCESS":
            return CampaignOutcome(reconciled.status, reconciled.message, campaign_id, reconciled.data)
        return None

    @staticmethod
    def _authority_changed(proposal: Mapping[str, Any], contract: Mapping[str, Any]) -> bool:
        approved = contract["authority_envelope"]
        return any(
            approved.get(key) != value
            for key, value in proposal.get("authority_envelope", {}).items()
        )

    def _next_campaign_id(self) -> str:
        occupied = {
            int(path.name[5:]) for path in (self.canonical / "campaigns").glob("CAMP-[0-9]*")
            if path.name[5:].isdigit() and (path / "campaign.json").is_file()
        } if (self.canonical / "campaigns").is_dir() else set()
        number = 1
        while number in occupied:
            number += 1
        return f"CAMP-{number:03d}"

    @staticmethod
    def _phases(mode: str, target: str) -> list[str]:
        if mode == "CHANGE":
            if target != "CHANGE_COMPLETE":
                raise ValueError("CHANGE mode has the implicit CHANGE_COMPLETE target")
            return ["IMPLEMENT"]
        if target not in TARGET_PHASE or target == "CHANGE_COMPLETE":
            raise ValueError("STAGED/CRITICAL require a maturity target")
        return list(ALL_PHASES)

    def plan(self, request: CampaignRequest) -> CampaignOutcome:
        if not request.idea.strip():
            return CampaignOutcome("INVALID", "Campaign idea must be non-empty")
        if request.mode not in {"CHANGE", "STAGED", "CRITICAL"}:
            return CampaignOutcome("INVALID", "mode must be CHANGE, STAGED, or CRITICAL")
        try:
            phases = self._phases(request.mode, request.target)
        except ValueError as error:
            return CampaignOutcome("INVALID", str(error))
        if self.planner is None:
            return CampaignOutcome("NOT_READY", "a Planner is required to create a Campaign proposal")
        campaign_id = self._next_campaign_id()
        try:
            proposal = dict(self.planner.plan(PlannerRequest(
                campaign_id, request.idea.strip(), request.mode, request.target, phases[0], self.root,
            )))
        except Exception as error:
            from autodev.engines.app_server import HumanInputPending
            if isinstance(error, HumanInputPending):
                _write_json_atomic(
                    self.canonical / "campaigns" / campaign_id / "planning-context.json",
                    {
                        "idea": request.idea, "mode": request.mode, "target": request.target,
                        "autonomy": request.autonomy,
                        "parent_campaign_id": request.parent_campaign_id,
                        "source_checkpoint": request.source_checkpoint,
                    },
                )
                return CampaignOutcome(
                    "NOT_READY", "Campaign planning is waiting for human input", campaign_id,
                    {"request_id": error.pending.request_id, "artifact": str(error.pending.artifact_path)},
                )
            return CampaignOutcome("INFRA_FAILURE", f"Planner failed: {error}", campaign_id)
        return self.propose_structured(request, proposal, campaign_id=campaign_id)

    def propose_structured(
        self,
        request: CampaignRequest,
        proposal: Mapping[str, Any],
        *,
        campaign_id: str | None = None,
    ) -> CampaignOutcome:
        """Validate and persist a Commander-supplied Campaign proposal.

        This is the planner-free Core entry point used by the Codex-native MCP
        surface.  It deliberately accepts data, not a Planner implementation, so
        invoking it cannot start App Server or a Codex subprocess.
        """

        if not request.idea.strip():
            return CampaignOutcome("INVALID", "Campaign idea must be non-empty")
        if request.mode not in {"CHANGE", "STAGED", "CRITICAL"}:
            return CampaignOutcome("INVALID", "mode must be CHANGE, STAGED, or CRITICAL")
        try:
            phases = self._phases(request.mode, request.target)
        except ValueError as error:
            return CampaignOutcome("INVALID", str(error))
        campaign_id = campaign_id or self._next_campaign_id()
        proposal = dict(proposal)
        schema = json.loads(_read_text("schemas/campaign-proposal.schema.json"))
        proposal_errors = sorted(error.message for error in Draft202012Validator(schema).iter_errors(proposal))
        if proposal_errors:
            return CampaignOutcome("INVALID", "invalid Campaign proposal", campaign_id, {"errors": proposal_errors})
        requirements = {
            "$schema": "https://autodev.local/schemas/requirements.schema.json",
            "schema_version": 1,
            "campaign_id": campaign_id,
            "requirements": proposal["requirements"],
        }
        authority = {**default_authority_envelope(), **proposal.get("authority_envelope", {})}
        if proposal["phase"] != phases[0]:
            return CampaignOutcome("INVALID", "proposal has the wrong initial phase", campaign_id)
        proposed_at = _now()
        frozen = {
            "idea": request.idea.strip(), "mode": request.mode, "target": request.target,
            "autonomy": request.autonomy, "requirements": requirements,
            "authority_envelope": authority, "phases": phases,
            "parent_campaign_id": request.parent_campaign_id,
            "source_checkpoint": request.source_checkpoint,
        }
        proposal_hash = _hash(frozen)
        contract = {
            "$schema": "https://autodev.local/schemas/campaign.schema.json",
            "schema_version": 1, "id": campaign_id, "idea": request.idea.strip(),
            "mode": request.mode, "target": request.target, "autonomy": request.autonomy,
            "requirements_hash": _hash(requirements), "authority_envelope": authority,
            "phases": phases, "proposal_hash": proposal_hash,
            "parent_campaign_id": request.parent_campaign_id,
            "source_checkpoint": request.source_checkpoint,
            "proposed_at": proposed_at, "approved_at": None,
        }
        result = self.control.execute(Command("campaign.propose", {
            "contract": contract, "requirements": requirements,
        }))
        if result.status != "SUCCESS":
            return CampaignOutcome(result.status, result.message, campaign_id, result.data)
        _write_json_atomic(
            self.canonical / "campaigns" / campaign_id / "phase-proposal.json",
            {"phase": proposal["phase"], "tasks": proposal["tasks"], "questions": proposal["questions"]},
        )
        return CampaignOutcome(
            "SUCCESS", f"Campaign {campaign_id} proposed", campaign_id,
            {"proposal_hash": proposal_hash, "questions": proposal["questions"], "proposal": contract},
        )

    def approve(self, campaign_id: str, proposal_hash: str) -> CampaignOutcome:
        directory = self.canonical / "campaigns" / campaign_id
        try:
            proposal = json.loads((directory / "phase-proposal.json").read_text(encoding="utf-8"))
            contract = json.loads((directory / "campaign.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            return CampaignOutcome("INVALID", f"cannot read Campaign proposal: {error}", campaign_id)
        if contract.get("proposal_hash") != proposal_hash:
            return CampaignOutcome("INVALID", "proposal hash mismatch", campaign_id)
        workspace = CampaignWorkspace(self.root, campaign_id)
        try:
            checkpoint = workspace.initialize(contract.get("source_checkpoint") or "HEAD")
        except CampaignWorkspaceError as error:
            return CampaignOutcome("BLOCKED", f"cannot initialize Campaign baseline: {error}", campaign_id)
        approved = self.control.execute(Command("campaign.approve", {
            "id": campaign_id, "proposal_hash": proposal_hash, "checkpoint": checkpoint,
        }))
        if approved.status != "SUCCESS":
            return CampaignOutcome(approved.status, approved.message, campaign_id, approved.data)
        if proposal.get("questions"):
            return self._request_planner_questions(campaign_id, proposal["questions"])
        admitted = self.admit(campaign_id, proposal.get("tasks", []))
        return admitted if admitted.status != "SUCCESS" else CampaignOutcome(
            "SUCCESS", f"Campaign {campaign_id} approved and first phase admitted",
            campaign_id, {"checkpoint": checkpoint, **dict(admitted.data)},
        )

    def _request_planner_questions(
        self, campaign_id: str, questions: Sequence[Mapping[str, Any]],
        *, request_id: str | None = None,
    ) -> CampaignOutcome:
        from autodev.human import (
            HumanOption, HumanQuestion, HumanRequest, HumanResponse,
            PersistentHumanInteraction,
        )

        try:
            request = HumanRequest(
                campaign_id,
                tuple(HumanQuestion(
                    str(item["id"]), str(item["header"]), str(item["question"]),
                    tuple(HumanOption(str(option["label"]), str(option["description"]))
                          for option in item.get("options", [])),
                    bool(item.get("isOther", True)), bool(item.get("isSecret", False)),
                ) for item in questions),
                **({"request_id": request_id} if request_id is not None else {}),
            )
        except (KeyError, TypeError, ValueError) as error:
            return CampaignOutcome("INVALID", f"invalid Planner questions: {error}", campaign_id)
        persistent = PersistentHumanInteraction(self.root)
        pending = persistent.request(request)
        response = self.interaction.request(request) if self.interaction is not None else pending
        if self._state().get("campaigns", {}).get(campaign_id, {}).get("status") == "WAITING_FOR_HUMAN":
            waiting = CampaignOutcome(
                "NOT_READY", f"Campaign {campaign_id} is already waiting for Planner answers",
                campaign_id,
            )
        else:
            transitioned = self.control.execute(Command("campaign.transition", {
                "id": campaign_id, "status": "WAITING_FOR_HUMAN",
                "next_action": f"Answer the Planner questions for {campaign_id}.",
            }))
            if transitioned.status != "SUCCESS":
                return CampaignOutcome(
                    transitioned.status, transitioned.message, campaign_id, transitioned.data,
                )
            waiting = CampaignOutcome(
                transitioned.status, transitioned.message, campaign_id, transitioned.data,
            )
        if isinstance(response, HumanResponse):
            persistent.answer(campaign_id, request.request_id, response.answers)
            return self.answer(campaign_id, request.request_id, response.answers)
        return CampaignOutcome("NOT_READY", waiting.message, campaign_id, {
            "questions": list(questions), "request_id": request.request_id,
            "artifact": str(pending.artifact_path),
        })

    def _normalize_tasks(
        self, campaign_id: str, phase: str, proposals: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        state = self._state()
        occupied = {
            int(task_id[5:]) for task_id in state["tasks"] if task_id[5:].isdigit()
        }
        next_number = 1
        contracts: list[dict[str, Any]] = []
        for raw in proposals:
            while next_number in occupied:
                next_number += 1
            task_id = raw.get("id") or f"TASK-{next_number:03d}"
            occupied.add(int(task_id[5:]) if str(task_id)[5:].isdigit() else next_number)
            next_number += 1
            change_classes = list(raw.get("change_classes", ["implementation"]))
            risk = str(raw.get("risk", "MEDIUM"))
            quality_mode = raw.get("quality_mode") or {
                "INTEGRATE": "INTEGRATION", "HARDEN": "HARDENING",
            }.get(phase, "BUILD")
            partial = {
                "risk": risk, "quality_mode": quality_mode, "change_classes": change_classes,
            }
            review_scope = self.attempts.decide_quality(partial).value
            if review_scope == "DIAGNOSTIC":
                review_scope = "NONE"
            prohibited_actions = list(raw.get("prohibited_actions", []))
            prohibited_keys = {
                str(action).strip().lower() for action in prohibited_actions
            }
            for permanent in ("commit", "push", "publish", "deploy", "remote side effects"):
                if permanent not in prohibited_keys:
                    prohibited_actions.append(permanent)
                    prohibited_keys.add(permanent)
            contracts.append({
                "$schema": "https://autodev.local/schemas/task-contract.schema.json",
                "schema_version": 1, "id": task_id,
                "title": raw.get("title", task_id), "objective": raw.get("objective", raw.get("title", task_id)),
                "requirements": list(raw.get("requirements", [])),
                "dependencies": list(raw.get("dependencies", [])),
                "priority": raw.get("priority", "MUST"), "blocking": raw.get("blocking", True),
                "risk": risk, "quality_mode": quality_mode, "change_classes": change_classes,
                "allowed_paths": list(raw.get("allowed_paths", [])),
                "out_of_scope": list(raw.get("out_of_scope", [])),
                "acceptance_criteria": list(raw.get("acceptance_criteria", [])),
                "validation_commands": list(raw.get("validation_commands", [])),
                "prohibited_actions": prohibited_actions,
                "created_at": _now(), "campaign_id": campaign_id, "phase": phase,
                "admission": "AUTO_ADMITTED", "review_scope": review_scope,
            })
        return contracts

    def admit(self, campaign_id: str, proposals: Sequence[Mapping[str, Any]]) -> CampaignOutcome:
        state = self._state()
        record = state.get("campaigns", {}).get(campaign_id)
        if record is None:
            return CampaignOutcome("INVALID", f"unknown Campaign: {campaign_id}", campaign_id)
        if not proposals:
            return CampaignOutcome("NOT_READY", "Phase Planner proposed no Tasks", campaign_id)
        contracts = self._normalize_tasks(campaign_id, record["phase"], proposals)
        result = self.control.execute(Command("task.admit_batch", {
            "campaign_id": campaign_id, "contracts": contracts,
        }))
        if result.status != "SUCCESS":
            if result.status == "BLOCKED":
                from autodev.human import (
                    HumanOption, HumanQuestion, HumanRequest, PersistentHumanInteraction,
                )

                request = HumanRequest(
                    campaign_id,
                    (HumanQuestion(
                        "decision", "Admission",
                        "The Task batch is outside automatic authority. Choose the smallest next step.",
                        (
                            HumanOption("Approve exception", "Admit this frozen batch with recorded human authority."),
                            HumanOption("Revise batch", "Ask a fresh Planner for an in-envelope batch."),
                        ),
                        allow_other=False,
                    ),),
                )
                pending = PersistentHumanInteraction(self.root).request(request)
                _write_json_atomic(
                    self.canonical / "campaigns" / campaign_id / "admission-context.json",
                    {
                        "request_id": request.request_id, "contracts": contracts,
                        "errors": list(result.data.get("errors", [])),
                    },
                )
                self.control.execute(Command("campaign.transition", {
                    "id": campaign_id, "status": "WAITING_FOR_HUMAN",
                    "next_action": "Approve the smallest Authority Envelope exception or revise the Task batch.",
                }))
                return CampaignOutcome(result.status, result.message, campaign_id, {
                    **dict(result.data), "request_id": request.request_id,
                    "artifact": str(pending.artifact_path),
                })
            return CampaignOutcome(result.status, result.message, campaign_id, result.data)
        return CampaignOutcome("SUCCESS", "Task proposal batch admitted", campaign_id, result.data)

    def status(self, campaign_id: str) -> CampaignOutcome:
        record = self._state().get("campaigns", {}).get(campaign_id)
        if record is None:
            return CampaignOutcome("INVALID", f"unknown Campaign: {campaign_id}", campaign_id)
        return CampaignOutcome("SUCCESS", f"Campaign {campaign_id} is {record['status']}", campaign_id, record)

    def answer(
        self, campaign_id: str, request_id: str, answers: Mapping[str, Sequence[str]],
    ) -> CampaignOutcome:
        from autodev.human import PersistentHumanInteraction

        persistent = PersistentHumanInteraction(self.root)
        try:
            persistent.validate_answer(campaign_id, request_id, answers)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            return CampaignOutcome("INVALID", f"cannot validate answer: {error}", campaign_id)
        state = self._state()
        pending_id = state.get("current_action_id")
        if pending_id is not None:
            try:
                pending_action = json.loads(
                    (self.canonical / "actions" / str(pending_id) / "action.json").read_text(
                        encoding="utf-8",
                    )
                )
            except (OSError, json.JSONDecodeError) as error:
                return CampaignOutcome("INVALID", f"pending Action cannot be read: {error}", campaign_id)
            if (
                pending_action.get("type") != "ASK_HUMAN"
                or pending_action.get("campaign_id") != campaign_id
                or pending_action.get("context", {}).get("request_id") != request_id
            ):
                return CampaignOutcome(
                    "INVALID", "answer does not match the pending ASK_HUMAN Action", campaign_id,
                )
        try:
            response = persistent.answer(campaign_id, request_id, answers)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            return CampaignOutcome("INVALID", f"cannot record answer: {error}", campaign_id)
        reconciled = self._reconcile_terminal(campaign_id, "ASK_HUMAN") if pending_id else None
        if reconciled is not None:
            return reconciled
        state = self._state()
        record = state.get("campaigns", {}).get(campaign_id)
        if record is None:
            context_path = self.canonical / "campaigns" / campaign_id / "planning-context.json"
            try:
                context = json.loads(context_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                return CampaignOutcome("INVALID", f"planning context is unavailable: {error}", campaign_id)
            if self.planner is None:
                return CampaignOutcome("NOT_READY", "answer recorded; a fresh Planner is required", campaign_id)
            original_planner = self.planner
            refined = dict(original_planner.plan(PlannerRequest(
                campaign_id, context["idea"], context["mode"], context["target"],
                self._phases(context["mode"], context["target"])[0], self.root, (),
                {key: values[0] for key, values in response.answers.items()},
            )))
            self.planner = FakePlanner([refined])
            try:
                return self.plan(CampaignRequest(**context))
            finally:
                self.planner = original_planner
        if record["status"] != "WAITING_FOR_HUMAN":
            blocker_path = self.canonical / "campaigns" / campaign_id / "blocker-context.json"
            if blocker_path.is_file():
                blocker = json.loads(blocker_path.read_text(encoding="utf-8"))
                if blocker.get("request_id") != request_id:
                    return CampaignOutcome("INVALID", "answer does not match the blocker", campaign_id)
                selected = response.answers["resolution"][0].lower()
                if selected == "retry campaign":
                    transitioned = self.control.execute(Command("project.transition", {"to": "ACTIVE"}))
                elif selected == "cancel campaign":
                    transitioned = self.control.execute(Command("campaign.transition", {
                        "id": campaign_id, "status": "CANCELLED",
                    }))
                else:
                    return CampaignOutcome("INVALID", "unknown blocker resolution", campaign_id)
                if transitioned.status != "SUCCESS":
                    return CampaignOutcome(
                        transitioned.status, transitioned.message, campaign_id, transitioned.data,
                    )
                blocker_path.unlink()
                return CampaignOutcome(
                    "SUCCESS", f"Campaign blocker resolved: {response.answers['resolution'][0]}",
                    campaign_id, transitioned.data,
                )
            return CampaignOutcome("NOT_READY", "Campaign is not waiting for an answer", campaign_id)
        native_plan_path = self.canonical / "campaigns" / campaign_id / "native-plan-context.json"
        if native_plan_path.is_file():
            try:
                native_plan = json.loads(native_plan_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                return CampaignOutcome(
                    "INVALID", f"native planning context is invalid: {error}", campaign_id,
                )
            if native_plan.get("request_id") == request_id:
                activated = self.control.execute(Command("campaign.transition", {
                    "id": campaign_id, "status": "ACTIVE",
                }))
                if activated.status != "SUCCESS":
                    return CampaignOutcome(
                        activated.status, activated.message, campaign_id, activated.data,
                    )
                native_plan.update(
                    status="ANSWERED",
                    answers={key: list(values) for key, values in response.answers.items()},
                    answered_at=_now(),
                )
                _write_json_atomic(native_plan_path, native_plan)
                return CampaignOutcome(
                    "SUCCESS", "Planner answers recorded; a fresh PLAN_PHASE is ready",
                    campaign_id, {"request_id": request_id},
                )
        already_activated = False
        admission_path = self.canonical / "campaigns" / campaign_id / "admission-context.json"
        if admission_path.is_file():
            context = json.loads(admission_path.read_text(encoding="utf-8"))
            if context.get("request_id") != request_id:
                return CampaignOutcome("INVALID", "answer does not match the pending admission", campaign_id)
            selected = next(iter(response.answers.values()))[0].lower()
            if selected == "approve exception":
                activated = self.control.execute(Command("campaign.transition", {
                    "id": campaign_id, "status": "ACTIVE",
                }))
                if activated.status != "SUCCESS":
                    return CampaignOutcome(
                        activated.status, activated.message, campaign_id, activated.data,
                    )
                contracts = [dict(item, admission="HUMAN_APPROVED") for item in context["contracts"]]
                admitted = self.control.execute(Command("task.admit_batch", {
                    "campaign_id": campaign_id, "contracts": contracts,
                    "human_approval_request_id": request_id,
                }))
                if admitted.status != "SUCCESS":
                    return CampaignOutcome(admitted.status, admitted.message, campaign_id, admitted.data)
                admission_path.unlink()
                return CampaignOutcome(
                    "SUCCESS", "Human-approved Task proposal batch admitted", campaign_id,
                    admitted.data,
                )
            # "Revise batch" keeps the Campaign and admission context waiting
            # until the fresh Planner has returned a valid replacement batch.
        gate_path = self.canonical / "campaigns" / campaign_id / "gate-context.json"
        if gate_path.is_file():
            gate = json.loads(gate_path.read_text(encoding="utf-8"))
            selected = next(iter(response.answers.values()))[0].lower()
            if selected != "approve":
                return CampaignOutcome("BLOCKED", "CRITICAL gate was not approved", campaign_id)
            activated_gate = self.control.execute(Command("campaign.transition", {
                "id": campaign_id, "status": "ACTIVE",
            }))
            if activated_gate.status != "SUCCESS":
                return CampaignOutcome(activated_gate.status, activated_gate.message, campaign_id, activated_gate.data)
            gate_path.unlink()
            if gate["kind"] == "final-writeback":
                reached = self.control.execute(Command("campaign.transition", {
                    "id": campaign_id, "status": "TARGET_REACHED", "phase": "TARGET_REACHED",
                }))
                if reached.status != "SUCCESS":
                    return CampaignOutcome(reached.status, reached.message, campaign_id, reached.data)
                return self.materialize(campaign_id)
            advanced = self.control.execute(Command("campaign.transition", {
                "id": campaign_id, "phase": gate["next_phase"],
            }))
            if advanced.status != "SUCCESS":
                return CampaignOutcome(advanced.status, advanced.message, campaign_id, advanced.data)
            record = self._state()["campaigns"][campaign_id]
        if self.planner is None:
            return CampaignOutcome("NOT_READY", "answer recorded; a fresh Planner is required", campaign_id)
        contract = json.loads(
            (self.canonical / "campaigns" / campaign_id / "campaign.json").read_text(encoding="utf-8")
        )
        requirements = json.loads(
            (self.canonical / "campaigns" / campaign_id / "requirements.json").read_text(encoding="utf-8")
        )["requirements"]
        proposal = dict(self.planner.plan(PlannerRequest(
            campaign_id, contract["idea"], contract["mode"], contract["target"],
            record["phase"], self.root, tuple(requirements),
            {key: values[0] for key, values in response.answers.items()},
        )))
        proposal_schema = json.loads(_read_text("schemas/campaign-proposal.schema.json"))
        proposal_errors = sorted(
            error.message
            for error in Draft202012Validator(proposal_schema).iter_errors(proposal)
        )
        if proposal_errors:
            return CampaignOutcome(
                "INVALID", "Planner returned an invalid phase proposal", campaign_id,
                {"errors": proposal_errors},
            )
        if proposal.get("requirements") and proposal["requirements"] != requirements:
            return CampaignOutcome("BLOCKED", "Planner attempted to change the approved Requirement Baseline", campaign_id)
        if self._authority_changed(proposal, contract):
            return CampaignOutcome(
                "BLOCKED", "Planner attempted to change the approved Authority Envelope", campaign_id,
            )
        if not already_activated:
            activated = self.control.execute(Command("campaign.transition", {
                "id": campaign_id, "status": "ACTIVE",
            }))
            if activated.status != "SUCCESS":
                return CampaignOutcome(activated.status, activated.message, campaign_id, activated.data)
        if admission_path.is_file():
            admission_path.unlink()
        if proposal.get("questions"):
            _write_json_atomic(
                self.canonical / "campaigns" / campaign_id / "phase-proposal.json",
                {"phase": record["phase"], "tasks": proposal.get("tasks", []),
                 "questions": proposal["questions"]},
            )
            return self._request_planner_questions(campaign_id, proposal["questions"])
        _write_json_atomic(
            self.canonical / "campaigns" / campaign_id / "phase-proposal.json",
            {"phase": record["phase"], "tasks": proposal.get("tasks", []), "questions": []},
        )
        return self.admit(campaign_id, proposal.get("tasks", []))

    def _critical_gate(self, campaign_id: str, *, kind: str, next_phase: str | None = None) -> str:
        from autodev.human import HumanOption, HumanQuestion, HumanRequest, PersistentHumanInteraction

        request_id = f"{campaign_id}-{kind}"
        request = HumanRequest(
            campaign_id,
            (HumanQuestion(
                "decision", "Crit Gate", f"Approve the CRITICAL {kind} gate?",
                (
                    HumanOption("Approve", "Continue within the approved Campaign contract."),
                    HumanOption("Cancel", "Stop before the gated transition."),
                ),
                allow_other=False,
            ),),
            request_id=request_id,
        )
        PersistentHumanInteraction(self.root).request(request)
        _write_json_atomic(
            self.canonical / "campaigns" / campaign_id / "gate-context.json",
            {"kind": kind, "next_phase": next_phase, "request_id": request_id},
        )
        return request_id

    def retarget(self, campaign_id: str, target: str) -> CampaignOutcome:
        reconciled = self._reconcile_terminal(campaign_id, "TARGET_REACHED")
        if reconciled is not None:
            return reconciled
        result = self.control.execute(Command("campaign.retarget", {"id": campaign_id, "target": target}))
        return CampaignOutcome(result.status, result.message, campaign_id, result.data)

    def phase_gate(
        self, campaign_id: str, *, reviewer_engine: ExecutionEngine | None = None,
        diagnostic_engine: ExecutionEngine | None = None,
    ) -> CampaignOutcome:
        """Validate and review the cumulative phase checkpoint, then advance or finish."""

        state = self._state()
        record = state.get("campaigns", {}).get(campaign_id)
        if record is None or record["status"] != "ACTIVE":
            return CampaignOutcome("NOT_READY", "Campaign is not active", campaign_id)
        phase = record["phase"]
        phase_tasks = [
            (task_id, task_record)
            for task_id, task_record in state["tasks"].items()
            if self._task_contract(task_id).get("campaign_id") == campaign_id
            and self._task_contract(task_id).get("phase") == phase
        ]
        incomplete = [task_id for task_id, item in phase_tasks if item["status"] != "ACCEPTED"]
        if not phase_tasks or incomplete:
            return CampaignOutcome(
                "NOT_READY", "Phase gate requires all phase Tasks to be accepted", campaign_id,
                {"incomplete_tasks": incomplete},
            )
        summary_path = self.canonical / "campaigns" / campaign_id / f"phase-summary-{phase}.json"
        if summary_path.is_file():
            try:
                accepted_summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                accepted_summary = {}
            if (
                accepted_summary.get("status") == "PASSED"
                and accepted_summary.get("checkpoint") == record["checkpoint"]
            ):
                return self._advance_phase_gate(campaign_id, record, phase, accepted_summary)
        workspace = CampaignWorkspace(self.root, campaign_id)
        gate_id = f"PHASE-{phase}-{uuid.uuid4().hex[:8]}"
        try:
            worktree = workspace.create_task_workspace(gate_id)
        except CampaignWorkspaceError as error:
            return CampaignOutcome("INFRA_FAILURE", f"cannot create Phase Gate workspace: {error}", campaign_id)
        validations: list[dict[str, Any]] = []
        try:
            commands: list[dict[str, Any]] = []
            seen: set[str] = set()
            for task_id, _ in phase_tasks:
                for validation in self._task_contract(task_id)["validation_commands"]:
                    key = json.dumps(validation, sort_keys=True)
                    if key not in seen:
                        commands.append(validation)
                        seen.add(key)
            validations = self.attempts.run_validations(
                {"validation_commands": commands}, worktree,
                self.canonical / "runs" / gate_id,
            )
            failed = [item for item in validations if item["returncode"] != 0]
            if failed:
                summary = {
                    "campaign_id": campaign_id, "phase": phase, "status": "FAILED",
                    "checkpoint": record["checkpoint"], "validations": validations,
                    "route": "DIAGNOSTIC",
                }
                _write_json_atomic(
                    self.canonical / "campaigns" / campaign_id / f"phase-summary-{phase}.json",
                    summary,
                )
                diagnosis: Mapping[str, Any] | None = None
                if diagnostic_engine is not None:
                    artifact_dir = self.canonical / "runs" / gate_id / "diagnostic-01"
                    artifact_dir.mkdir(parents=True, exist_ok=True)
                    schema_path = artifact_dir / "output-schema.json"
                    schema_path.write_text(_read_text("schemas/attempt-proposal.schema.json"), encoding="utf-8")
                    diagnosed = diagnostic_engine.execute(AttemptRequest(
                        gate_id, phase_tasks[0][0], "diagnostic", worktree,
                        "Diagnose this cumulative Phase Gate failure read-only; do not accept work.\n"
                        + json.dumps({"phase": phase, "failed_validations": failed}),
                        schema_path, artifact_dir, ":read-only", "codex-sandbox", 600, 2400,
                        self.canonical / "STOP",
                    ))
                    if diagnosed.status == "SUCCESS":
                        diagnosis = diagnosed.proposal
                        summary["diagnosis"] = diagnosis
                        _write_json_atomic(
                            self.canonical / "campaigns" / campaign_id / f"phase-summary-{phase}.json",
                            summary,
                        )
                if self.planner is not None:
                    contract = json.loads(
                        (self.canonical / "campaigns" / campaign_id / "campaign.json").read_text(encoding="utf-8")
                    )
                    requirements = json.loads(
                        (self.canonical / "campaigns" / campaign_id / "requirements.json").read_text(encoding="utf-8")
                    )["requirements"]
                    repair = dict(self.planner.plan(PlannerRequest(
                        campaign_id, contract["idea"], contract["mode"], contract["target"],
                        phase, self.root, tuple(requirements),
                        {"phase_gate_failure": json.dumps(diagnosis or failed, ensure_ascii=False)},
                    )))
                    proposal_schema = json.loads(_read_text("schemas/campaign-proposal.schema.json"))
                    repair_errors = sorted(
                        error.message
                        for error in Draft202012Validator(proposal_schema).iter_errors(repair)
                    )
                    if repair_errors:
                        return CampaignOutcome(
                            "INVALID", "Repair Planner returned an invalid proposal", campaign_id,
                            {"errors": repair_errors},
                        )
                    if repair.get("requirements") != requirements:
                        return CampaignOutcome(
                            "BLOCKED", "Repair Planner attempted to change the Requirement Baseline",
                            campaign_id,
                        )
                    if self._authority_changed(repair, contract):
                        return CampaignOutcome(
                            "BLOCKED", "Repair Planner attempted to change the Authority Envelope",
                            campaign_id,
                        )
                    if repair.get("questions"):
                        _write_json_atomic(
                            self.canonical / "campaigns" / campaign_id / "phase-proposal.json",
                            {"phase": phase, "tasks": repair.get("tasks", []),
                             "questions": repair["questions"]},
                        )
                        return self._request_planner_questions(campaign_id, repair["questions"])
                    if repair.get("phase") == phase and repair.get("tasks"):
                        return self.admit(campaign_id, repair["tasks"])
                return CampaignOutcome(
                    "NOT_READY", "Phase Gate failed; diagnostic repair planning is required",
                    campaign_id, summary,
                )
            needs_phase_review = any(
                self._task_contract(task_id).get("review_scope") == "PHASE"
                for task_id, _ in phase_tasks
            )
            review: Mapping[str, Any] | None = None
            if needs_phase_review:
                if reviewer_engine is None:
                    return CampaignOutcome("NOT_READY", "Phase Review engine is required", campaign_id)
                review_attempts = 0
                if summary_path.is_file():
                    try:
                        review_attempts = int(json.loads(
                            summary_path.read_text(encoding="utf-8")
                        ).get("review_attempts", 0))
                    except (OSError, json.JSONDecodeError, TypeError, ValueError):
                        review_attempts = 0
                if review_attempts >= 2:
                    return CampaignOutcome(
                        "BLOCKED", "Phase Review and rereview budget exhausted", campaign_id,
                    )
                baseline = json.loads(
                    (self.canonical / "campaigns" / campaign_id / "workspace-baseline.json").read_text()
                )["initial_commit"]
                previous_summaries = sorted(
                    (self.canonical / "campaigns" / campaign_id).glob("phase-summary-*.json")
                )
                for path in previous_summaries:
                    prior = json.loads(path.read_text(encoding="utf-8"))
                    if prior.get("status") == "PASSED" and prior.get("checkpoint"):
                        baseline = prior["checkpoint"]
                diff = subprocess.run(
                    ["git", "diff", "--binary", "--full-index", baseline, record["checkpoint"], "--"],
                    cwd=self.root, capture_output=True, text=True, check=False,
                ).stdout
                artifact_dir = self.canonical / "runs" / gate_id / "phase-review-01"
                artifact_dir.mkdir(parents=True, exist_ok=True)
                schema_path = artifact_dir / "output-schema.json"
                schema_path.write_text(_read_text("schemas/attempt-proposal.schema.json"), encoding="utf-8")
                request = AttemptRequest(
                    gate_id, phase_tasks[0][0], "reviewer", worktree,
                    "Review the cumulative Phase diff only. Do not report style-only findings. "
                    "Limit blocking and debt findings to five each.\n"
                    + json.dumps({"phase": phase, "diff": diff, "validations": validations}),
                    schema_path, artifact_dir, ":read-only", "codex-sandbox", 600, 2400,
                    self.canonical / "STOP",
                )
                reviewed = reviewer_engine.execute(request)
                if reviewed.status != "SUCCESS" or not reviewed.proposal:
                    return CampaignOutcome("INFRA_FAILURE", "Phase Reviewer failed", campaign_id)
                review = reviewed.proposal
                blocking = list(review.get("findings", []))
                budget_errors = self.attempts.review_budget_errors(review)
                if budget_errors:
                    return CampaignOutcome(
                        "BLOCKED", "; ".join(budget_errors), campaign_id,
                    )
                if review.get("outcome") in {"REWORK", "BLOCKED"}:
                    _write_json_atomic(summary_path, {
                        "campaign_id": campaign_id, "phase": phase,
                        "status": "REVIEW_REWORK", "checkpoint": record["checkpoint"],
                        "validations": validations, "review": review,
                        "review_attempts": review_attempts + 1,
                    })
                    return CampaignOutcome(
                        "NOT_READY" if review["outcome"] == "REWORK" else "BLOCKED",
                        "Phase Review did not pass", campaign_id, {"findings": blocking},
                    )
            summary = {
                "campaign_id": campaign_id, "phase": phase, "status": "PASSED",
                "checkpoint": record["checkpoint"], "validations": validations,
                "review": review,
                "review_attempts": review_attempts + 1 if needs_phase_review else 0,
            }
            _write_json_atomic(
                self.canonical / "campaigns" / campaign_id / f"phase-summary-{phase}.json",
                summary,
            )
        finally:
            workspace.remove_task_workspace(worktree)

        return self._advance_phase_gate(campaign_id, record, phase, summary)

    def _advance_phase_gate(
        self, campaign_id: str, record: Mapping[str, Any], phase: str,
        summary: Mapping[str, Any],
    ) -> CampaignOutcome:
        """Apply strategy-specific human gates after deterministic Phase acceptance."""

        if phase == TARGET_PHASE[record["target"]]:
            if record["mode"] == "CRITICAL":
                request_id = self._critical_gate(campaign_id, kind="final-writeback")
                result = self.control.execute(Command("campaign.transition", {
                    "id": campaign_id, "status": "WAITING_FOR_HUMAN",
                    "next_action": "Approve the CRITICAL final write-back gate.",
                }))
                return CampaignOutcome("NOT_READY", result.message, campaign_id, {
                    **summary, "request_id": request_id,
                })
            reached = self.control.execute(Command("campaign.transition", {
                "id": campaign_id, "status": "TARGET_REACHED", "phase": "TARGET_REACHED",
            }))
            if reached.status != "SUCCESS":
                return CampaignOutcome(reached.status, reached.message, campaign_id, reached.data)
            return self.materialize(campaign_id)

        contract = json.loads(
            (self.canonical / "campaigns" / campaign_id / "campaign.json").read_text(encoding="utf-8")
        )
        next_phase = contract["phases"][contract["phases"].index(phase) + 1]
        if record["mode"] == "CRITICAL" and phase in {"SCAFFOLD", "INTEGRATE"}:
            request_id = self._critical_gate(
                campaign_id, kind=f"{phase.lower()}-gate", next_phase=next_phase,
            )
            result = self.control.execute(Command("campaign.transition", {
                "id": campaign_id, "status": "WAITING_FOR_HUMAN",
                "next_action": f"Approve the CRITICAL {phase} Phase Gate.",
            }))
            return CampaignOutcome("NOT_READY", result.message, campaign_id, {
                **summary, "request_id": request_id,
            })
        advanced = self.control.execute(Command("campaign.transition", {"id": campaign_id, "phase": next_phase}))
        if advanced.status != "SUCCESS":
            return CampaignOutcome(advanced.status, advanced.message, campaign_id, advanced.data)
        if self.planner is None:
            return CampaignOutcome("NOT_READY", f"Campaign advanced to {next_phase}; Planner required", campaign_id)
        requirements = json.loads(
            (self.canonical / "campaigns" / campaign_id / "requirements.json").read_text(encoding="utf-8")
        )["requirements"]
        phase_proposal = dict(self.planner.plan(PlannerRequest(
            campaign_id, contract["idea"], contract["mode"], contract["target"], next_phase,
            self.root, tuple(requirements), {},
        )))
        proposal_schema = json.loads(_read_text("schemas/campaign-proposal.schema.json"))
        proposal_errors = sorted(
            error.message
            for error in Draft202012Validator(proposal_schema).iter_errors(phase_proposal)
        )
        if proposal_errors:
            return CampaignOutcome(
                "INVALID", "Phase Planner returned an invalid proposal", campaign_id,
                {"errors": proposal_errors},
            )
        if phase_proposal.get("phase") != next_phase:
            return CampaignOutcome("INVALID", "Phase Planner proposed the wrong phase", campaign_id)
        if phase_proposal.get("requirements") != requirements:
            return CampaignOutcome(
                "BLOCKED", "Phase Planner attempted to change the approved Requirement Baseline",
                campaign_id,
            )
        if self._authority_changed(phase_proposal, contract):
            return CampaignOutcome(
                "BLOCKED", "Phase Planner attempted to change the approved Authority Envelope",
                campaign_id,
            )
        _write_json_atomic(
            self.canonical / "campaigns" / campaign_id / "phase-proposal.json",
            {"phase": next_phase, "tasks": phase_proposal.get("tasks", []),
             "questions": phase_proposal.get("questions", [])},
        )
        if phase_proposal.get("questions"):
            return self._request_planner_questions(campaign_id, phase_proposal["questions"])
        return self.admit(campaign_id, phase_proposal.get("tasks", []))

    def _task_contract(self, task_id: str) -> dict[str, Any]:
        return json.loads(
            (self.canonical / "tasks" / task_id / "contract.json").read_text(encoding="utf-8")
        )

    def materialize(self, campaign_id: str) -> CampaignOutcome:
        state = self._state()
        record = state.get("campaigns", {}).get(campaign_id)
        if record is None:
            return CampaignOutcome("INVALID", f"unknown Campaign: {campaign_id}", campaign_id)
        if record["status"] != "TARGET_REACHED":
            return CampaignOutcome("NOT_READY", "only a reached Campaign can be materialized", campaign_id)
        if record["checkpoint"] == record["last_materialized_checkpoint"]:
            return CampaignOutcome(
                "SUCCESS", "Campaign checkpoint is already materialized", campaign_id,
                {"campaign_id": campaign_id, "checkpoint": record["checkpoint"], "applied": False},
            )
        pending_id = state.get("current_action_id")
        pending_type: str | None = None
        materialization_context: dict[str, Any] | None = None
        if pending_id is not None:
            try:
                pending = json.loads(
                    (self.canonical / "actions" / str(pending_id) / "action.json").read_text(
                        encoding="utf-8",
                    )
                )
            except (OSError, json.JSONDecodeError) as error:
                return CampaignOutcome(
                    "INFRA_FAILURE", f"cannot read pending Action: {error}", campaign_id,
                )
            pending_type = str(pending.get("type"))
            if pending.get("campaign_id") != campaign_id:
                return CampaignOutcome("NOT_READY", "another Campaign has a pending Action", campaign_id)
            if pending_type == "TARGET_REACHED":
                reconciled = self._reconcile_terminal(campaign_id, "TARGET_REACHED")
                if reconciled is not None:
                    return reconciled
            elif pending_type == "ASK_HUMAN":
                blocker_path = self.canonical / "campaigns" / campaign_id / "blocker-context.json"
                try:
                    materialization_context = json.loads(blocker_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    materialization_context = None
                if not (
                    materialization_context
                    and materialization_context.get("kind") == "materialization-conflict"
                    and materialization_context.get("status") == "PENDING"
                    and materialization_context.get("request_id")
                    == pending.get("context", {}).get("request_id")
                ):
                    return CampaignOutcome(
                        "NOT_READY", "an unrelated ASK_HUMAN Action is still pending", campaign_id,
                    )
            else:
                return CampaignOutcome("NOT_READY", "a non-terminal Action is still pending", campaign_id)
        try:
            materialized = CampaignWorkspace(self.root, campaign_id).materialize(
                from_commit=record["last_materialized_checkpoint"],
            )
        except CampaignWorkspaceError as error:
            self.control.execute(Command("project.transition", {
                "to": "BLOCKED", "blocker": f"Campaign materialization failed: {error}",
                "next_action": f"Resolve source conflicts, then run campaign materialize {campaign_id}.",
            }))
            return CampaignOutcome("BLOCKED", f"materialization blocked: {error}", campaign_id)
        if pending_type == "ASK_HUMAN":
            reconciled = self._reconcile_terminal(campaign_id, "ASK_HUMAN")
            if reconciled is not None:
                return reconciled
        result = self.control.execute(Command("campaign.materialized", {
            "id": campaign_id, "checkpoint": materialized.to_commit,
        }))
        if result.status == "SUCCESS" and materialization_context is not None:
            request_id = str(materialization_context["request_id"])
            request_path = (
                self.canonical / "campaigns" / campaign_id / "human-requests" / f"{request_id}.json"
            )
            request = json.loads(request_path.read_text(encoding="utf-8"))
            if request.get("status") == "PENDING":
                request.update(
                    status="AUTO_RESOLVED", answers={"resolution": ["Retry Campaign"]},
                    answered_at=_now(),
                )
                _write_json_atomic(request_path, request)
            materialization_context.update(status="RESOLVED", resolved_at=_now())
            _write_json_atomic(
                self.canonical / "campaigns" / campaign_id / "blocker-context.json",
                materialization_context,
            )
        return CampaignOutcome(result.status, result.message, campaign_id, {
            **dict(result.data), "patch_sha256": materialized.patch_sha256,
            "applied": materialized.applied,
        })

    def archive(self, campaign_id: str) -> CampaignOutcome:
        reconciled = self._reconcile_terminal(campaign_id, "ASK_HUMAN", "TARGET_REACHED")
        if reconciled is not None:
            return reconciled
        state = self._state()
        record = state.get("campaigns", {}).get(campaign_id)
        if record is None:
            return CampaignOutcome("INVALID", f"unknown Campaign: {campaign_id}", campaign_id)
        if record["status"] not in {"TARGET_REACHED", "ARCHIVED"}:
            return CampaignOutcome("NOT_READY", "only a reached Campaign can be archived", campaign_id)
        children = []
        for path in (self.canonical / "campaigns").glob("CAMP-*/campaign.json"):
            try:
                child = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if child.get("parent_campaign_id") == campaign_id:
                children.append(child["id"])
        results_materialized = record["checkpoint"] == record["last_materialized_checkpoint"]
        if not results_materialized or children:
            reason = "campaign results must be materialized" if not results_materialized else "campaign has child dependencies"
            return CampaignOutcome("BLOCKED", f"archive blocked: {reason}", campaign_id, {"children": children})
        result = None
        if record["status"] == "TARGET_REACHED":
            result = self.control.execute(Command("campaign.transition", {
                "id": campaign_id, "status": "ARCHIVED",
            }))
            if result.status != "SUCCESS":
                return CampaignOutcome(result.status, result.message, campaign_id, result.data)
        try:
            CampaignWorkspace(self.root, campaign_id).archive(
                results_materialized=True, has_child_dependencies=False,
            )
        except CampaignWorkspaceError as error:
            return CampaignOutcome(
                "INFRA_FAILURE", f"Campaign is archived but private-ref cleanup must be retried: {error}",
                campaign_id,
            )
        return CampaignOutcome(
            "SUCCESS", "Campaign archived and private ref removed", campaign_id,
            {} if result is None else result.data,
        )

    def run_until_target_or_blocked(
        self,
        campaign_id: str,
        engine: ExecutionEngine,
        *,
        reviewer_engine: ExecutionEngine | None = None,
        diagnostic_engine: ExecutionEngine | None = None,
        max_iterations: int = 30,
    ) -> CampaignOutcome:
        """Resume with fresh Task/Planner sessions until target or a real stop."""

        from autodev.run_controller import RunController, RunRequest

        state = self._state()
        record = state.get("campaigns", {}).get(campaign_id)
        if record is None:
            return CampaignOutcome("INVALID", f"unknown Campaign: {campaign_id}", campaign_id)
        if record["status"] == "TARGET_REACHED":
            if record["checkpoint"] != record["last_materialized_checkpoint"]:
                return self.materialize(campaign_id)
            return CampaignOutcome("SUCCESS", "Campaign target already reached", campaign_id, record)
        if record["status"] != "ACTIVE":
            return CampaignOutcome("NOT_READY", f"Campaign is {record['status']}", campaign_id, record)
        workspace = CampaignWorkspace(self.root, campaign_id)

        def record_recovered(journal: dict[str, Any]) -> int:
            result = self.control.execute(Command("campaign.checkpoint", {
                "id": campaign_id, "checkpoint": journal["commit"], "task_id": journal["task_id"],
            }))
            if result.status != "SUCCESS":
                raise CampaignWorkspaceError(result.message)
            reconciled = self.attempts.reconcile_accepted_checkpoint(campaign_id, journal)
            return reconciled or result.revision or 0

        try:
            workspace.recover_checkpoints(record_recovered)
            self.attempts.reconcile_accepted_checkpoint(campaign_id)
        except CampaignWorkspaceError as error:
            return CampaignOutcome("BLOCKED", f"checkpoint recovery blocked: {error}", campaign_id)
        for _ in range(max_iterations):
            state = self._state()
            record = state["campaigns"][campaign_id]
            ready = [
                task_id for task_id, task_record in state["tasks"].items()
                if task_record["status"] == "READY"
                and self._task_contract(task_id).get("campaign_id") == campaign_id
            ]
            if ready:
                outcome = RunController(
                    self.root, engine, reviewer_engine=reviewer_engine,
                    diagnostic_engine=diagnostic_engine,
                ).run(RunRequest(task_id=ready[0]))
                if outcome.status != "SUCCESS":
                    return CampaignOutcome(outcome.status, outcome.message, campaign_id, outcome.data)
                continue
            gate = self.phase_gate(
                campaign_id, reviewer_engine=reviewer_engine,
                diagnostic_engine=diagnostic_engine,
            )
            if gate.status != "SUCCESS":
                return gate
            latest = self._state()["campaigns"][campaign_id]
            if latest["status"] == "TARGET_REACHED":
                return CampaignOutcome("SUCCESS", "Campaign target reached", campaign_id, latest)
        return CampaignOutcome("BLOCKED", "Campaign iteration budget exhausted", campaign_id)
