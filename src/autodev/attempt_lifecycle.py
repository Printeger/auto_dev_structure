"""Shared deterministic attempt primitives for Action and headless adapters."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from autodev._workspace import GitWorkspace, _write_json_atomic
from autodev.campaign_workspace import (
    CampaignWorkspace,
    CampaignWorkspaceError,
    CheckpointResult,
)
from autodev.control_plane import Command, ControlPlane
from autodev.quality import QualityBudget, QualityRouter, validate_debt


def canonical_hash(value: Any) -> str:
    content = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()
    return hashlib.sha256(content).hexdigest()


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

    def recover_or_checkpoint(
        self, *, campaign_id: str, workspace: Path, task_id: str, run_id: str,
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
        state = json.loads((self.canonical / "state.json").read_text(encoding="utf-8"))
        if state["campaigns"][campaign_id]["checkpoint"] != checkpoint.commit:
            recorded = self.control.execute(Command("campaign.checkpoint", {
                "id": campaign_id, "checkpoint": checkpoint.commit, "task_id": task_id,
            }))
            if recorded.status != "SUCCESS":
                raise CampaignWorkspaceError(recorded.message)
            revision = recorded.revision or 0
        else:
            revision = int(state["revision"])
        owner.finalize_checkpoint(checkpoint, canonical_revision=revision)
        return checkpoint
