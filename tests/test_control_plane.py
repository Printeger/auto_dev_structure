from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from jsonschema import Draft202012Validator, FormatChecker


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "src"))

from autodev._resources import _read_text, _resource_manifest
from autodev.cli import main


NOW = "2026-08-28T12:00:00+08:00"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def canonical_tree(root: Path, *, status: str = "BOOTSTRAP") -> None:
    control = root / ".autodev"
    (control / "tasks").mkdir(parents=True)
    (control / "events").mkdir()
    (control / "locks").mkdir()
    write_json(
        control / "manifest.json",
        {
            "$schema": "https://autodev.local/schemas/manifest.schema.json",
            "schema_version": 1,
            "framework_version": "2.0.0a1",
            "project_name": "sample",
            "created_at": NOW,
        },
    )
    write_json(
        control / "config.json",
        {
            "$schema": "https://autodev.local/schemas/config.schema.json",
            "schema_version": 1,
            "requirements_path": "docs/REQUIREMENTS.md",
        },
    )
    write_json(
        control / "policy.json",
        {
            "$schema": "https://autodev.local/schemas/policy.schema.json",
            "schema_version": 1,
            "validation": {
                "allowed_executables": ["python3"],
                "allowed_cwds": [".", "src", "tests"],
            },
        },
    )
    write_json(
        control / "state.json",
        {
            "$schema": "https://autodev.local/schemas/state.schema.json",
            "schema_version": 1,
            "framework_version": "2.0.0a1",
            "revision": 0,
            "project_status": status,
            "current_milestone": None,
            "current_task_id": None,
            "current_run_id": None,
            "last_outcome": None,
            "last_checkpoint": None,
            "blocker": None,
            "next_owner": "COMMANDER",
            "next_action": "Activate the project.",
            "tasks": {},
            "accepted_requirement_ids": [],
            "blocking_debt_ids": [],
            "full_validation_passed": False,
            "active_lock": None,
            "updated_at": NOW,
        },
    )
    requirements = (
        "| ID | Priority | Requirement | Acceptance signal | Status |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| REQ-001 | MUST | Keep this prose out. | Tests pass. | ACCEPTED |\n"
        "| REQ-002 | SHOULD | Also secret prose. | Review passes. | ACCEPTED |\n"
    )
    (root / "docs").mkdir()
    (root / "docs" / "REQUIREMENTS.md").write_text(requirements, encoding="utf-8")


def full_contract(task_id: str = "TASK-001") -> dict[str, object]:
    return {
        "$schema": "https://autodev.local/schemas/task-contract.schema.json",
        "schema_version": 1,
        "id": task_id,
        "title": "Implement state",
        "objective": "Implement an executable state transition.",
        "requirements": ["REQ-001"],
        "dependencies": [],
        "priority": "MUST",
        "blocking": True,
        "risk": "HIGH",
        "quality_mode": "BUILD",
        "change_classes": ["shared_data_structure"],
        "allowed_paths": ["src", "tests"],
        "out_of_scope": ["Runner"],
        "acceptance_criteria": [{"id": "AC-001", "description": "Transition is atomic."}],
        "validation_commands": [
            {"argv": ["python3", "-m", "unittest"], "cwd": ".", "timeout": 60}
        ],
        "prohibited_actions": ["commit"],
        "created_at": NOW,
    }


class PackagedSchemaTests(unittest.TestCase):
    def test_every_schema_declares_draft_2020_12_and_checks_formats(self) -> None:
        manifest = _resource_manifest()
        self.assertGreaterEqual(len(manifest["schemas"]), 8)
        for relative in manifest["schemas"]:
            schema = json.loads(_read_text(relative))
            self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
            Draft202012Validator.check_schema(schema)

        event_schema = json.loads(_read_text("schemas/event.schema.json"))
        validator = Draft202012Validator(event_schema, format_checker=FormatChecker())
        valid = {
            "$schema": "https://autodev.local/schemas/event.schema.json",
            "schema_version": 1,
            "revision": 1,
            "previous_revision": 0,
            "command": "activate",
            "occurred_at": NOW,
            "payload": {},
        }
        self.assertFalse(list(validator.iter_errors(valid)))
        invalid = dict(valid, occurred_at="not-a-date")
        self.assertTrue(list(validator.iter_errors(invalid)))

        attempt_schema = json.loads(_read_text("schemas/attempt-outcome.schema.json"))
        attempt_validator = Draft202012Validator(attempt_schema, format_checker=FormatChecker())
        attempt = {
            "$schema": "https://autodev.local/schemas/attempt-outcome.schema.json",
            "schema_version": 1,
            "outcome": "PASS_WITH_DEBT",
            "summary": "Acceptance passed with recorded debt.",
        }
        self.assertFalse(list(attempt_validator.iter_errors(attempt)))
        self.assertTrue(list(attempt_validator.iter_errors(dict(attempt, outcome="DONE"))))

        proposal_schema = json.loads(_read_text("schemas/agent-proposal.schema.json"))
        proposal_validator = Draft202012Validator(proposal_schema, format_checker=FormatChecker())
        proposal = {
            "$schema": "https://autodev.local/schemas/agent-proposal.schema.json",
            "schema_version": 1,
            "kind": "COMPLETE",
            "task_id": None,
            "summary": "I propose completion without authority.",
        }
        self.assertFalse(list(proposal_validator.iter_errors(proposal)))
        self.assertTrue(list(proposal_validator.iter_errors(dict(proposal, environment={}))))

    def test_every_packaged_schema_accepts_valid_and_rejects_invalid_instances(self) -> None:
        state = {
            "$schema": "https://autodev.local/schemas/state.schema.json",
            "schema_version": 1,
            "framework_version": "2.0.0a1",
            "revision": 0,
            "project_status": "BOOTSTRAP",
            "current_milestone": None,
            "current_task_id": None,
            "current_run_id": None,
            "last_outcome": None,
            "last_checkpoint": None,
            "blocker": None,
            "next_owner": "COMMANDER",
            "next_action": "Activate.",
            "tasks": {},
            "accepted_requirement_ids": [],
            "blocking_debt_ids": [],
            "full_validation_passed": False,
            "active_lock": None,
            "updated_at": NOW,
        }
        instances = {
            "resource-manifest": (
                _resource_manifest(),
                {"framework_version": "bad"},
                None,
            ),
            "manifest": (
                {
                    "$schema": "https://autodev.local/schemas/manifest.schema.json",
                    "schema_version": 1,
                    "framework_version": "2.0.0a1",
                    "project_name": "sample",
                    "created_at": NOW,
                },
                {"schema_version": 1},
                "created_at",
            ),
            "config": (
                {
                    "$schema": "https://autodev.local/schemas/config.schema.json",
                    "schema_version": 1,
                    "requirements_path": "docs/REQUIREMENTS.md",
                },
                {
                    "$schema": "https://autodev.local/schemas/config.schema.json",
                    "schema_version": 1,
                    "requirements_path": "/outside.md",
                },
                None,
            ),
            "policy": (
                {
                    "$schema": "https://autodev.local/schemas/policy.schema.json",
                    "schema_version": 1,
                    "validation": {"allowed_executables": ["python3"], "allowed_cwds": ["."]},
                },
                {
                    "$schema": "https://autodev.local/schemas/policy.schema.json",
                    "schema_version": 1,
                    "validation": {"allowed_executables": [], "allowed_cwds": ["."]},
                },
                None,
            ),
            "state": (state, dict(state, revision="zero"), "updated_at"),
            "task-contract": (full_contract(), {"id": "TASK-001"}, "created_at"),
            "attempt-outcome": (
                {
                    "$schema": "https://autodev.local/schemas/attempt-outcome.schema.json",
                    "schema_version": 1,
                    "outcome": "PASS",
                    "summary": "Passed.",
                },
                {
                    "$schema": "https://autodev.local/schemas/attempt-outcome.schema.json",
                    "schema_version": 1,
                    "outcome": "DONE",
                    "summary": "No.",
                },
                None,
            ),
            "event": (
                {
                    "$schema": "https://autodev.local/schemas/event.schema.json",
                    "schema_version": 1,
                    "revision": 1,
                    "previous_revision": 0,
                    "command": "activate",
                    "occurred_at": NOW,
                    "payload": {},
                },
                {"revision": 1},
                "occurred_at",
            ),
            "command-result": (
                {
                    "$schema": "https://autodev.local/schemas/command-result.schema.json",
                    "schema_version": 1,
                    "status": "SUCCESS",
                    "exit_code": 0,
                    "message": "ok",
                    "revision": 0,
                    "data": {},
                },
                {
                    "$schema": "https://autodev.local/schemas/command-result.schema.json",
                    "schema_version": 1,
                    "status": "SUCCESS",
                    "exit_code": 9,
                    "message": "bad",
                    "revision": 0,
                    "data": {},
                },
                None,
            ),
            "agent-proposal": (
                {
                    "$schema": "https://autodev.local/schemas/agent-proposal.schema.json",
                    "schema_version": 1,
                    "kind": "WORK",
                    "task_id": "TASK-001",
                    "summary": "Work.",
                },
                {
                    "$schema": "https://autodev.local/schemas/agent-proposal.schema.json",
                    "schema_version": 1,
                    "kind": "MUTATE",
                    "task_id": "TASK-001",
                    "summary": "No.",
                },
                None,
            ),
            "attempt-proposal": (
                {
                    "outcome": "PASS", "summary": "Accepted.", "blocker": None,
                    "next_action": None, "findings": [], "debt_items": [],
                },
                {
                    "outcome": "DONE", "summary": "Invalid.", "blocker": None,
                    "next_action": None, "findings": [], "debt_items": [],
                },
                None,
            ),
        }
        self.assertEqual(
            set(instances),
            {Path(relative).name.removesuffix(".schema.json") for relative in _resource_manifest()["schemas"]},
        )
        for schema_name, (valid, invalid, formatted_field) in instances.items():
            with self.subTest(schema=schema_name):
                schema = json.loads(_read_text(f"schemas/{schema_name}.schema.json"))
                validator = Draft202012Validator(schema, format_checker=FormatChecker())
                self.assertFalse(list(validator.iter_errors(valid)))
                self.assertTrue(list(validator.iter_errors(invalid)))
                if formatted_field is not None:
                    malformed = dict(valid)
                    malformed[formatted_field] = "not-a-date"
                    self.assertTrue(list(validator.iter_errors(malformed)))


class FailClosedCommandTests(unittest.TestCase):
    MUTATIONS = ("activate", "complete", "project.transition", "task.create", "task.ready", "task.transition", "task.reopen")

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        canonical_tree(self.root)

    def control(self):
        from autodev import ControlPlane

        return ControlPlane(self.root)

    def ready_project(self, root: Path, *, active: bool = True, second_draft: bool = False) -> tuple[object, int]:
        from autodev import Command, ControlPlane

        control = ControlPlane(root)
        revision = 0
        created = control.execute(
            Command(
                "task.create",
                {
                    "id": "TASK-001",
                    "title": "First",
                    "risk": "HIGH",
                    "quality_mode": "BUILD",
                    "requirements": ["REQ-001"],
                },
                expected_revision=revision,
            )
        )
        revision = created.revision
        write_json(root / ".autodev/tasks/TASK-001/contract.json", full_contract())
        revision = control.execute(
            Command("task.ready", {"id": "TASK-001"}, expected_revision=revision)
        ).revision
        if second_draft:
            revision = control.execute(
                Command(
                    "task.create",
                    {
                        "id": "TASK-002",
                        "title": "Second",
                        "risk": "LOW",
                        "quality_mode": "BUILD",
                        "requirements": ["REQ-001"],
                    },
                    expected_revision=revision,
                )
            ).revision
            second = full_contract("TASK-002")
            second["title"] = "Second"
            write_json(root / ".autodev/tasks/TASK-002/contract.json", second)
        if active:
            revision = control.execute(Command("activate", expected_revision=revision)).revision
        return control, revision

    def test_every_mutation_rejects_a_preexisting_frozen_contract_mismatch(self) -> None:
        from autodev import Command

        for mutation in self.MUTATIONS:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                canonical_tree(root)
                control, revision = self.ready_project(
                    root,
                    active=mutation != "activate",
                    second_draft=mutation == "task.ready",
                )
                contract_path = root / ".autodev/tasks/TASK-001/contract.json"
                contract = json.loads(contract_path.read_text(encoding="utf-8"))
                contract["objective"] = "Tampered after READY."
                write_json(contract_path, contract)
                event_count = len(list((root / ".autodev/events").glob("*.json")))
                commands = {
                    "activate": Command("activate", expected_revision=revision),
                    "complete": Command("complete", expected_revision=revision),
                    "project.transition": Command(
                        "project.transition", {"to": "PAUSED"}, expected_revision=revision
                    ),
                    "task.create": Command(
                        "task.create",
                        {
                            "id": "TASK-003",
                            "title": "Third",
                            "risk": "LOW",
                            "quality_mode": "BUILD",
                            "requirements": ["REQ-001"],
                        },
                        expected_revision=revision,
                    ),
                    "task.ready": Command(
                        "task.ready", {"id": "TASK-002"}, expected_revision=revision
                    ),
                    "task.transition": Command(
                        "task.transition",
                        {"id": "TASK-001", "to": "CLAIMED"},
                        expected_revision=revision,
                    ),
                    "task.reopen": Command(
                        "task.reopen",
                        {"id": "TASK-001", "reason": "Try to hide mismatch."},
                        expected_revision=revision,
                    ),
                }
                result = control.execute(commands[mutation])
                self.assertEqual(result.exit_code, 1, result.message)
                state = json.loads((root / ".autodev/state.json").read_text(encoding="utf-8"))
                self.assertEqual(state["revision"], revision)
                self.assertEqual(len(list((root / ".autodev/events").glob("*.json"))), event_count)
                self.assertEqual(contract_path.read_text(encoding="utf-8"), json.dumps(contract, indent=2) + "\n")

    def test_reopen_refusal_preserves_claim_evidence_and_projection(self) -> None:
        from autodev import Command

        control, revision = self.ready_project(self.root)
        state_path = self.root / ".autodev/state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["tasks"]["TASK-001"]["claim_id"] = "claim-existing"
        state["tasks"]["TASK-001"]["evidence_ids"] = ["evidence-existing"]
        write_json(state_path, state)
        projection_path = self.root / ".autodev/tasks/TASK-001/contract.md"
        projection = projection_path.read_text(encoding="utf-8")
        contract_path = projection_path.with_name("contract.json")
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        contract["objective"] = "Mismatch evidence."
        write_json(contract_path, contract)
        result = control.execute(
            Command(
                "task.reopen",
                {"id": "TASK-001", "reason": "Should fail."},
                expected_revision=revision,
            )
        )
        self.assertEqual(result.exit_code, 1)
        record = json.loads(state_path.read_text(encoding="utf-8"))["tasks"]["TASK-001"]
        self.assertEqual(record["claim_id"], "claim-existing")
        self.assertEqual(record["evidence_ids"], ["evidence-existing"])
        self.assertEqual(projection_path.read_text(encoding="utf-8"), projection)

    def test_schema_invalid_state_types_return_invalid_for_every_command(self) -> None:
        from autodev import Command

        commands = (
            Command("validate"),
            Command("status"),
            Command("activate"),
            Command("complete"),
            Command("task.show", {"id": "TASK-001"}),
            Command("project.transition", {"to": "ACTIVE"}),
            Command("task.create", {"id": "TASK-001"}),
            Command("task.ready", {"id": "TASK-001"}),
            Command("task.transition", {"id": "TASK-001", "to": "READY"}),
            Command("task.reopen", {"id": "TASK-001", "reason": "Reason."}),
        )
        for malformed in ([], {"revision": "zero"}):
            for command in commands:
                with self.subTest(malformed=malformed, command=command.name):
                    write_json(self.root / ".autodev/state.json", malformed)
                    result = self.control().execute(command)
                    self.assertEqual(result.exit_code, 1)
                    self.assertEqual(
                        json.loads((self.root / ".autodev/state.json").read_text(encoding="utf-8")),
                        malformed,
                    )


class ControlPlaneFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        canonical_tree(self.root)

    def control(self):
        from autodev import ControlPlane

        return ControlPlane(self.root)

    def command(self, name: str, arguments: dict[str, object] | None = None, expected: int | None = None):
        from autodev import Command

        return Command(name, arguments or {}, expected_revision=expected)

    def state(self) -> dict[str, object]:
        return json.loads((self.root / ".autodev/state.json").read_text(encoding="utf-8"))


class ControlPlaneMutationTests(ControlPlaneFixture):

    def test_activate_is_revision_checked_atomic_and_evented(self) -> None:
        result = self.control().execute(self.command("activate", expected=0))
        self.assertEqual((result.status, result.exit_code, result.revision), ("SUCCESS", 0, 1))
        self.assertEqual(self.state()["project_status"], "ACTIVE")
        event = json.loads(
            (self.root / ".autodev/events/00000000000000000001.json").read_text(encoding="utf-8")
        )
        self.assertEqual((event["previous_revision"], event["revision"], event["command"]), (0, 1, "activate"))

        stale = self.control().execute(self.command("project.transition", {"to": "PAUSED"}, expected=0))
        self.assertEqual((stale.status, stale.exit_code), ("INVALID", 1))
        self.assertEqual(self.state()["revision"], 1)

    def test_same_expected_revision_has_exactly_one_winner(self) -> None:
        barrier = threading.Barrier(2)
        results = []

        def activate() -> None:
            barrier.wait()
            results.append(self.control().execute(self.command("activate", expected=0)))

        threads = [threading.Thread(target=activate) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(result.exit_code for result in results), [0, 1])
        self.assertEqual(self.state()["revision"], 1)
        self.assertEqual(len(list((self.root / ".autodev/events").glob("*.json"))), 1)

    def test_state_replace_failure_leaves_old_valid_state_and_orphan_event_is_recoverable(self) -> None:
        real_replace = os.replace

        def fail_state_replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
            if Path(destination).name == "state.json":
                raise OSError("injected state replacement failure")
            real_replace(source, destination)

        with mock.patch("autodev.control_plane.os.replace", side_effect=fail_state_replace):
            failed = self.control().execute(self.command("activate", expected=0))
        self.assertEqual((failed.status, failed.exit_code), ("INFRA_FAILURE", 5))
        self.assertEqual((self.state()["revision"], self.state()["project_status"]), (0, "BOOTSTRAP"))
        self.assertEqual(self.control().execute(self.command("validate")).exit_code, 0)

        recovered = self.control().execute(self.command("activate", expected=0))
        self.assertEqual(recovered.exit_code, 0)
        self.assertEqual(self.state()["revision"], 1)

    def test_temp_sync_and_event_replace_failures_never_expose_partial_state(self) -> None:
        with mock.patch("autodev.control_plane.os.fsync", side_effect=OSError("injected fsync")):
            failed_temp = self.control().execute(self.command("activate", expected=0))
        self.assertEqual(failed_temp.exit_code, 5)
        self.assertEqual(self.state()["revision"], 0)
        json.loads((self.root / ".autodev/state.json").read_text(encoding="utf-8"))
        self.assertFalse(list((self.root / ".autodev").rglob("*.tmp")))

        real_replace = os.replace

        def fail_event_replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
            if Path(destination).parent.name == "events":
                raise OSError("injected event replacement failure")
            real_replace(source, destination)

        with mock.patch("autodev.control_plane.os.replace", side_effect=fail_event_replace):
            failed_event = self.control().execute(self.command("activate", expected=0))
        self.assertEqual(failed_event.exit_code, 5)
        self.assertEqual(self.state()["revision"], 0)
        self.assertFalse(list((self.root / ".autodev/events").glob("*.json")))

    def test_expected_revision_is_serialized_across_processes(self) -> None:
        code = (
            "import sys; from autodev import Command, ControlPlane; "
            "r=ControlPlane(sys.argv[1]).execute(Command('activate', expected_revision=0)); "
            "print(r.exit_code)"
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(SOURCE_ROOT / "src")
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", code, str(self.root)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
            for _ in range(2)
        ]
        outputs = [process.communicate(timeout=10) for process in processes]
        self.assertEqual([process.returncode for process in processes], [0, 0])
        self.assertEqual(sorted(int(stdout.strip()) for stdout, _ in outputs), [0, 1])
        self.assertEqual(self.state()["revision"], 1)

    def test_legal_and_illegal_project_and_task_edges_do_not_bypass_tables(self) -> None:
        self.assertEqual(self.control().execute(self.command("activate", expected=0)).exit_code, 0)
        paused = self.control().execute(self.command("project.transition", {"to": "PAUSED"}, expected=1))
        self.assertEqual(paused.exit_code, 0)
        resumed = self.control().execute(self.command("project.transition", {"to": "ACTIVE"}, expected=2))
        self.assertEqual(resumed.exit_code, 0)
        illegal = self.control().execute(self.command("project.transition", {"to": "BOOTSTRAP"}, expected=3))
        self.assertEqual(illegal.exit_code, 1)
        self.assertEqual(self.state()["revision"], 3)

        created = self.control().execute(
            self.command(
                "task.create",
                {
                    "id": "TASK-001",
                    "title": "State transitions",
                    "risk": "HIGH",
                    "quality_mode": "BUILD",
                    "requirements": ["REQ-001"],
                },
                expected=3,
            )
        )
        self.assertEqual(created.exit_code, 0)
        illegal_task = self.control().execute(
            self.command("task.transition", {"id": "TASK-001", "to": "ACCEPTED"}, expected=4)
        )
        self.assertEqual(illegal_task.exit_code, 1)
        self.assertEqual(self.state()["tasks"]["TASK-001"]["status"], "DRAFT")
        self.assertEqual(self.state()["revision"], 4)

    def test_generic_transitions_cannot_bypass_ready_or_complete_gates(self) -> None:
        self.assertEqual(self.control().execute(self.command("activate", expected=0)).exit_code, 0)
        forced_complete = self.control().execute(
            self.command("project.transition", {"to": "COMPLETE"}, expected=1)
        )
        self.assertEqual(forced_complete.exit_code, 2)
        self.assertEqual((self.state()["project_status"], self.state()["revision"]), ("ACTIVE", 1))

        created = self.control().execute(
            self.command(
                "task.create",
                {
                    "id": "TASK-001",
                    "title": "State transitions",
                    "risk": "HIGH",
                    "quality_mode": "BUILD",
                    "requirements": ["REQ-001"],
                },
                expected=1,
            )
        )
        self.assertEqual(created.exit_code, 0)
        forced_ready = self.control().execute(
            self.command("task.transition", {"id": "TASK-001", "to": "READY"}, expected=2)
        )
        self.assertEqual(forced_ready.exit_code, 1)
        self.assertEqual(self.state()["tasks"]["TASK-001"]["status"], "DRAFT")

    def test_blocked_transition_requires_human_actionable_context(self) -> None:
        self.assertEqual(self.control().execute(self.command("activate", expected=0)).exit_code, 0)
        missing = self.control().execute(
            self.command("project.transition", {"to": "BLOCKED"}, expected=1)
        )
        self.assertEqual(missing.exit_code, 1)
        blocked = self.control().execute(
            self.command(
                "project.transition",
                {
                    "to": "BLOCKED",
                    "blocker": "Need a product decision.",
                    "next_action": "Choose the public behavior.",
                },
                expected=1,
            )
        )
        self.assertEqual(blocked.exit_code, 0)
        self.assertEqual(self.state()["next_owner"], "HUMAN")

    def test_every_published_project_edge_is_executable(self) -> None:
        edges = {
            "BOOTSTRAP": {"ACTIVE", "FAILED"},
            "ACTIVE": {"PAUSED", "BLOCKED", "COMPLETE", "FAILED", "STOPPED"},
            "PAUSED": {"ACTIVE", "BLOCKED", "FAILED", "STOPPED"},
            "BLOCKED": {"ACTIVE", "FAILED", "STOPPED"},
            "COMPLETE": set(),
            "FAILED": set(),
            "STOPPED": {"ACTIVE", "FAILED"},
        }
        for source, targets in edges.items():
            for target in targets:
                with self.subTest(source=source, target=target), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    canonical_tree(root, status=source)
                    state_path = root / ".autodev/state.json"
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    if source == "BLOCKED":
                        state.update(
                            blocker="Need a decision.",
                            next_owner="HUMAN",
                            next_action="Choose behavior.",
                        )
                    if target == "COMPLETE":
                        state.update(
                            accepted_requirement_ids=["REQ-001"],
                            full_validation_passed=True,
                        )
                    write_json(state_path, state)
                    arguments = {"to": target}
                    if target == "BLOCKED":
                        arguments.update(
                            blocker="Need a decision.", next_action="Choose behavior."
                        )
                    result = self.control().__class__(root).execute(
                        self.command("project.transition", arguments, expected=0)
                    )
                    self.assertEqual(result.exit_code, 0, result.message)

    def test_every_published_task_edge_is_executable(self) -> None:
        edges = {
            "DRAFT": {"READY", "CANCELLED"},
            "READY": {"CLAIMED", "DEFERRED", "BLOCKED", "CANCELLED"},
            "CLAIMED": {"RUNNING", "READY", "BLOCKED", "CANCELLED"},
            "RUNNING": {"VALIDATING", "READY", "BLOCKED", "CANCELLED"},
            "VALIDATING": {"REVIEWING", "ACCEPTED", "READY", "BLOCKED"},
            "REVIEWING": {"ACCEPTED", "READY", "BLOCKED"},
            "ACCEPTED": set(),
            "DEFERRED": {"READY", "CANCELLED"},
            "BLOCKED": {"READY", "CANCELLED"},
            "CANCELLED": set(),
        }
        for source, targets in edges.items():
            for target in targets:
                with self.subTest(source=source, target=target), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    canonical_tree(root)
                    from autodev import Command, ControlPlane

                    control = ControlPlane(root)
                    created = control.execute(
                        Command(
                            "task.create",
                            {
                                "id": "TASK-001",
                                "title": "State transition",
                                "risk": "HIGH",
                                "quality_mode": "BUILD",
                                "requirements": ["REQ-001"],
                            },
                            expected_revision=0,
                        )
                    )
                    write_json(root / ".autodev/tasks/TASK-001/contract.json", full_contract())
                    ready = control.execute(
                        Command(
                            "task.ready", {"id": "TASK-001"}, expected_revision=created.revision
                        )
                    )
                    self.assertEqual(ready.exit_code, 0)
                    state_path = root / ".autodev/state.json"
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    state["tasks"]["TASK-001"]["status"] = source
                    if source == "DRAFT":
                        state["tasks"]["TASK-001"]["contract_hash"] = None
                    write_json(state_path, state)
                    result = control.execute(
                        self.command(
                            "task.transition",
                            {"id": "TASK-001", "to": target},
                            expected=ready.revision,
                        )
                    )
                    self.assertEqual(result.exit_code, 0, result.message)
                    self.assertEqual(
                        json.loads(state_path.read_text(encoding="utf-8"))["tasks"]["TASK-001"]["status"],
                        target,
                    )


class TaskContractTests(ControlPlaneFixture):
    def create_task(self) -> int:
        result = self.control().execute(
            self.command(
                "task.create",
                {
                    "id": "TASK-001",
                    "title": "State transitions",
                    "risk": "HIGH",
                    "quality_mode": "BUILD",
                    "requirements": ["REQ-001"],
                },
                expected=0,
            )
        )
        self.assertEqual(result.exit_code, 0)
        return result.revision

    @property
    def contract_path(self) -> Path:
        return self.root / ".autodev/tasks/TASK-001/contract.json"

    def test_ready_freezes_hash_and_writes_deterministic_read_only_projection(self) -> None:
        revision = self.create_task()
        write_json(self.contract_path, full_contract())
        ready = self.control().execute(self.command("task.ready", {"id": "TASK-001"}, expected=revision))
        self.assertEqual(ready.exit_code, 0, ready.message)
        record = self.state()["tasks"]["TASK-001"]
        self.assertEqual(record["status"], "READY")
        self.assertRegex(record["contract_hash"], r"^[0-9a-f]{64}$")
        projection = self.contract_path.with_name("contract.md")
        first_projection = projection.read_text(encoding="utf-8")
        self.assertIn("# TASK-001: Implement state", first_projection)
        self.assertIn("- `AC-001`: Transition is atomic.", first_projection)
        self.assertEqual(projection.stat().st_mode & 0o222, 0)

        contract = full_contract()
        contract["objective"] = "Mutated behind the control plane."
        write_json(self.contract_path, contract)
        validation = self.control().execute(self.command("validate"))
        self.assertEqual(validation.exit_code, 1)
        self.assertIn("frozen contract hash mismatch", " ".join(validation.data["errors"]))
        refused = self.control().execute(self.command("task.ready", {"id": "TASK-001"}, expected=2))
        self.assertEqual(refused.exit_code, 1)
        self.assertEqual(self.state()["revision"], 2)

    def test_reopen_requires_reason_and_invalidates_claim_and_evidence(self) -> None:
        self.create_task()
        write_json(self.contract_path, full_contract())
        self.assertEqual(
            self.control().execute(self.command("task.ready", {"id": "TASK-001"}, expected=1)).exit_code,
            0,
        )
        self.assertEqual(
            self.control().execute(
                self.command("task.transition", {"id": "TASK-001", "to": "CLAIMED"}, expected=2)
            ).exit_code,
            0,
        )
        state = self.state()
        state["tasks"]["TASK-001"]["claim_id"] = "claim-old"
        state["tasks"]["TASK-001"]["evidence_ids"] = ["evidence-old"]
        state["accepted_requirement_ids"] = ["REQ-001"]
        write_json(self.root / ".autodev/state.json", state)

        empty = self.control().execute(
            self.command("task.reopen", {"id": "TASK-001", "reason": "  "}, expected=3)
        )
        self.assertEqual(empty.exit_code, 1)
        reopened = self.control().execute(
            self.command("task.reopen", {"id": "TASK-001", "reason": "Requirements changed."}, expected=3)
        )
        self.assertEqual(reopened.exit_code, 0, reopened.message)
        record = self.state()["tasks"]["TASK-001"]
        self.assertEqual(record["status"], "DRAFT")
        self.assertEqual(record["generation"], 2)
        self.assertIsNone(record["contract_hash"])
        self.assertIsNone(record["claim_id"])
        self.assertEqual(record["evidence_ids"], [])
        self.assertEqual(self.state()["accepted_requirement_ids"], [])
        self.assertFalse(self.contract_path.with_name("contract.md").exists())

        invalid_source = self.control().execute(
            self.command("task.reopen", {"id": "TASK-001", "reason": "Again."}, expected=4)
        )
        self.assertEqual(invalid_source.exit_code, 1)
        self.assertEqual(self.state()["revision"], 4)

    def test_missing_stale_or_writable_frozen_projection_invalidates_project(self) -> None:
        for fault in ("missing", "stale", "writable"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                canonical_tree(root)
                from autodev import Command, ControlPlane

                control = ControlPlane(root)
                created = control.execute(
                    Command(
                        "task.create",
                        {
                            "id": "TASK-001",
                            "title": "Projection",
                            "risk": "HIGH",
                            "quality_mode": "BUILD",
                            "requirements": ["REQ-001"],
                        },
                        expected_revision=0,
                    )
                )
                write_json(root / ".autodev/tasks/TASK-001/contract.json", full_contract())
                ready = control.execute(
                    Command("task.ready", {"id": "TASK-001"}, expected_revision=created.revision)
                )
                self.assertEqual(ready.exit_code, 0)
                projection = root / ".autodev/tasks/TASK-001/contract.md"
                if fault == "missing":
                    projection.unlink()
                elif fault == "stale":
                    projection.chmod(0o600)
                    projection.write_text("stale\n", encoding="utf-8")
                    projection.chmod(0o444)
                else:
                    projection.chmod(0o644)
                result = control.execute(Command("validate"))
                self.assertEqual(result.exit_code, 1)
                self.assertIn("contract.md", " ".join(result.data["errors"]))

    def test_create_ready_and_reopen_are_recovery_safe_at_event_and_state_boundaries(self) -> None:
        from autodev import Command, ControlPlane

        for phase in ("create", "ready", "reopen"):
            for failed_boundary in ("event", "state"):
                with (
                    self.subTest(phase=phase, failed_boundary=failed_boundary),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    root = Path(directory)
                    canonical_tree(root)
                    control = ControlPlane(root)
                    revision = 0
                    if phase in {"ready", "reopen"}:
                        revision = control.execute(
                            Command(
                                "task.create",
                                {
                                    "id": "TASK-001",
                                    "title": "Fault ordering",
                                    "risk": "HIGH",
                                    "quality_mode": "BUILD",
                                    "requirements": ["REQ-001"],
                                },
                                expected_revision=revision,
                            )
                        ).revision
                        write_json(root / ".autodev/tasks/TASK-001/contract.json", full_contract())
                    if phase == "reopen":
                        revision = control.execute(
                            Command(
                                "task.ready", {"id": "TASK-001"}, expected_revision=revision
                            )
                        ).revision
                    commands = {
                        "create": Command(
                            "task.create",
                            {
                                "id": "TASK-001",
                                "title": "Fault ordering",
                                "risk": "HIGH",
                                "quality_mode": "BUILD",
                                "requirements": ["REQ-001"],
                            },
                            expected_revision=revision,
                        ),
                        "ready": Command(
                            "task.ready", {"id": "TASK-001"}, expected_revision=revision
                        ),
                        "reopen": Command(
                            "task.reopen",
                            {"id": "TASK-001", "reason": "Fault injection."},
                            expected_revision=revision,
                        ),
                    }
                    real_replace = os.replace

                    def fail_replace(
                        source: str | os.PathLike[str], destination: str | os.PathLike[str]
                    ) -> None:
                        target = Path(destination)
                        should_fail = (
                            failed_boundary == "event" and target.parent.name == "events"
                        ) or (failed_boundary == "state" and target.name == "state.json")
                        if should_fail:
                            raise OSError(f"injected {failed_boundary} failure")
                        real_replace(source, destination)

                    with mock.patch("autodev.control_plane.os.replace", side_effect=fail_replace):
                        failed = control.execute(commands[phase])
                    self.assertEqual(failed.exit_code, 5)
                    state = json.loads((root / ".autodev/state.json").read_text(encoding="utf-8"))
                    self.assertEqual(state["revision"], revision)
                    self.assertEqual(control.execute(Command("validate")).exit_code, 0)
                    projection = root / ".autodev/tasks/TASK-001/contract.md"
                    if phase == "reopen":
                        self.assertTrue(projection.exists())
                    retry = control.execute(commands[phase])
                    self.assertEqual(retry.exit_code, 0, retry.message)
                    if phase == "reopen":
                        self.assertFalse(projection.exists())

    def test_ready_rejects_unsafe_validation_commands_and_incomplete_contracts(self) -> None:
        self.create_task()
        variants: dict[str, object] = {
            "shell string": "python3 -m unittest",
            "environment": {"argv": ["python3"], "cwd": ".", "timeout": 10, "env": {"TOKEN": "x"}},
            "empty argv": {"argv": [], "cwd": ".", "timeout": 10},
            "disallowed executable": {"argv": ["bash"], "cwd": ".", "timeout": 10},
            "absolute cwd": {"argv": ["python3"], "cwd": "/tmp", "timeout": 10},
            "escaping cwd": {"argv": ["python3"], "cwd": "../outside", "timeout": 10},
            "out of policy cwd": {"argv": ["python3"], "cwd": "docs", "timeout": 10},
        }
        for label, validation in variants.items():
            with self.subTest(label=label):
                contract = full_contract()
                contract["validation_commands"] = [validation]
                write_json(self.contract_path, contract)
                result = self.control().execute(
                    self.command("task.ready", {"id": "TASK-001"}, expected=1)
                )
                self.assertEqual(result.exit_code, 1)
                self.assertEqual(self.state()["revision"], 1)

        contract = full_contract()
        del contract["objective"]
        write_json(self.contract_path, contract)
        missing = self.control().execute(self.command("task.ready", {"id": "TASK-001"}, expected=1))
        self.assertEqual(missing.exit_code, 1)


class RequirementsAndCompletionTests(ControlPlaneFixture):
    def test_requirements_parser_returns_only_approved_fields_and_rejects_bad_rows(self) -> None:
        result = self.control().execute(self.command("validate"))
        self.assertEqual(result.exit_code, 0)
        parsed = result.data["requirements"]
        self.assertEqual(
            parsed[0],
            {
                "id": "REQ-001",
                "priority": "MUST",
                "status": "ACCEPTED",
                "acceptance_signal": "Tests pass.",
            },
        )
        self.assertNotIn("prose", json.dumps(parsed).lower())

        requirements_path = self.root / "docs/REQUIREMENTS.md"
        original = requirements_path.read_text(encoding="utf-8")
        for bad in (
            original.replace("| MUST |", "| URGENT |", 1),
            original.replace("| ACCEPTED |", "| DONE |", 1),
            original + "| REQ-001 | MUST | Duplicate. | Duplicate. | ACCEPTED |\n",
        ):
            requirements_path.write_text(bad, encoding="utf-8")
            self.assertEqual(self.control().execute(self.command("validate")).exit_code, 1)
        requirements_path.write_text(original, encoding="utf-8")

    def completion_state(self) -> dict[str, object]:
        state = self.state()
        state.update(
            project_status="ACTIVE",
            accepted_requirement_ids=["REQ-001"],
            blocking_debt_ids=[],
            full_validation_passed=True,
            current_task_id=None,
            current_run_id=None,
            active_lock=None,
            blocker=None,
        )
        return state

    def test_complete_fails_closed_for_each_missing_prerequisite(self) -> None:
        base = self.completion_state()
        cases = {
            "MUST evidence": {"accepted_requirement_ids": []},
            "blocking debt": {"blocking_debt_ids": ["DEBT-001"]},
            "full validation": {"full_validation_passed": False},
            "current Task": {"current_task_id": "TASK-999"},
            "current run": {"current_run_id": "RUN-001"},
            "active lock": {"active_lock": "LOCK-001"},
            "blocker": {"blocker": "Need a decision."},
        }
        for label, replacement in cases.items():
            with self.subTest(label=label):
                state = dict(base)
                state.update(replacement)
                write_json(self.root / ".autodev/state.json", state)
                result = self.control().execute(self.command("complete", expected=0))
                self.assertEqual(result.exit_code, 2)
                self.assertEqual(self.state()["project_status"], "ACTIVE")
                self.assertEqual(self.state()["revision"], 0)

        write_json(self.root / ".autodev/state.json", base)
        (self.root / ".autodev/locks/runner.lock").write_text("busy", encoding="utf-8")
        self.assertEqual(self.control().execute(self.command("complete", expected=0)).exit_code, 2)

    def test_complete_requires_blocking_tasks_accepted_and_succeeds_only_when_derived(self) -> None:
        created = self.control().execute(
            self.command(
                "task.create",
                {
                    "id": "TASK-001",
                    "title": "Completion task",
                    "risk": "HIGH",
                    "quality_mode": "BUILD",
                    "requirements": ["REQ-001"],
                },
                expected=0,
            )
        )
        write_json(self.root / ".autodev/tasks/TASK-001/contract.json", full_contract())
        ready = self.control().execute(
            self.command("task.ready", {"id": "TASK-001"}, expected=created.revision)
        )
        state = self.completion_state()
        record = state["tasks"]["TASK-001"]
        record["evidence_ids"] = ["EVIDENCE-001"]
        write_json(self.root / ".autodev/state.json", state)
        refused = self.control().execute(self.command("complete", expected=ready.revision))
        self.assertEqual(refused.exit_code, 2)

        record["status"] = "ACCEPTED"
        write_json(self.root / ".autodev/state.json", state)
        accepted = self.control().execute(
            self.command("complete", {"proposal": "COMPLETE"}, expected=ready.revision)
        )
        self.assertEqual(accepted.exit_code, 0, accepted.message)
        self.assertEqual(
            (self.state()["project_status"], self.state()["revision"]),
            ("COMPLETE", ready.revision + 1),
        )


class CliContractTests(ControlPlaneFixture):
    def invoke(self, arguments: list[str]) -> tuple[int, str, str]:
        output = io.StringIO()
        error = io.StringIO()
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                exit_code = main(arguments)
        finally:
            os.chdir(previous)
        return exit_code, output.getvalue(), error.getvalue()

    def test_json_and_human_rendering_are_stable(self) -> None:
        code, output, error = self.invoke(["status", "--json"])
        self.assertEqual((code, error), (0, ""))
        payload = json.loads(output)
        self.assertEqual((payload["status"], payload["exit_code"]), ("SUCCESS", 0))
        self.assertEqual(payload["data"]["project_status"], "BOOTSTRAP")

        code, output, error = self.invoke(["validate", "--ready"])
        self.assertEqual((code, error), (2, ""))
        self.assertEqual(output, "project is valid but not active\n")

        activated, output, _ = self.invoke(["activate"])
        self.assertEqual(activated, 0)
        self.assertEqual(output, "activate succeeded\n")

    def test_task_cli_routes_create_show_ready_and_reopen(self) -> None:
        created, _, _ = self.invoke(
            [
                "task",
                "create",
                "--id",
                "TASK-001",
                "--title",
                "State transition",
                "--risk",
                "HIGH",
                "--quality-mode",
                "BUILD",
                "--requirements",
                "REQ-001",
            ]
        )
        self.assertEqual(created, 0)
        shown, output, _ = self.invoke(["task", "show", "TASK-001"])
        self.assertEqual(shown, 0)
        self.assertIn("TASK-001", output)
        self.assertIn("State transition", output)

        write_json(self.root / ".autodev/tasks/TASK-001/contract.json", full_contract())
        self.assertEqual(self.invoke(["task", "ready", "TASK-001"])[0], 0)
        self.assertEqual(
            self.invoke(["task", "reopen", "TASK-001", "--reason", "Contract changed."])[0],
            0,
        )

    def test_cli_uses_all_frozen_exit_code_meanings(self) -> None:
        self.assertEqual(self.invoke(["task", "ready", "BAD"])[0], 1)
        self.assertEqual(self.invoke(["complete"])[0], 2)
        state_path = self.root / ".autodev/state.json"
        for project_status, expected in (("BLOCKED", 3), ("STOPPED", 4), ("FAILED", 5)):
            state = self.state()
            state["project_status"] = project_status
            if project_status == "BLOCKED":
                state["blocker"] = "Need a product decision."
                state["next_owner"] = "HUMAN"
            write_json(state_path, state)
            code, output, error = self.invoke(["status"])
            self.assertEqual((code, error), (expected, ""))
            self.assertEqual(output, f"project is {project_status}\n")

    def test_command_result_json_matches_packaged_schema(self) -> None:
        from autodev import CommandResult

        value = CommandResult("STOPPED", "stopped", 7, {"reason": "user"}).to_dict()
        schema = json.loads(_read_text("schemas/command-result.schema.json"))
        self.assertFalse(list(Draft202012Validator(schema).iter_errors(value)))


if __name__ == "__main__":
    unittest.main()
