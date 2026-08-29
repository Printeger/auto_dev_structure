"""One-task-at-a-time autonomous run lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker

from autodev._resources import _read_text
from autodev._workspace import (
    ConcurrentSourceChange,
    GitWorkspace,
    LockUnavailable,
    PatchPolicyViolation,
    ProjectLock,
    _write_json_atomic,
    git_baseline_status,
    recover_stale_workspaces,
    source_fingerprint,
)
from autodev.attempt_lifecycle import AttemptLifecycle
from autodev.campaign_workspace import CampaignWorkspace, CampaignWorkspaceError
from autodev.control_plane import Command, ControlPlane
from autodev.engines import AttemptRequest, EngineResult, ExecutionEngine
from autodev.quality import QualityDecision, requires_independent_review


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _hash(value: Any) -> str:
    if isinstance(value, bytes):
        content = value
    else:
        content = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True, slots=True)
class RunRequest:
    task_id: str | None = None
    until: str | None = None
    recover_stale: bool = False


@dataclass(frozen=True, slots=True)
class RunOutcome:
    status: str
    message: str
    task_id: str | None = None
    run_id: str | None = None
    evidence_id: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        return {"SUCCESS": 0, "INVALID": 1, "NOT_READY": 2, "BLOCKED": 3,
                "STOPPED": 4, "INFRA_FAILURE": 5}[self.status]


@dataclass(frozen=True, slots=True)
class RunLimits:
    max_iterations: int = 30
    max_seconds: int = 4 * 60 * 60
    max_work_attempts: int = 4
    max_reworks: int = 2
    max_stagnation: int = 2
    idle_timeout: int = 600
    hard_timeout: int = 2400
    infrastructure_retries: int = 1


class RunController:
    """Hide selection, execution, validation, review, evidence, and checkpointing."""

    def __init__(
        self,
        project_root: Path,
        engine: ExecutionEngine,
        *,
        reviewer_engine: ExecutionEngine | None = None,
        diagnostic_engine: ExecutionEngine | None = None,
        limits: RunLimits | None = None,
    ) -> None:
        self.root = project_root.resolve()
        self.canonical = self.root / ".autodev"
        self.control = ControlPlane(self.root)
        self.engine = engine
        self.reviewer_engine = reviewer_engine or engine
        self.diagnostic_engine = diagnostic_engine or self.reviewer_engine
        self.attempts = AttemptLifecycle(self.root)
        self.quality = self.attempts.quality
        try:
            policy = json.loads((self.canonical / "policy.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            policy = {}
        self.runtime = {
            "mode": "codex-sandbox",
            "build_permission_profile": ":workspace",
            "review_permission_profile": ":read-only",
            **policy.get("runtime", {}),
        }
        if limits is None:
            try:
                configured = policy.get("runner", {})
                limits = RunLimits(**configured)
            except (OSError, json.JSONDecodeError, TypeError):
                limits = RunLimits()
        self.limits = limits

    def _state(self) -> dict[str, Any]:
        return json.loads((self.canonical / "state.json").read_text(encoding="utf-8"))

    def _contract(self, task_id: str) -> dict[str, Any]:
        return json.loads((self.canonical / "tasks" / task_id / "contract.json").read_text(encoding="utf-8"))

    def _history(self, task_id: str) -> list[dict[str, Any]]:
        history: list[dict[str, Any]] = []
        for path in sorted((self.canonical / "runs").glob("*/evidence.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if value.get("task_id") == task_id:
                history.append(value)
        return history

    def _attempt_count(self, task_id: str) -> int:
        count = 0
        for path in (self.canonical / "runs").glob("*/context.json"):
            try:
                if json.loads(path.read_text(encoding="utf-8")).get("task_id") == task_id:
                    count += 1
            except (OSError, json.JSONDecodeError):
                continue
        return count

    def _select(self, requested: str | None) -> str | None:
        state = self._state()
        if requested:
            record = state["tasks"].get(requested)
            return requested if record and record["status"] == "READY" else None
        priority = {"MUST": 0, "SHOULD": 1, "COULD": 2}
        candidates: list[tuple[int, str, str]] = []
        for task_id, record in state["tasks"].items():
            if record["status"] != "READY":
                continue
            contract = self._contract(task_id)
            if any(state["tasks"].get(dep, {}).get("status") != "ACCEPTED" for dep in contract["dependencies"]):
                continue
            candidates.append((priority[contract["priority"]], record["created_at"], task_id))
        return min(candidates)[2] if candidates else None

    def _context(self, task_id: str, run_id: str, contract: dict[str, Any]) -> dict[str, Any]:
        status = self.control.execute(Command("status"))
        validation = self.control.execute(Command("validate"))
        requirements = [
            item for item in validation.data.get("requirements", [])
            if item["id"] in contract["requirements"]
        ]
        return {
            "run_id": run_id,
            "task_id": task_id,
            "contract_hash": self._state()["tasks"][task_id]["contract_hash"],
            "contract": contract,
            "requirements": requirements,
            "project_status": status.data.get("project_status"),
            "generated_at": _now(),
        }

    def _request(
        self, run_id: str, task_id: str, role: str, workspace: Path,
        context: dict[str, Any], artifact_dir: Path,
    ) -> AttemptRequest:
        schema_path = artifact_dir / "output-schema.json"
        schema_path.parent.mkdir(parents=True, exist_ok=True)
        schema_path.write_text(_read_text("schemas/attempt-proposal.schema.json"), encoding="utf-8")
        if role == "reviewer":
            brief = (
                "Review independently from this Task contract, current diff, and validation evidence. "
                "Inspect the named requirement and interface material in the workspace. Do not use "
                "Builder reasoning. Order findings by severity and identify every item of missing "
                "evidence in findings. Do not report style-only findings. Limit blocking findings "
                "and debt findings to five each. Return PASS, PASS_WITH_DEBT, REWORK, or BLOCKED."
            )
        elif role == "diagnostic":
            brief = (
                "Diagnose the repeated semantic failure read-only. Locate the root cause and propose "
                "a focused repair direction. Do not edit files and do not accept the Task. Return REWORK "
                "with the diagnosis in summary/findings, or BLOCKED only for a proven external blocker."
            )
        else:
            brief = (
                "Implement exactly this frozen Task in the isolated workspace. Do not mutate .autodev, "
                "commit, push, publish, deploy, or change protected files. Run relevant checks and "
                "self-review the final diff before proposing PASS; record residual risks in findings. "
                "Return a structured outcome proposal."
            )
        prompt = brief + "\n\nCONTEXT:\n" + json.dumps(context, indent=2, ensure_ascii=False)
        return AttemptRequest(
            run_id, task_id, role, workspace, prompt, schema_path, artifact_dir,
            permission_profile=self.runtime[
                "review_permission_profile" if role in {"reviewer", "diagnostic"} else "build_permission_profile"
            ],
            runtime_mode=self.runtime["mode"],
            idle_timeout=self.limits.idle_timeout, hard_timeout=self.limits.hard_timeout,
            stop_file=self.canonical / "STOP",
        )

    @staticmethod
    def _validate_proposal(result: EngineResult) -> list[str]:
        if result.status != "SUCCESS":
            return [result.infrastructure_error or f"engine status {result.status}"]
        schema = json.loads(_read_text("schemas/attempt-proposal.schema.json"))
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        return [error.message for error in validator.iter_errors(result.proposal)]

    def _execute_with_retry(self, engine: ExecutionEngine, request: AttemptRequest) -> EngineResult:
        result = engine.execute(request)
        retries = 0
        while result.status == "INFRA_FAILURE" and retries < self.limits.infrastructure_retries:
            retries += 1
            retry_request = AttemptRequest(
                request.run_id, request.task_id, request.role, request.workspace, request.prompt,
                request.output_schema, request.artifact_dir / f"infra-retry-{retries}",
                request.permission_profile, request.runtime_mode, request.idle_timeout,
                request.hard_timeout, request.stop_file,
            )
            result = engine.execute(retry_request)
        return result

    def _validations(self, contract: dict[str, Any], workspace: Path, artifact_dir: Path) -> list[dict[str, Any]]:
        return self.attempts.run_validations(contract, workspace, artifact_dir)

    @staticmethod
    def _stagnation_fingerprint(
        task_id: str, phase: str, patch: bytes, validations: list[dict[str, Any]], findings: list[str]
    ) -> str:
        value = {
            "task_id": task_id, "phase": phase, "diff_hash": _hash(patch),
            "failed_checks": [
                {"argv": item["argv"], "returncode": item["returncode"], "timed_out": item["timed_out"]}
                for item in validations if item["returncode"] != 0
            ],
            "blocking_findings": sorted(findings),
        }
        return _hash(value)

    def _write_evidence(
        self, run_dir: Path, task_id: str, run_id: str, outcome: str,
        contract: dict[str, Any], proposal: dict[str, Any], patch: bytes,
        validations: list[dict[str, Any]], review: dict[str, Any] | None,
        checkpoint_id: str | None,
    ) -> tuple[str, dict[str, Any]]:
        evidence = {
            "task_id": task_id, "run_id": run_id, "outcome": outcome,
            "created_at": _now(),
            "contract_hash": self._state()["tasks"][task_id]["contract_hash"],
            "proposal_hash": _hash(proposal), "diff_hash": _hash(patch),
            "validations": [
                {"argv": item["argv"], "returncode": item["returncode"],
                 "timed_out": item["timed_out"], "log_hash": _hash(item)}
                for item in validations
            ],
            "review_hash": _hash(review) if review else None,
            "checkpoint_id": checkpoint_id,
            "stagnation_fingerprint": self._stagnation_fingerprint(
                task_id, "REVIEWING" if review else "VALIDATING", patch, validations,
                list((review or {}).get("findings", [])),
            ),
        }
        evidence_id = f"EVIDENCE-{_hash(evidence)}"
        evidence["evidence_id"] = evidence_id
        _write_json_atomic(run_dir / "evidence.json", evidence)
        return evidence_id, evidence

    def _finish(
        self, run_id: str, outcome: str, *, evidence_id: str | None = None,
        checkpoint_id: str | None = None, proposal: Mapping[str, Any] | None = None,
        failure_class: str | None = None,
    ) -> RunOutcome:
        arguments: dict[str, Any] = {
            "run_id": run_id, "outcome": outcome, "evidence_id": evidence_id,
            "checkpoint_id": checkpoint_id,
        }
        if proposal:
            arguments.update(
                blocker=proposal.get("blocker"), next_action=proposal.get("next_action"),
                debt_items=proposal.get("debt_items", []),
            )
        result = self.control.execute(Command("run.finish", arguments))
        if result.status != "SUCCESS":
            status = result.status
        else:
            status = {
                "PASS": "SUCCESS", "PASS_WITH_DEBT": "SUCCESS",
                "REWORK": "NOT_READY", "NO_PROGRESS": "NOT_READY",
                "INFRA_FAILURE": "INFRA_FAILURE", "BLOCKED": "BLOCKED", "STOPPED": "STOPPED",
            }[outcome]
        data = dict(result.data)
        if failure_class is None and outcome in {"BLOCKED", "REWORK", "NO_PROGRESS"}:
            failure_class = "agent_task_failure"
        if failure_class is not None:
            data["failure_class"] = failure_class
        return RunOutcome(status, result.message, result.data.get("task_id"), run_id, evidence_id, data)

    def _run_one(self, task_id: str, *, recover_stale: bool) -> RunOutcome:
        history = self._history(task_id)
        reworks = sum(item.get("outcome") == "REWORK" for item in history)
        run_id = f"RUN-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        run_dir = self.canonical / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        contract = self._contract(task_id)
        context = self._context(task_id, run_id, contract)
        _write_json_atomic(run_dir / "context.json", context)
        claim = self.control.execute(Command("run.claim", {"task_id": task_id, "run_id": run_id}))
        if claim.status != "SUCCESS":
            return RunOutcome(claim.status, claim.message, task_id, run_id, data=claim.data)
        if self._attempt_count(task_id) > self.limits.max_work_attempts or reworks >= self.limits.max_reworks:
            return self._finish(run_id, "BLOCKED", proposal={
                "blocker": "Task attempt or rework budget exhausted.",
                "next_action": "Increase the Task budget or revise the contract.",
            })
        campaign_id = contract.get("campaign_id")
        campaign_workspace = CampaignWorkspace(self.root, campaign_id) if campaign_id else None
        workspace = GitWorkspace(self.root, run_id)
        try:
            if campaign_workspace is None:
                worktree = workspace.create()
            else:
                campaign_workspace.recover_checkpoints()
                worktree = campaign_workspace.create_task_workspace(run_id)
                workspace.path = worktree
        except (OSError, RuntimeError) as error:
            return self._finish(run_id, "INFRA_FAILURE", proposal={"summary": str(error)})
        self.control.execute(Command("run.phase", {"run_id": run_id, "to": "RUNNING"}))
        work = self._execute_with_retry(
            self.engine, self._request(run_id, task_id, "builder", worktree, context, run_dir / "attempt-01")
        )
        if work.status == "STOPPED":
            return self._finish(run_id, "STOPPED")
        if work.status != "SUCCESS":
            workspace.cleanup()
            return self._finish(
                run_id, "INFRA_FAILURE", proposal={"summary": work.infrastructure_error or work.status},
                failure_class=work.failure_class or "environment_runtime_failure",
            )
        proposal_errors = self._validate_proposal(work)
        if proposal_errors:
            workspace.cleanup()
            return self._finish(
                run_id, "INFRA_FAILURE", proposal={"summary": "; ".join(proposal_errors)},
                failure_class="agent_task_failure",
            )
        proposal = work.proposal
        if proposal["outcome"] == "BLOCKED":
            if not proposal.get("blocker"):
                proposal["blocker"] = proposal.get("summary", "Agent reported a blocker.")
            if not proposal.get("next_action"):
                proposal["next_action"] = "Resolve the reported blocker."
            evidence_id, _ = self._write_evidence(
                run_dir, task_id, run_id, "BLOCKED", contract, proposal, b"", [], None, None
            )
            workspace.cleanup()
            return self._finish(run_id, "BLOCKED", evidence_id=evidence_id, proposal=proposal)
        if proposal["outcome"] == "REWORK":
            evidence_id, _ = self._write_evidence(
                run_dir, task_id, run_id, "REWORK", contract, proposal, b"", [], None, None
            )
            workspace.cleanup()
            if reworks + 1 >= self.limits.max_reworks:
                return self._finish(run_id, "BLOCKED", evidence_id=evidence_id, proposal={
                    "blocker": "Task rework budget exhausted.",
                    "next_action": "Revise the Task contract or increase its rework budget.",
                })
            return self._finish(run_id, "REWORK", evidence_id=evidence_id, proposal=proposal)

        self.control.execute(Command("run.phase", {"run_id": run_id, "to": "VALIDATING"}))
        try:
            policy = json.loads((self.canonical / "policy.json").read_text(encoding="utf-8"))
            patch, changed_paths = self.attempts.derive_workspace(
                run_id=run_id, workspace=worktree, contract=contract,
                protected_paths=policy.get(
                    "protected_paths",
                    (".autodev/**", ".git/**", ".codex/config.toml", "Second version.md"),
                ),
            )
        except PatchPolicyViolation as error:
            evidence_id, _ = self._write_evidence(
                run_dir, task_id, run_id, "REWORK", contract,
                {"summary": str(error)}, b"", [], None, None,
            )
            workspace.cleanup()
            return self._finish(run_id, "REWORK", evidence_id=evidence_id, proposal={"summary": str(error)})
        validations = self._validations(contract, worktree, run_dir)
        if not patch or any(item["returncode"] != 0 for item in validations):
            outcome = "NO_PROGRESS" if not patch else "REWORK"
            evidence_id, evidence = self._write_evidence(
                run_dir, task_id, run_id, outcome, contract, proposal, patch, validations, None, None
            )
            fingerprints = [
                item.get("stagnation_fingerprint", "") for item in history
                if item.get("outcome") in {"REWORK", "NO_PROGRESS"}
            ] + [evidence["stagnation_fingerprint"]]
            diagnostic_used = any(
                (self.canonical / "runs" / str(item.get("run_id")) / "diagnostic.json").is_file()
                for item in history
            )
            if self.quality.decide(
                contract, failure_fingerprints=fingerprints, diagnostic_used=diagnostic_used,
            ) == QualityDecision.DIAGNOSTIC:
                diagnostic_context = {
                    "task": contract,
                    "failed_validations": [item for item in validations if item["returncode"] != 0],
                    "failure_fingerprint": evidence["stagnation_fingerprint"],
                    "prior_evidence": history[-1:] if history else [],
                }
                diagnosed = self._execute_with_retry(
                    self.diagnostic_engine,
                    self._request(
                        run_id, task_id, "diagnostic", worktree, diagnostic_context,
                        run_dir / "diagnostic-01",
                    ),
                )
                diagnostic = diagnosed.proposal if diagnosed.status == "SUCCESS" else {
                    "outcome": "BLOCKED", "summary": diagnosed.infrastructure_error or diagnosed.status,
                }
                _write_json_atomic(run_dir / "diagnostic.json", diagnostic)
            workspace.cleanup()
            if outcome == "NO_PROGRESS":
                same = 1
                for prior in reversed(history):
                    if prior.get("stagnation_fingerprint") != evidence["stagnation_fingerprint"]:
                        break
                    same += 1
                if same >= self.limits.max_stagnation:
                    return self._finish(run_id, "BLOCKED", evidence_id=evidence_id, proposal={
                        "blocker": "Semantic stagnation threshold reached.",
                        "next_action": "Revise the Task or provide missing product direction.",
                    })
            return self._finish(run_id, outcome, evidence_id=evidence_id, proposal=proposal)

        review: dict[str, Any] | None = None
        quality_decision = self.quality.decide(contract)
        immediate_review = (
            quality_decision == QualityDecision.IMMEDIATE
            if campaign_id else requires_independent_review(contract, rework_count=reworks)
        )
        if immediate_review:
            self.control.execute(Command("run.phase", {"run_id": run_id, "to": "REVIEWING"}))
            review_context = {
                "task": contract, "diff_sha256": _hash(patch),
                "diff": patch.decode("utf-8", errors="replace"),
                "requirements": context["requirements"],
                "validations": [
                    {key: item[key] for key in ("argv", "cwd", "returncode", "timed_out")}
                    for item in validations
                ],
            }
            reviewed = self._execute_with_retry(
                self.reviewer_engine,
                self._request(run_id, task_id, "reviewer", worktree, review_context, run_dir / "review-01"),
            )
            if reviewed.status != "SUCCESS":
                workspace.cleanup()
                return self._finish(
                    run_id, "INFRA_FAILURE",
                    proposal={"summary": reviewed.infrastructure_error or reviewed.status},
                    failure_class=reviewed.failure_class or "environment_runtime_failure",
                )
            review_errors = self._validate_proposal(reviewed)
            if review_errors:
                workspace.cleanup()
                return self._finish(
                    run_id, "INFRA_FAILURE",
                    proposal={"summary": "; ".join(review_errors)},
                    failure_class="agent_task_failure",
                )
            review = reviewed.proposal
            budget_errors = self.attempts.review_budget_errors(review)
            if budget_errors:
                workspace.cleanup()
                return self._finish(run_id, "BLOCKED", proposal={
                    "blocker": "; ".join(budget_errors),
                    "next_action": "Resolve or consolidate the blocking findings before rereview.",
                })
            if review["outcome"] in {"REWORK", "BLOCKED"}:
                if review["outcome"] == "BLOCKED":
                    if not review.get("blocker"):
                        review["blocker"] = review.get("summary", "Reviewer reported a blocker.")
                    if not review.get("next_action"):
                        review["next_action"] = "Resolve the Reviewer blocker."
                evidence_id, _ = self._write_evidence(
                    run_dir, task_id, run_id, review["outcome"], contract, proposal,
                    patch, validations, review, None,
                )
                workspace.cleanup()
                return self._finish(run_id, review["outcome"], evidence_id=evidence_id, proposal=review)
            proposal = review

        outcome = proposal["outcome"]
        if outcome == "PASS_WITH_DEBT":
            debt_errors = self.attempts.debt_errors(
                contract, outcome, {"debt_items": proposal.get("debt_items", [])},
            )
            if debt_errors:
                workspace.cleanup()
                return self._finish(run_id, "REWORK", proposal={"summary": "; ".join(debt_errors)})
        if campaign_workspace is None:
            checkpoint_path = workspace.checkpoint(patch, changed_paths)
            checkpoint_id = _hash(checkpoint_path.read_bytes())
        else:
            try:
                campaign_checkpoint = self.attempts.recover_or_checkpoint(
                    campaign_id=campaign_id, workspace=worktree,
                    task_id=task_id, run_id=run_id,
                )
            except CampaignWorkspaceError as error:
                workspace.cleanup()
                return self._finish(
                    run_id, "INFRA_FAILURE", proposal={"summary": str(error)},
                    failure_class="checkpoint_conflict",
                )
            checkpoint_id = campaign_checkpoint.commit
        evidence_id, _ = self._write_evidence(
            run_dir, task_id, run_id, outcome, contract, proposal, patch, validations, review, checkpoint_id
        )
        if campaign_workspace is None:
            try:
                workspace.apply(patch)
            except ConcurrentSourceChange as error:
                failure_id, _ = self._write_evidence(
                    run_dir, task_id, run_id, "INFRA_FAILURE", contract,
                    {"summary": str(error)}, patch, validations, review, checkpoint_id,
                )
                return self._finish(run_id, "INFRA_FAILURE", evidence_id=failure_id, proposal={"summary": str(error)})
        workspace.cleanup()
        return self._finish(
            run_id, outcome, evidence_id=evidence_id, checkpoint_id=checkpoint_id, proposal=proposal
        )

    def _project_validation(self) -> bool:
        state = self._state()
        artifact_dir = self.canonical / "runs" / f"FULL-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        all_results: list[dict[str, Any]] = []
        for task_id, record in sorted(state["tasks"].items()):
            if record["status"] == "ACCEPTED":
                all_results.extend(self._validations(self._contract(task_id), self.root, artifact_dir / task_id))
        passed = bool(all_results) and all(item["returncode"] == 0 for item in all_results)
        evidence_id = f"FULL-{_hash(all_results)}"
        self.control.execute(Command("validation.record", {"passed": passed, "evidence_id": evidence_id}))
        return passed

    def run(self, request: RunRequest) -> RunOutcome:
        if request.until not in {None, "complete-or-blocked"}:
            return RunOutcome("INVALID", "--until must be complete-or-blocked")
        engines = (self.engine, self.reviewer_engine, self.diagnostic_engine)
        if (
            any(engine.requires_live_authorization for engine in engines)
            and os.environ.get("AUTODEV_LIVE_CODEX") != "1"
        ):
            return RunOutcome("NOT_READY", "live Codex requires AUTODEV_LIVE_CODEX=1")
        validation = self.control.execute(Command("validate", {"ready": True}))
        if validation.status != "SUCCESS":
            return RunOutcome(validation.status, validation.message, data=validation.data)
        baseline = git_baseline_status(self.root)
        if not baseline["has_head"]:
            return RunOutcome("NOT_READY", baseline["error"] or "Git HEAD is missing", data=baseline)
        current_campaign_id = self._state().get("current_campaign_id")
        campaign_source_matches = False
        if current_campaign_id:
            try:
                campaign_baseline = json.loads((
                    self.canonical / "campaigns" / current_campaign_id / "workspace-baseline.json"
                ).read_text(encoding="utf-8"))
                campaign_source_matches = (
                    source_fingerprint(self.root).digest
                    == campaign_baseline["source_fingerprint"]["digest"]
                )
            except (OSError, json.JSONDecodeError, KeyError, RuntimeError):
                campaign_source_matches = False
        if (current_campaign_id and not campaign_source_matches) or (
            not current_campaign_id and not baseline["clean"]
        ):
            paths = ", ".join(baseline["dirty_paths"])
            return RunOutcome(
                "NOT_READY",
                f"source baseline must match the recorded Campaign source: {paths}"
                if current_campaign_id else f"source baseline must be clean outside .autodev: {paths}",
                data=baseline,
            )
        preflight_targets = (
            (self.engine, self.runtime["build_permission_profile"]),
            (self.reviewer_engine, self.runtime["review_permission_profile"]),
            (self.diagnostic_engine, self.runtime["review_permission_profile"]),
        )
        seen_preflights: set[tuple[int, str, str]] = set()
        for engine, permission_profile in preflight_targets:
            key = (id(engine), permission_profile, self.runtime["mode"])
            if key in seen_preflights:
                continue
            seen_preflights.add(key)
            if not engine.requires_live_authorization:
                continue
            diagnostic = engine.preflight(
                self.root, permission_profile=permission_profile,
                runtime_mode=self.runtime["mode"],
            )
            if not diagnostic["ready"]:
                return RunOutcome(
                    "INFRA_FAILURE",
                    f"Codex runtime preflight failed: {diagnostic['message']}",
                    data={"runtime_diagnostic": diagnostic},
                )
        lock = ProjectLock(self.root)
        try:
            lock.acquire(recover_stale=request.recover_stale)
        except LockUnavailable as error:
            return RunOutcome("NOT_READY", str(error))
        started = time.monotonic()
        last = RunOutcome("NOT_READY", "no READY Task")
        try:
            if request.recover_stale:
                recover_stale_workspaces(self.root)
            for _ in range(self.limits.max_iterations):
                if time.monotonic() - started > self.limits.max_seconds:
                    return RunOutcome("BLOCKED", "run wall-clock budget exhausted")
                if (self.canonical / "STOP").exists():
                    return RunOutcome("STOPPED", "STOP requested")
                task_id = self._select(request.task_id if last.run_id is None else None)
                if task_id is None:
                    state = self._state()
                    if state.get("current_campaign_id"):
                        return last
                    if not self._project_validation():
                        return last
                    lock.release()
                    completion = self.control.execute(Command("complete"))
                    if completion.status == "SUCCESS":
                        return RunOutcome("SUCCESS", "project COMPLETE")
                    return last
                last = self._run_one(task_id, recover_stale=request.recover_stale)
                lock.heartbeat()
                if request.until is None or last.status in {"BLOCKED", "STOPPED", "INFRA_FAILURE"}:
                    return last
            return RunOutcome("BLOCKED", "run iteration budget exhausted")
        finally:
            lock.release()
