"""Shared deterministic attempt primitives for Action and headless adapters."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from autodev._workspace import GitWorkspace, _write_json_atomic
from autodev.campaign_workspace import (
    CampaignWorkspace,
    CampaignWorkspaceError,
    CheckpointResult,
)
from autodev.control_plane import Command, CommandResult, ControlPlane
from autodev.quality import QualityBudget, QualityRouter, validate_debt


def canonical_hash(value: Any) -> str:
    content = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()
    return hashlib.sha256(content).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class CheckpointPublicationPending(CampaignWorkspaceError):
    """A private checkpoint is durable but canonical publication must be retried."""


class AttemptLifecycle:
    """Centralize the trust gates shared by every attempt transport."""

    def __init__(self, project_root: Path) -> None:
        self.root = project_root.resolve()
        self.canonical = self.root / ".autodev"
        self.control = ControlPlane(self.root)
        self.quality = QualityRouter()
        self.budget = QualityBudget()

    def load_contract(self, reference: str, expected_hash: str) -> dict[str, Any]:
        path = (self.canonical / reference).resolve()
        if self.canonical not in path.parents:
            raise ValueError("contract reference escapes canonical state")
        contract = json.loads(path.read_text(encoding="utf-8"))
        if canonical_hash(contract) != expected_hash:
            raise ValueError("frozen Task contract hash mismatch")
        return contract

    def derive_workspace(
        self, *, run_id: str, workspace: Path, contract: Mapping[str, Any],
        protected_paths: list[str] | tuple[str, ...],
    ) -> tuple[bytes, list[str]]:
        return GitWorkspace(self.root, run_id, path=workspace).collect_patch(
            allowed_paths=contract["allowed_paths"], protected_paths=protected_paths,
        )

    def run_validations(
        self, contract: Mapping[str, Any], workspace: Path,
        artifact_dir: Path | None = None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for index, validation in enumerate(contract["validation_commands"]):
            started = time.monotonic()
            try:
                process = subprocess.run(
                    validation["argv"], cwd=workspace / validation["cwd"], shell=False,
                    capture_output=True, text=True, timeout=validation["timeout"], check=False,
                )
                item = {
                    "index": index, "argv": validation["argv"], "cwd": validation["cwd"],
                    "returncode": process.returncode, "timed_out": False,
                    "stdout": process.stdout, "stderr": process.stderr,
                    "duration_seconds": time.monotonic() - started,
                }
            except subprocess.TimeoutExpired as error:
                item = {
                    "index": index, "argv": validation["argv"], "cwd": validation["cwd"],
                    "returncode": None, "timed_out": True,
                    "stdout": str(error.stdout or ""), "stderr": str(error.stderr or ""),
                    "duration_seconds": time.monotonic() - started,
                }
            results.append(item)
            if artifact_dir is not None:
                _write_json_atomic(artifact_dir / f"validation-{index:02d}.json", item)
        return results

    @staticmethod
    def debt_errors(
        contract: Mapping[str, Any], outcome: str, data: Mapping[str, Any],
    ) -> list[str]:
        debt_items = data.get("debt_items", [])
        if outcome == "PASS_WITH_DEBT":
            return validate_debt(contract, debt_items)
        return ["debt_items require PASS_WITH_DEBT"] if debt_items else []

    def review_budget_errors(self, result: Mapping[str, Any]) -> list[str]:
        """Apply the same bounded Reviewer output contract on every transport."""

        findings = result.get("findings", [])
        data = result.get("data", {})
        debt_items = data.get("debt_items", []) if isinstance(data, Mapping) else []
        if not debt_items:
            debt_items = result.get("debt_items", [])
        errors: list[str] = []
        if len(findings) > self.budget.max_blocking_findings:
            errors.append("Review exceeded the blocking finding budget")
        if len(debt_items) > self.budget.max_debt_findings:
            errors.append("Review exceeded the debt finding budget")
        return errors

    def finish(
        self, *, run_id: str, outcome: str, evidence_id: str | None = None,
        checkpoint_id: str | None = None, result: Mapping[str, Any] | None = None,
    ) -> CommandResult:
        """Record an attempt outcome through the one canonical writer."""

        arguments: dict[str, Any] = {
            "run_id": run_id, "outcome": outcome,
            "evidence_id": evidence_id, "checkpoint_id": checkpoint_id,
        }
        if result:
            data = result.get("data", {})
            debt_items = data.get("debt_items", []) if isinstance(data, Mapping) else []
            if not debt_items:
                debt_items = result.get("debt_items", [])
            arguments.update(
                blocker=result.get("blocker"), next_action=result.get("next_action"),
                debt_items=debt_items,
            )
        return self.control.execute(Command("run.finish", arguments))

    @staticmethod
    def public_status(outcome: str) -> str:
        return {
            "PASS": "SUCCESS", "PASS_WITH_DEBT": "SUCCESS",
            "REWORK": "NOT_READY", "NO_PROGRESS": "NOT_READY",
            "INFRA_FAILURE": "INFRA_FAILURE", "BLOCKED": "BLOCKED", "STOPPED": "STOPPED",
        }[outcome]

    @staticmethod
    def contract_gate_hash(contract: Mapping[str, Any]) -> str:
        """Hash only the frozen trust inputs that decide attempt acceptance."""

        fields = (
            "id", "objective", "requirements", "dependencies", "priority", "blocking",
            "risk", "quality_mode", "change_classes", "allowed_paths", "out_of_scope",
            "acceptance_criteria", "validation_commands", "prohibited_actions", "review_scope",
        )
        return canonical_hash({field: contract.get(field) for field in fields})

    @staticmethod
    def validation_evidence(validations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Project validation results without timing or adapter-specific fields."""

        evidence: list[dict[str, Any]] = []
        for item in validations:
            log = {
                "argv": item["argv"], "cwd": item.get("cwd", "."),
                "returncode": item["returncode"], "timed_out": item["timed_out"],
                "stdout": item.get("stdout", ""), "stderr": item.get("stderr", ""),
            }
            evidence.append({
                "argv": log["argv"], "cwd": log["cwd"],
                "returncode": log["returncode"], "timed_out": log["timed_out"],
                "log_hash": canonical_hash(log),
            })
        return evidence

    @staticmethod
    def stagnation_fingerprint(
        task_id: str, phase: str, patch: bytes, validations: list[dict[str, Any]],
        findings: list[str],
    ) -> str:
        value = {
            "task_id": task_id, "phase": phase,
            "diff_hash": hashlib.sha256(patch).hexdigest(),
            "failed_checks": [
                {
                    "argv": item["argv"], "returncode": item["returncode"],
                    "timed_out": item["timed_out"],
                }
                for item in validations if item["returncode"] != 0
            ],
            "blocking_findings": sorted(findings),
        }
        return canonical_hash(value)

    def write_evidence(
        self, *, run_dir: Path, task_id: str, run_id: str, outcome: str,
        contract: Mapping[str, Any], patch: bytes, changed_paths: list[str],
        validations: list[dict[str, Any]], quality_route: str,
        checkpoint_id: str | None, result: Mapping[str, Any] | None = None,
        review: Mapping[str, Any] | None = None, extra: Mapping[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Persist one canonical evidence envelope for every attempt adapter."""

        projected_validations = self.validation_evidence(validations)
        decision = review if review is not None else (result or {})
        decision_data = decision.get("data", {})
        debt_items = (
            decision_data.get("debt_items", [])
            if isinstance(decision_data, Mapping) else []
        )
        if not debt_items:
            debt_items = decision.get("debt_items", [])
        acceptance = {
            "schema_version": 1,
            "task_id": task_id,
            "outcome": outcome,
            "contract_gate_hash": self.contract_gate_hash(contract),
            "diff_hash": hashlib.sha256(patch).hexdigest(),
            "changed_paths": sorted(changed_paths),
            "validations": projected_validations,
            "quality_route": quality_route,
            "findings": decision.get("findings", []),
            "debt_items": debt_items,
        }
        evidence: dict[str, Any] = {
            **acceptance,
            "run_id": run_id,
            "created_at": _now(),
            "checkpoint_id": checkpoint_id,
            "stagnation_fingerprint": self.stagnation_fingerprint(
                task_id, "REVIEWING" if review else "VALIDATING", patch, validations,
                list((review or {}).get("findings", [])),
            ),
            "acceptance_hash": canonical_hash(acceptance),
        }
        if result is not None:
            evidence["result_hash"] = canonical_hash(result)
        if review is not None:
            evidence["review_hash"] = canonical_hash({
                "outcome": review.get("outcome"),
                "summary": review.get("summary"),
                "blocker": review.get("blocker"),
                "next_action": review.get("next_action"),
                "findings": review.get("findings", []),
                "debt_items": debt_items,
            })
        else:
            evidence["review_hash"] = None
        if extra:
            evidence.update(extra)
        evidence_id = f"EVIDENCE-{canonical_hash(evidence)}"
        evidence["evidence_id"] = evidence_id
        _write_json_atomic(run_dir / "evidence.json", evidence)
        return evidence_id, evidence

    def reconcile_accepted_checkpoint(
        self, campaign_id: str, journal: Mapping[str, Any] | None = None,
    ) -> int | None:
        """Finish a current Run whose private checkpoint and evidence are durable."""

        state = json.loads((self.canonical / "state.json").read_text(encoding="utf-8"))
        run_id = state.get("current_run_id")
        task_id = state.get("current_task_id")
        if not isinstance(run_id, str) or not isinstance(task_id, str):
            return None
        owner = CampaignWorkspace(self.root, campaign_id)
        if journal is None:
            path = owner.journals / f"{run_id}.json"
            if not path.is_file():
                return None
            journal = json.loads(path.read_text(encoding="utf-8"))
        if journal.get("run_id") != run_id or journal.get("task_id") != task_id:
            return None
        if journal.get("phase") not in {"REF_UPDATED", "COMMITTED"}:
            return None
        commit = str(journal.get("commit", ""))
        campaign = state.get("campaigns", {}).get(campaign_id, {})
        if campaign.get("checkpoint") != commit or owner.current_commit != commit:
            raise CampaignWorkspaceError(
                "durable checkpoint does not match canonical Campaign state and private ref"
            )
        try:
            evidence = json.loads(
                (self.canonical / "runs" / run_id / "evidence.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as error:
            raise CampaignWorkspaceError(
                f"checkpoint exists without provable acceptance evidence: {error}"
            ) from error
        acceptance_fields = (
            "schema_version", "task_id", "outcome", "contract_gate_hash", "diff_hash",
            "changed_paths", "validations", "quality_route", "findings", "debt_items",
        )
        acceptance = {field: evidence.get(field) for field in acceptance_fields}
        contract_record = state.get("tasks", {}).get(task_id, {})
        try:
            contract = self.load_contract(
                f"tasks/{task_id}/contract.json", str(contract_record["contract_hash"]),
            )
        except (KeyError, OSError, json.JSONDecodeError, ValueError) as error:
            raise CampaignWorkspaceError(
                f"checkpoint contract cannot prove Task acceptance: {error}"
            ) from error
        expected_evidence_id = f"EVIDENCE-{canonical_hash({
            key: value for key, value in evidence.items() if key != 'evidence_id'
        })}"
        if (
            evidence.get("run_id") != run_id
            or evidence.get("task_id") != task_id
            or evidence.get("checkpoint_id") != commit
            or evidence.get("outcome") not in {"PASS", "PASS_WITH_DEBT"}
            or evidence.get("contract_gate_hash") != self.contract_gate_hash(contract)
            or evidence.get("evidence_id") != expected_evidence_id
            or evidence.get("acceptance_hash") != canonical_hash(acceptance)
        ):
            raise CampaignWorkspaceError("checkpoint evidence does not prove Task acceptance")
        finished = self.finish(
            run_id=run_id, outcome=str(evidence["outcome"]),
            evidence_id=str(evidence["evidence_id"]), checkpoint_id=commit,
            result={"debt_items": evidence.get("debt_items", [])},
        )
        if finished.status != "SUCCESS":
            raise CampaignWorkspaceError(finished.message)
        return finished.revision

    def recover_or_checkpoint(
        self, *, campaign_id: str, workspace: Path, task_id: str, run_id: str,
        before_record: Callable[[CheckpointResult], None] | None = None,
    ) -> CheckpointResult:
        owner = CampaignWorkspace(self.root, campaign_id)
        journal_path = owner.journals / f"{run_id}.json"
        if journal_path.is_file():
            owner.recover_checkpoints()
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            if journal.get("phase") not in {"REF_UPDATED", "COMMITTED"}:
                raise CampaignWorkspaceError(
                    f"checkpoint journal cannot be recovered from {journal.get('phase')}"
                )
            checkpoint = CheckpointResult(
                campaign_id, task_id, run_id, str(journal["base_commit"]),
                str(journal["commit"]), str(journal["tree"]), journal_path,
            )
        else:
            checkpoint = owner.checkpoint(workspace, task_id=task_id, run_id=run_id)
        if before_record is not None:
            before_record(checkpoint)
        state = json.loads((self.canonical / "state.json").read_text(encoding="utf-8"))
        if state["campaigns"][campaign_id]["checkpoint"] != checkpoint.commit:
            recorded = self.control.execute(Command("campaign.checkpoint", {
                "id": campaign_id, "checkpoint": checkpoint.commit, "task_id": task_id,
            }))
            if recorded.status != "SUCCESS":
                journal = json.loads(checkpoint.journal_path.read_text(encoding="utf-8"))
                if (
                    journal.get("phase") == "REF_UPDATED"
                    and owner.current_commit == checkpoint.commit
                ):
                    raise CheckpointPublicationPending(recorded.message)
                raise CampaignWorkspaceError(recorded.message)
            revision = recorded.revision or 0
        else:
            revision = int(state["revision"])
        owner.finalize_checkpoint(checkpoint, canonical_revision=revision)
        return checkpoint
