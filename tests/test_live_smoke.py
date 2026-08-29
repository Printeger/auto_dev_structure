from __future__ import annotations

import importlib.util
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
SCRIPT_PATH = SOURCE_ROOT / "examples/build-low-greeting/run_live_smoke.py"
SPEC = importlib.util.spec_from_file_location("build_low_greeting_smoke", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
SMOKE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SMOKE)


class LiveSmokeBoundaryTests(unittest.TestCase):
    def test_only_live_authorization_is_added_to_the_model_child(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            SMOKE, "_command", return_value=completed,
        ) as command:
            SMOKE._cli(Path(directory), "status")
            non_live_environment = command.call_args.kwargs["environment"]
            self.assertIsNone(non_live_environment.get("AUTODEV_LIVE_CODEX"))

            SMOKE._cli(Path(directory), "status", live=True)
            live_environment = command.call_args.kwargs["environment"]
            self.assertEqual(live_environment["AUTODEV_LIVE_CODEX"], "1")
            self.assertFalse(any("LANDLOCK" in key for key in live_environment))

    def test_fixture_disables_infrastructure_process_retries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            SMOKE._prepare_project(root)
            policy = json.loads((root / ".autodev/policy.json").read_text(encoding="utf-8"))
            self.assertEqual(policy["runner"]["infrastructure_retries"], 0)


if __name__ == "__main__":
    unittest.main()
