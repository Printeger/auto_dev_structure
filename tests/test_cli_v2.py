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

from autodev.cli import main
from autodev import Command, ControlPlane
from autodev._project import initialize_project


@contextlib.contextmanager
def working_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class V2CliTests(unittest.TestCase):
    def test_init_activate_status_and_stop_use_stable_codes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["init", str(target), "--name", "cli-project"]), 0)
            with working_directory(target):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["validate"]), 0)
                    self.assertEqual(main(["activate"]), 0)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main(["status", "--json"]), 0)
                self.assertEqual(json.loads(output.getvalue())["data"]["project_status"], "ACTIVE")
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["stop"]), 4)
                state = json.loads((target / ".autodev/state.json").read_text())
                self.assertEqual(state["project_status"], "STOPPED")

    def test_usage_errors_return_invalid(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["run", "--until", "forever"]), 1)

    def test_run_and_resume_require_live_gate_before_any_state_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(initialize_project(root, "gate").status, "SUCCESS")
            control = ControlPlane(root)
            self.assertEqual(control.execute(Command("activate")).status, "SUCCESS")
            stop = root / ".autodev/STOP"
            stop.write_text("STOP\n", encoding="utf-8")
            before = (root / ".autodev/state.json").read_bytes()
            with working_directory(root), mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("AUTODEV_LIVE_CODEX", None)
                for command in (["run"], ["resume"]):
                    with self.subTest(command=command), contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(main(command), 2)
            self.assertEqual((root / ".autodev/state.json").read_bytes(), before)
            self.assertTrue(stop.exists())

    def test_doctor_reports_each_live_and_git_readiness_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            self.assertEqual(initialize_project(root, "doctor").status, "SUCCESS")
            control = ControlPlane(root)
            self.assertEqual(control.execute(Command("activate")).status, "SUCCESS")
            (root / "tracked.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "tracked.txt", ".codex", "docs"], check=True)
            subprocess.run([
                "git", "-C", str(root), "-c", "user.name=Test",
                "-c", "user.email=test@example.com", "commit", "-qm", "base",
            ], check=True)
            probe = {
                "ready": True,
                "command_parse": {"ready": True, "returncode": 0},
                "login": {"ready": True, "returncode": 0},
                "sandbox_preflight": {
                    "ready": True, "classification": "ready", "returncode": 0,
                },
            }
            output = io.StringIO()
            with working_directory(root), mock.patch(
                "autodev.cli.CodexExecEngine.probe", return_value=probe,
            ), mock.patch.dict(os.environ, {"AUTODEV_LIVE_CODEX": "1"}), contextlib.redirect_stdout(output):
                self.assertEqual(main(["doctor", "--json"]), 0)
            checks = json.loads(output.getvalue())["checks"]
            self.assertTrue(checks["codex_command_parse"]["ready"])
            self.assertTrue(checks["codex_login"]["ready"])
            self.assertTrue(checks["codex_sandbox_preflight"]["ready"])
            self.assertTrue(checks["live_authorization"]["ready"])
            self.assertTrue(checks["git_head"]["ready"])
            self.assertTrue(checks["clean_baseline"]["ready"])
            self.assertTrue(checks["canonical_state"]["ready"])


if __name__ == "__main__":
    unittest.main()
