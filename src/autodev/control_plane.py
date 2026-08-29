"""The sole mutation boundary for AutoDev's canonical project and campaign state."""

from __future__ import annotations

import copy
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from autodev._resources import _read_text
from autodev.quality import validate_debt


_RESULT_EXIT_CODES = {
    "SUCCESS": 0,
    "INVALID": 1,
    "NOT_READY": 2,
    "BLOCKED": 3,
    "STOPPED": 4,
    "INFRA_FAILURE": 5,
}

_PROJECT_TRANSITIONS = {
    "BOOTSTRAP": frozenset({"IDLE", "ACTIVE", "FAILED"}),
    "IDLE": frozenset({"ACTIVE", "BLOCKED", "FAILED", "STOPPED"}),
    "ACTIVE": frozenset({"IDLE", "PAUSED", "BLOCKED", "COMPLETE", "FAILED", "STOPPED"}),
    "PAUSED": frozenset({"IDLE", "ACTIVE", "BLOCKED", "FAILED", "STOPPED"}),
    "BLOCKED": frozenset({"IDLE", "ACTIVE", "FAILED", "STOPPED"}),
    "COMPLETE": frozenset(),
    "FAILED": frozenset(),
    "STOPPED": frozenset({"ACTIVE", "FAILED"}),
}

_TASK_TRANSITIONS = {
    "DRAFT": frozenset({"READY", "CANCELLED"}),
    "READY": frozenset({"CLAIMED", "DEFERRED", "BLOCKED", "CANCELLED"}),
    "CLAIMED": frozenset({"RUNNING", "READY", "BLOCKED", "CANCELLED"}),
    "RUNNING": frozenset({"VALIDATING", "READY", "BLOCKED", "CANCELLED"}),
    "VALIDATING": frozenset({"REVIEWING", "ACCEPTED", "READY", "BLOCKED"}),
    "REVIEWING": frozenset({"ACCEPTED", "READY", "BLOCKED"}),
    "ACCEPTED": frozenset(),
    "DEFERRED": frozenset({"READY", "CANCELLED"}),
    "BLOCKED": frozenset({"READY", "CANCELLED"}),
    "CANCELLED": frozenset(),
}

_TASK_ID = re.compile(r"^TASK-[0-9]{3,}$")
_REQ_ID = re.compile(r"^REQ-[0-9]{3,}$")
_CAMPAIGN_ID = re.compile(r"^CAMP-[0-9]{3,}$")
_GLOBAL_WRITE_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class Command:
    """One request to the canonical control plane."""

    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    expected_revision: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))


@dataclass(frozen=True, slots=True)
class CommandResult:
    """A stable result whose status determines the CLI exit code."""

    status: str
    message: str
    revision: int | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in _RESULT_EXIT_CODES:
            raise ValueError(f"unknown command result status: {self.status}")
        object.__setattr__(self, "data", MappingProxyType(dict(self.data)))

    @property
    def exit_code(self) -> int:
        return _RESULT_EXIT_CODES[self.status]

    def to_dict(self) -> dict[str, Any]:
        return {
            "$schema": "https://autodev.local/schemas/command-result.schema.json",
            "schema_version": 1,
            "status": self.status,
            "exit_code": self.exit_code,
            "message": self.message,
            "revision": self.revision,
            "data": dict(self.data),
        }


class _CommandFailure(Exception):
    def __init__(self, status: str, message: str, *, data: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.data = data or {}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_replace_json(path: Path, value: Any) -> None:
    _atomic_replace_bytes(path, _json_bytes(value))


def _atomic_replace_bytes(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class ControlPlane:
    """Validate, query, and atomically mutate one project's canonical state."""

    def __init__(self, project_root: str | os.PathLike[str]) -> None:
        self.root = Path(project_root).resolve()
        self.canonical = self.root / ".autodev"
        self._schemas: dict[str, dict[str, Any]] = {}

    def execute(self, command: Command) -> CommandResult:
        """Execute one command without leaking transition exceptions to the CLI."""

        try:
            if command.name == "validate":
                return self._validate_result(ready=bool(command.arguments.get("ready", False)))
            if command.name == "status":
                return self._status()
            if command.name == "activate":
                return self._activate(command)
            if command.name == "complete":
                return self._complete(command)
            if command.name == "project.transition":
                return self._project_transition(command)
            if command.name == "task.create":
                return self._task_create(command)
            if command.name == "task.ready":
                return self._task_ready(command)
            if command.name == "task.show":
                return self._task_show(command)
            if command.name == "task.reopen":
                return self._task_reopen(command)
            if command.name == "task.transition":
                return self._task_transition(command)
            if command.name == "run.claim":
                return self._run_claim(command)
            if command.name == "run.phase":
                return self._run_phase(command)
            if command.name == "run.finish":
                return self._run_finish(command)
            if command.name == "validation.record":
                return self._record_full_validation(command)
            if command.name == "campaign.propose":
                return self._campaign_propose(command)
            if command.name == "campaign.approve":
                return self._campaign_approve(command)
            if command.name == "campaign.transition":
                return self._campaign_transition(command)
            if command.name == "campaign.checkpoint":
                return self._campaign_checkpoint(command)
            if command.name == "campaign.materialized":
                return self._campaign_materialized(command)
            if command.name == "campaign.retarget":
                return self._campaign_retarget(command)
            if command.name == "task.admit_batch":
                return self._task_admit_batch(command)
            if command.name == "action.create":
                return self._action_create(command)
            if command.name == "action.resolve":
                return self._action_resolve(command)
            if command.name == "action.pause":
                return self._action_pause(command)
            if command.name == "action.continue":
                return self._action_continue(command)
            raise _CommandFailure("INVALID", f"unknown command: {command.name}")
        except _CommandFailure as error:
            revision = self._best_effort_revision()
            return CommandResult(error.status, error.message, revision, error.data)
        except (OSError, json.JSONDecodeError) as error:
            return CommandResult("INFRA_FAILURE", f"canonical infrastructure failure: {error}", self._best_effort_revision())

    def _schema(self, name: str) -> dict[str, Any]:
        if name not in self._schemas:
            self._schemas[name] = json.loads(_read_text(f"schemas/{name}.schema.json"))
        return self._schemas[name]

    def _validate_document(self, schema_name: str, document: Any, label: str) -> list[str]:
        validator = Draft202012Validator(self._schema(schema_name), format_checker=FormatChecker())
        errors = sorted(
            validator.iter_errors(document),
            key=lambda item: tuple(str(part) for part in item.absolute_path),
        )
        return [f"{label}{self._error_path(error.absolute_path)}: {error.message}" for error in errors]

    @staticmethod
    def _error_path(parts: Any) -> str:
        rendered = "".join(f"[{part!r}]" for part in parts)
        return rendered

    @staticmethod
    def _read_json(path: Path) -> Any:
        with path.open("r", encoding="utf-8") as stream:
            return json.load(stream)

    def _state(self) -> dict[str, Any]:
        value = self._read_json(self.canonical / "state.json")
        if not isinstance(value, dict):
            raise _CommandFailure("INVALID", "state.json must contain an object")
        return value

    def _best_effort_revision(self) -> int | None:
        try:
            revision = self._state().get("revision")
            return revision if isinstance(revision, int) else None
        except Exception:
            return None

    def _validated_tree(self) -> tuple[dict[str, Any], list[dict[str, str]], list[str]]:
        errors: list[str] = []
        documents: dict[str, Any] = {}
        valid_documents: set[str] = set()
        for basename, schema_name in (
            ("manifest.json", "manifest"),
            ("config.json", "config"),
            ("policy.json", "policy"),
            ("state.json", "state"),
        ):
            path = self.canonical / basename
            try:
                document = self._read_json(path)
            except (OSError, json.JSONDecodeError) as error:
                errors.append(f"{basename}: {error}")
                continue
            documents[basename] = document
            schema_errors = self._validate_document(schema_name, document, basename)
            errors.extend(schema_errors)
            if not schema_errors:
                valid_documents.add(basename)

        state_document = documents.get("state.json")
        state = state_document if isinstance(state_document, dict) else {}
        is_v3 = "state.json" in valid_documents and "campaigns" in state
        requirements: list[dict[str, str]] = []
        # A V3 Campaign's JSON baseline is the sole requirement source. The
        # configured Markdown table remains mandatory only for legacy V2 state.
        requirements_valid = is_v3
        config = documents.get("config.json")
        if not is_v3 and "config.json" in valid_documents and isinstance(config, dict):
            try:
                requirements_path = self._project_relative(config["requirements_path"])
                requirements = self._parse_requirements(requirements_path)
                requirements_valid = True
            except (KeyError, OSError, _CommandFailure) as error:
                errors.append(str(error))

        if "state.json" in valid_documents:
            errors.extend(self._validate_committed_events(state))
            errors.extend(self._validate_campaigns(state))
            for campaign_id in state.get("campaigns", {}):
                try:
                    campaign_requirements = self._read_json(
                        self.canonical / "campaigns" / campaign_id / "requirements.json"
                    )["requirements"]
                except (OSError, json.JSONDecodeError, KeyError, TypeError):
                    continue
                known_ids = {item["id"] for item in requirements}
                for item in campaign_requirements:
                    if item.get("id") not in known_ids:
                        requirements.append({
                            "id": item["id"], "priority": item["priority"],
                            "status": "PROPOSED", "acceptance_signal": item["acceptance_signal"],
                        })
                        known_ids.add(item["id"])
            policy = documents.get("policy.json")
            if "policy.json" in valid_documents and isinstance(policy, dict):
                errors.extend(self._validate_tasks(state, policy))
            if requirements_valid:
                errors.extend(self._state_invariant_errors(state, requirements))
        return state, requirements, errors

    def _validate_campaigns(self, state: Mapping[str, Any]) -> list[str]:
        errors: list[str] = []
        campaigns = state.get("campaigns", {})
        if not isinstance(campaigns, Mapping):
            return errors
        for campaign_id, record in campaigns.items():
            directory = self.canonical / "campaigns" / campaign_id
            try:
                contract = self._read_json(directory / "campaign.json")
                requirements = self._read_json(directory / "requirements.json")
            except (OSError, json.JSONDecodeError) as error:
                errors.append(f"{campaign_id}: {error}")
                continue
            errors.extend(self._validate_document("campaign", contract, f"{campaign_id}/campaign.json"))
            errors.extend(self._validate_document("requirements", requirements, f"{campaign_id}/requirements.json"))
            if not isinstance(contract, dict) or not isinstance(requirements, dict):
                continue
            if contract.get("id") != campaign_id or requirements.get("campaign_id") != campaign_id:
                errors.append(f"{campaign_id}: campaign identity mismatch")
            if contract.get("requirements_hash") != _canonical_hash(requirements):
                errors.append(f"{campaign_id}: requirement baseline hash mismatch")
            requirement_items = requirements.get("requirements", [])
            if isinstance(requirement_items, list):
                identifiers = [
                    item.get("id") for item in requirement_items if isinstance(item, Mapping)
                ]
                if len(identifiers) != len(set(identifiers)):
                    errors.append(f"{campaign_id}: duplicate requirement IDs")
            for field_name in ("mode", "target", "proposal_hash", "approved_at"):
                if record.get(field_name) != contract.get(field_name):
                    errors.append(f"{campaign_id}: state {field_name} does not match frozen contract")
            if record.get("status") not in {"PROPOSED", "WAITING_FOR_HUMAN"} and not contract.get("approved_at"):
                errors.append(f"{campaign_id}: active campaign has no approval time")
        current = state.get("current_campaign_id")
        if current is not None and current not in campaigns:
            errors.append("state.json: current_campaign_id is unknown")
        return errors

    def _validate_result(self, *, ready: bool) -> CommandResult:
        state, requirements, errors = self._validated_tree()
        candidate_revision = state.get("revision")
        revision = candidate_revision if isinstance(candidate_revision, int) else None
        if errors:
            return CommandResult("INVALID", "canonical project is invalid", revision, {"valid": False, "ready": False, "errors": errors})
        is_ready = state["project_status"] == "ACTIVE"
        data = {
            "valid": True,
            "ready": is_ready,
            "revision": revision,
            "project_status": state["project_status"],
            "requirements": requirements,
        }
        if ready and not is_ready:
            return CommandResult("NOT_READY", "project is valid but not active", revision, data)
        return CommandResult("SUCCESS", "canonical project is valid", revision, data)

    def _status(self) -> CommandResult:
        validation = self._validate_result(ready=False)
        if validation.status != "SUCCESS":
            return validation
        state = self._state()
        return CommandResult(
            self._status_for_project(state["project_status"]),
            f"project is {state['project_status']}",
            state["revision"],
            {
                "project_status": state["project_status"],
                "current_task_id": state["current_task_id"],
                "current_run_id": state["current_run_id"],
                "blocker": state["blocker"],
                "next_owner": state["next_owner"],
                "next_action": state["next_action"],
            },
        )

    @staticmethod
    def _status_for_project(project_status: str) -> str:
        return {
            "PAUSED": "NOT_READY",
            "BLOCKED": "BLOCKED",
            "STOPPED": "STOPPED",
            "FAILED": "INFRA_FAILURE",
        }.get(project_status, "SUCCESS")

    def _activate(self, command: Command) -> CommandResult:
        state, _, errors = self._validated_tree()
        if errors:
            raise _CommandFailure("INVALID", "cannot activate an invalid project", data={"errors": errors})
        if state["project_status"] not in {"BOOTSTRAP", "PAUSED", "BLOCKED", "STOPPED"}:
            raise _CommandFailure("INVALID", f"cannot activate project from {state['project_status']}")
        return self._mutate(
            command,
            lambda current: self._set_project_status(current, "ACTIVE", next_action="Select a READY Task."),
        )

    def _project_transition(self, command: Command) -> CommandResult:
        target = command.arguments.get("to")
        if not isinstance(target, str):
            raise _CommandFailure("INVALID", "project transition requires a string 'to' state")
        if target == "COMPLETE":
            return self._complete(command)

        def transform(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            source = state["project_status"]
            if target not in _PROJECT_TRANSITIONS[source]:
                raise _CommandFailure("INVALID", f"illegal Project transition: {source} -> {target}")
            return self._set_project_status(
                state,
                target,
                blocker=command.arguments.get("blocker"),
                next_action=command.arguments.get("next_action"),
            )

        return self._mutate(command, transform)

    def _set_project_status(
        self,
        state: dict[str, Any],
        target: str,
        *,
        blocker: Any = None,
        next_action: Any = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        source = state["project_status"]
        if target not in _PROJECT_TRANSITIONS[source]:
            raise _CommandFailure("INVALID", f"illegal Project transition: {source} -> {target}")
        state["project_status"] = target
        if target == "ACTIVE":
            state["blocker"] = None
            state["next_owner"] = "COMMANDER"
        elif target == "BLOCKED":
            if not isinstance(blocker, str) or not blocker.strip():
                raise _CommandFailure("INVALID", "BLOCKED requires a non-empty blocker")
            if not isinstance(next_action, str) or not next_action.strip():
                raise _CommandFailure("INVALID", "BLOCKED requires a non-empty next_action")
            state["blocker"] = blocker.strip()
            state["next_owner"] = "HUMAN"
        elif target == "COMPLETE":
            state["next_owner"] = "NONE"
            state["next_action"] = None
        state["next_action"] = next_action.strip() if isinstance(next_action, str) else state["next_action"]
        return state, {"from": source, "to": target}

    def _task_create(self, command: Command) -> CommandResult:
        arguments = command.arguments
        task_id = arguments.get("id")
        title = arguments.get("title")
        requirements = arguments.get("requirements")
        risk = arguments.get("risk")
        quality_mode = arguments.get("quality_mode")
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            raise _CommandFailure("INVALID", "Task id must match TASK-NNN")
        if not isinstance(title, str) or not title.strip():
            raise _CommandFailure("INVALID", "Task title must be non-empty")
        if not isinstance(requirements, list) or any(not isinstance(item, str) for item in requirements):
            raise _CommandFailure("INVALID", "Task requirements must be a list of IDs")
        now = _utc_now()
        contract = {
            "$schema": "https://autodev.local/schemas/task-contract.schema.json",
            "schema_version": 1,
            "id": task_id,
            "title": title.strip(),
            "objective": "",
            "requirements": requirements,
            "dependencies": [],
            "priority": "MUST",
            "blocking": True,
            "risk": risk,
            "quality_mode": quality_mode,
            "change_classes": [],
            "allowed_paths": [],
            "out_of_scope": [],
            "acceptance_criteria": [],
            "validation_commands": [],
            "prohibited_actions": [],
            "created_at": now,
        }
        schema_errors = self._validate_document("task-contract", contract, "contract.json")
        if schema_errors:
            raise _CommandFailure("INVALID", "invalid Task draft", data={"errors": schema_errors})

        def transform(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            if task_id in state["tasks"]:
                raise _CommandFailure("INVALID", f"Task already exists: {task_id}")
            state["tasks"][task_id] = {
                "status": "DRAFT",
                "generation": 1,
                "contract_hash": None,
                "claim_id": None,
                "evidence_ids": [],
                "blocking": True,
                "requirement_ids": list(requirements),
                "created_at": now,
                "updated_at": now,
            }
            return state, {"task_id": task_id, "status": "DRAFT"}

        return self._mutate(
            command,
            transform,
            prepare=lambda: _atomic_replace_json(self.canonical / "tasks" / task_id / "contract.json", contract),
        )

    def _task_transition(self, command: Command) -> CommandResult:
        task_id = command.arguments.get("id")
        target = command.arguments.get("to")
        if not isinstance(task_id, str) or not isinstance(target, str):
            raise _CommandFailure("INVALID", "task transition requires 'id' and 'to'")
        if target == "READY":
            state, _, errors = self._validated_tree()
            if errors:
                raise _CommandFailure(
                    "INVALID", "canonical project is invalid", data={"errors": errors}
                )
            current = state["tasks"].get(task_id)
            if current is not None and current.get("status") == "DRAFT":
                return self._task_ready(
                    Command("task.ready", {"id": task_id}, command.expected_revision)
                )

        def transform(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            record = state["tasks"].get(task_id)
            if record is None:
                raise _CommandFailure("INVALID", f"unknown Task: {task_id}")
            source = record["status"]
            if target not in _TASK_TRANSITIONS[source]:
                raise _CommandFailure("INVALID", f"illegal Task transition: {source} -> {target}")
            record["status"] = target
            record["updated_at"] = _utc_now()
            return state, {"task_id": task_id, "from": source, "to": target}

        return self._mutate(command, transform)

    def _task_ready(self, command: Command) -> CommandResult:
        task_id = command.arguments.get("id")
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            raise _CommandFailure("INVALID", "task ready requires a valid Task id")
        holder: dict[str, Any] = {}

        def transform(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            record = state["tasks"].get(task_id)
            if record is None:
                raise _CommandFailure("INVALID", f"unknown Task: {task_id}")
            if record["status"] != "DRAFT":
                raise _CommandFailure("INVALID", f"Task {task_id} can become READY only from DRAFT")
            contract_path = self.canonical / "tasks" / task_id / "contract.json"
            try:
                contract = self._read_json(contract_path)
            except (OSError, json.JSONDecodeError) as error:
                raise _CommandFailure("INVALID", f"invalid {task_id}/contract.json: {error}") from error
            errors = self._validate_document("task-contract", contract, f"{task_id}/contract.json")
            policy = self._read_json(self.canonical / "policy.json")
            if not errors:
                errors.extend(self._ready_contract_errors(contract, policy))
            if contract.get("id") != task_id:
                errors.append(f"{task_id}/contract.json: id does not match Task")
            if contract.get("campaign_id") is not None:
                errors.append("contract.json: Campaign Tasks must be admitted atomically by ControlPlane")
            known_requirements = {
                item["id"]
                for item in self._parse_requirements(
                    self._project_relative(self._read_json(self.canonical / "config.json")["requirements_path"])
                )
            }
            unknown_requirements = sorted(set(contract.get("requirements", [])) - known_requirements)
            if unknown_requirements:
                errors.append(f"contract.json: unknown requirement IDs: {', '.join(unknown_requirements)}")
            unknown_dependencies = sorted(set(contract.get("dependencies", [])) - set(state["tasks"]))
            if unknown_dependencies:
                errors.append(f"contract.json: unknown dependency IDs: {', '.join(unknown_dependencies)}")
            if task_id in contract.get("dependencies", []):
                errors.append("contract.json: a Task cannot depend on itself")
            if errors:
                raise _CommandFailure("INVALID", f"Task {task_id} is not ready", data={"errors": errors})
            contract_hash = _canonical_hash(contract)
            holder.update(contract=contract, contract_hash=contract_hash)
            record.update(
                status="READY",
                contract_hash=contract_hash,
                blocking=contract["blocking"],
                requirement_ids=list(contract["requirements"]),
                updated_at=_utc_now(),
            )
            return state, {
                "task_id": task_id,
                "from": "DRAFT",
                "to": "READY",
                "generation": record["generation"],
                "contract_hash": contract_hash,
            }

        def prepare() -> None:
            projection = self._contract_projection(holder["contract"], holder["contract_hash"])
            _atomic_replace_bytes(
                self.canonical / "tasks" / task_id / "contract.md",
                projection.encode("utf-8"),
                mode=0o444,
            )

        return self._mutate(command, transform, prepare=prepare)

    def _task_reopen(self, command: Command) -> CommandResult:
        task_id = command.arguments.get("id")
        reason = command.arguments.get("reason")
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            raise _CommandFailure("INVALID", "task reopen requires a valid Task id")
        if not isinstance(reason, str) or not reason.strip():
            raise _CommandFailure("INVALID", "task reopen requires a non-empty reason")

        def transform(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            record = state["tasks"].get(task_id)
            if record is None:
                raise _CommandFailure("INVALID", f"unknown Task: {task_id}")
            source = record["status"]
            if source == "DRAFT" or record["contract_hash"] is None:
                raise _CommandFailure("INVALID", f"Task {task_id} is not frozen")
            invalidated_claim = record["claim_id"]
            invalidated_evidence = list(record["evidence_ids"])
            state["accepted_requirement_ids"] = sorted(
                set(state["accepted_requirement_ids"]) - set(record["requirement_ids"])
            )
            record.update(
                status="DRAFT",
                generation=record["generation"] + 1,
                contract_hash=None,
                claim_id=None,
                evidence_ids=[],
                updated_at=_utc_now(),
            )
            return state, {
                "task_id": task_id,
                "from": source,
                "to": "DRAFT",
                "reason": reason.strip(),
                "generation": record["generation"],
                "invalidated_claim_id": invalidated_claim,
                "invalidated_evidence_ids": invalidated_evidence,
            }

        def after_commit() -> None:
            try:
                (self.canonical / "tasks" / task_id / "contract.md").unlink()
                _fsync_directory(self.canonical / "tasks" / task_id)
            except OSError:
                pass

        return self._mutate(command, transform, after_commit=after_commit)

    def _task_show(self, command: Command) -> CommandResult:
        task_id = command.arguments.get("id")
        state, _, errors = self._validated_tree()
        if errors:
            raise _CommandFailure("INVALID", "canonical project is invalid", data={"errors": errors})
        record = state.get("tasks", {}).get(task_id)
        if record is None:
            raise _CommandFailure("INVALID", f"unknown Task: {task_id}")
        contract = self._read_json(self.canonical / "tasks" / str(task_id) / "contract.json")
        return CommandResult("SUCCESS", f"Task {task_id}", state["revision"], {"record": record, "contract": contract})

    def _run_claim(self, command: Command) -> CommandResult:
        task_id = command.arguments.get("task_id")
        run_id = command.arguments.get("run_id")
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            raise _CommandFailure("INVALID", "run claim requires a valid task_id")
        if not isinstance(run_id, str) or not run_id.strip():
            raise _CommandFailure("INVALID", "run claim requires a run_id")

        def transform(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            if state["project_status"] != "ACTIVE":
                raise _CommandFailure("NOT_READY", "project must be ACTIVE to claim work")
            if state["current_task_id"] is not None or state["current_run_id"] is not None:
                raise _CommandFailure("NOT_READY", "another Task or run is current")
            record = state["tasks"].get(task_id)
            if record is None or record["status"] != "READY":
                raise _CommandFailure("NOT_READY", f"Task {task_id} is not READY")
            record.update(status="CLAIMED", claim_id=run_id, updated_at=_utc_now())
            state.update(
                current_task_id=task_id,
                current_run_id=run_id,
                last_outcome=None,
                next_owner="COMMANDER",
                next_action=f"Execute {task_id} in run {run_id}.",
            )
            return state, {"task_id": task_id, "run_id": run_id, "to": "CLAIMED"}

        return self._mutate(command, transform)

    def _run_phase(self, command: Command) -> CommandResult:
        run_id = command.arguments.get("run_id")
        target = command.arguments.get("to")
        if not isinstance(run_id, str) or target not in {"RUNNING", "VALIDATING", "REVIEWING"}:
            raise _CommandFailure("INVALID", "run phase requires run_id and a runtime Task phase")

        def transform(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            if state["current_run_id"] != run_id or state["current_task_id"] is None:
                raise _CommandFailure("INVALID", "run is not current")
            record = state["tasks"][state["current_task_id"]]
            source = record["status"]
            if target not in _TASK_TRANSITIONS[source]:
                raise _CommandFailure("INVALID", f"illegal Task transition: {source} -> {target}")
            record.update(status=target, updated_at=_utc_now())
            return state, {"task_id": state["current_task_id"], "run_id": run_id, "from": source, "to": target}

        return self._mutate(command, transform)

    def _run_finish(self, command: Command) -> CommandResult:
        run_id = command.arguments.get("run_id")
        outcome = command.arguments.get("outcome")
        evidence_id = command.arguments.get("evidence_id")
        blocker = command.arguments.get("blocker")
        allowed = {"PASS", "PASS_WITH_DEBT", "REWORK", "NO_PROGRESS", "INFRA_FAILURE", "BLOCKED", "STOPPED"}
        if not isinstance(run_id, str) or outcome not in allowed:
            raise _CommandFailure("INVALID", "run finish requires run_id and a valid outcome")
        if outcome in {"PASS", "PASS_WITH_DEBT"} and not isinstance(evidence_id, str):
            raise _CommandFailure("INVALID", "accepted outcomes require evidence_id")
        debt_items = command.arguments.get("debt_items", [])
        if not isinstance(debt_items, list):
            raise _CommandFailure("INVALID", "debt_items must be a list")
        current_task = self._state().get("current_task_id")
        if outcome == "PASS_WITH_DEBT" and isinstance(current_task, str):
            contract = self._read_json(self.canonical / "tasks" / current_task / "contract.json")
            debt_errors = validate_debt(contract, debt_items)
            if debt_errors:
                raise _CommandFailure("INVALID", "debt gate rejected acceptance", data={"errors": debt_errors})
        elif debt_items:
            raise _CommandFailure("INVALID", "debt_items require PASS_WITH_DEBT")

        def transform(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            task_id = state["current_task_id"]
            if state["current_run_id"] != run_id or task_id is None:
                raise _CommandFailure("INVALID", "run is not current")
            record = state["tasks"][task_id]
            if outcome in {"PASS", "PASS_WITH_DEBT"}:
                if record["status"] not in {"VALIDATING", "REVIEWING"}:
                    raise _CommandFailure("INVALID", "acceptance requires validation or review phase")
                record["status"] = "ACCEPTED"
                record["evidence_ids"] = sorted(set(record["evidence_ids"] + [evidence_id]))
                state["accepted_requirement_ids"] = sorted(
                    set(state["accepted_requirement_ids"] + record["requirement_ids"])
                )
                state["last_checkpoint"] = str(command.arguments.get("checkpoint_id") or evidence_id)
            elif outcome in {"REWORK", "NO_PROGRESS", "INFRA_FAILURE", "STOPPED"}:
                record["status"] = "READY"
                record["claim_id"] = None
            elif outcome == "BLOCKED":
                if not isinstance(blocker, str) or not blocker.strip():
                    raise _CommandFailure("INVALID", "BLOCKED outcome requires blocker")
                record["status"] = "BLOCKED"
            record["updated_at"] = _utc_now()
            state["current_task_id"] = None
            state["current_run_id"] = None
            state["last_outcome"] = outcome
            if outcome == "INFRA_FAILURE":
                state.update(project_status="PAUSED", next_owner="COMMANDER", next_action="Resume after infrastructure recovery.")
            elif outcome == "STOPPED":
                state.update(project_status="STOPPED", next_owner="COMMANDER", next_action="Resume when ready.")
            elif outcome == "BLOCKED":
                state.update(project_status="BLOCKED", blocker=blocker.strip(), next_owner="HUMAN", next_action=str(command.arguments.get("next_action") or "Resolve the blocker."))
            else:
                state.update(next_owner="COMMANDER", next_action="Select the next READY Task.")
            return state, {"task_id": task_id, "run_id": run_id, "outcome": outcome, "evidence_id": evidence_id}

        def prepare_debt() -> None:
            if outcome != "PASS_WITH_DEBT":
                return
            path = self.canonical / "debt.json"
            try:
                debt = self._read_json(path)
            except FileNotFoundError:
                debt = {"schema_version": 1, "items": []}
            existing = {item["id"]: item for item in debt.get("items", [])}
            for item in debt_items:
                existing[item["id"]] = dict(item, status="OPEN")
            _atomic_replace_json(path, {"schema_version": 1, "items": [existing[key] for key in sorted(existing)]})

        return self._mutate(command, transform, prepare=prepare_debt)

    def _record_full_validation(self, command: Command) -> CommandResult:
        passed = command.arguments.get("passed")
        if not isinstance(passed, bool):
            raise _CommandFailure("INVALID", "validation.record requires boolean passed")

        def transform(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            state["full_validation_passed"] = passed
            return state, {"passed": passed, "evidence_id": command.arguments.get("evidence_id")}

        return self._mutate(command, transform)

    def _complete(self, command: Command) -> CommandResult:
        state, requirements, errors = self._validated_tree()
        if errors:
            raise _CommandFailure("INVALID", "cannot complete an invalid project", data={"errors": errors})
        if "campaigns" in state:
            raise _CommandFailure(
                "INVALID", "V3 completion is derived from Campaign target state; COMPLETE is legacy-only",
            )
        missing: list[str] = []
        if state["project_status"] != "ACTIVE":
            missing.append("project must be ACTIVE")
        must_ids = {
            item["id"]
            for item in requirements
            if item["priority"] == "MUST" and item["status"] not in {"SUPERSEDED", "REJECTED"}
        }
        missing_must = sorted(must_ids - set(state["accepted_requirement_ids"]))
        if missing_must:
            missing.append(f"MUST requirements lack accepted evidence: {', '.join(missing_must)}")
        blocking_tasks = sorted(
            task_id
            for task_id, record in state["tasks"].items()
            if record["blocking"] and record["status"] != "ACCEPTED"
        )
        if blocking_tasks:
            missing.append(f"blocking Tasks are not ACCEPTED: {', '.join(blocking_tasks)}")
        if state["blocking_debt_ids"]:
            missing.append("blocking debt remains")
        if not state["full_validation_passed"]:
            missing.append("project full validation has not passed")
        for field_name in ("current_task_id", "current_run_id", "active_lock", "blocker"):
            if state[field_name] is not None:
                missing.append(f"{field_name} must be empty")
        locks_path = self.canonical / "locks"
        if locks_path.exists() and any(locks_path.iterdir()):
            missing.append("lock artifacts remain")
        if missing:
            raise _CommandFailure("NOT_READY", "completion prerequisites are not satisfied", data={"missing": missing})
        return self._mutate(
            command,
            lambda current: self._set_project_status(current, "COMPLETE"),
        )

    def _campaign_propose(self, command: Command) -> CommandResult:
        contract = command.arguments.get("contract")
        requirements = command.arguments.get("requirements")
        if not isinstance(contract, dict) or not isinstance(requirements, dict):
            raise _CommandFailure("INVALID", "campaign proposal requires contract and requirements objects")
        campaign_id = contract.get("id")
        if not isinstance(campaign_id, str) or not _CAMPAIGN_ID.fullmatch(campaign_id):
            raise _CommandFailure("INVALID", "Campaign id must match CAMP-NNN")
        errors = self._validate_document("requirements", requirements, "requirements.json")
        errors.extend(self._validate_document("campaign", contract, "campaign.json"))
        if requirements.get("campaign_id") != campaign_id:
            errors.append("requirements.json: campaign_id mismatch")
        if contract.get("requirements_hash") != _canonical_hash(requirements):
            errors.append("campaign.json: requirements_hash mismatch")
        requirement_items = requirements.get("requirements", [])
        if isinstance(requirement_items, list):
            identifiers = [item.get("id") for item in requirement_items if isinstance(item, Mapping)]
            if len(identifiers) != len(set(identifiers)):
                errors.append("requirements.json: duplicate requirement IDs")
        if contract.get("approved_at") is not None:
            errors.append("campaign.json: proposed Campaign cannot already be approved")
        if errors:
            raise _CommandFailure("INVALID", "invalid Campaign proposal", data={"errors": errors})

        def transform(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            campaigns = state.setdefault("campaigns", {})
            state.setdefault("current_campaign_id", None)
            if campaign_id in campaigns:
                raise _CommandFailure("INVALID", f"Campaign already exists: {campaign_id}")
            campaigns[campaign_id] = {
                "status": "PROPOSED",
                "phase": contract["phases"][0],
                "mode": contract["mode"],
                "target": contract["target"],
                "proposal_hash": contract["proposal_hash"],
                "checkpoint": contract.get("source_checkpoint"),
                "last_materialized_checkpoint": contract.get("source_checkpoint"),
                "approved_at": None,
            }
            return state, {"campaign_id": campaign_id, "status": "PROPOSED", "proposal_hash": contract["proposal_hash"]}

        def prepare() -> None:
            directory = self.canonical / "campaigns" / campaign_id
            _atomic_replace_json(directory / "requirements.json", requirements)
            _atomic_replace_json(directory / "campaign.json", contract)

        return self._mutate(command, transform, prepare=prepare)

    def _campaign_approve(self, command: Command) -> CommandResult:
        campaign_id = command.arguments.get("id")
        proposal_hash = command.arguments.get("proposal_hash")
        checkpoint = command.arguments.get("checkpoint")
        approved_at = command.arguments.get("approved_at") or _utc_now()
        if not isinstance(campaign_id, str) or not _CAMPAIGN_ID.fullmatch(campaign_id):
            raise _CommandFailure("INVALID", "campaign approve requires a valid id")
        if not isinstance(checkpoint, str) or not re.fullmatch(r"[0-9a-f]{40,64}", checkpoint):
            raise _CommandFailure("INVALID", "campaign approve requires a Git checkpoint")
        holder: dict[str, Any] = {}

        def transform(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            record = state.get("campaigns", {}).get(campaign_id)
            if record is None or record["status"] not in {"PROPOSED", "WAITING_FOR_HUMAN"}:
                raise _CommandFailure("INVALID", f"Campaign {campaign_id} is not proposed")
            if proposal_hash != record["proposal_hash"]:
                raise _CommandFailure("INVALID", "proposal hash mismatch")
            if state.get("current_campaign_id") not in {None, campaign_id}:
                current = state["campaigns"].get(state["current_campaign_id"], {})
                if current.get("status") in {"ACTIVE", "WAITING_FOR_HUMAN"}:
                    raise _CommandFailure("NOT_READY", "another Campaign is active")
            contract = self._read_json(self.canonical / "campaigns" / campaign_id / "campaign.json")
            contract["approved_at"] = approved_at
            holder["contract"] = contract
            record.update(status="ACTIVE", checkpoint=checkpoint, last_materialized_checkpoint=checkpoint, approved_at=approved_at)
            state["current_campaign_id"] = campaign_id
            state["project_status"] = "ACTIVE"
            state["blocker"] = None
            state["next_owner"] = "COMMANDER"
            state["next_action"] = f"Plan and execute {record['phase']} for {campaign_id}."
            return state, {"campaign_id": campaign_id, "status": "ACTIVE", "checkpoint": checkpoint}

        def prepare() -> None:
            _atomic_replace_json(self.canonical / "campaigns" / campaign_id / "campaign.json", holder["contract"])

        return self._mutate(command, transform, prepare=prepare)

    def _campaign_transition(self, command: Command) -> CommandResult:
        campaign_id = command.arguments.get("id")
        target_status = command.arguments.get("status")
        target_phase = command.arguments.get("phase")
        allowed_status = {
            "PROPOSED": {"ACTIVE", "WAITING_FOR_HUMAN", "CANCELLED"},
            "ACTIVE": {"WAITING_FOR_HUMAN", "TARGET_REACHED", "CANCELLED"},
            "WAITING_FOR_HUMAN": {"ACTIVE", "CANCELLED"},
            "TARGET_REACHED": {"ACTIVE", "ARCHIVED"},
            "ARCHIVED": set(), "CANCELLED": set(),
        }

        def transform(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            record = state.get("campaigns", {}).get(campaign_id)
            if record is None:
                raise _CommandFailure("INVALID", f"unknown Campaign: {campaign_id}")
            old_status, old_phase = record["status"], record["phase"]
            if target_status is not None:
                if target_status not in allowed_status[old_status]:
                    raise _CommandFailure("INVALID", f"illegal Campaign transition: {old_status} -> {target_status}")
                record["status"] = target_status
            if target_phase is not None:
                contract = self._read_json(self.canonical / "campaigns" / str(campaign_id) / "campaign.json")
                phases = contract["phases"]
                if target_phase == "TARGET_REACHED":
                    if record["status"] != "TARGET_REACHED":
                        raise _CommandFailure("INVALID", "TARGET_REACHED phase requires target status")
                elif target_phase not in phases or phases.index(target_phase) != phases.index(old_phase) + 1:
                    raise _CommandFailure("INVALID", f"illegal Campaign phase transition: {old_phase} -> {target_phase}")
                record["phase"] = target_phase
            if record["status"] == "WAITING_FOR_HUMAN":
                state["project_status"] = "PAUSED"
                state["next_owner"] = "HUMAN"
                state["next_action"] = command.arguments.get("next_action") or f"Answer the pending request for {campaign_id}."
            elif record["status"] == "ACTIVE":
                state["project_status"] = "ACTIVE"
                state["next_owner"] = "COMMANDER"
                state["next_action"] = f"Continue {record['phase']} for {campaign_id}."
            elif record["status"] == "TARGET_REACHED":
                record["phase"] = "TARGET_REACHED"
                state["project_status"] = "IDLE"
                state["next_owner"] = "HUMAN"
                state["next_action"] = f"Review, retarget, or archive {campaign_id}."
            elif record["status"] in {"ARCHIVED", "CANCELLED"}:
                state["project_status"] = "IDLE"
                state["current_campaign_id"] = None
                state["next_owner"] = "COMMANDER"
                state["next_action"] = "Start or resume a Campaign."
            return state, {
                "campaign_id": campaign_id, "from_status": old_status,
                "to_status": record["status"], "from_phase": old_phase, "to_phase": record["phase"],
            }

        return self._mutate(command, transform)

    def _campaign_checkpoint(self, command: Command) -> CommandResult:
        campaign_id = command.arguments.get("id")
        checkpoint = command.arguments.get("checkpoint")
        task_id = command.arguments.get("task_id")
        if not isinstance(checkpoint, str) or not re.fullmatch(r"[0-9a-f]{40,64}", checkpoint):
            raise _CommandFailure("INVALID", "campaign checkpoint requires a Git commit")

        def transform(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            record = state.get("campaigns", {}).get(campaign_id)
            if record is None or record["status"] != "ACTIVE":
                raise _CommandFailure("NOT_READY", "Campaign is not active")
            previous = record["checkpoint"]
            record["checkpoint"] = checkpoint
            return state, {"campaign_id": campaign_id, "task_id": task_id, "from": previous, "to": checkpoint}

        return self._mutate(command, transform)

    def _campaign_materialized(self, command: Command) -> CommandResult:
        campaign_id = command.arguments.get("id")
        checkpoint = command.arguments.get("checkpoint")

        def transform(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            record = state.get("campaigns", {}).get(campaign_id)
            if record is None or record["status"] != "TARGET_REACHED":
                raise _CommandFailure("NOT_READY", "only a reached Campaign can be materialized")
            if checkpoint != record["checkpoint"]:
                raise _CommandFailure("INVALID", "materialized checkpoint is not current")
            record["last_materialized_checkpoint"] = checkpoint
            state.update(
                project_status="IDLE", blocker=None, next_owner="HUMAN",
                next_action=f"Review, retarget, or archive {campaign_id}.",
            )
            return state, {"campaign_id": campaign_id, "checkpoint": checkpoint}

        return self._mutate(command, transform)

    def _campaign_retarget(self, command: Command) -> CommandResult:
        campaign_id = command.arguments.get("id")
        target = command.arguments.get("target")
        order = ["ARCHITECTURE_BASELINE", "WORKING_MVP", "INTEGRATED_SYSTEM", "RELEASE_CANDIDATE"]
        holder: dict[str, Any] = {}

        def transform(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            record = state.get("campaigns", {}).get(campaign_id)
            if record is None or record["status"] != "TARGET_REACHED" or record["mode"] == "CHANGE":
                raise _CommandFailure("NOT_READY", "Campaign cannot be retargeted")
            if target not in order or order.index(target) <= order.index(record["target"]):
                raise _CommandFailure("INVALID", "retarget may only increase maturity")
            old = record["target"]
            record["target"] = target
            contract = self._read_json(self.canonical / "campaigns" / str(campaign_id) / "campaign.json")
            contract["target"] = target
            holder["contract"] = contract
            phases = contract["phases"]
            previous_phase = {
                "ARCHITECTURE_BASELINE": "SCAFFOLD", "WORKING_MVP": "COMPONENT_VERIFY",
                "INTEGRATED_SYSTEM": "INTEGRATE", "RELEASE_CANDIDATE": "HARDEN",
            }[old]
            index = phases.index(previous_phase)
            record.update(status="ACTIVE", phase=phases[index + 1])
            state.update(project_status="ACTIVE", current_campaign_id=campaign_id, next_owner="COMMANDER", blocker=None)
            state["next_action"] = f"Plan {record['phase']} for retargeted {campaign_id}."
            return state, {"campaign_id": campaign_id, "from": old, "to": target}

        def prepare() -> None:
            _atomic_replace_json(self.canonical / "campaigns" / str(campaign_id) / "campaign.json", holder["contract"])

        return self._mutate(command, transform, prepare=prepare)

    def _action_create(self, command: Command) -> CommandResult:
        action = command.arguments.get("action")
        if not isinstance(action, dict):
            raise _CommandFailure("INVALID", "action.create requires an Action object")
        errors = self._validate_document("action", action, "action.json")
        if errors:
            raise _CommandFailure("INVALID", "invalid Action", data={"errors": errors})
        action_id = action["id"]
        campaign_id = action["campaign_id"]

        def transform(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            record = state.get("campaigns", {}).get(campaign_id)
            if record is None:
                raise _CommandFailure("INVALID", f"unknown Campaign: {campaign_id}")
            if state.get("current_action_id") is not None:
                raise _CommandFailure("NOT_READY", "another Action is pending")
            if action["canonical_revision"] != state["revision"] + 1:
                raise _CommandFailure("INVALID", "Action canonical revision is not the next revision")
            state["current_action_id"] = action_id
            state["next_owner"] = "HUMAN" if action["role"] == "human" else "COMMANDER"
            state["next_action"] = f"Complete {action['type']} for {campaign_id}."
            return state, {"action_id": action_id, "campaign_id": campaign_id, "type": action["type"]}

        def prepare() -> None:
            path = self.canonical / "actions" / action_id / "action.json"
            if path.exists():
                existing = self._read_json(path)
                if existing != action:
                    raise _CommandFailure("INVALID", "Action ID already has different content")
                return
            _atomic_replace_json(path, action)
            for migration_path in sorted((self.canonical / "migrations").glob("v3-*.json")):
                metadata = self._read_json(migration_path)
                if metadata.get("kind") == "v3-to-v4" and not metadata.get("v4_progress_started"):
                    metadata["v4_progress_started"] = True
                    metadata["first_v4_action_id"] = action_id
                    _atomic_replace_json(migration_path, metadata)

        return self._mutate(command, transform, prepare=prepare)

    def _action_resolve(self, command: Command) -> CommandResult:
        action_id = command.arguments.get("action_id")
        result = command.arguments.get("result")
        outcome = command.arguments.get("outcome")
        if not isinstance(action_id, str) or not isinstance(result, dict) or not isinstance(outcome, dict):
            raise _CommandFailure("INVALID", "action.resolve requires action_id, result, and outcome")

        def transform(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            if state.get("current_action_id") != action_id:
                raise _CommandFailure("INVALID", "Action is not pending")
            state["current_action_id"] = None
            if state.get("pause_requested"):
                state.update(
                    pause_requested=False, project_status="PAUSED", next_owner="COMMANDER",
                    next_action="Continue the Campaign when ready.",
                )
            return state, {"action_id": action_id, "result_hash": command.arguments.get("result_hash")}

        def prepare() -> None:
            directory = self.canonical / "actions" / action_id
            _atomic_replace_json(directory / "result.json", result)
            _atomic_replace_json(directory / "outcome.json", outcome)

        return self._mutate(command, transform, prepare=prepare)

    def _action_pause(self, command: Command) -> CommandResult:
        campaign_id = command.arguments.get("campaign_id")

        def transform(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            record = state.get("campaigns", {}).get(campaign_id)
            if record is None or record.get("status") != "ACTIVE":
                raise _CommandFailure("NOT_READY", "Campaign is not active")
            if state.get("current_action_id") is None:
                state.update(project_status="PAUSED", pause_requested=False)
            else:
                state["pause_requested"] = True
            state["next_owner"] = "COMMANDER"
            state["next_action"] = "Finish the pending Action, then pause."
            return state, {"campaign_id": campaign_id, "graceful": state["pause_requested"]}

        return self._mutate(command, transform)

    def _action_continue(self, command: Command) -> CommandResult:
        campaign_id = command.arguments.get("campaign_id")

        def transform(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            record = state.get("campaigns", {}).get(campaign_id)
            if record is None or record.get("status") != "ACTIVE":
                raise _CommandFailure("NOT_READY", "Campaign is not active")
            pending_id = state.get("current_action_id")
            if pending_id is not None:
                try:
                    action = self._read_json(
                        self.canonical / "actions" / str(pending_id) / "action.json"
                    )
                except (OSError, json.JSONDecodeError) as error:
                    raise _CommandFailure("INVALID", f"pending Action cannot be read: {error}") from error
                if action.get("type") != "PAUSED" or action.get("campaign_id") != campaign_id:
                    raise _CommandFailure("NOT_READY", "a non-pause Action is still pending")
            state.update(
                current_action_id=None, pause_requested=False, project_status="ACTIVE",
                next_owner="COMMANDER", next_action=f"Continue Campaign {campaign_id}.",
            )
            return state, {"campaign_id": campaign_id, "cleared_action_id": pending_id}

        return self._mutate(command, transform)

    def _task_admit_batch(self, command: Command) -> CommandResult:
        campaign_id = command.arguments.get("campaign_id")
        contracts = command.arguments.get("contracts")
        if not isinstance(campaign_id, str) or not isinstance(contracts, list) or not contracts:
            raise _CommandFailure("INVALID", "task admission requires campaign_id and a non-empty contract batch")
        if any(not isinstance(item, dict) for item in contracts):
            raise _CommandFailure("INVALID", "every Task proposal must be an object")
        approval_request_id = command.arguments.get("human_approval_request_id")
        human_approved = False
        if approval_request_id is not None:
            if not isinstance(approval_request_id, str) or not re.fullmatch(r"HUMAN-[0-9a-f]{12}", approval_request_id):
                raise _CommandFailure("INVALID", "invalid human approval request id")
            try:
                approval = self._read_json(
                    self.canonical / "campaigns" / str(campaign_id)
                    / "human-requests" / f"{approval_request_id}.json"
                )
            except (OSError, json.JSONDecodeError) as error:
                raise _CommandFailure("INVALID", f"human approval evidence is unavailable: {error}") from error
            decision = approval.get("answers", {}).get("decision", [])
            human_approved = (
                approval.get("campaign_id") == campaign_id
                and approval.get("status") == "ANSWERED"
                and isinstance(decision, list)
                and bool(decision)
                and str(decision[0]).lower() == "approve exception"
            )
            if not human_approved:
                raise _CommandFailure("INVALID", "human approval evidence does not approve this exception")
            try:
                admission_context = self._read_json(
                    self.canonical / "campaigns" / str(campaign_id) / "admission-context.json"
                )
            except (OSError, json.JSONDecodeError) as error:
                raise _CommandFailure("INVALID", f"admission context is unavailable: {error}") from error
            approved_contracts = [
                dict(item, admission="HUMAN_APPROVED")
                for item in admission_context.get("contracts", [])
                if isinstance(item, dict)
            ]
            if (
                admission_context.get("request_id") != approval_request_id
                or approved_contracts != contracts
            ):
                raise _CommandFailure("INVALID", "human approval is not bound to this frozen Task batch")
        holder: dict[str, Any] = {}

        def transform(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            campaign = state.get("campaigns", {}).get(campaign_id)
            if campaign is None or campaign["status"] != "ACTIVE":
                raise _CommandFailure("NOT_READY", "Campaign is not active")
            campaign_contract = self._read_json(self.canonical / "campaigns" / campaign_id / "campaign.json")
            requirement_doc = self._read_json(self.canonical / "campaigns" / campaign_id / "requirements.json")
            known_requirements = {item["id"] for item in requirement_doc["requirements"]}
            policy = self._read_json(self.canonical / "policy.json")
            envelope = campaign_contract["authority_envelope"]
            errors: list[str] = []
            authority_errors: list[str] = []
            ids = [item.get("id") for item in contracts]
            if len(ids) != len(set(ids)):
                errors.append("Task batch contains duplicate IDs")
            available = set(state["tasks"]) | {item for item in ids if isinstance(item, str)}
            risk_order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
            sensitive = {
                "public-api": "public_api_changes", "public-interface": "public_api_changes",
                "security": "security_changes", "migration": "data_migration",
                "permission-expansion": "permission_expansion", "remote-side-effect": "remote_actions",
            }
            graph: dict[str, list[str]] = {}
            for index, contract in enumerate(contracts):
                label = f"contracts[{index}]"
                errors.extend(self._validate_document("task-contract", contract, label))
                task_id = contract.get("id")
                if task_id in state["tasks"]:
                    errors.append(f"{label}: Task already exists")
                if contract.get("campaign_id") != campaign_id:
                    errors.append(f"{label}: campaign_id mismatch")
                if contract.get("phase") != campaign["phase"]:
                    errors.append(f"{label}: phase is not the active Campaign phase")
                expected_admission = "HUMAN_APPROVED" if human_approved else "AUTO_ADMITTED"
                if contract.get("admission") != expected_admission:
                    errors.append(f"{label}: admission does not match recorded authority")
                unknown = sorted(set(contract.get("requirements", [])) - known_requirements)
                if unknown:
                    errors.append(f"{label}: unknown requirements: {', '.join(unknown)}")
                if risk_order.get(contract.get("risk"), 99) > risk_order[envelope["max_task_risk"]]:
                    authority_errors.append(f"{label}: risk exceeds Authority Envelope")
                disallowed = sorted(set(contract.get("change_classes", [])) - set(envelope["allowed_change_classes"]))
                if disallowed:
                    authority_errors.append(f"{label}: change classes exceed Authority Envelope: {', '.join(disallowed)}")
                for change_class, rule in sensitive.items():
                    if change_class not in contract.get("change_classes", []):
                        continue
                    if envelope[rule] == "require-human":
                        authority_errors.append(f"{label}: {change_class} requires human authority")
                    elif envelope[rule] == "forbidden":
                        errors.append(f"{label}: {change_class} is forbidden by the Authority Envelope")
                for path in contract.get("allowed_paths", []):
                    if path in {".", "*", "**", "**/*"}:
                        errors.append(f"{label}: broad root path includes protected state")
                    if any(
                        fnmatch.fnmatchcase(path, pattern) or path == pattern.rstrip("/**")
                        for pattern in policy.get("protected_paths", [])
                    ):
                        errors.append(f"{label}: protected path requested: {path}")
                    if envelope["dependency_policy"] == "existing-only" and PurePosixPath(path).name in {
                        "pyproject.toml", "requirements.txt", "Pipfile", "poetry.lock",
                        "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
                    }:
                        authority_errors.append(f"{label}: dependency manifest change requires human authority")
                required_prohibitions = {"commit", "push", "publish", "deploy"}
                actual_prohibitions = {
                    str(item).strip().lower() for item in contract.get("prohibited_actions", [])
                }
                missing_prohibitions = sorted(required_prohibitions - actual_prohibitions)
                if missing_prohibitions:
                    errors.append(f"{label}: missing prohibited actions: {', '.join(missing_prohibitions)}")
                for validation in contract.get("validation_commands", []):
                    argv = [str(item).lower() for item in validation.get("argv", [])]
                    if argv and argv[0] == "git" and any(
                        verb in argv[1:] for verb in ("push", "pull", "fetch", "send-pack")
                    ):
                        errors.append(f"{label}: validation command has a remote Git action")
                errors.extend(self._ready_contract_errors(contract, policy))
                dependencies = contract.get("dependencies", [])
                unknown_dependencies = sorted(set(dependencies) - available)
                if unknown_dependencies:
                    errors.append(f"{label}: unknown dependencies: {', '.join(unknown_dependencies)}")
                if isinstance(task_id, str):
                    graph[task_id] = list(dependencies)
            visiting: set[str] = set()
            visited: set[str] = set()

            def visit(task_id: str) -> None:
                if task_id in visiting:
                    errors.append("Task dependency graph contains a cycle")
                    return
                if task_id in visited:
                    return
                visiting.add(task_id)
                for dependency in graph.get(task_id, []):
                    if dependency in graph:
                        visit(dependency)
                visiting.remove(task_id)
                visited.add(task_id)

            for task_id in graph:
                visit(task_id)
            if not human_approved:
                errors.extend(authority_errors)
            if errors:
                raise _CommandFailure(
                    "BLOCKED", "Task proposal batch exceeds admission authority",
                    data={"errors": errors, "human_approvable": not errors or bool(authority_errors)},
                )
            now = _utc_now()
            hashes: dict[str, str] = {}
            for contract in contracts:
                task_id = contract["id"]
                contract_hash = _canonical_hash(contract)
                hashes[task_id] = contract_hash
                state["tasks"][task_id] = {
                    "status": "READY", "generation": 1, "contract_hash": contract_hash,
                    "claim_id": None, "evidence_ids": [], "blocking": contract["blocking"],
                    "requirement_ids": list(contract["requirements"]), "created_at": now, "updated_at": now,
                }
            holder["hashes"] = hashes
            return state, {
                "campaign_id": campaign_id, "task_ids": ids,
                "admission": "HUMAN_APPROVED" if human_approved else "AUTO_ADMITTED",
                "human_approval_request_id": approval_request_id,
            }

        def prepare() -> None:
            for contract in contracts:
                task_id = contract["id"]
                contract_hash = holder["hashes"][task_id]
                _atomic_replace_json(self.canonical / "tasks" / task_id / "contract.json", contract)
                _atomic_replace_bytes(
                    self.canonical / "tasks" / task_id / "contract.md",
                    self._contract_projection(contract, contract_hash).encode("utf-8"), mode=0o444,
                )

        return self._mutate(command, transform, prepare=prepare)

    def _mutate(
        self,
        command: Command,
        transform: Callable[[dict[str, Any]], tuple[dict[str, Any], dict[str, Any]]],
        *,
        prepare: Callable[[], None] | None = None,
        after_commit: Callable[[], None] | None = None,
    ) -> CommandResult:
        self.canonical.mkdir(parents=True, exist_ok=True)
        lock_path = self.canonical / ".control-plane.lock"
        with _GLOBAL_WRITE_LOCK, lock_path.open("a+b") as lock_stream:
            fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX)
            current, _, canonical_errors = self._validated_tree()
            if canonical_errors:
                raise _CommandFailure(
                    "INVALID",
                    "canonical project is invalid; mutation refused",
                    data={"errors": canonical_errors},
                )
            current_revision = current.get("revision")
            if command.expected_revision is not None and command.expected_revision != current_revision:
                raise _CommandFailure(
                    "INVALID",
                    f"revision conflict: expected {command.expected_revision}, found {current_revision}",
                )
            candidate, payload = transform(copy.deepcopy(current))
            candidate["revision"] = current_revision + 1
            candidate["updated_at"] = _utc_now()
            state_errors = self._validate_document("state", candidate, "state.json")
            if state_errors:
                raise _CommandFailure("INVALID", "mutation would create invalid state", data={"errors": state_errors})
            if prepare is not None:
                prepare()
            event = {
                "$schema": "https://autodev.local/schemas/event.schema.json",
                "schema_version": 1,
                "revision": candidate["revision"],
                "previous_revision": current_revision,
                "command": command.name,
                "occurred_at": candidate["updated_at"],
                "payload": payload,
            }
            event_errors = self._validate_document("event", event, "event")
            if event_errors:
                raise _CommandFailure("INVALID", "mutation event is invalid", data={"errors": event_errors})
            event_path = self.canonical / "events" / f"{candidate['revision']:020d}.json"
            _atomic_replace_json(event_path, event)
            _atomic_replace_json(self.canonical / "state.json", candidate)
            if after_commit is not None:
                after_commit()
            return CommandResult("SUCCESS", f"{command.name} succeeded", candidate["revision"], payload)

    def _validate_committed_events(self, state: Mapping[str, Any]) -> list[str]:
        errors: list[str] = []
        for revision in range(1, state["revision"] + 1):
            path = self.canonical / "events" / f"{revision:020d}.json"
            try:
                event = self._read_json(path)
            except (OSError, json.JSONDecodeError) as error:
                errors.append(f"event revision {revision}: {error}")
                continue
            schema_errors = self._validate_document("event", event, path.name)
            errors.extend(schema_errors)
            if not isinstance(event, dict):
                continue
            if event.get("revision") != revision or event.get("previous_revision") != revision - 1:
                errors.append(f"{path.name}: revision chain mismatch")
        return errors

    def _validate_tasks(self, state: Mapping[str, Any], policy: Mapping[str, Any]) -> list[str]:
        errors: list[str] = []
        for task_id, record in state.get("tasks", {}).items():
            path = self.canonical / "tasks" / task_id / "contract.json"
            try:
                contract = self._read_json(path)
            except (OSError, json.JSONDecodeError) as error:
                errors.append(f"{task_id}/contract.json: {error}")
                continue
            schema_errors = self._validate_document("task-contract", contract, f"{task_id}/contract.json")
            errors.extend(schema_errors)
            if not isinstance(contract, dict):
                continue
            if contract.get("id") != task_id:
                errors.append(f"{task_id}/contract.json: id does not match state key")
            if record["status"] not in {"DRAFT", "CANCELLED"} and record["contract_hash"] is None:
                errors.append(f"{task_id}: {record['status']} requires a frozen contract hash")
            if record["contract_hash"] is not None:
                actual_hash = _canonical_hash(contract)
                if actual_hash != record["contract_hash"]:
                    errors.append(f"{task_id}/contract.json: frozen contract hash mismatch")
                if record["blocking"] != contract.get("blocking"):
                    errors.append(f"{task_id}: state blocking flag does not match frozen contract")
                if record["requirement_ids"] != contract.get("requirements"):
                    errors.append(f"{task_id}: state requirement IDs do not match frozen contract")
                if not schema_errors:
                    errors.extend(self._ready_contract_errors(contract, policy))
                    expected_projection = self._contract_projection(contract, record["contract_hash"])
                    projection_path = self.canonical / "tasks" / task_id / "contract.md"
                    try:
                        actual_projection = projection_path.read_text(encoding="utf-8")
                    except OSError as error:
                        errors.append(f"{task_id}/contract.md: {error}")
                    else:
                        if actual_projection != expected_projection:
                            errors.append(f"{task_id}/contract.md: deterministic projection mismatch")
                        if projection_path.stat().st_mode & 0o222:
                            errors.append(f"{task_id}/contract.md: frozen projection must be read-only")
        return errors

    def _ready_contract_errors(self, contract: Mapping[str, Any], policy: Mapping[str, Any]) -> list[str]:
        errors: list[str] = []
        for field_name in ("title", "objective"):
            if not isinstance(contract.get(field_name), str) or not contract[field_name].strip():
                errors.append(f"contract.json: {field_name} must be non-empty before READY")
        for field_name in (
            "requirements",
            "change_classes",
            "allowed_paths",
            "acceptance_criteria",
            "validation_commands",
            "prohibited_actions",
        ):
            if not contract.get(field_name):
                errors.append(f"contract.json: {field_name} must be non-empty before READY")
        criterion_ids = [item.get("id") for item in contract.get("acceptance_criteria", []) if isinstance(item, dict)]
        if len(criterion_ids) != len(set(criterion_ids)):
            errors.append("contract.json: acceptance criterion IDs must be unique")
        validation_policy = policy.get("validation", {})
        allowed_executables = set(validation_policy.get("allowed_executables", []))
        allowed_cwds = set(validation_policy.get("allowed_cwds", []))
        for index, validation in enumerate(contract.get("validation_commands", [])):
            argv = validation.get("argv", [])
            cwd = validation.get("cwd")
            if argv and argv[0] not in allowed_executables:
                errors.append(f"contract.json: validation_commands[{index}] executable is not allowed")
            if cwd not in allowed_cwds:
                errors.append(f"contract.json: validation_commands[{index}] cwd is not allowed")
            try:
                self._project_relative(cwd)
            except (TypeError, _CommandFailure) as error:
                errors.append(f"contract.json: validation_commands[{index}] {error}")
        for index, allowed_path in enumerate(contract.get("allowed_paths", [])):
            try:
                self._project_relative(allowed_path)
            except (TypeError, _CommandFailure) as error:
                errors.append(f"contract.json: allowed_paths[{index}] {error}")
        return errors

    @staticmethod
    def _contract_projection(contract: Mapping[str, Any], contract_hash: str) -> str:
        def bullets(values: list[str]) -> list[str]:
            return [f"- {value}" for value in values] or ["- None"]

        lines = [
            f"# {contract['id']}: {contract['title']}",
            "",
            f"- Generation contract SHA-256: `{contract_hash}`",
            f"- Priority: `{contract['priority']}`",
            f"- Blocking: `{str(contract['blocking']).lower()}`",
            f"- Risk: `{contract['risk']}`",
            f"- Quality mode: `{contract['quality_mode']}`",
            "",
            "## Objective",
            "",
            contract["objective"],
            "",
            "## Requirements",
            "",
            *bullets([f"`{item}`" for item in contract["requirements"]]),
            "",
            "## Change Classes",
            "",
            *bullets([f"`{item}`" for item in contract["change_classes"]]),
            "",
            "## Dependencies",
            "",
            *bullets([f"`{item}`" for item in contract["dependencies"]]),
            "",
            "## Acceptance Criteria",
            "",
            *[
                f"- `{criterion['id']}`: {criterion['description']}"
                for criterion in contract["acceptance_criteria"]
            ],
            "",
            "## Validation Commands",
            "",
            *[
                f"- `{json.dumps(item['argv'])}` in `{item['cwd']}` (timeout {item['timeout']}s)"
                for item in contract["validation_commands"]
            ],
            "",
            "## Allowed Paths",
            "",
            *bullets([f"`{item}`" for item in contract["allowed_paths"]]),
            "",
            "## Out of Scope",
            "",
            *bullets(list(contract["out_of_scope"])),
            "",
            "## Prohibited Actions",
            "",
            *bullets(list(contract["prohibited_actions"])),
            "",
        ]
        return "\n".join(lines)

    def _state_invariant_errors(
        self, state: Mapping[str, Any], requirements: list[dict[str, str]]
    ) -> list[str]:
        errors: list[str] = []
        if state["project_status"] == "BLOCKED":
            if not isinstance(state["blocker"], str) or not state["blocker"].strip():
                errors.append("state.json: BLOCKED requires a non-empty blocker")
            if state["next_owner"] != "HUMAN":
                errors.append("state.json: BLOCKED requires next_owner HUMAN")
            if not isinstance(state["next_action"], str) or not state["next_action"].strip():
                errors.append("state.json: BLOCKED requires a non-empty next_action")
        if state["project_status"] == "COMPLETE":
            must_ids = {
                item["id"]
                for item in requirements
                if item["priority"] == "MUST" and item["status"] not in {"SUPERSEDED", "REJECTED"}
            }
            if not must_ids.issubset(state["accepted_requirement_ids"]):
                errors.append("state.json: COMPLETE lacks accepted MUST requirement evidence")
            if any(
                record["blocking"] and record["status"] != "ACCEPTED"
                for record in state["tasks"].values()
            ):
                errors.append("state.json: COMPLETE has an unaccepted blocking Task")
            if state["blocking_debt_ids"]:
                errors.append("state.json: COMPLETE has blocking debt")
            if not state["full_validation_passed"]:
                errors.append("state.json: COMPLETE requires successful full validation")
            for field_name in ("current_task_id", "current_run_id", "active_lock", "blocker"):
                if state[field_name] is not None:
                    errors.append(f"state.json: COMPLETE requires empty {field_name}")
            if state["next_owner"] != "NONE" or state["next_action"] is not None:
                errors.append("state.json: COMPLETE requires no next owner or action")
        known_requirements = {item["id"] for item in requirements}
        unknown_evidence = sorted(set(state["accepted_requirement_ids"]) - known_requirements)
        if unknown_evidence:
            errors.append(f"state.json: accepted evidence references unknown requirements: {', '.join(unknown_evidence)}")
        return errors

    def _project_relative(self, relative: str) -> Path:
        if not isinstance(relative, str):
            raise _CommandFailure("INVALID", "path must be a string")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or any(part == ".." for part in pure.parts):
            raise _CommandFailure("INVALID", f"path escapes project root: {relative!r}")
        candidate = (self.root / Path(*pure.parts)).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise _CommandFailure("INVALID", f"path escapes project root: {relative!r}")
        return candidate

    def _parse_requirements(self, path: Path) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        seen: set[str] = set()
        header_found = False
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip().startswith("|"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if cells == ["ID", "Priority", "Requirement", "Acceptance signal", "Status"]:
                header_found = True
                continue
            if not header_found or all(cell and set(cell) <= {"-", ":"} for cell in cells):
                continue
            if len(cells) != 5:
                raise _CommandFailure("INVALID", "requirements table row must have exactly five columns")
            requirement_id, priority, _prose, acceptance_signal, status = cells
            if not _REQ_ID.fullmatch(requirement_id):
                raise _CommandFailure("INVALID", f"malformed requirement ID: {requirement_id}")
            if requirement_id in seen:
                raise _CommandFailure("INVALID", f"duplicate requirement ID: {requirement_id}")
            if priority not in {"MUST", "SHOULD", "COULD"}:
                raise _CommandFailure("INVALID", f"invalid requirement priority: {priority}")
            if status not in {"PROPOSED", "ACCEPTED", "SUPERSEDED", "REJECTED"}:
                raise _CommandFailure("INVALID", f"invalid requirement status: {status}")
            if not acceptance_signal:
                raise _CommandFailure("INVALID", f"missing acceptance signal for {requirement_id}")
            seen.add(requirement_id)
            rows.append(
                {
                    "id": requirement_id,
                    "priority": priority,
                    "status": status,
                    "acceptance_signal": acceptance_signal,
                }
            )
        if not header_found:
            raise _CommandFailure("INVALID", "requirements table header not found")
        return rows
