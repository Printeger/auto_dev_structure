from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "src"))

from autodev import Command, ControlPlane
from autodev._project import initialize_project
from autodev.engines import CodexExecEngine, EngineResult, FakeCodexRunner
from autodev.run_controller import RunController, RunRequest


def git(root: Path, *arguments: str) -> None:
    result = subprocess.run(["git", *arguments], cwd=root, capture_output=True, text=True, check=False)
    if result.returncode:
        raise AssertionError(result.stderr)


class RunnerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "test@example.com")
        git(self.root, "config", "user.name", "Test")
        self.assertEqual(initialize_project(self.root, "runner").status, "SUCCESS")
        (self.root / "app.txt").write_text("base\n", encoding="utf-8")
        (self.root / "docs/REQUIREMENTS.md").write_text(
            "# Requirements\n\n| ID | Priority | Requirement | Acceptance signal | Status |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| REQ-001 | MUST | Change app. | app contains changed. | ACCEPTED |\n",
            encoding="utf-8",
        )
        git(self.root, "add", "app.txt", "docs", ".codex")
        git(self.root, "commit", "-qm", "base")
        self.control = ControlPlane(self.root)
        self.assertEqual(self.control.execute(Command("activate")).status, "SUCCESS")

    def ready_task(
        self, *, risk: str = "LOW", quality_mode: str = "BUILD",
        allowed: list[str] | None = None,
    ) -> None:
        created = self.control.execute(Command("task.create", {
            "id": "TASK-001", "title": "Change app", "risk": risk,
            "quality_mode": quality_mode, "requirements": ["REQ-001"],
        }))
        self.assertEqual(created.status, "SUCCESS", created)
        path = self.root / ".autodev/tasks/TASK-001/contract.json"
        contract = json.loads(path.read_text(encoding="utf-8"))
        contract.update(
            objective="Change app.txt.", change_classes=["implementation"],
            allowed_paths=allowed or ["app.txt"],
            acceptance_criteria=[{"id": "AC-001", "description": "app is changed"}],
            validation_commands=[{
                "argv": ["python3", "-c", "from pathlib import Path; assert Path('app.txt').read_text() == 'changed\\n'"],
                "cwd": ".", "timeout": 10,
            }],
            prohibited_actions=["commit"],
        )
        path.write_text(json.dumps(contract, indent=2) + "\n", encoding="utf-8")
        ready = self.control.execute(Command("task.ready", {"id": "TASK-001"}))
        self.assertEqual(ready.status, "SUCCESS", ready)

    @staticmethod
    def pass_result() -> EngineResult:
        return EngineResult("SUCCESS", {
            "outcome": "PASS", "summary": "done", "blocker": None,
            "next_action": None, "findings": [], "debt_items": [],
        })

    def test_fake_success_validates_applies_and_records_evidence(self) -> None:
        self.ready_task()
        engine = FakeCodexRunner([self.pass_result()], [{"app.txt": "changed\n"}])
        outcome = RunController(self.root, engine).run(RunRequest())
        self.assertEqual(outcome.status, "SUCCESS", outcome)
        self.assertEqual((self.root / "app.txt").read_text(), "changed\n")
        self.assertIn("self-review", engine.requests[0].prompt)
        state = json.loads((self.root / ".autodev/state.json").read_text())
        self.assertEqual(state["tasks"]["TASK-001"]["status"], "ACCEPTED")
        self.assertTrue(state["tasks"]["TASK-001"]["evidence_ids"])

    def test_explicit_until_runs_full_validation_and_derives_complete(self) -> None:
        self.ready_task()
        engine = FakeCodexRunner([self.pass_result()], [{"app.txt": "changed\n"}])
        outcome = RunController(self.root, engine).run(RunRequest(until="complete-or-blocked"))
        self.assertEqual(outcome.status, "SUCCESS", outcome)
        state = json.loads((self.root / ".autodev/state.json").read_text())
        self.assertEqual(state["project_status"], "COMPLETE")
        self.assertTrue(state["full_validation_passed"])

    def test_high_risk_uses_fresh_read_only_reviewer(self) -> None:
        self.ready_task(risk="HIGH")
        builder = FakeCodexRunner([self.pass_result()], [{"app.txt": "changed\n"}])
        reviewer = FakeCodexRunner([self.pass_result()])
        outcome = RunController(self.root, builder, reviewer_engine=reviewer).run(RunRequest())
        self.assertEqual(outcome.status, "SUCCESS", outcome)
        self.assertEqual(reviewer.requests[0].role, "reviewer")
        self.assertEqual(reviewer.requests[0].permission_profile, ":read-only")
        self.assertNotIn('"summary": "done"', reviewer.requests[0].prompt)
        self.assertIn("Order findings by severity", reviewer.requests[0].prompt)
        self.assertIn("missing evidence", reviewer.requests[0].prompt)

    def test_build_medium_uses_direct_builder_without_reviewer(self) -> None:
        self.ready_task(risk="MEDIUM")
        builder = FakeCodexRunner([self.pass_result()], [{"app.txt": "changed\n"}])
        reviewer = FakeCodexRunner([])
        outcome = RunController(
            self.root, builder, reviewer_engine=reviewer,
        ).run(RunRequest())
        self.assertEqual(outcome.status, "SUCCESS", outcome)
        self.assertEqual(len(builder.requests), 1)
        self.assertEqual(builder.requests[0].role, "builder")
        self.assertEqual(reviewer.requests, [])

    def test_run_refuses_missing_head_or_dirty_source_before_claim(self) -> None:
        self.ready_task()
        (self.root / "app.txt").write_text("user edit\n", encoding="utf-8")
        (self.root / "untracked.txt").write_text("user file\n", encoding="utf-8")
        before = (self.root / ".autodev/state.json").read_bytes()
        outcome = RunController(
            self.root, FakeCodexRunner([self.pass_result()], [{"app.txt": "changed\n"}]),
        ).run(RunRequest())
        self.assertEqual(outcome.status, "NOT_READY", outcome)
        self.assertIn("clean", outcome.message.lower())
        self.assertEqual((self.root / ".autodev/state.json").read_bytes(), before)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git(root, "init", "-q")
            self.assertEqual(initialize_project(root, "no-head").status, "SUCCESS")
            control = ControlPlane(root)
            self.assertEqual(control.execute(Command("activate")).status, "SUCCESS")
            state_before = (root / ".autodev/state.json").read_bytes()
            no_head = RunController(root, FakeCodexRunner([])).run(RunRequest())
            self.assertEqual(no_head.status, "NOT_READY", no_head)
            self.assertIn("HEAD", no_head.message)
            self.assertEqual((root / ".autodev/state.json").read_bytes(), state_before)

    def test_autodev_only_changes_are_allowed_at_baseline(self) -> None:
        self.ready_task()
        (self.root / ".autodev/local-runtime.tmp").write_text("runtime\n", encoding="utf-8")
        outcome = RunController(
            self.root, FakeCodexRunner([self.pass_result()], [{"app.txt": "changed\n"}]),
        ).run(RunRequest())
        self.assertEqual(outcome.status, "SUCCESS", outcome)

    def test_controller_requires_live_gate_before_claiming_for_codex(self) -> None:
        self.ready_task()
        before = (self.root / ".autodev/state.json").read_bytes()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AUTODEV_LIVE_CODEX", None)
            outcome = RunController(
                self.root, CodexExecEngine(str(self.root / "missing-codex")),
            ).run(RunRequest())
        self.assertEqual(outcome.status, "NOT_READY", outcome)
        self.assertEqual((self.root / ".autodev/state.json").read_bytes(), before)
        self.assertFalse(list((self.root / ".autodev/runs").glob("*/context.json")))

    def test_codex_sandbox_preflight_fails_before_claim_or_model(self) -> None:
        self.ready_task()
        before = (self.root / ".autodev/state.json").read_bytes()
        marker = self.root / ".autodev/model-started"
        executable = self.root / ".autodev/fake-codex"
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "from pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "if 'sandbox' in args:\n"
            "    print('bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted')\n"
            "    raise SystemExit(1)\n"
            f"Path({str(marker)!r}).write_text('started')\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        with mock.patch.dict(os.environ, {"AUTODEV_LIVE_CODEX": "1"}):
            outcome = RunController(self.root, CodexExecEngine(str(executable))).run(RunRequest())
        self.assertEqual(outcome.status, "INFRA_FAILURE", outcome)
        self.assertEqual(outcome.data["runtime_diagnostic"]["classification"], "nested_sandbox_restriction")
        self.assertEqual((self.root / ".autodev/state.json").read_bytes(), before)
        self.assertFalse(marker.exists())
        self.assertFalse(list((self.root / ".autodev/runs").glob("*/context.json")))

    def test_reviewer_profile_preflight_fails_before_claim_or_builder_model(self) -> None:
        self.ready_task(risk="HIGH")
        before = (self.root / ".autodev/state.json").read_bytes()
        builder_marker = self.root / ".autodev/builder-model-started"
        reviewer_marker = self.root / ".autodev/reviewer-model-started"
        builder_executable = self.root / ".autodev/fake-builder-codex"
        builder_executable.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "from pathlib import Path\n"
            "if 'sandbox' in sys.argv[1:]:\n"
            "    raise SystemExit(0)\n"
            f"Path({str(builder_marker)!r}).write_text('started')\n",
            encoding="utf-8",
        )
        builder_executable.chmod(0o755)
        reviewer_executable = self.root / ".autodev/fake-reviewer-codex"
        reviewer_executable.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "from pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "if 'sandbox' in args:\n"
            "    if 'default_permissions=\":read-only\"' in args:\n"
            "        print('Error loading configuration: reviewer profile unavailable')\n"
            "        raise SystemExit(1)\n"
            "    raise SystemExit(0)\n"
            f"Path({str(reviewer_marker)!r}).write_text('started')\n",
            encoding="utf-8",
        )
        reviewer_executable.chmod(0o755)
        with mock.patch.dict(os.environ, {"AUTODEV_LIVE_CODEX": "1"}):
            outcome = RunController(
                self.root, CodexExecEngine(str(builder_executable)),
                reviewer_engine=CodexExecEngine(str(reviewer_executable)),
            ).run(RunRequest())
        self.assertEqual(outcome.status, "INFRA_FAILURE", outcome)
        self.assertEqual(
            outcome.data["runtime_diagnostic"]["classification"],
            "codex_configuration_error",
        )
        self.assertEqual((self.root / ".autodev/state.json").read_bytes(), before)
        self.assertFalse(builder_marker.exists())
        self.assertFalse(reviewer_marker.exists())
        self.assertFalse(list((self.root / ".autodev/runs").glob("*/context.json")))

    def test_no_diff_rework_blocked_and_infrastructure_retry_route_outcomes(self) -> None:
        self.ready_task()
        no_diff = RunController(self.root, FakeCodexRunner([self.pass_result()])).run(RunRequest())
        self.assertEqual(no_diff.status, "NOT_READY")
        self.assertEqual(json.loads((self.root / ".autodev/state.json").read_text())["last_outcome"], "NO_PROGRESS")

        retry = FakeCodexRunner(
            [EngineResult("INFRA_FAILURE", infrastructure_error="temporary"), self.pass_result()],
            [None, {"app.txt": "changed\n"}],
        )
        passed = RunController(self.root, retry).run(RunRequest())
        self.assertEqual(passed.status, "SUCCESS", passed)

    def test_agent_task_failure_is_distinct_from_runtime_failure(self) -> None:
        self.ready_task()
        blocked = EngineResult("SUCCESS", {
            "outcome": "BLOCKED", "summary": "Task detail is missing",
            "blocker": "No expected format is specified",
            "next_action": "Clarify the task contract", "findings": [], "debt_items": [],
        })
        outcome = RunController(self.root, FakeCodexRunner([blocked])).run(RunRequest())
        self.assertEqual(outcome.status, "BLOCKED", outcome)
        self.assertEqual(outcome.data["failure_class"], "agent_task_failure")

    def test_stagnation_and_rework_budgets_escalate_to_blocked(self) -> None:
        self.ready_task()
        first = RunController(self.root, FakeCodexRunner([self.pass_result()])).run(RunRequest())
        self.assertEqual(first.status, "NOT_READY")
        second = RunController(self.root, FakeCodexRunner([self.pass_result()])).run(RunRequest())
        self.assertEqual(second.status, "BLOCKED", second)
        state = json.loads((self.root / ".autodev/state.json").read_text())
        self.assertEqual(state["project_status"], "BLOCKED")
        self.assertIn("stagnation", state["blocker"].lower())

    def test_pass_with_debt_gate_records_only_permitted_debt(self) -> None:
        self.ready_task()
        debt = {
            "id": "DEBT-001", "source_task": "TASK-001", "reason": "Minor cleanup",
            "severity": "LOW", "module": "app", "fix_before": "M2",
            "classification": "maintainability",
        }
        proposal = EngineResult("SUCCESS", {
            "outcome": "PASS_WITH_DEBT", "summary": "passes", "blocker": None,
            "next_action": None, "findings": [], "debt_items": [debt],
        })
        outcome = RunController(self.root, FakeCodexRunner([proposal], [{"app.txt": "changed\n"}])).run(RunRequest())
        self.assertEqual(outcome.status, "SUCCESS", outcome)
        recorded = json.loads((self.root / ".autodev/debt.json").read_text())
        self.assertEqual(recorded["items"][0]["id"], "DEBT-001")
        self.assertEqual(recorded["items"][0]["status"], "OPEN")

    def test_protected_path_and_concurrent_source_change_do_not_overwrite_user(self) -> None:
        self.ready_task(allowed=["**"])
        protected = FakeCodexRunner([self.pass_result()], [{".codex/config.toml": "bad\n"}])
        outcome = RunController(self.root, protected).run(RunRequest())
        self.assertEqual(outcome.status, "NOT_READY")
        self.assertFalse((self.root / ".codex/config.toml").exists())

        def concurrent(request: object) -> None:
            request.workspace.joinpath("app.txt").write_text("changed\n", encoding="utf-8")
            self.root.joinpath("app.txt").write_text("user\n", encoding="utf-8")

        concurrent_engine = FakeCodexRunner([self.pass_result()], [concurrent])
        paused = RunController(self.root, concurrent_engine).run(RunRequest())
        self.assertEqual(paused.status, "INFRA_FAILURE")
        self.assertEqual((self.root / "app.txt").read_text(), "user\n")
        self.assertEqual(json.loads((self.root / ".autodev/state.json").read_text())["project_status"], "PAUSED")
        self.assertTrue(list((self.root / ".autodev/runs").glob("*/checkpoint.patch")))


if __name__ == "__main__":
    unittest.main()
