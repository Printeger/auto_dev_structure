from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "src"))

from autodev._project import initialize_project
from autodev.campaign import CampaignController, CampaignRequest, FakePlanner
from autodev.control_plane import Command, ControlPlane
from autodev.engines import EngineResult, FakeCodexRunner
from autodev.run_controller import RunController, RunRequest
from autodev.reporting import render_report


def git(root: Path, *arguments: str) -> None:
    result = subprocess.run(["git", *arguments], cwd=root, capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stderr)


def task(*, task_id: str = "TASK-001", risk: str = "MEDIUM", dependencies: list[str] | None = None) -> dict[str, object]:
    return {
        "id": task_id, "title": "Implement the requirement", "objective": "Change app.py.",
        "requirements": ["REQ-001"], "dependencies": dependencies or [],
        "priority": "MUST", "blocking": True, "risk": risk, "quality_mode": "BUILD",
        "change_classes": ["implementation"], "allowed_paths": ["app.py"], "out_of_scope": [],
        "acceptance_criteria": [{"id": "AC-001", "description": "The behavior works."}],
        "validation_commands": [{"argv": ["python3", "-m", "unittest"], "cwd": ".", "timeout": 60}],
        "prohibited_actions": ["commit", "push", "publish", "deploy"],
    }


def proposal(tasks: list[dict[str, object]]) -> dict[str, object]:
    return {
        "requirements": [{
            "id": "REQ-001", "priority": "MUST", "statement": "Provide behavior.",
            "acceptance_signal": "Tests pass.",
        }],
        "authority_envelope": {
            "max_task_risk": "MEDIUM",
            "allowed_change_classes": ["implementation", "test", "documentation", "architecture", "internal-interface", "shared-internal-data"],
            "dependency_policy": "existing-only", "public_api_changes": "require-human",
            "security_changes": "require-human", "data_migration": "require-human",
            "permission_expansion": "require-human", "remote_actions": "forbidden",
        }, "phase": "SCAFFOLD", "tasks": tasks, "questions": [],
    }


class CampaignControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "test@example.com")
        git(self.root, "config", "user.name", "Test")
        self.assertEqual(initialize_project(self.root, "campaign").status, "SUCCESS")
        (self.root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "base")

    def test_plan_approve_freezes_json_baseline_and_atomically_admits_tasks(self) -> None:
        planner = FakePlanner([proposal([task()])])
        controller = CampaignController(self.root, planner)
        planned = controller.plan(CampaignRequest("Build a useful system", target="ARCHITECTURE_BASELINE"))
        self.assertEqual(planned.status, "SUCCESS", planned)
        approved = controller.approve(planned.campaign_id or "", planned.data["proposal_hash"])
        self.assertEqual(approved.status, "SUCCESS", approved)

        campaign_id = planned.campaign_id or ""
        requirements = json.loads((self.root / f".autodev/campaigns/{campaign_id}/requirements.json").read_text())
        self.assertEqual(requirements["requirements"][0]["statement"], "Provide behavior.")
        contract = json.loads((self.root / ".autodev/tasks/TASK-001/contract.json").read_text())
        self.assertEqual(
            {key: contract[key] for key in ("campaign_id", "phase", "admission", "review_scope")},
            {"campaign_id": campaign_id, "phase": "SCAFFOLD", "admission": "AUTO_ADMITTED", "review_scope": "NONE"},
        )
        state = json.loads((self.root / ".autodev/state.json").read_text())
        self.assertEqual(state["tasks"]["TASK-001"]["status"], "READY")
        self.assertEqual(ControlPlane(self.root).execute(Command("validate")).status, "SUCCESS")

        # Once a V3 Campaign exists, its JSON baseline is canonical; the legacy
        # Markdown view may be removed without invalidating the control plane.
        (self.root / "docs/REQUIREMENTS.md").unlink()
        self.assertEqual(ControlPlane(self.root).execute(Command("validate")).status, "SUCCESS")

    def test_modes_and_targets_have_the_frozen_phase_sequences(self) -> None:
        self.assertEqual(CampaignController._phases("CHANGE", "CHANGE_COMPLETE"), ["IMPLEMENT"])
        for mode in ("STAGED", "CRITICAL"):
            for target in (
                "ARCHITECTURE_BASELINE", "WORKING_MVP",
                "INTEGRATED_SYSTEM", "RELEASE_CANDIDATE",
            ):
                self.assertEqual(
                    CampaignController._phases(mode, target),
                    ["SCAFFOLD", "IMPLEMENT", "COMPONENT_VERIFY", "INTEGRATE", "HARDEN"],
                )

    def test_duplicate_requirement_ids_are_rejected_before_campaign_state_is_written(self) -> None:
        duplicated = proposal([task()])
        duplicated["requirements"] = [
            duplicated["requirements"][0], duplicated["requirements"][0],
        ]
        planned = CampaignController(self.root, FakePlanner([duplicated])).plan(
            CampaignRequest("Build", target="ARCHITECTURE_BASELINE")
        )
        self.assertEqual(planned.status, "INVALID", planned)
        state = json.loads((self.root / ".autodev/state.json").read_text())
        self.assertNotIn("campaigns", state)

    def test_one_authority_violation_rejects_the_entire_batch(self) -> None:
        planner = FakePlanner([proposal([task(), task(task_id="TASK-002", risk="HIGH")])])
        controller = CampaignController(self.root, planner)
        planned = controller.plan(CampaignRequest("Build", target="ARCHITECTURE_BASELINE"))
        approved = controller.approve(planned.campaign_id or "", planned.data["proposal_hash"])
        self.assertEqual(approved.status, "BLOCKED", approved)
        state = json.loads((self.root / ".autodev/state.json").read_text())
        self.assertEqual(state["tasks"], {})
        self.assertEqual(state["campaigns"][planned.campaign_id]["status"], "WAITING_FOR_HUMAN")
        self.assertIn("request_id", approved.data)
        resumed = controller.answer(
            planned.campaign_id or "", approved.data["request_id"],
            {"decision": ["Approve exception"]},
        )
        self.assertEqual(resumed.status, "SUCCESS", resumed)
        state = json.loads((self.root / ".autodev/state.json").read_text())
        self.assertEqual(set(state["tasks"]), {"TASK-001", "TASK-002"})
        contract = json.loads((self.root / ".autodev/tasks/TASK-002/contract.json").read_text())
        self.assertEqual(contract["admission"], "HUMAN_APPROVED")

    def test_cycle_rejects_the_entire_batch(self) -> None:
        cyclic = [
            task(task_id="TASK-001", dependencies=["TASK-002"]),
            task(task_id="TASK-002", dependencies=["TASK-001"]),
        ]
        planner = FakePlanner([proposal(cyclic)])
        controller = CampaignController(self.root, planner)
        planned = controller.plan(CampaignRequest("Build", target="ARCHITECTURE_BASELINE"))
        approved = controller.approve(planned.campaign_id or "", planned.data["proposal_hash"])
        self.assertEqual(approved.status, "BLOCKED", approved)
        self.assertTrue(any("cycle" in item for item in approved.data["errors"]))

    def test_two_tasks_share_private_checkpoint_and_target_writes_back_once(self) -> None:
        first = task()
        second = task(task_id="TASK-002", dependencies=["TASK-001"])
        second["allowed_paths"] = ["second.py"]
        first["validation_commands"] = [{"argv": ["python3", "-c", "pass"], "cwd": ".", "timeout": 60}]
        second["validation_commands"] = [{"argv": ["python3", "-c", "pass"], "cwd": ".", "timeout": 60}]
        planner = FakePlanner([proposal([first, second])])
        controller = CampaignController(self.root, planner)
        planned = controller.plan(CampaignRequest("Build", target="ARCHITECTURE_BASELINE"))
        self.assertEqual(
            controller.approve(planned.campaign_id or "", planned.data["proposal_hash"]).status,
            "SUCCESS",
        )
        passed = EngineResult("SUCCESS", {
            "outcome": "PASS", "summary": "done", "blocker": None,
            "next_action": None, "findings": [], "debt_items": [],
        })
        first_engine = FakeCodexRunner([passed], [{"app.py": "VALUE = 2\n"}])
        first_outcome = RunController(self.root, first_engine).run(RunRequest())
        self.assertEqual(first_outcome.status, "SUCCESS", first_outcome)
        self.assertEqual((self.root / "app.py").read_text(), "VALUE = 1\n")

        def second_change(request: object) -> None:
            self.assertEqual(request.workspace.joinpath("app.py").read_text(), "VALUE = 2\n")
            request.workspace.joinpath("second.py").write_text("READY = True\n", encoding="utf-8")

        second_outcome = RunController(
            self.root, FakeCodexRunner([passed], [second_change]),
        ).run(RunRequest())
        self.assertEqual(second_outcome.status, "SUCCESS", second_outcome)
        self.assertFalse((self.root / "second.py").exists())

        reached = controller.phase_gate(planned.campaign_id or "")
        self.assertEqual(reached.status, "SUCCESS", reached)
        self.assertEqual((self.root / "app.py").read_text(), "VALUE = 2\n")
        self.assertEqual((self.root / "second.py").read_text(), "READY = True\n")
        state = json.loads((self.root / ".autodev/state.json").read_text())
        self.assertEqual(state["campaigns"][planned.campaign_id]["status"], "TARGET_REACHED")
        requirements_md = render_report(self.root, "requirements", campaign_id=planned.campaign_id)
        phase_md = render_report(self.root, "phase", campaign_id=planned.campaign_id)
        release_md = render_report(self.root, "release", campaign_id=planned.campaign_id)
        self.assertIn("REQ-001", requirements_md)
        self.assertIn("SCAFFOLD", phase_md)
        self.assertIn("Accepted attempts: 2", release_md)
        campaign_id = planned.campaign_id or ""
        archived = controller.archive(campaign_id)
        self.assertEqual(archived.status, "SUCCESS", archived)
        self.assertEqual(controller.archive(campaign_id).status, "SUCCESS")
        self.assertNotEqual(subprocess.run(
            ["git", "show-ref", "--verify", "refs/autodev/campaigns/CAMP-001/current"],
            cwd=self.root, capture_output=True,
        ).returncode, 0)

    def test_architecture_change_reviews_the_cumulative_phase_not_the_task(self) -> None:
        architecture = task()
        architecture["change_classes"] = ["architecture"]
        architecture["validation_commands"] = [{"argv": ["python3", "-c", "pass"], "cwd": ".", "timeout": 10}]
        controller = CampaignController(self.root, FakePlanner([proposal([architecture])]))
        planned = controller.plan(CampaignRequest("Architecture", target="ARCHITECTURE_BASELINE"))
        self.assertEqual(controller.approve("CAMP-001", planned.data["proposal_hash"]).status, "SUCCESS")
        passed = EngineResult("SUCCESS", {
            "outcome": "PASS", "summary": "done", "blocker": None,
            "next_action": None, "findings": [], "debt_items": [],
        })
        task_reviewer = FakeCodexRunner([])
        outcome = RunController(
            self.root, FakeCodexRunner([passed], [{"app.py": "VALUE = 2\n"}]),
            reviewer_engine=task_reviewer,
        ).run(RunRequest())
        self.assertEqual(outcome.status, "SUCCESS", outcome)
        self.assertEqual(task_reviewer.requests, [])
        phase_reviewer = FakeCodexRunner([passed])
        gate = controller.phase_gate("CAMP-001", reviewer_engine=phase_reviewer)
        self.assertEqual(gate.status, "SUCCESS", gate)
        self.assertEqual(len(phase_reviewer.requests), 1)
        self.assertIn("cumulative Phase diff", phase_reviewer.requests[0].prompt)

    def test_next_phase_questions_are_persisted_and_wait_for_a_fresh_planner(self) -> None:
        first = task()
        first["validation_commands"] = [{"argv": ["python3", "-c", "pass"], "cwd": ".", "timeout": 10}]
        initial = proposal([first])
        next_phase = proposal([])
        next_phase["phase"] = "IMPLEMENT"
        next_phase["questions"] = [{
            "id": "direction", "header": "Direction", "question": "Choose the implementation direction.",
            "options": [
                {"label": "Narrow", "description": "Keep the approved scope small."},
                {"label": "Broad", "description": "Use the larger internal design."},
            ],
            "isOther": True, "isSecret": False,
        }]
        controller = CampaignController(self.root, FakePlanner([initial, next_phase]))
        planned = controller.plan(CampaignRequest("Build", target="WORKING_MVP"))
        self.assertEqual(controller.approve("CAMP-001", planned.data["proposal_hash"]).status, "SUCCESS")
        passed = EngineResult("SUCCESS", {
            "outcome": "PASS", "summary": "done", "blocker": None,
            "next_action": None, "findings": [], "debt_items": [],
        })
        self.assertEqual(RunController(
            self.root, FakeCodexRunner([passed], [{"app.py": "VALUE = 2\n"}]),
        ).run(RunRequest()).status, "SUCCESS")
        waiting = controller.phase_gate("CAMP-001")
        self.assertEqual(waiting.status, "NOT_READY", waiting)
        artifact = (
            self.root / ".autodev/campaigns/CAMP-001/human-requests"
            / f"{waiting.data['request_id']}.json"
        )
        self.assertTrue(artifact.is_file())
        state = json.loads((self.root / ".autodev/state.json").read_text())
        self.assertEqual(state["campaigns"]["CAMP-001"]["status"], "WAITING_FOR_HUMAN")
        self.assertEqual(state["campaigns"]["CAMP-001"]["phase"], "IMPLEMENT")

    def test_materialization_conflict_blocks_project_and_can_be_retried(self) -> None:
        only_task = task()
        only_task["validation_commands"] = [{"argv": ["python3", "-c", "pass"], "cwd": ".", "timeout": 10}]
        controller = CampaignController(self.root, FakePlanner([proposal([only_task])]))
        planned = controller.plan(CampaignRequest("Build", target="ARCHITECTURE_BASELINE"))
        self.assertEqual(controller.approve("CAMP-001", planned.data["proposal_hash"]).status, "SUCCESS")
        passed = EngineResult("SUCCESS", {
            "outcome": "PASS", "summary": "done", "blocker": None,
            "next_action": None, "findings": [], "debt_items": [],
        })
        self.assertEqual(RunController(
            self.root, FakeCodexRunner([passed], [{"app.py": "VALUE = 2\n"}]),
        ).run(RunRequest()).status, "SUCCESS")
        (self.root / "user.txt").write_text("concurrent\n", encoding="utf-8")

        blocked = controller.phase_gate("CAMP-001")
        self.assertEqual(blocked.status, "BLOCKED", blocked)
        state = json.loads((self.root / ".autodev/state.json").read_text())
        self.assertEqual(state["project_status"], "BLOCKED")
        self.assertEqual(state["campaigns"]["CAMP-001"]["status"], "TARGET_REACHED")
        (self.root / "user.txt").unlink()
        retried = controller.materialize("CAMP-001")
        self.assertEqual(retried.status, "SUCCESS", retried)
        state = json.loads((self.root / ".autodev/state.json").read_text())
        self.assertEqual(state["project_status"], "IDLE")
        checkpoint = state["campaigns"]["CAMP-001"]["checkpoint"]
        retargeted = controller.retarget("CAMP-001", "WORKING_MVP")
        self.assertEqual(retargeted.status, "SUCCESS", retargeted)
        state = json.loads((self.root / ".autodev/state.json").read_text())
        self.assertEqual(state["campaigns"]["CAMP-001"]["phase"], "IMPLEMENT")
        self.assertEqual(state["campaigns"]["CAMP-001"]["checkpoint"], checkpoint)

    def test_archive_refuses_active_campaign_without_deleting_private_ref(self) -> None:
        controller = CampaignController(self.root, FakePlanner([proposal([task()])]))
        planned = controller.plan(CampaignRequest("Build", target="ARCHITECTURE_BASELINE"))
        self.assertEqual(controller.approve("CAMP-001", planned.data["proposal_hash"]).status, "SUCCESS")
        before = (self.root / "app.py").read_bytes()
        refused_materialization = controller.materialize("CAMP-001")
        self.assertEqual(refused_materialization.status, "NOT_READY", refused_materialization)
        self.assertEqual((self.root / "app.py").read_bytes(), before)
        refused = controller.archive("CAMP-001")
        self.assertEqual(refused.status, "NOT_READY", refused)
        result = subprocess.run(
            ["git", "show-ref", "--verify", "refs/autodev/campaigns/CAMP-001/current"],
            cwd=self.root, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_legacy_complete_command_is_read_only_compatibility_in_v3(self) -> None:
        controller = CampaignController(self.root, FakePlanner([proposal([task()])]))
        planned = controller.plan(CampaignRequest("Build", target="ARCHITECTURE_BASELINE"))
        self.assertEqual(controller.approve("CAMP-001", planned.data["proposal_hash"]).status, "SUCCESS")
        completed = ControlPlane(self.root).execute(Command("complete"))
        self.assertEqual(completed.status, "INVALID", completed)
        self.assertIn("Campaign target", completed.message)


if __name__ == "__main__":
    unittest.main()
