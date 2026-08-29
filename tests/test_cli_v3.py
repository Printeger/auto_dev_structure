from __future__ import annotations

import contextlib
import io
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

from autodev._project import initialize_project
from autodev.campaign import CampaignController, CampaignOutcome, CampaignRequest, FakePlanner
from autodev.cli import _confirm_start_approval, main


@contextlib.contextmanager
def cwd(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class V3CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.root, check=True)
        self.assertEqual(initialize_project(self.root, "v3-cli").status, "SUCCESS")
        (self.root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.root, check=True)

    def test_campaign_status_report_and_v2_migration_commands_are_routed(self) -> None:
        proposal = {
            "requirements": [{
                "id": "REQ-001", "priority": "MUST", "statement": "Work.",
                "acceptance_signal": "Pass.",
            }],
            "authority_envelope": {
                "max_task_risk": "MEDIUM", "allowed_change_classes": ["implementation"],
                "dependency_policy": "existing-only", "public_api_changes": "require-human",
                "security_changes": "require-human", "data_migration": "require-human",
                "permission_expansion": "require-human", "remote_actions": "forbidden",
            }, "phase": "SCAFFOLD", "tasks": [{
                "id": "TASK-001", "title": "Work", "objective": "Work",
                "requirements": ["REQ-001"], "dependencies": [], "priority": "MUST",
                "blocking": True, "risk": "LOW", "quality_mode": "BUILD",
                "change_classes": ["implementation"], "allowed_paths": ["app.py"],
                "out_of_scope": [],
                "acceptance_criteria": [{"id": "AC-001", "description": "Pass"}],
                "validation_commands": [{"argv": ["python3", "-c", "pass"], "cwd": ".", "timeout": 10}],
                "prohibited_actions": ["commit", "push", "publish", "deploy"],
            }], "questions": [],
        }
        controller = CampaignController(self.root, FakePlanner([proposal]))
        planned = controller.plan(CampaignRequest("Work", target="ARCHITECTURE_BASELINE"))
        self.assertEqual(controller.approve(planned.campaign_id or "", planned.data["proposal_hash"]).status, "SUCCESS")
        with cwd(self.root):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["campaign", "status", "CAMP-001"]), 0)
            self.assertIn("ACTIVE", output.getvalue())
            report = io.StringIO()
            with contextlib.redirect_stdout(report):
                self.assertEqual(main(["report", "requirements", "--campaign", "CAMP-001"]), 0)
            self.assertIn("REQ-001", report.getvalue())

    def test_campaign_live_commands_fail_before_writes_without_authorization(self) -> None:
        before = (self.root / ".autodev/state.json").read_bytes()
        with cwd(self.root), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            os.environ.pop("AUTODEV_LIVE_CODEX", None)
            self.assertEqual(main(["campaign", "plan", "--idea", "Build"]), 2)
            self.assertEqual(main(["resume", "--campaign", "CAMP-001", "--until", "target-or-blocked"]), 2)
        self.assertEqual((self.root / ".autodev/state.json").read_bytes(), before)

    def test_interactive_start_requires_one_explicit_proposal_approval(self) -> None:
        outcome = CampaignOutcome(
            "SUCCESS", "Campaign proposed", "CAMP-001",
            {"proposal_hash": "a" * 64},
        )

        class TTY(io.StringIO):
            def isatty(self) -> bool:
                return True

        with mock.patch("autodev.cli.sys.stdin", TTY("yes\n")), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(_confirm_start_approval(outcome))
        with mock.patch("autodev.cli.sys.stdin", TTY("no\n")), contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(_confirm_start_approval(outcome))
        with mock.patch("autodev.cli.sys.stdin", io.StringIO()), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(_confirm_start_approval(outcome))


if __name__ == "__main__":
    unittest.main()
