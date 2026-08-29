from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "src"))

from autodev._project import initialize_project
from autodev.action import ActionController
from autodev.campaign import CampaignController, CampaignRequest, FakePlanner
from autodev.engines import EngineResult, FakeCodexRunner
from autodev.run_controller import RunController, RunRequest


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(["git", *arguments], cwd=root, capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def proposal(*, risk: str = "MEDIUM") -> dict[str, object]:
    task = {
        "id": "TASK-001", "title": "Change the value", "objective": "Change app.py.",
        "requirements": ["REQ-001"], "dependencies": [], "priority": "MUST",
        "blocking": True, "risk": risk, "quality_mode": "BUILD",
        "change_classes": ["implementation"], "allowed_paths": ["app.py"],
        "out_of_scope": [],
        "acceptance_criteria": [{"id": "AC-001", "description": "The value changes."}],
        "validation_commands": [{
            "argv": ["python3", "-c", "assert open('app.py').read() == 'VALUE = 2\\n'"],
            "cwd": ".", "timeout": 20,
        }],
        "prohibited_actions": ["commit", "push", "publish", "deploy"],
    }
    return {
        "requirements": [{
            "id": "REQ-001", "priority": "MUST", "statement": "Change the value.",
            "acceptance_signal": "Validation passes.",
        }],
        "authority_envelope": {
            "max_task_risk": "HIGH", "allowed_change_classes": ["implementation", "security"],
            "dependency_policy": "existing-only", "public_api_changes": "allow",
            "security_changes": "allow", "data_migration": "require-human",
            "permission_expansion": "require-human", "remote_actions": "forbidden",
        },
        "phase": "SCAFFOLD", "tasks": [task], "questions": [],
    }


class AttemptParityTests(unittest.TestCase):
    def project(self, *, risk: str = "MEDIUM") -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        git(root, "init", "-q")
        git(root, "config", "user.email", "test@example.com")
        git(root, "config", "user.name", "Test")
        self.assertEqual(initialize_project(root, "attempt-parity").status, "SUCCESS")
        (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        git(root, "add", ".")
        git(root, "commit", "-qm", "base")
        campaign = CampaignController(root, FakePlanner([proposal(risk=risk)]))
        planned = campaign.plan(CampaignRequest("Change value", target="ARCHITECTURE_BASELINE"))
        self.assertEqual(planned.status, "SUCCESS", planned)
        self.assertEqual(
            campaign.approve("CAMP-001", planned.data["proposal_hash"]).status, "SUCCESS",
        )
        return temporary, root

    @staticmethod
    def action_result(action: dict[str, object]) -> dict[str, object]:
        return {
            "action_id": action["id"], "canonical_revision": action["canonical_revision"],
            "outcome": "PASS", "summary": "done", "data": {}, "findings": [],
            "blocker": None, "next_action": None,
        }

    @staticmethod
    def evidence(root: Path) -> dict[str, object]:
        paths = list((root / ".autodev/runs").glob("*/evidence.json"))
        if len(paths) != 1:
            raise AssertionError(f"expected one evidence file, found {paths}")
        return json.loads(paths[0].read_text())

    @staticmethod
    def acceptance_evidence(evidence: dict[str, object]) -> dict[str, object]:
        fields = {
            "schema_version", "task_id", "outcome", "contract_gate_hash", "diff_hash",
            "changed_paths", "validations", "quality_route", "acceptance_hash",
        }
        return {key: evidence[key] for key in fields}

    def test_successful_action_and_headless_attempts_share_acceptance_evidence(self) -> None:
        _, action_root = self.project()
        action_controller = ActionController(action_root)
        action = dict(action_controller.get_next_action("CAMP-001").action or {})
        Path(action["workspace"]).joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")

        action_outcome = action_controller.submit_action_result(
            action["id"], self.action_result(action),
        )

        _, headless_root = self.project()
        engine = FakeCodexRunner([
            EngineResult("SUCCESS", {
                "outcome": "PASS", "summary": "done", "blocker": None,
                "next_action": None, "findings": [], "debt_items": [],
            }),
        ], [{"app.py": "VALUE = 2\n"}])
        headless_outcome = RunController(headless_root, engine).run(RunRequest())

        self.assertEqual((action_outcome.status, action_outcome.exit_code), ("SUCCESS", 0))
        self.assertEqual((headless_outcome.status, headless_outcome.exit_code), ("SUCCESS", 0))
        action_state = json.loads((action_root / ".autodev/state.json").read_text())
        headless_state = json.loads((headless_root / ".autodev/state.json").read_text())
        self.assertEqual(action_state["tasks"]["TASK-001"]["status"], "ACCEPTED")
        self.assertEqual(headless_state["tasks"]["TASK-001"]["status"], "ACCEPTED")
        self.assertEqual(action_state["last_outcome"], headless_state["last_outcome"])
        self.assertEqual(action["quality_route"], "NONE")
        self.assertEqual([request.role for request in engine.requests], ["builder"])
        action_checkpoint = action_state["campaigns"]["CAMP-001"]["checkpoint"]
        headless_checkpoint = headless_state["campaigns"]["CAMP-001"]["checkpoint"]
        self.assertEqual(git(action_root, "show", f"{action_checkpoint}:app.py"), "VALUE = 2")
        self.assertEqual(git(headless_root, "show", f"{headless_checkpoint}:app.py"), "VALUE = 2")
        action_evidence = self.evidence(action_root)
        headless_evidence = self.evidence(headless_root)
        self.assertEqual(
            self.acceptance_evidence(action_evidence),
            self.acceptance_evidence(headless_evidence),
        )
        for root, state in ((action_root, action_state), (headless_root, headless_state)):
            run_id = next((root / ".autodev/runs").glob("*/evidence.json")).parent.name
            journal = json.loads(
                (root / ".autodev/campaigns/CAMP-001/checkpoint-journal" / f"{run_id}.json").read_text()
            )
            self.assertEqual(journal["phase"], "COMMITTED")
            self.assertEqual(journal["commit"], state["campaigns"]["CAMP-001"]["checkpoint"])

    def test_blocked_results_share_canonical_outcome_and_exit_code(self) -> None:
        _, action_root = self.project()
        action_controller = ActionController(action_root)
        action = dict(action_controller.get_next_action("CAMP-001").action or {})
        action_result = self.action_result(action)
        action_result.update(
            outcome="BLOCKED", summary="credential missing",
            blocker="Credential is unavailable.", next_action="Provide the credential.",
        )
        action_outcome = action_controller.submit_action_result(action["id"], action_result)

        _, headless_root = self.project()
        headless_result = EngineResult("SUCCESS", {
            "outcome": "BLOCKED", "summary": "credential missing",
            "blocker": "Credential is unavailable.", "next_action": "Provide the credential.",
            "findings": [], "debt_items": [],
        })
        headless_outcome = RunController(
            headless_root, FakeCodexRunner([headless_result]),
        ).run(RunRequest())

        self.assertEqual((action_outcome.status, action_outcome.exit_code), ("BLOCKED", 3))
        self.assertEqual((headless_outcome.status, headless_outcome.exit_code), ("BLOCKED", 3))
        for root in (action_root, headless_root):
            state = json.loads((root / ".autodev/state.json").read_text())
            self.assertEqual(state["tasks"]["TASK-001"]["status"], "BLOCKED")
            self.assertEqual(state["last_outcome"], "BLOCKED")
        self.assertEqual(
            self.acceptance_evidence(self.evidence(action_root)),
            self.acceptance_evidence(self.evidence(headless_root)),
        )

    def test_immediate_review_route_uses_fresh_reviewer_and_shared_evidence(self) -> None:
        _, action_root = self.project(risk="HIGH")
        action_controller = ActionController(action_root)
        worker = dict(action_controller.get_next_action("CAMP-001").action or {})
        Path(worker["workspace"]).joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")
        review_outcome = action_controller.submit_action_result(
            worker["id"], self.action_result(worker),
        )
        review = dict(review_outcome.action or {})
        action_outcome = action_controller.submit_action_result(
            review["id"], self.action_result(review),
        )

        _, headless_root = self.project(risk="HIGH")
        passing = EngineResult("SUCCESS", {
            "outcome": "PASS", "summary": "done", "blocker": None,
            "next_action": None, "findings": [], "debt_items": [],
        })
        builder = FakeCodexRunner([passing], [{"app.py": "VALUE = 2\n"}])
        reviewer = FakeCodexRunner([passing])
        headless_outcome = RunController(
            headless_root, builder, reviewer_engine=reviewer,
        ).run(RunRequest())

        self.assertEqual(worker["quality_route"], "IMMEDIATE")
        self.assertEqual((review["type"], review["role"]), ("RUN_IMMEDIATE_REVIEW", "reviewer"))
        self.assertNotEqual(worker["id"], review["id"])
        self.assertEqual([request.role for request in builder.requests], ["builder"])
        self.assertEqual([request.role for request in reviewer.requests], ["reviewer"])
        self.assertEqual(reviewer.requests[0].permission_profile, ":read-only")
        self.assertEqual((action_outcome.status, headless_outcome.status), ("SUCCESS", "SUCCESS"))
        self.assertEqual(
            self.acceptance_evidence(self.evidence(action_root)),
            self.acceptance_evidence(self.evidence(headless_root)),
        )

    def test_both_adapters_enforce_path_and_validation_gates_without_checkpointing(self) -> None:
        _, action_path_root = self.project()
        action_controller = ActionController(action_path_root)
        action = dict(action_controller.get_next_action("CAMP-001").action or {})
        initial_action_checkpoint = json.loads(
            (action_path_root / ".autodev/state.json").read_text()
        )["campaigns"]["CAMP-001"]["checkpoint"]
        Path(action["workspace"]).joinpath("forbidden.txt").write_text("no\n", encoding="utf-8")
        action_path = action_controller.submit_action_result(action["id"], self.action_result(action))

        _, headless_path_root = self.project()
        initial_headless_checkpoint = json.loads(
            (headless_path_root / ".autodev/state.json").read_text()
        )["campaigns"]["CAMP-001"]["checkpoint"]
        headless_path = RunController(
            headless_path_root,
            FakeCodexRunner([
                EngineResult("SUCCESS", {
                    "outcome": "PASS", "summary": "done", "blocker": None,
                    "next_action": None, "findings": [], "debt_items": [],
                }),
            ], [{"forbidden.txt": "no\n"}]),
        ).run(RunRequest())

        self.assertEqual((action_path.status, action_path.exit_code), ("INVALID", 1))
        self.assertEqual((headless_path.status, headless_path.exit_code), ("NOT_READY", 2))
        self.assertIn("outside-allowed", action_path.message)
        self.assertEqual(
            json.loads((action_path_root / ".autodev/state.json").read_text())
            ["campaigns"]["CAMP-001"]["checkpoint"], initial_action_checkpoint,
        )
        self.assertEqual(
            json.loads((headless_path_root / ".autodev/state.json").read_text())
            ["campaigns"]["CAMP-001"]["checkpoint"], initial_headless_checkpoint,
        )

        _, action_validation_root = self.project()
        validation_controller = ActionController(action_validation_root)
        validation_action = dict(validation_controller.get_next_action("CAMP-001").action or {})
        Path(validation_action["workspace"]).joinpath("app.py").write_text(
            "VALUE = 3\n", encoding="utf-8",
        )
        action_validation = validation_controller.submit_action_result(
            validation_action["id"], self.action_result(validation_action),
        )
        _, headless_validation_root = self.project()
        headless_validation = RunController(
            headless_validation_root,
            FakeCodexRunner([
                EngineResult("SUCCESS", {
                    "outcome": "PASS", "summary": "done", "blocker": None,
                    "next_action": None, "findings": [], "debt_items": [],
                }),
            ], [{"app.py": "VALUE = 3\n"}]),
        ).run(RunRequest())
        self.assertEqual((action_validation.status, action_validation.exit_code), ("SUCCESS", 0))
        self.assertEqual(action_validation.action["type"], "EXECUTE_TASK")
        self.assertEqual(
            (headless_validation.status, headless_validation.exit_code), ("NOT_READY", 2),
        )
        self.assertEqual(
            json.loads((headless_validation_root / ".autodev/state.json").read_text())
            ["last_outcome"], "REWORK",
        )
        self.assertEqual(
            self.acceptance_evidence(self.evidence(action_validation_root)),
            self.acceptance_evidence(self.evidence(headless_validation_root)),
        )

    def test_review_finding_and_debt_budgets_are_shared_across_adapters(self) -> None:
        debt_items = [
            {
                "id": f"DEBT-{index:03d}", "source_task": "TASK-001",
                "reason": "Deferred cleanup.", "severity": "LOW", "module": "app",
                "fix_before": "M9", "classification": "maintainability",
            }
            for index in range(6)
        ]
        cases = (
            ("findings", "Review exceeded the blocking finding budget"),
            ("debt", "Review exceeded the debt finding budget"),
        )
        for kind, expected_error in cases:
            with self.subTest(kind=kind):
                _, action_root = self.project(risk="HIGH")
                action_controller = ActionController(action_root)
                worker = dict(action_controller.get_next_action("CAMP-001").action or {})
                initial_action_checkpoint = json.loads(
                    (action_root / ".autodev/state.json").read_text()
                )["campaigns"]["CAMP-001"]["checkpoint"]
                Path(worker["workspace"]).joinpath("app.py").write_text(
                    "VALUE = 2\n", encoding="utf-8",
                )
                review = dict(action_controller.submit_action_result(
                    worker["id"], self.action_result(worker),
                ).action or {})
                review_result = self.action_result(review)
                if kind == "findings":
                    review_result["findings"] = [f"blocking-{index}" for index in range(6)]
                else:
                    review_result["outcome"] = "PASS_WITH_DEBT"
                    review_result["data"] = {"debt_items": debt_items}
                before_rejection = (action_root / ".autodev/state.json").read_bytes()
                action_rejection = action_controller.submit_action_result(
                    review["id"], review_result,
                )

                _, headless_root = self.project(risk="HIGH")
                initial_headless_checkpoint = json.loads(
                    (headless_root / ".autodev/state.json").read_text()
                )["campaigns"]["CAMP-001"]["checkpoint"]
                passing = EngineResult("SUCCESS", {
                    "outcome": "PASS", "summary": "done", "blocker": None,
                    "next_action": None, "findings": [], "debt_items": [],
                })
                rejected_proposal = {
                    "outcome": "PASS", "summary": "reviewed", "blocker": None,
                    "next_action": None, "findings": [], "debt_items": [],
                }
                if kind == "findings":
                    rejected_proposal["findings"] = [
                        f"blocking-{index}" for index in range(6)
                    ]
                else:
                    rejected_proposal["outcome"] = "PASS_WITH_DEBT"
                    rejected_proposal["debt_items"] = debt_items
                headless_rejection = RunController(
                    headless_root,
                    FakeCodexRunner([passing], [{"app.py": "VALUE = 2\n"}]),
                    reviewer_engine=FakeCodexRunner([
                        EngineResult("SUCCESS", rejected_proposal),
                    ]),
                ).run(RunRequest())

                self.assertEqual((action_rejection.status, action_rejection.exit_code), ("INVALID", 1))
                self.assertIn(expected_error, action_rejection.message)
                self.assertEqual((headless_rejection.status, headless_rejection.exit_code), ("BLOCKED", 3))
                self.assertIn(
                    expected_error,
                    json.loads((headless_root / ".autodev/state.json").read_text())["blocker"],
                )
                self.assertEqual(
                    (action_root / ".autodev/state.json").read_bytes(), before_rejection,
                )
                self.assertEqual(
                    json.loads((action_root / ".autodev/state.json").read_text())
                    ["campaigns"]["CAMP-001"]["checkpoint"], initial_action_checkpoint,
                )
                self.assertEqual(
                    json.loads((headless_root / ".autodev/state.json").read_text())
                    ["campaigns"]["CAMP-001"]["checkpoint"], initial_headless_checkpoint,
                )

    def test_ref_advanced_checkpoint_fault_recovers_in_both_public_flows(self) -> None:
        from autodev import control_plane

        def fault(root: Path):
            initial = json.loads((root / ".autodev/state.json").read_text())[
                "campaigns"
            ]["CAMP-001"]["checkpoint"]
            original = control_plane._atomic_replace_json
            failed_once = False

            def fail_checkpoint_state(path: Path, value: object) -> None:
                nonlocal failed_once
                if (
                    not failed_once and path.name == "state.json" and isinstance(value, dict)
                    and value.get("campaigns", {}).get("CAMP-001", {}).get("checkpoint") != initial
                ):
                    failed_once = True
                    raise OSError("injected canonical checkpoint state fault")
                original(path, value)

            return mock.patch.object(
                control_plane, "_atomic_replace_json", side_effect=fail_checkpoint_state,
            )

        _, action_root = self.project()
        action_controller = ActionController(action_root)
        action = dict(action_controller.get_next_action("CAMP-001").action or {})
        Path(action["workspace"]).joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")
        action_result = self.action_result(action)
        with fault(action_root):
            action_interrupted = action_controller.submit_action_result(action["id"], action_result)
        action_recovered = ActionController(action_root).submit_action_result(
            action["id"], action_result,
        )

        _, headless_root = self.project()
        engine = FakeCodexRunner([
            EngineResult("SUCCESS", {
                "outcome": "PASS", "summary": "done", "blocker": None,
                "next_action": None, "findings": [], "debt_items": [],
            }),
        ], [{"app.py": "VALUE = 2\n"}])
        campaign = CampaignController(headless_root)
        with fault(headless_root):
            headless_interrupted = campaign.run_until_target_or_blocked(
                "CAMP-001", engine,
            )
        if json.loads((headless_root / ".autodev/state.json").read_text())["project_status"] != "ACTIVE":
            self.assertEqual(
                control_plane.ControlPlane(headless_root).execute(
                    control_plane.Command("activate"),
                ).status,
                "SUCCESS",
            )
        headless_recovered = CampaignController(headless_root).run_until_target_or_blocked(
            "CAMP-001", engine,
        )

        self.assertEqual(action_interrupted.status, "INFRA_FAILURE")
        self.assertEqual(headless_interrupted.status, "INFRA_FAILURE")
        self.assertEqual(action_recovered.status, "SUCCESS", action_recovered)
        self.assertEqual(headless_recovered.status, "SUCCESS", headless_recovered)
        for root in (action_root, headless_root):
            state = json.loads((root / ".autodev/state.json").read_text())
            self.assertEqual(state["tasks"]["TASK-001"]["status"], "ACCEPTED")
            journals = list(
                (root / ".autodev/campaigns/CAMP-001/checkpoint-journal").glob("*.json")
            )
            self.assertEqual(len(journals), 1)
            self.assertEqual(json.loads(journals[0].read_text())["phase"], "COMMITTED")


if __name__ == "__main__":
    unittest.main()
