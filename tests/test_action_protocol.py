from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import inspect
from pathlib import Path


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
                "internal-interface", "shared-internal-data",
            ],
            "dependency_policy": "existing-only",
            "public_api_changes": "require-human",
            "security_changes": "require-human",
            "data_migration": "require-human",
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
