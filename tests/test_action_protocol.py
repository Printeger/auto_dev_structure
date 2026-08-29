from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import inspect
from pathlib import Path
from unittest import mock


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "src"))

from autodev._project import initialize_project
from autodev.action import ActionController
from autodev.campaign import CampaignController, CampaignRequest, FakePlanner
from autodev.campaign_workspace import CampaignWorkspace, CampaignWorkspaceError
from autodev.control_plane import Command, ControlPlane
from autodev.quality import QualityDecision, QualityRouter


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(["git", *arguments], cwd=root, capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def task(*, risk: str = "MEDIUM", change_classes: list[str] | None = None) -> dict[str, object]:
    return {
        "id": "TASK-001",
        "title": "Change the value",
        "objective": "Change app.py.",
        "requirements": ["REQ-001"],
        "dependencies": [],
        "priority": "MUST",
        "blocking": True,
        "risk": risk,
        "quality_mode": "BUILD",
        "change_classes": change_classes or ["implementation"],
        "allowed_paths": ["app.py"],
        "out_of_scope": [],
        "acceptance_criteria": [{"id": "AC-001", "description": "The value changes."}],
        "validation_commands": [
            {"argv": ["python3", "-c", "assert open('app.py').read() == 'VALUE = 2\\n'"], "cwd": ".", "timeout": 20}
        ],
        "prohibited_actions": ["commit", "push", "publish", "deploy"],
    }


def proposal(tasks: list[dict[str, object]]) -> dict[str, object]:
    return {
        "requirements": [{
            "id": "REQ-001",
            "priority": "MUST",
            "statement": "Change the value.",
            "acceptance_signal": "Validation passes.",
        }],
        "authority_envelope": {
            "max_task_risk": "HIGH",
            "allowed_change_classes": [
                "implementation", "test", "documentation", "architecture",
                "internal-interface", "shared-internal-data", "security", "migration",
            ],
            "dependency_policy": "existing-only",
            "public_api_changes": "allow",
            "security_changes": "allow",
            "data_migration": "allow",
            "permission_expansion": "require-human",
            "remote_actions": "forbidden",
        },
        "phase": "SCAFFOLD",
        "tasks": tasks,
        "questions": [],
    }


def planner_question() -> dict[str, object]:
    return {
        "id": "approach", "header": "Approach",
        "question": "Which implementation should be used?",
        "options": [
            {"label": "Simple", "description": "Use the smallest implementation."},
            {"label": "Layered", "description": "Add an abstraction layer."},
        ],
        "isOther": False, "isSecret": False,
    }


class ActionProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "test@example.com")
        git(self.root, "config", "user.name", "Test")
        self.assertEqual(initialize_project(self.root, "action-protocol").status, "SUCCESS")
        (self.root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "base")

    def approve(self, proposed_task: dict[str, object] | None = None) -> str:
        campaign = CampaignController(
            self.root, FakePlanner([proposal([proposed_task or task()])])
        )
        planned = campaign.plan(CampaignRequest("Change value", target="ARCHITECTURE_BASELINE"))
        self.assertEqual(planned.status, "SUCCESS", planned)
        approved = campaign.approve("CAMP-001", planned.data["proposal_hash"])
        self.assertEqual(approved.status, "SUCCESS", approved)
        return "CAMP-001"

    def approve_without_phase_tasks(self) -> str:
        campaign = CampaignController(self.root, FakePlanner([proposal([])]))
        planned = campaign.plan(CampaignRequest("Plan then change", target="ARCHITECTURE_BASELINE"))
        self.assertEqual(planned.status, "SUCCESS", planned)
        approved = campaign.approve("CAMP-001", planned.data["proposal_hash"])
        self.assertEqual(approved.status, "NOT_READY", approved)
        return "CAMP-001"

    def snapshot(self) -> tuple[dict[str, bytes], str]:
        files = {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in sorted((self.root / ".autodev").rglob("*"))
            if path.is_file() and path.name != ".control-plane.lock"
        }
        refs = git(self.root, "for-each-ref", "--format=%(refname) %(objectname)", "refs/autodev")
        return files, refs

    def test_pending_action_is_persistent_and_stable_across_restart(self) -> None:
        campaign_id = self.approve()

        first = ActionController(self.root).get_next_action(campaign_id)
        second = ActionController(self.root).get_next_action(campaign_id)

        self.assertEqual(first.status, "SUCCESS", first)
        self.assertEqual(first.action, second.action)
        self.assertEqual(first.action["type"], "EXECUTE_TASK")
        self.assertEqual(first.action["task_id"], "TASK-001")
        self.assertEqual(first.action["quality_route"], "NONE")
        self.assertTrue(Path(first.action["workspace"]).is_dir())
        persisted = json.loads(
            (self.root / ".autodev/actions" / first.action["id"] / "action.json").read_text()
        )
        self.assertEqual(persisted, first.action)

    def test_public_controller_exposes_only_the_two_workflow_methods(self) -> None:
        methods = {
            name for name, member in inspect.getmembers(ActionController, inspect.isfunction)
            if not name.startswith("_")
        }
        self.assertEqual(methods, {"get_next_action", "submit_action_result"})

    def test_action_context_is_bounded_and_uses_canonical_references(self) -> None:
        huge_task = task()
        huge_task["objective"] = "x" * 100_000
        campaign_id = self.approve(huge_task)

        action = ActionController(self.root).get_next_action(campaign_id).action

        encoded_context = json.dumps(action["context"], separators=(",", ":")).encode()
        self.assertLess(len(encoded_context), 2_048)
        self.assertEqual(action["context"]["contract_ref"], "tasks/TASK-001/contract.json")
        self.assertIn("contract_hash", action["context"])
        self.assertNotIn("contract", action["context"])
        self.assertNotIn("x" * 100, encoded_context.decode())

    def test_restart_publishes_action_after_state_write_fault_between_running_and_publication(self) -> None:
        campaign_id = self.approve()
        from autodev import control_plane

        original = control_plane._atomic_replace_json
        failed_once = False

        def fail_action_state(path: Path, value: object) -> None:
            nonlocal failed_once
            if (
                not failed_once
                and path.name == "state.json"
                and isinstance(value, dict)
                and value.get("current_action_id") is not None
            ):
                failed_once = True
                raise OSError("injected Action publication state fault")
            original(path, value)

        with mock.patch.object(control_plane, "_atomic_replace_json", side_effect=fail_action_state):
            interrupted = ActionController(self.root).get_next_action(campaign_id)
        self.assertEqual(interrupted.status, "INFRA_FAILURE", interrupted)
        interrupted_state = json.loads((self.root / ".autodev/state.json").read_text())
        self.assertEqual(interrupted_state["tasks"]["TASK-001"]["status"], "RUNNING")
        self.assertIsNone(interrupted_state["current_action_id"])

        recovered = ActionController(self.root).get_next_action(campaign_id)

        self.assertEqual(recovered.status, "SUCCESS", recovered)
        self.assertEqual(recovered.action["type"], "EXECUTE_TASK")
        self.assertEqual(recovered.action["task_id"], "TASK-001")
        self.assertEqual(
            json.loads((self.root / ".autodev/state.json").read_text())["current_action_id"],
            recovered.action["id"],
        )

    def test_restart_publishes_immediate_review_after_worker_handoff_fault(self) -> None:
        campaign_id = self.approve(task(risk="HIGH"))
        controller = ActionController(self.root)
        worker = dict(controller.get_next_action(campaign_id).action or {})
        Path(worker["workspace"]).joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")
        from autodev import control_plane

        original = control_plane._atomic_replace_json
        failed_once = False

        def fail_reviewer_publication(path: Path, value: object) -> None:
            nonlocal failed_once
            if (
                not failed_once and path.name == "state.json" and isinstance(value, dict)
                and value.get("current_action_id") is not None
                and value.get("current_action_id") != worker["id"]
                and value.get("tasks", {}).get("TASK-001", {}).get("status") == "REVIEWING"
            ):
                failed_once = True
                raise OSError("injected immediate Reviewer publication fault")
            original(path, value)

        with mock.patch.object(control_plane, "_atomic_replace_json", side_effect=fail_reviewer_publication):
            interrupted = controller.submit_action_result(worker["id"], self.passing_result(worker))

        self.assertEqual(interrupted.status, "INFRA_FAILURE", interrupted)
        state = json.loads((self.root / ".autodev/state.json").read_text())
        self.assertEqual(state["tasks"]["TASK-001"]["status"], "REVIEWING")
        self.assertIsNone(state["current_action_id"])

        result = self.passing_result(worker)
        conflicting = dict(result, summary="conflicting duplicate")
        before_conflict = self.snapshot()
        conflict = ActionController(self.root).submit_action_result(worker["id"], conflicting)
        self.assertEqual(conflict.status, "INVALID", conflict)
        self.assertEqual(self.snapshot(), before_conflict)

        recovered = ActionController(self.root).submit_action_result(worker["id"], result)
        repeated = ActionController(self.root).get_next_action(campaign_id)

        self.assertEqual(recovered.status, "SUCCESS", recovered)
        self.assertEqual(recovered.action["type"], "RUN_IMMEDIATE_REVIEW")
        self.assertEqual(recovered.action, repeated.action)
        attempt = json.loads(
            (self.root / f".autodev/runs/{worker['context']['run_id']}/action-attempt.json").read_text()
        )
        self.assertEqual(recovered.action["id"], attempt["review_action_id"])
        self.assertEqual(len(list((self.root / ".autodev/actions").glob("*/action.json"))), 2)

    def test_retry_reconciles_ref_advanced_before_canonical_checkpoint(self) -> None:
        campaign_id = self.approve()
        controller = ActionController(self.root)
        action = dict(controller.get_next_action(campaign_id).action or {})
        Path(action["workspace"]).joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")
        result = self.passing_result(action)
        initial_checkpoint = json.loads(
            (self.root / ".autodev/state.json").read_text()
        )["campaigns"][campaign_id]["checkpoint"]
        from autodev import control_plane

        original = control_plane._atomic_replace_json
        failed_once = False

        def fail_checkpoint_state(path: Path, value: object) -> None:
            nonlocal failed_once
            if (
                not failed_once
                and path.name == "state.json"
                and isinstance(value, dict)
                and value.get("campaigns", {}).get(campaign_id, {}).get("checkpoint")
                != initial_checkpoint
            ):
                failed_once = True
                raise OSError("injected canonical checkpoint state fault")
            original(path, value)

        with mock.patch.object(control_plane, "_atomic_replace_json", side_effect=fail_checkpoint_state):
            interrupted = controller.submit_action_result(action["id"], result)
        self.assertEqual(interrupted.status, "INFRA_FAILURE", interrupted)
        self.assertNotEqual(
            git(self.root, "rev-parse", "refs/autodev/campaigns/CAMP-001/current"),
            initial_checkpoint,
        )
        self.assertEqual(
            json.loads((self.root / ".autodev/state.json").read_text())["campaigns"][campaign_id]["checkpoint"],
            initial_checkpoint,
        )

        recovered = ActionController(self.root).submit_action_result(action["id"], result)

        self.assertEqual(recovered.status, "SUCCESS", recovered)
        self.assertEqual(recovered.action["type"], "TARGET_REACHED")
        journals = list((self.root / ".autodev/campaigns/CAMP-001/checkpoint-journal").glob("*.json"))
        self.assertEqual(len(journals), 1)
        self.assertEqual(json.loads(journals[0].read_text())["phase"], "COMMITTED")

    def test_retry_converges_after_accepted_task_cleanup_fault(self) -> None:
        campaign_id = self.approve()
        controller = ActionController(self.root)
        action = dict(controller.get_next_action(campaign_id).action or {})
        Path(action["workspace"]).joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")
        result = self.passing_result(action)
        original_cleanup = CampaignWorkspace.remove_task_workspace
        failed_once = False

        def fail_accepted_cleanup(owner: CampaignWorkspace, workspace: Path) -> None:
            nonlocal failed_once
            state = json.loads((self.root / ".autodev/state.json").read_text())
            finalization = json.loads(
                (self.root / f".autodev/actions/{action['id']}/finalization.json").read_text()
            )
            if (
                not failed_once
                and workspace.resolve() == Path(action["workspace"]).resolve()
                and state["tasks"]["TASK-001"]["status"] == "ACCEPTED"
                and finalization["phase"] == "PROGRESSED"
            ):
                failed_once = True
                raise CampaignWorkspaceError("injected accepted cleanup fault")
            original_cleanup(owner, workspace)

        with mock.patch.object(
            CampaignWorkspace, "remove_task_workspace", autospec=True,
            side_effect=fail_accepted_cleanup,
        ):
            interrupted = controller.submit_action_result(action["id"], result)

        self.assertEqual(interrupted.status, "INFRA_FAILURE", interrupted)
        accepted = json.loads((self.root / ".autodev/state.json").read_text())
        self.assertEqual(accepted["tasks"]["TASK-001"]["status"], "ACCEPTED")
        self.assertNotEqual(accepted["current_action_id"], action["id"])

        recovered = ActionController(self.root).submit_action_result(action["id"], result)

        self.assertEqual(recovered.status, "SUCCESS", recovered)
        self.assertEqual(recovered.action["type"], "TARGET_REACHED")
        self.assertFalse(Path(action["workspace"]).exists())

    def test_retry_converges_after_accepted_task_action_resolve_fault(self) -> None:
        campaign_id = self.approve()
        controller = ActionController(self.root)
        action = dict(controller.get_next_action(campaign_id).action or {})
        Path(action["workspace"]).joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")
        result = self.passing_result(action)
        from autodev import control_plane

        original = control_plane._atomic_replace_json
        failed_once = False

        def fail_resolve_state(path: Path, value: object) -> None:
            nonlocal failed_once
            if (
                not failed_once and path.name == "state.json" and isinstance(value, dict)
                and value.get("current_action_id") is None
                and value.get("tasks", {}).get("TASK-001", {}).get("status") == "ACCEPTED"
            ):
                failed_once = True
                raise OSError("injected accepted Action resolve fault")
            original(path, value)

        with mock.patch.object(control_plane, "_atomic_replace_json", side_effect=fail_resolve_state):
            interrupted = controller.submit_action_result(action["id"], result)

        self.assertEqual(interrupted.status, "INFRA_FAILURE", interrupted)
        state = json.loads((self.root / ".autodev/state.json").read_text())
        self.assertEqual(state["tasks"]["TASK-001"]["status"], "ACCEPTED")
        self.assertEqual(state["current_action_id"], action["id"])

        recovered = ActionController(self.root).submit_action_result(action["id"], result)

        self.assertEqual(recovered.status, "SUCCESS", recovered)
        self.assertEqual(recovered.action["type"], "TARGET_REACHED")

    def test_retry_converges_after_accepted_immediate_review_cleanup_fault(self) -> None:
        campaign_id = self.approve(task(risk="HIGH"))
        controller = ActionController(self.root)
        worker = dict(controller.get_next_action(campaign_id).action or {})
        Path(worker["workspace"]).joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")
        review = dict(
            controller.submit_action_result(worker["id"], self.passing_result(worker)).action or {}
        )
        result = self.passing_result(review)
        original_cleanup = CampaignWorkspace.remove_task_workspace
        failed_once = False

        def fail_accepted_review_cleanup(owner: CampaignWorkspace, workspace: Path) -> None:
            nonlocal failed_once
            state = json.loads((self.root / ".autodev/state.json").read_text())
            if (
                not failed_once
                and workspace.resolve() == Path(review["workspace"]).resolve()
                and state["tasks"]["TASK-001"]["status"] == "ACCEPTED"
                and state["current_action_id"] != review["id"]
            ):
                failed_once = True
                raise CampaignWorkspaceError("injected accepted Reviewer cleanup fault")
            original_cleanup(owner, workspace)

        with mock.patch.object(
            CampaignWorkspace, "remove_task_workspace", autospec=True,
            side_effect=fail_accepted_review_cleanup,
        ):
            interrupted = controller.submit_action_result(review["id"], result)

        self.assertEqual(interrupted.status, "INFRA_FAILURE", interrupted)
        accepted = json.loads((self.root / ".autodev/state.json").read_text())
        self.assertEqual(accepted["tasks"]["TASK-001"]["status"], "ACCEPTED")
        self.assertNotEqual(accepted["current_action_id"], review["id"])
        conflicting = dict(result)
        conflicting["summary"] = "Conflicting retry content."
        before_conflict = self.snapshot()

        rejected = ActionController(self.root).submit_action_result(review["id"], conflicting)

        self.assertEqual(rejected.status, "INVALID", rejected)
        self.assertEqual(self.snapshot(), before_conflict)

        recovered = ActionController(self.root).submit_action_result(review["id"], result)

        self.assertEqual(recovered.status, "SUCCESS", recovered)
        self.assertEqual(recovered.action["type"], "TARGET_REACHED")
        self.assertFalse(Path(review["workspace"]).exists())

    def test_retry_converges_after_accepted_immediate_review_resolve_fault(self) -> None:
        campaign_id = self.approve(task(risk="HIGH"))
        controller = ActionController(self.root)
        worker = dict(controller.get_next_action(campaign_id).action or {})
        Path(worker["workspace"]).joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")
        review = dict(
            controller.submit_action_result(worker["id"], self.passing_result(worker)).action or {}
        )
        result = self.passing_result(review)
        from autodev import control_plane

        original = control_plane._atomic_replace_json
        failed_once = False

        def fail_reviewer_resolve_state(path: Path, value: object) -> None:
            nonlocal failed_once
            if (
                not failed_once and path.name == "state.json" and isinstance(value, dict)
                and value.get("current_action_id") is None
                and value.get("tasks", {}).get("TASK-001", {}).get("status") == "ACCEPTED"
            ):
                failed_once = True
                raise OSError("injected accepted Reviewer resolve fault")
            original(path, value)

        with mock.patch.object(control_plane, "_atomic_replace_json", side_effect=fail_reviewer_resolve_state):
            interrupted = controller.submit_action_result(review["id"], result)

        self.assertEqual(interrupted.status, "INFRA_FAILURE", interrupted)
        state = json.loads((self.root / ".autodev/state.json").read_text())
        self.assertEqual(state["tasks"]["TASK-001"]["status"], "ACCEPTED")
        self.assertEqual(state["current_action_id"], review["id"])
        conflicting = dict(result, summary="conflicting immediate Review")
        before = self.snapshot()
        self.assertEqual(
            ActionController(self.root).submit_action_result(review["id"], conflicting).status,
            "INVALID",
        )
        self.assertEqual(self.snapshot(), before)

        recovered = ActionController(self.root).submit_action_result(review["id"], result)

        self.assertEqual(recovered.status, "SUCCESS", recovered)
        self.assertEqual(recovered.action["type"], "TARGET_REACHED")

    def test_full_fake_campaign_trace_starts_with_plan_and_reaches_target(self) -> None:
        campaign_id = self.approve_without_phase_tasks()
        controller = ActionController(self.root)

        planning = controller.get_next_action(campaign_id)
        self.assertEqual(planning.action["type"], "PLAN_PHASE")
        plan_result = self.passing_result(dict(planning.action))
        plan_result["data"] = {"phase": "SCAFFOLD", "tasks": [task()], "questions": []}
        executing = controller.submit_action_result(planning.action["id"], plan_result)
        self.assertEqual(executing.status, "SUCCESS", executing)
        self.assertEqual(executing.action["type"], "EXECUTE_TASK")
        Path(executing.action["workspace"]).joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")

        reached = controller.submit_action_result(
            executing.action["id"], self.passing_result(dict(executing.action)),
        )

        self.assertEqual(reached.status, "SUCCESS", reached)
        self.assertEqual(reached.action["type"], "TARGET_REACHED")
        materialization_journal = json.loads(
            (self.root / ".autodev/campaigns/CAMP-001/materialization-journal.json").read_text()
        )
        self.assertEqual(materialization_journal["phase"], "COMMITTED")

    def test_native_plan_questions_persist_and_resume_with_a_fresh_plan_after_restart(self) -> None:
        campaign_id = self.approve_without_phase_tasks()
        controller = ActionController(self.root)
        planning = dict(controller.get_next_action(campaign_id).action or {})
        question = planner_question()
        result = self.passing_result(planning)
        result["data"] = {"phase": "SCAFFOLD", "tasks": [], "questions": [question]}

        asking = controller.submit_action_result(planning["id"], result)

        self.assertEqual(asking.status, "SUCCESS", asking)
        self.assertEqual(asking.action["type"], "ASK_HUMAN")
        self.assertEqual(asking.action, ActionController(self.root).get_next_action(campaign_id).action)
        request_id = asking.action["context"]["request_id"]
        answered = CampaignController(self.root).answer(
            campaign_id, request_id, {"approach": ["Simple"]},
        )
        self.assertEqual(answered.status, "SUCCESS", answered)

        resumed = dict(ActionController(self.root).get_next_action(campaign_id).action or {})
        self.assertEqual(resumed["type"], "PLAN_PHASE")
        self.assertNotEqual(resumed["id"], planning["id"])
        self.assertEqual(
            resumed["context"]["answers"],
            [{"question_id": "approach", "values": ["Simple"]}],
        )
        self.assertEqual(resumed, ActionController(self.root).get_next_action(campaign_id).action)
        resumed_result = self.passing_result(resumed)
        resumed_result["data"] = {"phase": "SCAFFOLD", "tasks": [task()], "questions": []}
        executing = ActionController(self.root).submit_action_result(resumed["id"], resumed_result)
        self.assertEqual(executing.action["type"], "EXECUTE_TASK")
        Path(executing.action["workspace"]).joinpath("app.py").write_text(
            "VALUE = 2\n", encoding="utf-8",
        )
        reached = ActionController(self.root).submit_action_result(
            executing.action["id"], self.passing_result(dict(executing.action)),
        )
        self.assertEqual(reached.action["type"], "TARGET_REACHED")

    def test_plan_admission_retries_between_operation_resolve_and_cleanup(self) -> None:
        campaign_id = self.approve_without_phase_tasks()
        controller = ActionController(self.root)
        planning = dict(controller.get_next_action(campaign_id).action or {})
        result = self.passing_result(planning)
        result["data"] = {"phase": "SCAFFOLD", "tasks": [task()], "questions": []}
        from autodev import control_plane

        original = control_plane._atomic_replace_json
        failed_once = False

        def fail_plan_resolve(path: Path, value: object) -> None:
            nonlocal failed_once
            if (
                not failed_once and path.name == "state.json" and isinstance(value, dict)
                and value.get("current_action_id") is None
                and "TASK-001" in value.get("tasks", {})
            ):
                failed_once = True
                raise OSError("injected Planner resolve fault")
            original(path, value)

        with mock.patch.object(control_plane, "_atomic_replace_json", side_effect=fail_plan_resolve):
            interrupted = controller.submit_action_result(planning["id"], result)
        self.assertEqual(interrupted.status, "INFRA_FAILURE", interrupted)
        self.assertTrue(Path(planning["workspace"]).exists())
        self.assertEqual(list(json.loads((self.root / ".autodev/state.json").read_text())["tasks"]), ["TASK-001"])
        conflicting = dict(result, summary="conflicting Planner result")
        before = self.snapshot()
        self.assertEqual(
            ActionController(self.root).submit_action_result(planning["id"], conflicting).status,
            "INVALID",
        )
        self.assertEqual(self.snapshot(), before)

        recovered = ActionController(self.root).submit_action_result(planning["id"], result)

        self.assertEqual(recovered.action["type"], "EXECUTE_TASK")
        self.assertEqual(list(json.loads((self.root / ".autodev/state.json").read_text())["tasks"]), ["TASK-001"])
        self.assertFalse(Path(planning["workspace"]).exists())

    def test_plan_admission_retries_cleanup_after_resolve(self) -> None:
        campaign_id = self.approve_without_phase_tasks()
        controller = ActionController(self.root)
        planning = dict(controller.get_next_action(campaign_id).action or {})
        result = self.passing_result(planning)
        result["data"] = {"phase": "SCAFFOLD", "tasks": [task()], "questions": []}
        original_cleanup = CampaignWorkspace.remove_task_workspace
        failed_once = False

        def fail_plan_cleanup(owner: CampaignWorkspace, workspace: Path) -> None:
            nonlocal failed_once
            intent = json.loads(
                (self.root / f".autodev/actions/{planning['id']}/finalization.json").read_text()
            )
            if (
                not failed_once and workspace.resolve() == Path(planning["workspace"]).resolve()
                and intent["phase"] == "PROGRESSED"
            ):
                failed_once = True
                raise CampaignWorkspaceError("injected Planner cleanup fault")
            original_cleanup(owner, workspace)

        with mock.patch.object(
            CampaignWorkspace, "remove_task_workspace", autospec=True, side_effect=fail_plan_cleanup,
        ):
            interrupted = controller.submit_action_result(planning["id"], result)
        self.assertEqual(interrupted.status, "INFRA_FAILURE", interrupted)
        self.assertTrue(Path(planning["workspace"]).exists())
        recovered = ActionController(self.root).submit_action_result(planning["id"], result)
        self.assertEqual(recovered.action["type"], "EXECUTE_TASK")
        self.assertFalse(Path(planning["workspace"]).exists())

    def _assert_plan_question_recovery(self, fault_window: str) -> None:
        campaign_id = self.approve_without_phase_tasks()
        controller = ActionController(self.root)
        planning = dict(controller.get_next_action(campaign_id).action or {})
        result = self.passing_result(planning)
        result["data"] = {
            "phase": "SCAFFOLD", "tasks": [], "questions": [planner_question()],
        }
        if fault_window == "resolve":
            from autodev import control_plane
            original = control_plane._atomic_replace_json
            failed_once = False

            def fail(path: Path, value: object) -> None:
                nonlocal failed_once
                if (
                    not failed_once and path.name == "state.json" and isinstance(value, dict)
                    and value.get("current_action_id") is None
                ):
                    failed_once = True
                    raise OSError("injected question resolve fault")
                original(path, value)

            context = mock.patch.object(control_plane, "_atomic_replace_json", side_effect=fail)
        else:
            original_cleanup = CampaignWorkspace.remove_task_workspace
            failed_once = False

            def fail(owner: CampaignWorkspace, workspace: Path) -> None:
                nonlocal failed_once
                intent = json.loads(
                    (self.root / f".autodev/actions/{planning['id']}/finalization.json").read_text()
                )
                if (
                    not failed_once and workspace.resolve() == Path(planning["workspace"]).resolve()
                    and intent["phase"] == "PROGRESSED"
                ):
                    failed_once = True
                    raise CampaignWorkspaceError("injected question cleanup fault")
                original_cleanup(owner, workspace)

            context = mock.patch.object(
                CampaignWorkspace, "remove_task_workspace", autospec=True, side_effect=fail,
            )
        with context:
            interrupted = controller.submit_action_result(planning["id"], result)
        self.assertEqual(interrupted.status, "INFRA_FAILURE", interrupted)
        requests = list((self.root / ".autodev/campaigns/CAMP-001/human-requests").glob("*.json"))
        self.assertEqual(len(requests), 1)
        recovered = ActionController(self.root).submit_action_result(planning["id"], result)
        self.assertEqual(recovered.action["type"], "ASK_HUMAN")
        self.assertEqual(len(list(requests[0].parent.glob("*.json"))), 1)
        self.assertFalse(Path(planning["workspace"]).exists())

    def test_plan_questions_retry_between_request_and_resolve(self) -> None:
        self._assert_plan_question_recovery("resolve")

    def test_plan_questions_retry_cleanup_after_resolve(self) -> None:
        self._assert_plan_question_recovery("cleanup")

    def test_read_only_planner_rejects_a_clean_commit_without_mutation(self) -> None:
        campaign_id = self.approve_without_phase_tasks()
        controller = ActionController(self.root)
        planning = dict(controller.get_next_action(campaign_id).action or {})
        git(Path(planning["workspace"]), "commit", "--allow-empty", "-qm", "forbidden")
        result = self.passing_result(planning)
        result["data"] = {"phase": "SCAFFOLD", "tasks": [task()], "questions": []}
        before = self.snapshot()

        rejected = controller.submit_action_result(planning["id"], result)

        self.assertEqual(rejected.status, "INVALID", rejected)
        self.assertIn("read-only", rejected.message)
        self.assertEqual(self.snapshot(), before)

    @staticmethod
    def passing_result(action: dict[str, object]) -> dict[str, object]:
        return {
            "action_id": action["id"],
            "action_type": action["type"],
            "canonical_revision": action["canonical_revision"],
            "outcome": "PASS",
            "summary": "Implemented the requested change.",
            "data": {},
            "findings": [],
            "blocker": None,
            "next_action": None,
        }

    def diagnostic_action(self, campaign_id: str) -> dict[str, object]:
        controller = ActionController(self.root)
        first = dict(controller.get_next_action(campaign_id).action or {})
        Path(first["workspace"]).joinpath("app.py").write_text("VALUE = 3\n", encoding="utf-8")
        self.assertEqual(
            controller.submit_action_result(first["id"], self.passing_result(first)).status,
            "NOT_READY",
        )
        second = dict(controller.get_next_action(campaign_id).action or {})
        Path(second["workspace"]).joinpath("app.py").write_text("VALUE = 3\n", encoding="utf-8")
        diagnostic = controller.submit_action_result(second["id"], self.passing_result(second))
        self.assertEqual(diagnostic.action["type"], "RUN_DIAGNOSTIC")
        return dict(diagnostic.action)

    def test_worker_submission_is_independently_validated_checkpointed_and_retry_safe(self) -> None:
        campaign_id = self.approve()
        controller = ActionController(self.root)
        pending = controller.get_next_action(campaign_id)
        action = dict(pending.action or {})
        Path(action["workspace"]).joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")
        result = self.passing_result(action)

        accepted = controller.submit_action_result(action["id"], result)
        retry = ActionController(self.root).submit_action_result(action["id"], result)

        self.assertEqual(accepted.status, "SUCCESS", accepted)
        self.assertEqual(accepted, retry)
        self.assertEqual(accepted.action["type"], "TARGET_REACHED")
        self.assertEqual((self.root / "app.py").read_text(), "VALUE = 2\n")
        state = json.loads((self.root / ".autodev/state.json").read_text())
        self.assertEqual(state["tasks"]["TASK-001"]["status"], "ACCEPTED")
        self.assertEqual(state["campaigns"][campaign_id]["status"], "TARGET_REACHED")
        self.assertEqual(
            state["campaigns"][campaign_id]["checkpoint"],
            state["campaigns"][campaign_id]["last_materialized_checkpoint"],
        )
        self.assertTrue((self.root / ".autodev/runs" / action["context"]["run_id"] / "evidence.json").is_file())

    def test_unknown_malformed_stale_and_conflicting_results_have_zero_mutation(self) -> None:
        campaign_id = self.approve()
        controller = ActionController(self.root)
        action = dict(controller.get_next_action(campaign_id).action or {})
        Path(action["workspace"]).joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")

        before = self.snapshot()
        unknown = controller.submit_action_result("ACTION-" + "0" * 32, self.passing_result(action))
        self.assertEqual(unknown.status, "INVALID")
        self.assertEqual(self.snapshot(), before)

        malformed = self.passing_result(action)
        malformed["untrusted_extra"] = True
        self.assertEqual(controller.submit_action_result(action["id"], malformed).status, "INVALID")
        self.assertEqual(self.snapshot(), before)

        stale = self.passing_result(action)
        stale["canonical_revision"] = int(action["canonical_revision"]) - 1
        self.assertEqual(controller.submit_action_result(action["id"], stale).status, "INVALID")
        self.assertEqual(self.snapshot(), before)

        accepted_result = self.passing_result(action)
        self.assertEqual(controller.submit_action_result(action["id"], accepted_result).status, "SUCCESS")
        after = self.snapshot()
        conflicting = dict(accepted_result, summary="A conflicting duplicate.")
        self.assertEqual(controller.submit_action_result(action["id"], conflicting).status, "INVALID")
        self.assertEqual(self.snapshot(), after)

    def test_worker_rejects_plan_data_before_canonical_mutation(self) -> None:
        campaign_id = self.approve()
        controller = ActionController(self.root)
        action = dict(controller.get_next_action(campaign_id).action or {})
        Path(action["workspace"]).joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")
        result = self.passing_result(action)
        result["data"] = {"phase": "SCAFFOLD", "tasks": [task()], "questions": []}
        before = self.snapshot()

        rejected = controller.submit_action_result(action["id"], result)

        self.assertEqual(rejected.status, "INVALID", rejected)
        self.assertEqual(rejected.message, "malformed Action result")
        self.assertEqual(self.snapshot(), before)

    def test_plan_rejects_open_nested_task_and_action_type_mismatch(self) -> None:
        campaign_id = self.approve_without_phase_tasks()
        controller = ActionController(self.root)
        action = dict(controller.get_next_action(campaign_id).action or {})
        open_task = task()
        open_task["agent_claim"] = "trust me"
        result = self.passing_result(action)
        result["data"] = {"phase": "SCAFFOLD", "tasks": [open_task], "questions": []}
        before = self.snapshot()

        malformed = controller.submit_action_result(action["id"], result)

        self.assertEqual(malformed.status, "INVALID", malformed)
        self.assertEqual(malformed.message, "malformed Action result")
        self.assertEqual(self.snapshot(), before)

        mismatched = self.passing_result(action)
        mismatched["action_type"] = "EXECUTE_TASK"
        before = self.snapshot()
        rejected = controller.submit_action_result(action["id"], mismatched)
        self.assertEqual(rejected.status, "INVALID", rejected)
        self.assertEqual(rejected.message, "Action result type mismatch")
        self.assertEqual(self.snapshot(), before)

    def test_worker_path_rejection_resolves_rework_while_concurrent_source_is_zero_mutation(self) -> None:
        campaign_id = self.approve()
        controller = ActionController(self.root)
        action = dict(controller.get_next_action(campaign_id).action or {})
        workspace = Path(action["workspace"])
        workspace.joinpath("outside.txt").write_text("not allowed\n", encoding="utf-8")
        rejected = controller.submit_action_result(action["id"], self.passing_result(action))

        self.assertEqual(rejected.status, "NOT_READY", rejected)
        self.assertIn("outside-allowed", rejected.message)
        state = json.loads((self.root / ".autodev/state.json").read_text())
        self.assertEqual(state["tasks"]["TASK-001"]["status"], "READY")
        self.assertEqual(state["last_outcome"], "REWORK")

        action = dict(controller.get_next_action(campaign_id).action or {})
        Path(action["workspace"]).joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")
        self.root.joinpath("user-note.txt").write_text("concurrent\n", encoding="utf-8")
        before_concurrent = self.snapshot()

        concurrent = controller.submit_action_result(action["id"], self.passing_result(action))

        self.assertEqual(concurrent.status, "INVALID", concurrent)
        self.assertIn("source changed concurrently", concurrent.message)
        self.assertEqual(self.snapshot(), before_concurrent)

    def test_path_rejection_retry_converges_between_run_finish_and_action_resolve(self) -> None:
        campaign_id = self.approve()
        controller = ActionController(self.root)
        action = dict(controller.get_next_action(campaign_id).action or {})
        Path(action["workspace"]).joinpath("outside.txt").write_text("not allowed\n", encoding="utf-8")
        result = self.passing_result(action)
        from autodev import control_plane

        original = control_plane._atomic_replace_json
        failed_once = False

        def fail_path_resolve(path: Path, value: object) -> None:
            nonlocal failed_once
            if (
                not failed_once and path.name == "state.json" and isinstance(value, dict)
                and value.get("current_action_id") is None
                and value.get("tasks", {}).get("TASK-001", {}).get("status") == "READY"
            ):
                failed_once = True
                raise OSError("injected path rejection resolve fault")
            original(path, value)

        with mock.patch.object(control_plane, "_atomic_replace_json", side_effect=fail_path_resolve):
            interrupted = controller.submit_action_result(action["id"], result)
        self.assertEqual(interrupted.status, "INFRA_FAILURE", interrupted)
        self.assertTrue(Path(action["workspace"]).exists())
        conflicting = dict(result, summary="conflicting duplicate")
        before = self.snapshot()
        self.assertEqual(
            ActionController(self.root).submit_action_result(action["id"], conflicting).status,
            "INVALID",
        )
        self.assertEqual(self.snapshot(), before)

        recovered = ActionController(self.root).submit_action_result(action["id"], result)

        self.assertEqual(recovered.status, "NOT_READY", recovered)
        self.assertIn("outside-allowed", recovered.message)
        self.assertFalse(Path(action["workspace"]).exists())

    def test_path_rejection_retry_converges_after_resolve_before_cleanup(self) -> None:
        campaign_id = self.approve()
        controller = ActionController(self.root)
        action = dict(controller.get_next_action(campaign_id).action or {})
        Path(action["workspace"]).joinpath("outside.txt").write_text("not allowed\n", encoding="utf-8")
        result = self.passing_result(action)
        original_cleanup = CampaignWorkspace.remove_task_workspace
        failed_once = False

        def fail_path_cleanup(owner: CampaignWorkspace, workspace: Path) -> None:
            nonlocal failed_once
            intent = json.loads(
                (self.root / f".autodev/actions/{action['id']}/finalization.json").read_text()
            )
            if (
                not failed_once and workspace.resolve() == Path(action["workspace"]).resolve()
                and intent["phase"] == "PROGRESSED"
            ):
                failed_once = True
                raise CampaignWorkspaceError("injected path rejection cleanup fault")
            original_cleanup(owner, workspace)

        with mock.patch.object(
            CampaignWorkspace, "remove_task_workspace", autospec=True,
            side_effect=fail_path_cleanup,
        ):
            interrupted = controller.submit_action_result(action["id"], result)
        self.assertEqual(interrupted.status, "INFRA_FAILURE", interrupted)
        self.assertTrue(Path(action["workspace"]).exists())

        recovered = ActionController(self.root).submit_action_result(action["id"], result)

        self.assertEqual(recovered.status, "NOT_READY", recovered)
        self.assertFalse(Path(action["workspace"]).exists())

    def test_path_rejection_recovers_after_run_finish_before_operated_journal(self) -> None:
        self._assert_rework_finish_to_operated_recovery("TRUST_REJECTION")

    def test_rework_boundary_recovery_rejects_tampered_evidence(self) -> None:
        self._assert_rework_finish_to_operated_recovery("TRUST_REJECTION", tamper=True)

    def test_worker_rework_and_blocked_results_are_overridden_by_actual_path_policy(self) -> None:
        campaign_id = self.approve()
        controller = ActionController(self.root)
        action = dict(controller.get_next_action(campaign_id).action or {})
        Path(action["workspace"]).joinpath("forbidden.txt").write_text("forbidden\n", encoding="utf-8")
        rework = self.passing_result(action)
        rework["outcome"] = "REWORK"
        rejected_rework = controller.submit_action_result(action["id"], rework)

        self.assertEqual(rejected_rework.status, "NOT_READY", rejected_rework)
        self.assertIn("outside-allowed", rejected_rework.message)
        action = dict(controller.get_next_action(campaign_id).action or {})
        Path(action["workspace"]).joinpath("forbidden.txt").write_text(
            "forbidden\n", encoding="utf-8",
        )
        blocked = self.passing_result(action)
        blocked.update(
            outcome="BLOCKED", blocker="External credential is unavailable.",
            next_action="Provide the credential.",
        )
        rejected_blocked = controller.submit_action_result(action["id"], blocked)
        self.assertEqual(rejected_blocked.status, "NOT_READY", rejected_blocked)
        self.assertIn("outside-allowed", rejected_blocked.message)
        state = json.loads((self.root / ".autodev/state.json").read_text())
        self.assertEqual(state["tasks"]["TASK-001"]["status"], "READY")
        self.assertEqual(state["last_outcome"], "REWORK")

    def test_quality_router_requires_fresh_read_only_immediate_review(self) -> None:
        campaign_id = self.approve(task(risk="HIGH"))
        controller = ActionController(self.root)
        worker = dict(controller.get_next_action(campaign_id).action or {})
        self.assertEqual(worker["quality_route"], "IMMEDIATE")
        Path(worker["workspace"]).joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")

        review_outcome = controller.submit_action_result(worker["id"], self.passing_result(worker))

        self.assertEqual(review_outcome.status, "SUCCESS", review_outcome)
        review = dict(review_outcome.action or {})
        self.assertEqual(review["type"], "RUN_IMMEDIATE_REVIEW")
        self.assertEqual(review["role"], "reviewer")
        self.assertNotEqual(review["id"], worker["id"])
        review_workspace = Path(review["workspace"])
        review_workspace.joinpath("review-write.txt").write_text("forbidden\n", encoding="utf-8")
        before = self.snapshot()

        rejected = controller.submit_action_result(review["id"], self.passing_result(review))

        self.assertEqual(rejected.status, "INVALID", rejected)
        self.assertIn("read-only", rejected.message)
        self.assertEqual(self.snapshot(), before)
        review_workspace.joinpath("review-write.txt").unlink()
        reached = controller.submit_action_result(review["id"], self.passing_result(review))
        self.assertEqual(reached.status, "SUCCESS", reached)
        self.assertEqual(reached.action["type"], "TARGET_REACHED")

    def test_read_only_immediate_reviewer_rejects_a_clean_commit(self) -> None:
        campaign_id = self.approve(task(risk="HIGH"))
        controller = ActionController(self.root)
        worker = dict(controller.get_next_action(campaign_id).action or {})
        Path(worker["workspace"]).joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")
        review = dict(controller.submit_action_result(
            worker["id"], self.passing_result(worker),
        ).action or {})
        git(Path(review["workspace"]), "commit", "--allow-empty", "-qm", "forbidden")
        before = self.snapshot()

        rejected = controller.submit_action_result(review["id"], self.passing_result(review))

        self.assertEqual(rejected.status, "INVALID", rejected)
        self.assertIn("read-only", rejected.message)
        self.assertEqual(self.snapshot(), before)

    def test_immediate_review_enforces_shared_finding_and_debt_budgets_without_mutation(self) -> None:
        campaign_id = self.approve(task(risk="HIGH"))
        controller = ActionController(self.root)
        worker = dict(controller.get_next_action(campaign_id).action or {})
        Path(worker["workspace"]).joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")
        review = dict(
            controller.submit_action_result(worker["id"], self.passing_result(worker)).action or {}
        )
        too_many_findings = self.passing_result(review)
        too_many_findings["findings"] = [f"blocking-{index}" for index in range(6)]
        before_findings = self.snapshot()

        rejected_findings = controller.submit_action_result(review["id"], too_many_findings)

        self.assertEqual(rejected_findings.status, "INVALID", rejected_findings)
        self.assertIn("finding budget", rejected_findings.message)
        self.assertEqual(self.snapshot(), before_findings)
        too_many_debt = self.passing_result(review)
        too_many_debt["outcome"] = "PASS_WITH_DEBT"
        too_many_debt["data"] = {"debt_items": [
            {
                "id": f"DEBT-{index:03d}", "source_task": "TASK-001",
                "reason": "Deferred cleanup.", "severity": "LOW", "module": "app",
                "fix_before": "M9", "classification": "maintainability",
            }
            for index in range(6)
        ]}
        before_debt = self.snapshot()

        rejected_debt = controller.submit_action_result(review["id"], too_many_debt)

        self.assertEqual(rejected_debt.status, "INVALID", rejected_debt)
        self.assertIn("debt finding budget", rejected_debt.message)
        self.assertEqual(self.snapshot(), before_debt)

    def test_reviewer_pass_with_debt_is_final_and_records_validated_canonical_debt(self) -> None:
        campaign_id = self.approve(task(change_classes=["security"]))
        controller = ActionController(self.root)
        worker = dict(controller.get_next_action(campaign_id).action or {})
        self.assertEqual(worker["quality_route"], "IMMEDIATE")
        Path(worker["workspace"]).joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")
        review = dict(
            controller.submit_action_result(worker["id"], self.passing_result(worker)).action or {}
        )
        debt = {
            "id": "DEBT-001", "source_task": "TASK-001",
            "reason": "A bounded cleanup remains.", "severity": "LOW",
            "module": "app", "fix_before": "M9",
            "classification": "maintainability",
        }
        review_result = self.passing_result(review)
        review_result["outcome"] = "PASS_WITH_DEBT"
        review_result["data"] = {"debt_items": [debt]}

        reached = controller.submit_action_result(review["id"], review_result)

        self.assertEqual(reached.status, "SUCCESS", reached)
        state = json.loads((self.root / ".autodev/state.json").read_text())
        self.assertEqual(state["last_outcome"], "PASS_WITH_DEBT")
        canonical_debt = json.loads((self.root / ".autodev/debt.json").read_text())
        self.assertEqual(canonical_debt["items"], [{**debt, "status": "OPEN"}])

    def test_phase_quality_route_runs_one_cumulative_read_only_review(self) -> None:
        campaign_id = self.approve(task(change_classes=["architecture"]))
        controller = ActionController(self.root)
        worker = dict(controller.get_next_action(campaign_id).action or {})
        self.assertEqual(worker["quality_route"], "PHASE")
        Path(worker["workspace"]).joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")

        phase_outcome = controller.submit_action_result(worker["id"], self.passing_result(worker))

        self.assertEqual(phase_outcome.status, "SUCCESS", phase_outcome)
        review = dict(phase_outcome.action or {})
        self.assertEqual(review["type"], "RUN_PHASE_REVIEW")
        self.assertEqual(review["quality_route"], "PHASE")
        review_workspace = Path(review["workspace"])
        review_workspace.joinpath("app.py").write_text("VALUE = 99\n", encoding="utf-8")
        rejected = controller.submit_action_result(review["id"], self.passing_result(review))
        self.assertEqual(rejected.status, "INVALID", rejected)
        self.assertIn("read-only", rejected.message)
        review_workspace.joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")
        reached = controller.submit_action_result(review["id"], self.passing_result(review))
        self.assertEqual(reached.status, "SUCCESS", reached)
        self.assertEqual(reached.action["type"], "TARGET_REACHED")

    def test_read_only_phase_reviewer_rejects_a_clean_commit(self) -> None:
        campaign_id = self.approve(task(change_classes=["architecture"]))
        controller = ActionController(self.root)
        worker = dict(controller.get_next_action(campaign_id).action or {})
        Path(worker["workspace"]).joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")
        review = dict(controller.submit_action_result(
            worker["id"], self.passing_result(worker),
        ).action or {})
        git(Path(review["workspace"]), "commit", "--allow-empty", "-qm", "forbidden")
        before = self.snapshot()

        rejected = controller.submit_action_result(review["id"], self.passing_result(review))

        self.assertEqual(rejected.status, "INVALID", rejected)
        self.assertIn("read-only", rejected.message)
        self.assertEqual(self.snapshot(), before)

    def test_phase_rework_requires_repair_plan_then_blocks_after_one_rereview(self) -> None:
        campaign_id = self.approve(task(change_classes=["architecture"]))
        controller = ActionController(self.root)
        worker = dict(controller.get_next_action(campaign_id).action or {})
        Path(worker["workspace"]).joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")
        first_review = dict(
            controller.submit_action_result(worker["id"], self.passing_result(worker)).action or {}
        )
        rework = self.passing_result(first_review)
        rework.update(outcome="REWORK", summary="The Phase needs one focused repair.")
        rework["findings"] = ["Carry the architecture invariant into the integration seam."]

        repair_plan = controller.submit_action_result(first_review["id"], rework)

        self.assertEqual(repair_plan.status, "SUCCESS", repair_plan)
        self.assertEqual(repair_plan.action["type"], "PLAN_PHASE")
        self.assertEqual(repair_plan.action["context"]["purpose"], "PHASE_REPAIR")
        repair_task = task(change_classes=["architecture"])
        repair_task.update(
            id="TASK-002", title="Repair the Phase", objective="Apply the Phase Review repair.",
            dependencies=["TASK-001"],
            allowed_paths=["architecture.txt"],
            validation_commands=[{
                "argv": ["python3", "-c", "assert open('architecture.txt').read() == 'repaired\\n'"],
                "cwd": ".", "timeout": 20,
            }],
        )
        plan_result = self.passing_result(dict(repair_plan.action))
        plan_result["data"] = {"phase": "SCAFFOLD", "tasks": [repair_task], "questions": []}
        repair_worker = controller.submit_action_result(repair_plan.action["id"], plan_result)
        self.assertEqual(repair_worker.action["type"], "EXECUTE_TASK")
        Path(repair_worker.action["workspace"]).joinpath("architecture.txt").write_text(
            "repaired\n", encoding="utf-8",
        )
        second_review = controller.submit_action_result(
            repair_worker.action["id"], self.passing_result(dict(repair_worker.action)),
        )
        self.assertEqual(second_review.action["type"], "RUN_PHASE_REVIEW")
        second_rework = self.passing_result(dict(second_review.action))
        second_rework.update(outcome="REWORK", summary="The rereview still fails.")
        second_rework["findings"] = ["The invariant remains incomplete."]

        blocked = controller.submit_action_result(second_review.action["id"], second_rework)

        self.assertEqual(blocked.status, "SUCCESS", blocked)
        self.assertEqual(blocked.action["type"], "ASK_HUMAN")
        state = json.loads((self.root / ".autodev/state.json").read_text())
        self.assertEqual(state["project_status"], "BLOCKED")
        flow = json.loads(
            (self.root / ".autodev/campaigns/CAMP-001/phase-flow-SCAFFOLD.json").read_text()
        )
        self.assertEqual(flow["review_attempts"], 2)

    def test_phase_diagnostic_requires_two_durable_identical_validation_failures(self) -> None:
        first_task = task(change_classes=["architecture"])
        second_task = task()
        second_task.update(
            id="TASK-002", title="Change the value again", objective="Change app.py again.",
            dependencies=["TASK-001"],
            validation_commands=[{
                "argv": ["python3", "-c", "assert open('app.py').read() == 'VALUE = 3\\n'"],
                "cwd": ".", "timeout": 20,
            }],
        )
        campaign = CampaignController(
            self.root, FakePlanner([proposal([first_task, second_task])]),
        )
        planned = campaign.plan(CampaignRequest(
            "Exercise repeated Phase failures", target="ARCHITECTURE_BASELINE",
        ))
        self.assertEqual(planned.status, "SUCCESS", planned)
        self.assertEqual(
            campaign.approve("CAMP-001", planned.data["proposal_hash"]).status, "SUCCESS",
        )
        controller = ActionController(self.root)
        first = dict(controller.get_next_action("CAMP-001").action or {})
        Path(first["workspace"]).joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")
        second = dict(
            controller.submit_action_result(first["id"], self.passing_result(first)).action or {}
        )
        Path(second["workspace"]).joinpath("app.py").write_text("VALUE = 3\n", encoding="utf-8")

        first_failure = controller.submit_action_result(second["id"], self.passing_result(second))

        self.assertEqual(first_failure.status, "SUCCESS", first_failure)
        self.assertEqual(first_failure.action["type"], "PLAN_PHASE")
        self.assertEqual(first_failure.action["context"]["purpose"], "PHASE_REPAIR")
        flow_path = self.root / ".autodev/campaigns/CAMP-001/phase-flow-SCAFFOLD.json"
        first_flow = json.loads(flow_path.read_text())
        self.assertEqual(len(first_flow["failure_fingerprints"]), 1)
        repair_task = task(change_classes=["architecture"])
        repair_task.update(
            id="TASK-003", title="Attempt the Phase repair", objective="Record the focused repair.",
            dependencies=["TASK-002"], allowed_paths=["repair.txt"],
            validation_commands=[{
                "argv": ["python3", "-c", "assert open('repair.txt').read() == 'attempted\\n'"],
                "cwd": ".", "timeout": 20,
            }],
        )
        plan_result = self.passing_result(dict(first_failure.action))
        plan_result["data"] = {"phase": "SCAFFOLD", "tasks": [repair_task], "questions": []}
        repair = controller.submit_action_result(first_failure.action["id"], plan_result)
        Path(repair.action["workspace"]).joinpath("repair.txt").write_text(
            "attempted\n", encoding="utf-8",
        )

        repeated = controller.submit_action_result(
            repair.action["id"], self.passing_result(dict(repair.action)),
        )

        self.assertEqual(repeated.status, "SUCCESS", repeated)
        self.assertEqual(repeated.action["type"], "RUN_DIAGNOSTIC")
        second_flow = json.loads(flow_path.read_text())
        self.assertEqual(len(second_flow["failure_fingerprints"]), 2)
        self.assertEqual(
            second_flow["failure_fingerprints"][0], second_flow["failure_fingerprints"][1],
        )

    def test_phase_reviewer_pass_with_debt_is_validated_and_canonical(self) -> None:
        campaign_id = self.approve(task(change_classes=["architecture"]))
        controller = ActionController(self.root)
        worker = dict(controller.get_next_action(campaign_id).action or {})
        Path(worker["workspace"]).joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")
        review = dict(
            controller.submit_action_result(worker["id"], self.passing_result(worker)).action or {}
        )
        debt = {
            "id": "DEBT-PHASE-001", "source_task": "TASK-001",
            "reason": "A non-blocking architecture note remains.", "severity": "LOW",
            "module": "app", "fix_before": "M9", "classification": "maintainability",
        }
        result = self.passing_result(review)
        result["outcome"] = "PASS_WITH_DEBT"
        result["data"] = {"debt_items": [debt]}

        reached = controller.submit_action_result(review["id"], result)

        self.assertEqual(reached.status, "SUCCESS", reached)
        self.assertEqual(reached.action["type"], "TARGET_REACHED")
        state = json.loads((self.root / ".autodev/state.json").read_text())
        self.assertEqual(state["last_outcome"], "PASS_WITH_DEBT")
        canonical = json.loads((self.root / ".autodev/debt.json").read_text())
        self.assertEqual(canonical["items"], [{**debt, "status": "OPEN"}])

    def test_retry_converges_after_passed_phase_review_resolve_fault(self) -> None:
        campaign_id = self.approve(task(change_classes=["architecture"]))
        controller = ActionController(self.root)
        worker = dict(controller.get_next_action(campaign_id).action or {})
        Path(worker["workspace"]).joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")
        review = dict(
            controller.submit_action_result(worker["id"], self.passing_result(worker)).action or {}
        )
        result = self.passing_result(review)
        from autodev import control_plane

        original = control_plane._atomic_replace_json
        failed_once = False

        def fail_phase_review_resolve(path: Path, value: object) -> None:
            nonlocal failed_once
            if (
                not failed_once and path.name == "state.json" and isinstance(value, dict)
                and value.get("current_action_id") is None
                and value.get("tasks", {}).get("TASK-001", {}).get("status") == "ACCEPTED"
            ):
                failed_once = True
                raise OSError("injected passed Phase Review resolve fault")
            original(path, value)

        with mock.patch.object(control_plane, "_atomic_replace_json", side_effect=fail_phase_review_resolve):
            interrupted = controller.submit_action_result(review["id"], result)

        self.assertEqual(interrupted.status, "INFRA_FAILURE", interrupted)
        state = json.loads((self.root / ".autodev/state.json").read_text())
        self.assertEqual(state["current_action_id"], review["id"])
        summary = json.loads(
            (self.root / ".autodev/campaigns/CAMP-001/phase-summary-SCAFFOLD.json").read_text()
        )
        self.assertEqual(summary["status"], "PASSED")
        conflicting = dict(result, summary="conflicting Phase Review")
        before = self.snapshot()
        self.assertEqual(
            ActionController(self.root).submit_action_result(review["id"], conflicting).status,
            "INVALID",
        )
        self.assertEqual(self.snapshot(), before)

        recovered = ActionController(self.root).submit_action_result(review["id"], result)

        self.assertEqual(recovered.status, "SUCCESS", recovered)
        self.assertEqual(recovered.action["type"], "TARGET_REACHED")

    def test_retry_converges_after_passed_phase_review_cleanup_fault(self) -> None:
        campaign_id = self.approve(task(change_classes=["architecture"]))
        controller = ActionController(self.root)
        worker = dict(controller.get_next_action(campaign_id).action or {})
        Path(worker["workspace"]).joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")
        review = dict(
            controller.submit_action_result(worker["id"], self.passing_result(worker)).action or {}
        )
        result = self.passing_result(review)
        original_cleanup = CampaignWorkspace.remove_task_workspace
        failed_once = False

        def fail_passed_phase_cleanup(owner: CampaignWorkspace, workspace: Path) -> None:
            nonlocal failed_once
            summary_path = self.root / ".autodev/campaigns/CAMP-001/phase-summary-SCAFFOLD.json"
            if not failed_once and summary_path.is_file():
                summary = json.loads(summary_path.read_text())
                if (
                    summary.get("status") == "PASSED"
                    and workspace.resolve() == Path(review["workspace"]).resolve()
                ):
                    failed_once = True
                    raise CampaignWorkspaceError("injected passed Phase Review cleanup fault")
            original_cleanup(owner, workspace)

        with mock.patch.object(
            CampaignWorkspace, "remove_task_workspace", autospec=True,
            side_effect=fail_passed_phase_cleanup,
        ):
            interrupted = controller.submit_action_result(review["id"], result)

        self.assertEqual(interrupted.status, "INFRA_FAILURE", interrupted)
        state = json.loads((self.root / ".autodev/state.json").read_text())
        self.assertNotEqual(state["current_action_id"], review["id"])
        conflicting = dict(result, summary="conflicting Phase cleanup retry")
        before = self.snapshot()
        self.assertEqual(
            ActionController(self.root).submit_action_result(review["id"], conflicting).status,
            "INVALID",
        )
        self.assertEqual(self.snapshot(), before)

        recovered = ActionController(self.root).submit_action_result(review["id"], result)

        self.assertEqual(recovered.status, "SUCCESS", recovered)
        self.assertEqual(recovered.action["type"], "TARGET_REACHED")

    def test_successful_phase_review_obeys_critical_final_writeback_gate(self) -> None:
        campaign = CampaignController(
            self.root, FakePlanner([proposal([task(change_classes=["architecture"])])]),
        )
        planned = campaign.plan(CampaignRequest(
            "Critical change", mode="CRITICAL", target="ARCHITECTURE_BASELINE",
        ))
        self.assertEqual(planned.status, "SUCCESS", planned)
        self.assertEqual(
            campaign.approve("CAMP-001", planned.data["proposal_hash"]).status, "SUCCESS",
        )
        controller = ActionController(self.root)
        worker = dict(controller.get_next_action("CAMP-001").action or {})
        Path(worker["workspace"]).joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")
        review = dict(
            controller.submit_action_result(worker["id"], self.passing_result(worker)).action or {}
        )

        gated = controller.submit_action_result(review["id"], self.passing_result(review))

        self.assertEqual(gated.status, "SUCCESS", gated)
        self.assertEqual(gated.action["type"], "ASK_HUMAN")
        state = json.loads((self.root / ".autodev/state.json").read_text())
        self.assertEqual(state["campaigns"]["CAMP-001"]["status"], "WAITING_FOR_HUMAN")
        gate = json.loads(
            (self.root / ".autodev/campaigns/CAMP-001/gate-context.json").read_text()
        )
        self.assertEqual(gate["kind"], "final-writeback")

    def test_pause_finishes_current_action_and_restart_continues_without_duplicate_checkpoint(self) -> None:
        campaign_id = self.approve()
        controller = ActionController(self.root)
        worker = dict(controller.get_next_action(campaign_id).action or {})
        Path(worker["workspace"]).joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")
        paused_request = ControlPlane(self.root).execute(Command(
            "action.pause", {"campaign_id": campaign_id},
        ))
        self.assertEqual(paused_request.status, "SUCCESS", paused_request)

        paused = controller.submit_action_result(worker["id"], self.passing_result(worker))

        self.assertEqual(paused.status, "SUCCESS", paused)
        self.assertEqual(paused.action["type"], "PAUSED")
        state = json.loads((self.root / ".autodev/state.json").read_text())
        self.assertEqual(state["project_status"], "PAUSED")
        checkpoint = state["campaigns"][campaign_id]["checkpoint"]
        continued = ControlPlane(self.root).execute(Command(
            "action.continue", {"campaign_id": campaign_id},
        ))
        self.assertEqual(continued.status, "SUCCESS", continued)

        reached = ActionController(self.root).get_next_action(campaign_id)

        self.assertEqual(reached.status, "SUCCESS", reached)
        self.assertEqual(reached.action["type"], "TARGET_REACHED")
        state = json.loads((self.root / ".autodev/state.json").read_text())
        self.assertEqual(state["campaigns"][campaign_id]["checkpoint"], checkpoint)
        journals = list((self.root / ".autodev/campaigns/CAMP-001/checkpoint-journal").glob("*.json"))
        self.assertEqual(len(journals), 1)

    def test_already_materialized_target_is_a_true_noop_before_retarget(self) -> None:
        campaign_id = self.approve()
        controller = ActionController(self.root)
        worker = dict(controller.get_next_action(campaign_id).action or {})
        Path(worker["workspace"]).joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")
        target = controller.submit_action_result(worker["id"], self.passing_result(worker))
        self.assertEqual(target.action["type"], "TARGET_REACHED")

        state_before = json.loads((self.root / ".autodev/state.json").read_text())
        journal_path = self.root / ".autodev/campaigns/CAMP-001/materialization-journal.json"
        journal_before = journal_path.read_bytes()
        materialized = CampaignController(self.root).materialize(campaign_id)

        self.assertEqual(materialized.status, "SUCCESS", materialized)
        self.assertFalse(materialized.data["applied"])
        self.assertEqual(
            json.loads((self.root / ".autodev/state.json").read_text())["revision"],
            state_before["revision"],
        )
        self.assertEqual(journal_path.read_bytes(), journal_before)
        same_target = ActionController(self.root).get_next_action(campaign_id)
        self.assertEqual(same_target.action, target.action)
        retargeted = CampaignController(self.root).retarget(campaign_id, "WORKING_MVP")
        self.assertEqual(retargeted.status, "SUCCESS", retargeted)
        planning = ActionController(self.root).get_next_action(campaign_id)
        self.assertEqual(planning.status, "SUCCESS", planning)
        self.assertEqual(planning.action["type"], "PLAN_PHASE")

    def test_archive_reconciles_terminal_target_action_and_future_reads_are_not_stale(self) -> None:
        campaign_id = self.approve()
        controller = ActionController(self.root)
        worker = dict(controller.get_next_action(campaign_id).action or {})
        Path(worker["workspace"]).joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")
        target = controller.submit_action_result(worker["id"], self.passing_result(worker))
        self.assertEqual(target.action["type"], "TARGET_REACHED")

        archived = CampaignController(self.root).archive(campaign_id)
        after_archive = ActionController(self.root).get_next_action(campaign_id)

        self.assertEqual(archived.status, "SUCCESS", archived)
        self.assertEqual(after_archive.status, "NOT_READY", after_archive)
        self.assertIsNone(after_archive.action)
        state = json.loads((self.root / ".autodev/state.json").read_text())
        self.assertEqual(state["campaigns"][campaign_id]["status"], "ARCHIVED")
        self.assertIsNone(state["current_action_id"])

    def test_terminal_ask_human_action_is_reconciled_before_answer(self) -> None:
        initial = proposal([])
        initial["questions"] = [{
            "id": "decision", "header": "Choice", "question": "Proceed?",
            "options": [
                {"label": "Proceed", "description": "Continue."},
                {"label": "Stop", "description": "Do not continue."},
            ],
            "isOther": False, "isSecret": False,
        }]
        planner = FakePlanner([initial, proposal([task()])])
        campaign = CampaignController(self.root, planner)
        planned = campaign.plan(CampaignRequest("Ask then work", target="ARCHITECTURE_BASELINE"))
        waiting = campaign.approve("CAMP-001", planned.data["proposal_hash"])
        self.assertEqual(waiting.status, "NOT_READY", waiting)
        ask = ActionController(self.root).get_next_action("CAMP-001")
        self.assertEqual(ask.action["type"], "ASK_HUMAN")

        answered = campaign.answer(
            "CAMP-001", waiting.data["request_id"], {"decision": ["Proceed"]},
        )

        self.assertEqual(answered.status, "SUCCESS", answered)
        executing = ActionController(self.root).get_next_action("CAMP-001")
        self.assertEqual(executing.status, "SUCCESS", executing)
        self.assertEqual(executing.action["type"], "EXECUTE_TASK")
        self.assertNotEqual(executing.action["id"], ask.action["id"])

    def test_repeated_identical_validation_failure_routes_one_read_only_diagnostic(self) -> None:
        campaign_id = self.approve()
        controller = ActionController(self.root)
        first = dict(controller.get_next_action(campaign_id).action or {})
        Path(first["workspace"]).joinpath("app.py").write_text("VALUE = 3\n", encoding="utf-8")

        second_outcome = controller.submit_action_result(first["id"], self.passing_result(first))
        self.assertEqual(second_outcome.status, "NOT_READY", second_outcome)
        second = dict(controller.get_next_action(campaign_id).action or {})
        self.assertEqual(second["type"], "EXECUTE_TASK")
        Path(second["workspace"]).joinpath("app.py").write_text("VALUE = 3\n", encoding="utf-8")
        diagnostic_outcome = controller.submit_action_result(
            second["id"], self.passing_result(second),
        )

        self.assertEqual(diagnostic_outcome.status, "SUCCESS", diagnostic_outcome)
        diagnostic = dict(diagnostic_outcome.action or {})
        self.assertEqual(diagnostic["type"], "RUN_DIAGNOSTIC")
        self.assertEqual(diagnostic["quality_route"], "DIAGNOSTIC")
        diagnostic_result = self.passing_result(diagnostic)
        diagnostic_result["outcome"] = "REWORK"
        repaired = controller.submit_action_result(diagnostic["id"], diagnostic_result)
        self.assertEqual(repaired.status, "SUCCESS", repaired)
        self.assertEqual(repaired.action["type"], "EXECUTE_TASK")
        diagnostics = list((self.root / ".autodev/runs").glob("*/diagnostic.json"))
        self.assertEqual(len(diagnostics), 1)

    def test_validation_failure_retry_converges_between_run_finish_and_action_resolve(self) -> None:
        campaign_id = self.approve()
        controller = ActionController(self.root)
        action = dict(controller.get_next_action(campaign_id).action or {})
        Path(action["workspace"]).joinpath("app.py").write_text("VALUE = 3\n", encoding="utf-8")
        result = self.passing_result(action)
        from autodev import control_plane

        original = control_plane._atomic_replace_json
        failed_once = False

        def fail_validation_resolve(path: Path, value: object) -> None:
            nonlocal failed_once
            if (
                not failed_once and path.name == "state.json" and isinstance(value, dict)
                and value.get("current_action_id") is None
                and value.get("tasks", {}).get("TASK-001", {}).get("status") == "READY"
            ):
                failed_once = True
                raise OSError("injected validation failure resolve fault")
            original(path, value)

        with mock.patch.object(
            control_plane, "_atomic_replace_json", side_effect=fail_validation_resolve,
        ):
            interrupted = controller.submit_action_result(action["id"], result)
        self.assertEqual(interrupted.status, "INFRA_FAILURE", interrupted)
        self.assertTrue(Path(action["workspace"]).exists())
        conflicting = dict(result, summary="conflicting duplicate")
        before = self.snapshot()
        self.assertEqual(
            ActionController(self.root).submit_action_result(action["id"], conflicting).status,
            "INVALID",
        )
        self.assertEqual(self.snapshot(), before)

        recovered = ActionController(self.root).submit_action_result(action["id"], result)

        self.assertEqual(recovered.status, "NOT_READY", recovered)
        self.assertEqual(recovered.data["validations"][0]["returncode"], 1)
        self.assertFalse(Path(action["workspace"]).exists())

    def test_validation_failure_retry_converges_after_resolve_before_cleanup(self) -> None:
        campaign_id = self.approve()
        controller = ActionController(self.root)
        action = dict(controller.get_next_action(campaign_id).action or {})
        Path(action["workspace"]).joinpath("app.py").write_text("VALUE = 3\n", encoding="utf-8")
        result = self.passing_result(action)
        original_cleanup = CampaignWorkspace.remove_task_workspace
        failed_once = False

        def fail_validation_cleanup(owner: CampaignWorkspace, workspace: Path) -> None:
            nonlocal failed_once
            intent = json.loads(
                (self.root / f".autodev/actions/{action['id']}/finalization.json").read_text()
            )
            if (
                not failed_once and workspace.resolve() == Path(action["workspace"]).resolve()
                and intent["phase"] == "PROGRESSED"
            ):
                failed_once = True
                raise CampaignWorkspaceError("injected validation cleanup fault")
            original_cleanup(owner, workspace)

        with mock.patch.object(
            CampaignWorkspace, "remove_task_workspace", autospec=True,
            side_effect=fail_validation_cleanup,
        ):
            interrupted = controller.submit_action_result(action["id"], result)
        self.assertEqual(interrupted.status, "INFRA_FAILURE", interrupted)
        self.assertTrue(Path(action["workspace"]).exists())

        recovered = ActionController(self.root).submit_action_result(action["id"], result)

        self.assertEqual(recovered.status, "NOT_READY", recovered)
        self.assertFalse(Path(action["workspace"]).exists())

    def test_validation_failure_recovers_after_run_finish_before_operated_journal(self) -> None:
        self._assert_rework_finish_to_operated_recovery("VALIDATION_FAILURE")

    def test_finished_validation_failure_does_not_rerun_changing_output(self) -> None:
        self._assert_finished_validation_recovery_without_rerun(returncode=1)

    def test_finished_validation_failure_cannot_be_bypassed_by_transient_pass(self) -> None:
        self._assert_finished_validation_recovery_without_rerun(returncode=0)

    def test_finished_validation_failure_rejects_tampered_persisted_failure(self) -> None:
        self._assert_finished_validation_recovery_without_rerun(returncode=0, tamper=True)

    def _assert_finished_validation_recovery_without_rerun(
        self, *, returncode: int, tamper: bool = False,
    ) -> None:
        campaign_id = self.approve()
        controller = ActionController(self.root)
        action = dict(controller.get_next_action(campaign_id).action or {})
        workspace = Path(action["workspace"])
        workspace.joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")
        result = self.passing_result(action)
        first_validation = {
            "index": 0, "argv": ["changing-check"], "cwd": ".", "returncode": 1,
            "timed_out": False, "stdout": "first failure\n", "stderr": "first stderr\n",
            "duration_seconds": 0.1,
        }
        retry_validation = {
            **first_validation, "returncode": returncode, "stdout": "changed output\n",
            "stderr": "changed stderr\n", "duration_seconds": 0.2,
        }
        validation_calls = 0

        def changing_validation(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
            nonlocal validation_calls
            validation_calls += 1
            return [first_validation if validation_calls == 1 else retry_validation]

        from autodev import action as action_module
        original = action_module._write_json_atomic
        failed_once = False

        def fail_operated_journal(path: Path, value: object) -> None:
            nonlocal failed_once
            if (
                not failed_once
                and path == self.root / f".autodev/actions/{action['id']}/finalization.json"
                and isinstance(value, dict)
                and value.get("phase") == "OPERATED"
            ):
                failed_once = True
                raise OSError("injected validation operated-journal fault")
            original(path, value)

        with mock.patch(
            "autodev.attempt_lifecycle.AttemptLifecycle.run_validations",
            autospec=True, side_effect=changing_validation,
        ):
            with mock.patch.object(
                action_module, "_write_json_atomic", side_effect=fail_operated_journal,
            ):
                interrupted = controller.submit_action_result(action["id"], result)
            self.assertEqual(interrupted.status, "INFRA_FAILURE", interrupted)
            self.assertEqual(validation_calls, 1)

            conflicting = dict(result, summary="conflicting duplicate")
            before_conflict = self.snapshot()
            self.assertEqual(
                ActionController(self.root).submit_action_result(
                    action["id"], conflicting,
                ).status,
                "INVALID",
            )
            self.assertEqual(self.snapshot(), before_conflict)
            self.assertEqual(validation_calls, 1)

            if tamper:
                failure_path = self.root / f".autodev/runs/{action['context']['run_id']}/failure.json"
                failure = json.loads(failure_path.read_text())
                failure["validations"][0]["stdout"] = "tampered\n"
                failure_path.write_text(json.dumps(failure), encoding="utf-8")
                before_retry = self.snapshot()
                recovered = ActionController(self.root).submit_action_result(action["id"], result)
                self.assertEqual(recovered.status, "INVALID", recovered)
                self.assertEqual(self.snapshot(), before_retry)
                self.assertEqual(validation_calls, 1)
                return

            recovered = ActionController(self.root).submit_action_result(action["id"], result)

        self.assertEqual(recovered.status, "NOT_READY", recovered)
        self.assertEqual(recovered.data["validations"], [first_validation])
        self.assertEqual(validation_calls, 1)
        self.assertFalse(workspace.exists())

    def _assert_rework_finish_to_operated_recovery(
        self, kind: str, *, tamper: bool = False,
    ) -> None:
        campaign_id = self.approve()
        controller = ActionController(self.root)
        action = dict(controller.get_next_action(campaign_id).action or {})
        workspace = Path(action["workspace"])
        if kind == "TRUST_REJECTION":
            workspace.joinpath("outside.txt").write_text("not allowed\n", encoding="utf-8")
        else:
            workspace.joinpath("app.py").write_text("VALUE = 3\n", encoding="utf-8")
        result = self.passing_result(action)
        from autodev import action as action_module

        original = action_module._write_json_atomic
        failed_once = False

        def fail_operated_journal(path: Path, value: object) -> None:
            nonlocal failed_once
            if (
                not failed_once
                and path == self.root / f".autodev/actions/{action['id']}/finalization.json"
                and isinstance(value, dict)
                and value.get("phase") == "OPERATED"
            ):
                failed_once = True
                raise OSError("injected REWORK operated-journal fault")
            original(path, value)

        with mock.patch.object(
            action_module, "_write_json_atomic", side_effect=fail_operated_journal,
        ):
            interrupted = controller.submit_action_result(action["id"], result)

        self.assertEqual(interrupted.status, "INFRA_FAILURE", interrupted)
        state = json.loads((self.root / ".autodev/state.json").read_text())
        intent = json.loads(
            (self.root / f".autodev/actions/{action['id']}/finalization.json").read_text()
        )
        self.assertEqual(state["tasks"]["TASK-001"]["status"], "READY")
        self.assertEqual(state["current_action_id"], action["id"])
        self.assertEqual(intent["phase"], "PREPARED")
        self.assertTrue(workspace.exists())

        conflicting = dict(result, summary="conflicting duplicate")
        before = self.snapshot()
        self.assertEqual(
            ActionController(self.root).submit_action_result(action["id"], conflicting).status,
            "INVALID",
        )
        self.assertEqual(self.snapshot(), before)

        if tamper:
            evidence_path = self.root / f".autodev/runs/{action['context']['run_id']}/evidence.json"
            evidence = json.loads(evidence_path.read_text())
            evidence["created_at"] = "2026-01-01T00:00:00Z"
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
            before_tampered_retry = self.snapshot()
            rejected = ActionController(self.root).submit_action_result(action["id"], result)
            self.assertEqual(rejected.status, "INVALID", rejected)
            self.assertEqual(self.snapshot(), before_tampered_retry)
            self.assertTrue(workspace.exists())
            return

        recovered = ActionController(self.root).submit_action_result(action["id"], result)

        self.assertEqual(recovered.status, "NOT_READY", recovered)
        self.assertFalse(workspace.exists())

    def test_validation_retry_republishes_same_diagnostic_from_identical_submit(self) -> None:
        campaign_id = self.approve()
        controller = ActionController(self.root)
        first = dict(controller.get_next_action(campaign_id).action or {})
        Path(first["workspace"]).joinpath("app.py").write_text("VALUE = 3\n", encoding="utf-8")
        self.assertEqual(
            controller.submit_action_result(first["id"], self.passing_result(first)).status,
            "NOT_READY",
        )
        second = dict(controller.get_next_action(campaign_id).action or {})
        Path(second["workspace"]).joinpath("app.py").write_text("VALUE = 3\n", encoding="utf-8")
        result = self.passing_result(second)
        from autodev import control_plane

        original = control_plane._atomic_replace_json
        failed_once = False

        def fail_diagnostic_publication(path: Path, value: object) -> None:
            nonlocal failed_once
            if (
                not failed_once and path.name == "state.json" and isinstance(value, dict)
                and value.get("current_action_id") is not None
                and value.get("current_action_id") != second["id"]
                and value.get("tasks", {}).get("TASK-001", {}).get("status") == "READY"
            ):
                failed_once = True
                raise OSError("injected Diagnostic publication fault")
            original(path, value)

        with mock.patch.object(
            control_plane, "_atomic_replace_json", side_effect=fail_diagnostic_publication,
        ):
            interrupted = controller.submit_action_result(second["id"], result)
        self.assertEqual(interrupted.status, "INFRA_FAILURE", interrupted)
        failure = json.loads(
            (self.root / f".autodev/runs/{second['context']['run_id']}/failure.json").read_text()
        )

        recovered = ActionController(self.root).submit_action_result(second["id"], result)
        repeated = ActionController(self.root).get_next_action(campaign_id)

        self.assertEqual(recovered.status, "SUCCESS", recovered)
        self.assertEqual(recovered.action["type"], "RUN_DIAGNOSTIC")
        self.assertEqual(recovered.action["id"], failure["diagnostic_action_id"])
        self.assertEqual(recovered.action, repeated.action)

    def test_repeated_failure_obeys_the_shared_quality_router_decision(self) -> None:
        campaign_id = self.approve()
        controller = ActionController(self.root)
        first = dict(controller.get_next_action(campaign_id).action or {})
        Path(first["workspace"]).joinpath("app.py").write_text("VALUE = 3\n", encoding="utf-8")
        self.assertEqual(
            controller.submit_action_result(first["id"], self.passing_result(first)).status,
            "NOT_READY",
        )
        second = dict(controller.get_next_action(campaign_id).action or {})
        Path(second["workspace"]).joinpath("app.py").write_text("VALUE = 3\n", encoding="utf-8")

        with mock.patch.object(QualityRouter, "decide", return_value=QualityDecision.NONE):
            outcome = controller.submit_action_result(second["id"], self.passing_result(second))

        self.assertEqual(outcome.status, "NOT_READY", outcome)
        self.assertIsNone(outcome.action)
        self.assertFalse(list((self.root / ".autodev/runs").glob("*/diagnostic.json")))

    def test_read_only_diagnostic_rejects_a_clean_commit(self) -> None:
        campaign_id = self.approve()
        controller = ActionController(self.root)
        first = dict(controller.get_next_action(campaign_id).action or {})
        Path(first["workspace"]).joinpath("app.py").write_text("VALUE = 3\n", encoding="utf-8")
        self.assertEqual(
            controller.submit_action_result(first["id"], self.passing_result(first)).status,
            "NOT_READY",
        )
        second = dict(controller.get_next_action(campaign_id).action or {})
        Path(second["workspace"]).joinpath("app.py").write_text("VALUE = 3\n", encoding="utf-8")
        diagnostic = dict(controller.submit_action_result(
            second["id"], self.passing_result(second),
        ).action or {})
        git(Path(diagnostic["workspace"]), "commit", "--allow-empty", "-qm", "forbidden")
        before = self.snapshot()

        rejected = controller.submit_action_result(
            diagnostic["id"], self.passing_result(diagnostic),
        )

        self.assertEqual(rejected.status, "INVALID", rejected)
        self.assertIn("read-only", rejected.message)
        self.assertEqual(self.snapshot(), before)

    def test_diagnostic_retries_between_artifact_and_action_resolve(self) -> None:
        campaign_id = self.approve()
        diagnostic = self.diagnostic_action(campaign_id)
        result = self.passing_result(diagnostic)
        result["data"] = {"diagnosis": {
            "root_cause": "The implementation used the wrong value.",
            "evidence": ["Validation expected VALUE = 2."],
            "recommended_action": "Retry with the required value.",
        }}
        from autodev import control_plane
        original = control_plane._atomic_replace_json
        failed_once = False

        def fail_diagnostic_resolve(path: Path, value: object) -> None:
            nonlocal failed_once
            if (
                not failed_once and path.name == "state.json" and isinstance(value, dict)
                and value.get("current_action_id") is None
            ):
                failed_once = True
                raise OSError("injected Diagnostic resolve fault")
            original(path, value)

        with mock.patch.object(
            control_plane, "_atomic_replace_json", side_effect=fail_diagnostic_resolve,
        ):
            interrupted = ActionController(self.root).submit_action_result(diagnostic["id"], result)
        self.assertEqual(interrupted.status, "INFRA_FAILURE", interrupted)
        self.assertTrue(Path(diagnostic["workspace"]).exists())
        conflicting = dict(result, summary="conflicting diagnosis")
        before = self.snapshot()
        self.assertEqual(
            ActionController(self.root).submit_action_result(diagnostic["id"], conflicting).status,
            "INVALID",
        )
        self.assertEqual(self.snapshot(), before)
        recovered = ActionController(self.root).submit_action_result(diagnostic["id"], result)
        self.assertEqual(recovered.action["type"], "EXECUTE_TASK")
        self.assertFalse(Path(diagnostic["workspace"]).exists())

    def test_diagnostic_retries_cleanup_after_resolve(self) -> None:
        campaign_id = self.approve()
        diagnostic = self.diagnostic_action(campaign_id)
        result = self.passing_result(diagnostic)
        original_cleanup = CampaignWorkspace.remove_task_workspace
        failed_once = False

        def fail_diagnostic_cleanup(owner: CampaignWorkspace, workspace: Path) -> None:
            nonlocal failed_once
            intent = json.loads(
                (self.root / f".autodev/actions/{diagnostic['id']}/finalization.json").read_text()
            )
            if (
                not failed_once and workspace.resolve() == Path(diagnostic["workspace"]).resolve()
                and intent["phase"] == "PROGRESSED"
            ):
                failed_once = True
                raise CampaignWorkspaceError("injected Diagnostic cleanup fault")
            original_cleanup(owner, workspace)

        with mock.patch.object(
            CampaignWorkspace, "remove_task_workspace", autospec=True,
            side_effect=fail_diagnostic_cleanup,
        ):
            interrupted = ActionController(self.root).submit_action_result(diagnostic["id"], result)
        self.assertEqual(interrupted.status, "INFRA_FAILURE", interrupted)
        self.assertTrue(Path(diagnostic["workspace"]).exists())
        recovered = ActionController(self.root).submit_action_result(diagnostic["id"], result)
        self.assertEqual(recovered.action["type"], "EXECUTE_TASK")
        self.assertFalse(Path(diagnostic["workspace"]).exists())


if __name__ == "__main__":
    unittest.main()
