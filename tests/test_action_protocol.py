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
from autodev.control_plane import Command, ControlPlane


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

    @staticmethod
    def passing_result(action: dict[str, object]) -> dict[str, object]:
        return {
            "action_id": action["id"],
            "canonical_revision": action["canonical_revision"],
            "outcome": "PASS",
            "summary": "Implemented the requested change.",
            "data": {},
            "findings": [],
            "blocker": None,
            "next_action": None,
        }

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

    def test_worker_path_and_concurrent_source_changes_are_rejected_without_core_mutation(self) -> None:
        campaign_id = self.approve()
        controller = ActionController(self.root)
        action = dict(controller.get_next_action(campaign_id).action or {})
        workspace = Path(action["workspace"])
        workspace.joinpath("outside.txt").write_text("not allowed\n", encoding="utf-8")
        before = self.snapshot()

        rejected = controller.submit_action_result(action["id"], self.passing_result(action))

        self.assertEqual(rejected.status, "INVALID", rejected)
        self.assertIn("outside-allowed", rejected.message)
        self.assertEqual(self.snapshot(), before)

        workspace.joinpath("outside.txt").unlink()
        workspace.joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")
        self.root.joinpath("user-note.txt").write_text("concurrent\n", encoding="utf-8")
        before_concurrent = self.snapshot()

        concurrent = controller.submit_action_result(action["id"], self.passing_result(action))

        self.assertEqual(concurrent.status, "INVALID", concurrent)
        self.assertIn("source changed concurrently", concurrent.message)
        self.assertEqual(self.snapshot(), before_concurrent)

    def test_worker_rework_and_blocked_results_still_enforce_actual_path_policy(self) -> None:
        campaign_id = self.approve()
        controller = ActionController(self.root)
        action = dict(controller.get_next_action(campaign_id).action or {})
        Path(action["workspace"]).joinpath("forbidden.txt").write_text("forbidden\n", encoding="utf-8")
        rework = self.passing_result(action)
        rework["outcome"] = "REWORK"
        before = self.snapshot()

        rejected_rework = controller.submit_action_result(action["id"], rework)

        self.assertEqual(rejected_rework.status, "INVALID", rejected_rework)
        self.assertIn("outside-allowed", rejected_rework.message)
        self.assertEqual(self.snapshot(), before)
        blocked = self.passing_result(action)
        blocked.update(
            outcome="BLOCKED", blocker="External credential is unavailable.",
            next_action="Provide the credential.",
        )
        rejected_blocked = controller.submit_action_result(action["id"], blocked)
        self.assertEqual(rejected_blocked.status, "INVALID", rejected_blocked)
        self.assertEqual(self.snapshot(), before)

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

    def test_terminal_target_action_is_reconciled_before_retarget_and_materialize_retry(self) -> None:
        campaign_id = self.approve()
        controller = ActionController(self.root)
        worker = dict(controller.get_next_action(campaign_id).action or {})
        Path(worker["workspace"]).joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")
        target = controller.submit_action_result(worker["id"], self.passing_result(worker))
        self.assertEqual(target.action["type"], "TARGET_REACHED")

        materialized = CampaignController(self.root).materialize(campaign_id)

        self.assertEqual(materialized.status, "SUCCESS", materialized)
        replacement_target = ActionController(self.root).get_next_action(campaign_id)
        self.assertEqual(replacement_target.action["type"], "TARGET_REACHED")
        self.assertNotEqual(replacement_target.action["id"], target.action["id"])
        retargeted = CampaignController(self.root).retarget(campaign_id, "WORKING_MVP")
        self.assertEqual(retargeted.status, "SUCCESS", retargeted)
        planning = ActionController(self.root).get_next_action(campaign_id)
        self.assertEqual(planning.status, "SUCCESS", planning)
        self.assertEqual(planning.action["type"], "PLAN_PHASE")

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
        self.assertEqual(second_outcome.status, "SUCCESS", second_outcome)
        second = dict(second_outcome.action or {})
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


if __name__ == "__main__":
    unittest.main()
