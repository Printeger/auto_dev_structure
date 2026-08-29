"""Project installation and explicit V1-to-V2 migration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autodev import __version__
from autodev._resources import _read_text
from autodev.control_plane import Command, ControlPlane


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
            "locks/*\n"
            "workspaces/*\n"
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
    for name in ("tasks", "runs", "events", "locks", "workspaces", "migrations")
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
    """Install only V2 contracts/state/templates after a full conflict preflight."""

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
        return ProjectOperation("SUCCESS", "migration applied", {"migration_id": migration_id})
    except OSError as error:
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
    return ProjectOperation("SUCCESS", "migration rolled back", {"migration_id": migration_id})
