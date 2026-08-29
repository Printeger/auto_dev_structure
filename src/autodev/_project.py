"""Project installation and explicit V1/V2 migration into V3."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autodev import __version__
from autodev._resources import _read_text
from autodev.control_plane import Command, ControlPlane
from autodev.campaign import default_authority_envelope
from autodev.campaign_workspace import CampaignWorkspace
from autodev._workspace import _write_json_atomic, git_baseline_status, source_fingerprint
from autodev.quality import QualityRouter


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _dump(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bootstrap_documents(name: str, created_at: str | None = None) -> dict[str, str]:
    timestamp = created_at or _now()
    return {
        ".autodev/manifest.json": _dump({
            "$schema": "https://autodev.local/schemas/manifest.schema.json",
            "schema_version": 1,
            "framework_version": __version__,
            "project_name": name,
            "created_at": timestamp,
        }),
        ".autodev/config.json": _dump({
            "$schema": "https://autodev.local/schemas/config.schema.json",
            "schema_version": 1,
            "requirements_path": "docs/REQUIREMENTS.md",
        }),
        ".autodev/policy.json": _dump({
            "$schema": "https://autodev.local/schemas/policy.schema.json",
            "schema_version": 1,
            "validation": {
                "allowed_executables": ["python", "python3", "pytest", "git"],
                "allowed_cwds": ["."],
            },
            "runtime": {
                "mode": "codex-sandbox",
                "build_permission_profile": ":workspace",
                "review_permission_profile": ":read-only",
            },
            "runner": {
                "max_iterations": 30,
                "max_seconds": 14400,
                "max_work_attempts": 4,
                "max_reworks": 2,
                "max_stagnation": 2,
                "idle_timeout": 600,
                "hard_timeout": 2400,
                "infrastructure_retries": 1
            },
            "protected_paths": [".autodev/**", ".git/**", ".codex/config.toml", "Second version.md"],
        }),
        ".autodev/state.json": _dump({
            "$schema": "https://autodev.local/schemas/state.schema.json",
            "schema_version": 1,
            "framework_version": __version__,
            "revision": 0,
            "project_status": "BOOTSTRAP",
            "current_milestone": None,
            "current_task_id": None,
            "current_run_id": None,
            "current_action_id": None,
            "pause_requested": False,
            "last_outcome": None,
            "last_checkpoint": None,
            "blocker": None,
            "next_owner": "COMMANDER",
            "next_action": "Complete project contracts, then run autodev activate.",
            "tasks": {},
            "accepted_requirement_ids": [],
            "blocking_debt_ids": [],
            "full_validation_passed": False,
            "active_lock": None,
            "updated_at": timestamp,
        }),
        ".autodev/debt.json": _dump({"schema_version": 1, "items": []}),
        ".autodev/.gitignore": (
            ".control-plane.lock\n"
            "STOP\n"
            "events/*\n"
            "runs/*\n"
            "locks/*\n"
            "workspaces/*\n"
            "actions/*\n"
            "campaigns/*/checkpoint-journal/*\n"
            "campaigns/*/phase-summary-*.json\n"
            "campaigns/*/materialization-journal.json\n"
        ),
        ".codex/agents/autodev-builder.toml": _read_text(
            "templates/.codex/agents/autodev-builder.toml"
        ),
        "docs/REQUIREMENTS.md": (
            "# Requirements\n\n"
            "| ID | Priority | Requirement | Acceptance signal | Status |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| REQ-001 | MUST | Describe the first required outcome. | Define observable evidence. | PROPOSED |\n"
        ),
    }


_CANONICAL_DIRS = tuple(
    f".autodev/{name}"
    for name in ("tasks", "runs", "events", "locks", "workspaces", "migrations", "actions")
)


@dataclass(frozen=True, slots=True)
class ProjectOperation:
    status: str
    message: str
    data: dict[str, Any]

    @property
    def exit_code(self) -> int:
        return {"SUCCESS": 0, "INVALID": 1, "NOT_READY": 2, "BLOCKED": 3,
                "STOPPED": 4, "INFRA_FAILURE": 5}[self.status]


def initialize_project(target: Path, name: str, *, merge: bool = False) -> ProjectOperation:
    """Install contracts, canonical state, and templates after a conflict preflight."""

    target = target.resolve()
    if not name.strip() or any(character in name for character in "\r\n"):
        return ProjectOperation("INVALID", "project name must be non-empty and single-line", {})
    documents = _bootstrap_documents(name.strip())
    conflicts = sorted(relative for relative in documents if (target / relative).exists())
    if conflicts and not merge:
        return ProjectOperation("NOT_READY", "initialization conflicts; nothing written", {"conflicts": conflicts})
    copied: list[str] = []
    try:
        target.mkdir(parents=True, exist_ok=True)
        for relative in _CANONICAL_DIRS:
            (target / relative).mkdir(parents=True, exist_ok=True)
        for relative, content in documents.items():
            destination = target / relative
            if destination.exists():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
            copied.append(relative)
    except OSError as error:
        return ProjectOperation("INFRA_FAILURE", f"initialization failed: {error}", {"copied": copied})
    status = "NOT_READY" if conflicts else "SUCCESS"
    message = "initialized with preserved conflicts" if conflicts else "project initialized"
    return ProjectOperation(status, message, {"copied": copied, "conflicts": conflicts})


def _v1_checksum_report(root: Path) -> tuple[list[str], list[str]]:
    manifest = json.loads(_read_text("v1-checksums.json"))
    unmodified: list[str] = []
    conflicts: list[str] = []
    for relative, expected in manifest["files"].items():
        path = root / relative
        if not path.exists():
            continue
        (unmodified if _sha256(path) == expected else conflicts).append(relative)
    return sorted(unmodified), sorted(conflicts)


def check_migration(root: Path) -> ProjectOperation:
    root = root.resolve()
    if (root / ".autodev").exists():
        return ProjectOperation("INVALID", "V2 canonical state already exists", {})
    state_path = root / ".agent" / "STATE.json"
    if not state_path.is_file():
        return ProjectOperation("NOT_READY", "no V1 .agent/STATE.json found", {})
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return ProjectOperation("INVALID", f"invalid V1 state: {error}", {})
    unmodified, conflicts = _v1_checksum_report(root)
    data = {
        "v1_status": state.get("project_status"),
        "framework_files": unmodified,
        "modified_framework_conflicts": conflicts,
        "applicable": not conflicts,
    }
    if conflicts:
        return ProjectOperation("BLOCKED", "modified V1 framework files require manual resolution", data)
    return ProjectOperation("SUCCESS", "migration is applicable", data)


def _task_title(path: Path) -> str:
    try:
        first = path.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        return path.stem
    match = re.match(r"^#\s+TASK-[0-9]{3,}:?\s*(.*)$", first)
    return match.group(1).strip() if match and match.group(1).strip() else path.stem


def _migrated_state(root: Path, name: str, timestamp: str) -> tuple[dict[str, Any], dict[str, str]]:
    legacy = json.loads((root / ".agent" / "STATE.json").read_text(encoding="utf-8"))
    state = json.loads(_bootstrap_documents(name, timestamp)[".autodev/state.json"])
    status = legacy.get("project_status", "BOOTSTRAP")
    if status == "COMPLETE":
        state.update(
            project_status="BLOCKED",
            blocker="V1 completion requires V2 evidence review.",
            next_owner="HUMAN",
            next_action="Review migrated completion evidence, then unblock through V2.",
            last_outcome="BLOCKED",
        )
    elif status in {"BOOTSTRAP", "ACTIVE", "BLOCKED"}:
        state["project_status"] = status
        state["blocker"] = legacy.get("blocker") if status == "BLOCKED" else None
        state["next_owner"] = "HUMAN" if status == "BLOCKED" else "COMMANDER"
        state["next_action"] = legacy.get("next_action") or state["next_action"]
    contracts: dict[str, str] = {}
    for path in sorted((root / ".agent" / "tasks").glob("TASK-[0-9][0-9][0-9]*.md")):
        task_id = path.stem
        contract = {
            "$schema": "https://autodev.local/schemas/task-contract.schema.json",
            "schema_version": 1,
            "id": task_id,
            "title": _task_title(path),
            "objective": "Review and complete the migrated V1 Task contract.",
            "requirements": [], "dependencies": [], "priority": "MUST", "blocking": True,
            "risk": "HIGH", "quality_mode": "BUILD", "change_classes": ["migration"],
            "allowed_paths": ["."], "out_of_scope": [], "acceptance_criteria": [],
            "validation_commands": [],
            "prohibited_actions": ["Edit canonical state outside ControlPlane"],
            "created_at": timestamp,
        }
        contracts[task_id] = _dump(contract)
        state["tasks"][task_id] = {
            "status": "DRAFT", "generation": 1, "contract_hash": None,
            "claim_id": None, "evidence_ids": [], "blocking": True,
            "requirement_ids": [], "created_at": timestamp, "updated_at": timestamp,
        }
    state["updated_at"] = timestamp
    return state, contracts


def _migrated_debt(root: Path) -> dict[str, Any]:
    path = root / ".agent" / "DEBT.md"
    items: list[dict[str, str]] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.lstrip().startswith("|"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) != 7 or not re.fullmatch(r"DEBT-[A-Za-z0-9._-]+", cells[0]):
                continue
            debt_id, severity, module, reason, fix_before, source_task, status = cells
            items.append({
                "id": debt_id, "severity": severity, "module": module, "reason": reason,
                "fix_before": fix_before, "source_task": source_task,
                "classification": "migrated-v1", "status": status,
            })
    return {"schema_version": 1, "items": items}


def _stage_v1_campaign(
    root: Path, canonical: Path, state: dict[str, Any], contracts: dict[str, str],
    *, timestamp: str,
) -> None:
    """Make the staged V1 import a V3 CHANGE Campaign before it is installed."""

    requirements = _markdown_requirement_baseline(root / "docs/REQUIREMENTS.md", "CAMP-001")
    router = QualityRouter()
    for task_id, rendered in list(contracts.items()):
        contract = json.loads(rendered)
        contract.update(
            campaign_id="CAMP-001", phase="IMPLEMENT", admission="HUMAN_APPROVED",
            review_scope=router.decide(contract).value,
        )
        contracts[task_id] = _dump(contract)
    payload = {
        "idea": "Continue the migrated V1 project.", "mode": "CHANGE",
        "target": "CHANGE_COMPLETE", "requirements": requirements,
        "authority_envelope": default_authority_envelope(), "phases": ["IMPLEMENT"],
    }
    campaign = {
        "$schema": "https://autodev.local/schemas/campaign.schema.json",
        "schema_version": 1, "id": "CAMP-001", "idea": payload["idea"],
        "mode": "CHANGE", "target": "CHANGE_COMPLETE", "autonomy": "HUMAN_ON_BLOCKED",
        "requirements_hash": hashlib.sha256(json.dumps(
            requirements, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()).hexdigest(),
        "authority_envelope": payload["authority_envelope"], "phases": ["IMPLEMENT"],
        "proposal_hash": hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()).hexdigest(),
        "parent_campaign_id": None, "source_checkpoint": None,
        "proposed_at": timestamp, "approved_at": timestamp,
    }
    campaign_dir = canonical / "campaigns" / "CAMP-001"
    campaign_dir.mkdir(parents=True, exist_ok=True)
    (campaign_dir / "requirements.json").write_text(_dump(requirements), encoding="utf-8")
    (campaign_dir / "campaign.json").write_text(_dump(campaign), encoding="utf-8")
    waiting = state["project_status"] == "BLOCKED"
    state["framework_version"] = __version__
    state["current_campaign_id"] = "CAMP-001"
    state["campaigns"] = {"CAMP-001": {
        "status": "WAITING_FOR_HUMAN" if waiting else "ACTIVE",
        "phase": "IMPLEMENT", "mode": "CHANGE", "target": "CHANGE_COMPLETE",
        "proposal_hash": campaign["proposal_hash"], "checkpoint": None,
        "last_materialized_checkpoint": None, "approved_at": timestamp,
    }}


def _initialize_v1_campaign_ref(root: Path) -> str | None:
    """Adopt a Git-backed V1 source snapshot into its new private Campaign ref."""

    baseline = git_baseline_status(root)
    if not baseline["has_head"]:
        return None
    workspace = CampaignWorkspace(root, "CAMP-001")
    initial = workspace.initialize("HEAD")
    checkpoint = initial
    if baseline["dirty_paths"]:
        worktree = workspace.create_task_workspace("MIGRATE-V1")
        try:
            for relative in baseline["dirty_paths"]:
                source = root / relative
                destination = worktree / relative
                if source.is_file() or source.is_symlink():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if source.is_symlink():
                        destination.unlink(missing_ok=True)
                        destination.symlink_to(os.readlink(source))
                    else:
                        shutil.copy2(source, destination)
                elif not source.exists() and destination.exists():
                    if destination.is_dir():
                        shutil.rmtree(destination)
                    else:
                        destination.unlink()
            adopted = workspace.checkpoint(
                worktree, task_id="TASK-MIGRATION", run_id="MIGRATE-V1",
            )
            checkpoint = adopted.commit
            workspace.finalize_checkpoint(adopted, canonical_revision=0)
            baseline_path = root / ".autodev/campaigns/CAMP-001/workspace-baseline.json"
            baseline_document = json.loads(baseline_path.read_text(encoding="utf-8"))
            baseline_document["last_materialized_commit"] = checkpoint
            _write_json_atomic(baseline_path, baseline_document)
        finally:
            workspace.remove_task_workspace(worktree)
    campaign_path = root / ".autodev/campaigns/CAMP-001/campaign.json"
    campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
    campaign["source_checkpoint"] = initial
    _write_json_atomic(campaign_path, campaign)
    state_path = root / ".autodev/state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["campaigns"]["CAMP-001"]["checkpoint"] = checkpoint
    state["campaigns"]["CAMP-001"]["last_materialized_checkpoint"] = checkpoint
    _write_json_atomic(state_path, state)
    return checkpoint


def apply_migration(root: Path) -> ProjectOperation:
    root = root.resolve()
    report = check_migration(root)
    if report.status != "SUCCESS":
        return report
    legacy_state = json.loads((root / ".agent" / "STATE.json").read_text(encoding="utf-8"))
    name = str(legacy_state.get("project_name") or root.name)
    migration_id = f"v1-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    timestamp = _now()
    temporary = Path(tempfile.mkdtemp(prefix=".autodev-migrate-", dir=root))
    try:
        for relative, content in _bootstrap_documents(name, timestamp).items():
            if relative.startswith(".autodev/"):
                destination = temporary / relative.removeprefix(".autodev/")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")
        for relative in (item.removeprefix(".autodev/") for item in _CANONICAL_DIRS):
            (temporary / relative).mkdir(parents=True, exist_ok=True)
        state, contracts = _migrated_state(root, name, timestamp)
        _stage_v1_campaign(root, temporary, state, contracts, timestamp=timestamp)
        debt = _migrated_debt(root)
        state["blocking_debt_ids"] = sorted(
            item["id"] for item in debt["items"]
            if item.get("severity") not in {"LOW", "MEDIUM"} and item.get("status") != "CLOSED"
        )
        (temporary / "state.json").write_text(_dump(state), encoding="utf-8")
        (temporary / "debt.json").write_text(_dump(debt), encoding="utf-8")
        for task_id, content in contracts.items():
            task_dir = temporary / "tasks" / task_id
            task_dir.mkdir(parents=True, exist_ok=True)
            (task_dir / "contract.json").write_text(content, encoding="utf-8")
        backup_dir = temporary / "migrations" / migration_id / "agent-backup"
        shutil.copytree(root / ".agent", backup_dir)
        metadata = {
            "migration_id": migration_id,
            "applied_at": timestamp,
            "legacy_frozen_path": f".agent.v1-frozen-{migration_id}",
            "framework_files": report.data["framework_files"],
            "initial_revision": state["revision"],
        }
        (temporary / "migrations" / f"{migration_id}.json").write_text(_dump(metadata), encoding="utf-8")

        wrapper = Path(tempfile.mkdtemp(prefix="autodev-validate-"))
        try:
            os.symlink(temporary, wrapper / ".autodev", target_is_directory=True)
            (wrapper / "docs").mkdir()
            requirements = root / "docs" / "REQUIREMENTS.md"
            if requirements.exists():
                shutil.copy2(requirements, wrapper / "docs" / "REQUIREMENTS.md")
            else:
                (wrapper / "docs" / "REQUIREMENTS.md").write_text(
                    _bootstrap_documents(name)["docs/REQUIREMENTS.md"], encoding="utf-8"
                )
            validation = ControlPlane(wrapper).execute(Command("validate"))
            if validation.status != "SUCCESS":
                return ProjectOperation("INVALID", "staged migration failed validation", dict(validation.data))
        finally:
            shutil.rmtree(wrapper, ignore_errors=True)

        frozen = root / metadata["legacy_frozen_path"]
        os.replace(root / ".agent", frozen)
        try:
            os.replace(temporary, root / ".autodev")
        except OSError:
            os.replace(frozen, root / ".agent")
            raise
        for relative in report.data["framework_files"]:
            path = root / relative
            if path.exists():
                destination = root / ".autodev" / "migrations" / migration_id / "framework-backup" / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(path, destination)
        checkpoint = _initialize_v1_campaign_ref(root)
        validation = ControlPlane(root).execute(Command("validate"))
        if validation.status != "SUCCESS":
            raise RuntimeError("; ".join(validation.data.get("errors", [validation.message])))
        return ProjectOperation(
            "SUCCESS", "migration applied",
            {"migration_id": migration_id, "campaign_id": "CAMP-001", "checkpoint": checkpoint},
        )
    except (OSError, RuntimeError, json.JSONDecodeError) as error:
        subprocess.run(
            ["git", "update-ref", "-d", "refs/autodev/campaigns/CAMP-001/current"],
            cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        return ProjectOperation("INFRA_FAILURE", f"migration failed: {error}", {"migration_id": migration_id})
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)


def rollback_migration(root: Path, migration_id: str) -> ProjectOperation:
    root = root.resolve()
    canonical = root / ".autodev"
    try:
        metadata = json.loads((canonical / "migrations" / f"{migration_id}.json").read_text(encoding="utf-8"))
        state = json.loads((canonical / "state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return ProjectOperation("INVALID", f"cannot read migration: {error}", {})
    runs = canonical / "runs"
    if state.get("revision") != metadata.get("initial_revision") or (runs.exists() and any(runs.iterdir())):
        return ProjectOperation("BLOCKED", "rollback refused after V2 state progress or runs", {})
    frozen = root / metadata["legacy_frozen_path"]
    if not frozen.is_dir() or (root / ".agent").exists():
        return ProjectOperation("BLOCKED", "legacy backup is unavailable or .agent already exists", {})
    tombstone = root / f".autodev.rollback-{uuid.uuid4().hex}"
    try:
        os.replace(canonical, tombstone)
        os.replace(frozen, root / ".agent")
        for relative in metadata.get("framework_files", []):
            source = tombstone / "migrations" / migration_id / "framework-backup" / relative
            destination = root / relative
            if source.exists() and not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, destination)
        shutil.rmtree(tombstone)
    except OSError as error:
        return ProjectOperation("INFRA_FAILURE", f"rollback failed: {error}", {})
    subprocess.run(
        ["git", "update-ref", "-d", "refs/autodev/campaigns/CAMP-001/current"],
        cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    return ProjectOperation("SUCCESS", "migration rolled back", {"migration_id": migration_id})


def _markdown_requirement_baseline(path: Path, campaign_id: str) -> dict[str, Any]:
    requirements: list[dict[str, str]] = []
    header = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells == ["ID", "Priority", "Requirement", "Acceptance signal", "Status"]:
            header = True
            continue
        if not header or len(cells) != 5 or all(set(cell) <= {"-", ":"} for cell in cells):
            continue
        requirement_id, priority, statement, acceptance_signal, _status = cells
        if re.fullmatch(r"REQ-[0-9]{3,}", requirement_id):
            requirements.append({
                "id": requirement_id, "priority": priority, "statement": statement,
                "acceptance_signal": acceptance_signal,
            })
    if not requirements:
        raise ValueError("V2 Markdown contains no importable requirements")
    return {
        "$schema": "https://autodev.local/schemas/requirements.schema.json",
        "schema_version": 1, "campaign_id": campaign_id, "requirements": requirements,
    }


def check_v2_migration(root: Path) -> ProjectOperation:
    """Inspect an existing V2 canonical tree without writing it."""

    root = root.resolve()
    canonical = root / ".autodev"
    try:
        state = json.loads((canonical / "state.json").read_text(encoding="utf-8"))
        config = json.loads((canonical / "config.json").read_text(encoding="utf-8"))
        requirements = _markdown_requirement_baseline(root / config["requirements_path"], "CAMP-001")
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as error:
        return ProjectOperation("INVALID", f"cannot inspect V2 project: {error}", {})
    if state.get("campaigns") is not None:
        return ProjectOperation("INVALID", "V3 Campaign state already exists", {})
    baseline = git_baseline_status(root)
    if not baseline["has_head"]:
        return ProjectOperation("BLOCKED", baseline["error"] or "Git HEAD is missing", baseline)
    dirty = not baseline["clean"]
    fingerprint = source_fingerprint(root).digest if dirty else None
    return ProjectOperation("SUCCESS", "V2 migration is applicable", {
        "applicable": True, "v2_status": state.get("project_status"),
        "requirements": len(requirements["requirements"]),
        "dirty_paths": baseline["dirty_paths"], "adopt_source_fingerprint": fingerprint,
    })


def apply_v2_migration(root: Path, *, adopt_source: str | None = None) -> ProjectOperation:
    """Upgrade V2 in place, preserving a recoverable frozen canonical backup."""

    root = root.resolve()
    report = check_v2_migration(root)
    if report.status != "SUCCESS":
        return report
    required_fingerprint = report.data.get("adopt_source_fingerprint")
    if required_fingerprint and adopt_source != required_fingerprint:
        return ProjectOperation("BLOCKED", "dirty V2 source requires exact --adopt-source fingerprint", {
            "required_fingerprint": required_fingerprint,
        })
    migration_id = f"v2-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    canonical = root / ".autodev"
    backup = root / f".autodev.v2-frozen-{migration_id}"
    try:
        shutil.copytree(canonical, backup)
        state = json.loads((canonical / "state.json").read_text(encoding="utf-8"))
        config = json.loads((canonical / "config.json").read_text(encoding="utf-8"))
        requirements = _markdown_requirement_baseline(root / config["requirements_path"], "CAMP-001")
        workspace = CampaignWorkspace(root, "CAMP-001")
        initial = workspace.initialize("HEAD")
        checkpoint = initial
        if report.data["dirty_paths"]:
            worktree = workspace.create_task_workspace(f"MIGRATE-{migration_id}")
            try:
                for relative in report.data["dirty_paths"]:
                    source = root / relative
                    destination = worktree / relative
                    if source.is_file() or source.is_symlink():
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        if source.is_symlink():
                            destination.symlink_to(os.readlink(source))
                        else:
                            shutil.copy2(source, destination)
                    elif not source.exists() and destination.exists():
                        destination.unlink()
                adopted = workspace.checkpoint(
                    worktree, task_id="TASK-MIGRATION", run_id=f"MIGRATE-{migration_id}",
                )
                checkpoint = adopted.commit
                workspace.finalize_checkpoint(adopted, canonical_revision=state["revision"])
                baseline_path = canonical / "campaigns/CAMP-001/workspace-baseline.json"
                baseline_document = json.loads(baseline_path.read_text(encoding="utf-8"))
                baseline_document["last_materialized_commit"] = checkpoint
                _write = json.dumps(baseline_document, indent=2, sort_keys=True) + "\n"
                baseline_path.write_text(_write, encoding="utf-8")
            finally:
                workspace.remove_task_workspace(worktree)
        target_reached = state.get("project_status") == "COMPLETE"
        proposed_at = _now()
        proposal_payload = {
            "idea": "Continue the migrated V2 project.", "mode": "CHANGE",
            "target": "CHANGE_COMPLETE", "requirements": requirements,
            "authority_envelope": default_authority_envelope(), "phases": ["IMPLEMENT"],
        }
        campaign = {
            "$schema": "https://autodev.local/schemas/campaign.schema.json",
            "schema_version": 1, "id": "CAMP-001", "idea": proposal_payload["idea"],
            "mode": "CHANGE", "target": "CHANGE_COMPLETE", "autonomy": "HUMAN_ON_BLOCKED",
            "requirements_hash": hashlib.sha256(json.dumps(
                requirements, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ).encode()).hexdigest(),
            "authority_envelope": proposal_payload["authority_envelope"], "phases": ["IMPLEMENT"],
            "proposal_hash": hashlib.sha256(json.dumps(
                proposal_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ).encode()).hexdigest(),
            "parent_campaign_id": None, "source_checkpoint": initial,
            "proposed_at": proposed_at, "approved_at": proposed_at,
        }
        campaign_dir = canonical / "campaigns/CAMP-001"
        (campaign_dir / "requirements.json").write_text(_dump(requirements), encoding="utf-8")
        (campaign_dir / "campaign.json").write_text(_dump(campaign), encoding="utf-8")
        router = QualityRouter()
        control = ControlPlane(root)
        for task_id, record in state.get("tasks", {}).items():
            path = canonical / "tasks" / task_id / "contract.json"
            contract = json.loads(path.read_text(encoding="utf-8"))
            contract.update(
                campaign_id="CAMP-001", phase="IMPLEMENT", admission="HUMAN_APPROVED",
                review_scope=router.decide(contract).value,
            )
            path.write_text(_dump(contract), encoding="utf-8")
            if record.get("contract_hash") is not None:
                contract_hash = hashlib.sha256(json.dumps(
                    contract, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                ).encode()).hexdigest()
                record["contract_hash"] = contract_hash
                projection = canonical / "tasks" / task_id / "contract.md"
                projection.chmod(0o600) if projection.exists() else None
                projection.write_text(control._contract_projection(contract, contract_hash), encoding="utf-8")
                projection.chmod(0o444)
        state["framework_version"] = __version__
        state["current_campaign_id"] = None if target_reached else "CAMP-001"
        state["campaigns"] = {"CAMP-001": {
            "status": "TARGET_REACHED" if target_reached else "ACTIVE",
            "phase": "TARGET_REACHED" if target_reached else "IMPLEMENT",
            "mode": "CHANGE", "target": "CHANGE_COMPLETE", "proposal_hash": campaign["proposal_hash"],
            "checkpoint": checkpoint, "last_materialized_checkpoint": checkpoint, "approved_at": proposed_at,
        }}
        state["project_status"] = "IDLE" if target_reached else "ACTIVE"
        state["next_owner"] = "HUMAN" if target_reached else "COMMANDER"
        state["next_action"] = (
            "Review or archive the migrated Campaign." if target_reached
            else "Continue the migrated CHANGE Campaign."
        )
        (canonical / "state.json").write_text(_dump(state), encoding="utf-8")
        metadata = {
            "migration_id": migration_id, "kind": "v2-to-v3", "applied_at": proposed_at,
            "backup_path": backup.name, "initial_revision": state["revision"],
            "campaign_id": "CAMP-001", "adopted_source_fingerprint": required_fingerprint,
        }
        (canonical / "migrations").mkdir(exist_ok=True)
        (canonical / "migrations" / f"{migration_id}.json").write_text(_dump(metadata), encoding="utf-8")
        validation = ControlPlane(root).execute(Command("validate"))
        if validation.status != "SUCCESS":
            raise RuntimeError("; ".join(validation.data.get("errors", [validation.message])))
        return ProjectOperation("SUCCESS", "V2 project migrated to V3", {
            "migration_id": migration_id, "campaign_id": "CAMP-001", "checkpoint": checkpoint,
        })
    except (OSError, RuntimeError, json.JSONDecodeError) as error:
        subprocess.run(
            ["git", "update-ref", "-d", "refs/autodev/campaigns/CAMP-001/current"],
            cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        if backup.exists():
            failed = root / f".autodev.v3-failed-{uuid.uuid4().hex}"
            if canonical.exists():
                os.replace(canonical, failed)
            shutil.copytree(backup, canonical)
            shutil.rmtree(failed, ignore_errors=True)
        return ProjectOperation("INFRA_FAILURE", f"V2 migration failed: {error}", {"migration_id": migration_id})


def rollback_v2_migration(root: Path, migration_id: str) -> ProjectOperation:
    root = root.resolve()
    canonical = root / ".autodev"
    try:
        metadata = json.loads((canonical / "migrations" / f"{migration_id}.json").read_text(encoding="utf-8"))
        state = json.loads((canonical / "state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return ProjectOperation("INVALID", f"cannot read V2 migration: {error}", {})
    if metadata.get("kind") != "v2-to-v3":
        return ProjectOperation("INVALID", "migration is not V2-to-V3", {})
    if state.get("revision") != metadata.get("initial_revision"):
        return ProjectOperation("BLOCKED", "rollback refused after first V3 state progress", {})
    backup = root / metadata["backup_path"]
    if not backup.is_dir():
        return ProjectOperation("BLOCKED", "V2 frozen backup is unavailable", {})
    tombstone = root / f".autodev.v3-rollback-{uuid.uuid4().hex}"
    try:
        try:
            CampaignWorkspace(root, metadata["campaign_id"]).archive(results_materialized=True)
        except Exception:
            pass
        os.replace(canonical, tombstone)
        os.replace(backup, canonical)
        shutil.rmtree(tombstone)
    except OSError as error:
        return ProjectOperation("INFRA_FAILURE", f"V2 rollback failed: {error}", {})
    return ProjectOperation("SUCCESS", "V2 migration rolled back", {"migration_id": migration_id})


def _v3_asset_hashes(canonical: Path) -> dict[str, str]:
    preserved_roots = ("campaigns", "tasks", "runs")
    return {
        str(path.relative_to(canonical)): _sha256(path)
        for name in preserved_roots
        for path in sorted((canonical / name).rglob("*"))
        if path.is_file()
    }


def _campaign_refs(root: Path) -> dict[str, str]:
    process = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname) %(objectname)", "refs/autodev/campaigns"],
        cwd=root, capture_output=True, text=True, check=False,
    )
    if process.returncode:
        raise RuntimeError(process.stderr.strip() or "cannot inspect Campaign refs")
    return dict(line.split(" ", 1) for line in process.stdout.splitlines() if " " in line)


def check_v3_migration(root: Path) -> ProjectOperation:
    """Read-only applicability and preservation report for V3-to-V4."""

    root = root.resolve()
    canonical = root / ".autodev"
    try:
        state = json.loads((canonical / "state.json").read_text(encoding="utf-8"))
        manifest = json.loads((canonical / "manifest.json").read_text(encoding="utf-8"))
        refs = _campaign_refs(root)
        assets = _v3_asset_hashes(canonical)
    except (OSError, json.JSONDecodeError, RuntimeError) as error:
        return ProjectOperation("INVALID", f"cannot inspect V3 project: {error}", {})
    version = str(state.get("framework_version", manifest.get("framework_version", "")))
    already_v4 = "current_action_id" in state or "pause_requested" in state
    if not version.startswith("3.") or already_v4:
        return ProjectOperation("INVALID", "V3 migration is not applicable", {})
    return ProjectOperation("SUCCESS", "V3 migration is applicable", {
        "applicable": True,
        "framework_version": version,
        "asset_hashes": assets,
        "campaign_refs": refs,
    })


def apply_v3_migration(root: Path) -> ProjectOperation:
    """Stage, validate, and atomically install V4 Action state."""

    root = root.resolve()
    checked = check_v3_migration(root)
    if checked.status != "SUCCESS":
        return checked
    migration_id = f"v3-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    canonical = root / ".autodev"
    backup = root / f".autodev.v3-frozen-{migration_id}"
    staging = root / f".autodev.v4-staging-{migration_id}"
    replaced = root / f".autodev.v3-replaced-{migration_id}"
    try:
        shutil.copytree(canonical, backup)
        shutil.copytree(canonical, staging)
        state = json.loads((staging / "state.json").read_text(encoding="utf-8"))
        manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
        state.update(
            framework_version=__version__, current_action_id=None, pause_requested=False,
        )
        manifest["framework_version"] = __version__
        (staging / "actions").mkdir(exist_ok=True)
        gitignore = staging / ".gitignore"
        ignored = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
        if "actions/*\n" not in ignored:
            ignored += "actions/*\n"
        gitignore.write_text(ignored, encoding="utf-8")
        (staging / "state.json").write_text(_dump(state), encoding="utf-8")
        (staging / "manifest.json").write_text(_dump(manifest), encoding="utf-8")
        if _v3_asset_hashes(staging) != checked.data["asset_hashes"]:
            raise RuntimeError("staging changed a preserved V3 asset")
        if _campaign_refs(root) != checked.data["campaign_refs"]:
            raise RuntimeError("staging changed a Campaign private ref")
        os.replace(canonical, replaced)
        os.replace(staging, canonical)
        metadata = {
            "migration_id": migration_id,
            "kind": "v3-to-v4",
            "applied_at": _now(),
            "backup_path": backup.name,
            "asset_hashes": checked.data["asset_hashes"],
            "campaign_refs": checked.data["campaign_refs"],
            "v4_progress_started": False,
        }
        _write_json_atomic(canonical / "migrations" / f"{migration_id}.json", metadata)
        validation = ControlPlane(root).execute(Command("validate"))
        if validation.status != "SUCCESS":
            raise RuntimeError("; ".join(validation.data.get("errors", [validation.message])))
        shutil.rmtree(replaced)
        return ProjectOperation("SUCCESS", "V3 project migrated to V4", {
            "migration_id": migration_id,
            "preserved_assets": len(checked.data["asset_hashes"]),
            "preserved_refs": len(checked.data["campaign_refs"]),
        })
    except (OSError, RuntimeError, json.JSONDecodeError) as error:
        if replaced.exists():
            if canonical.exists():
                failed = root / f".autodev.v4-failed-{uuid.uuid4().hex}"
                os.replace(canonical, failed)
                shutil.rmtree(failed, ignore_errors=True)
            os.replace(replaced, canonical)
        shutil.rmtree(staging, ignore_errors=True)
        return ProjectOperation(
            "INFRA_FAILURE", f"V3 migration failed: {error}", {"migration_id": migration_id},
        )


def rollback_v3_migration(root: Path, migration_id: str) -> ProjectOperation:
    """Restore the frozen V3 canonical tree unless any V4 Action has existed."""

    root = root.resolve()
    canonical = root / ".autodev"
    try:
        metadata = json.loads(
            (canonical / "migrations" / f"{migration_id}.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as error:
        return ProjectOperation("INVALID", f"cannot read V3 migration: {error}", {})
    if metadata.get("kind") != "v3-to-v4":
        return ProjectOperation("INVALID", "migration is not V3-to-V4", {})
    action_created = metadata.get("v4_progress_started") or any(
        path.is_file() for path in (canonical / "actions").glob("*/action.json")
    )
    if action_created:
        return ProjectOperation(
            "BLOCKED", "rollback permanently refused after the first V4 Action", {},
        )
    backup = root / str(metadata.get("backup_path", ""))
    if not backup.is_dir():
        return ProjectOperation("BLOCKED", "V3 frozen backup is unavailable", {})
    if _campaign_refs(root) != metadata.get("campaign_refs"):
        return ProjectOperation("BLOCKED", "Campaign private refs changed after migration", {})
    tombstone = root / f".autodev.v4-rollback-{uuid.uuid4().hex}"
    try:
        os.replace(canonical, tombstone)
        shutil.copytree(backup, canonical)
        shutil.rmtree(tombstone)
    except OSError as error:
        if not canonical.exists() and tombstone.exists():
            os.replace(tombstone, canonical)
        return ProjectOperation("INFRA_FAILURE", f"V3 rollback failed: {error}", {})
    return ProjectOperation("SUCCESS", "V3 migration rolled back", {"migration_id": migration_id})
