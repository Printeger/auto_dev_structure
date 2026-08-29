from __future__ import annotations

import hashlib
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

from autodev._project import (
    apply_v3_migration,
    check_v3_migration,
    initialize_project,
    rollback_v3_migration,
)
from autodev.action import ActionController
from autodev.campaign import CampaignController, CampaignRequest, FakePlanner


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(["git", *arguments], cwd=root, capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def digest_tree(path: Path) -> dict[str, str]:
    return {
        str(item.relative_to(path)): hashlib.sha256(item.read_bytes()).hexdigest()
        for item in sorted(path.rglob("*"))
        if item.is_file() and item.name != ".control-plane.lock"
    }


class V4MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "test@example.com")
        git(self.root, "config", "user.name", "Test")
        self.assertEqual(initialize_project(self.root, "v3-fixture").status, "SUCCESS")
        (self.root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "base")
        task = {
            "id": "TASK-001", "title": "Change", "objective": "Change app.py.",
            "requirements": ["REQ-001"], "dependencies": [], "priority": "MUST",
            "blocking": True, "risk": "MEDIUM", "quality_mode": "BUILD",
            "change_classes": ["implementation"], "allowed_paths": ["app.py"],
            "out_of_scope": [],
            "acceptance_criteria": [{"id": "AC-001", "description": "Value changes."}],
            "validation_commands": [{"argv": ["python3", "-c", "pass"], "cwd": ".", "timeout": 20}],
            "prohibited_actions": ["commit", "push", "publish", "deploy"],
        }
        proposal = {
            "requirements": [{"id": "REQ-001", "priority": "MUST", "statement": "Change.", "acceptance_signal": "Pass."}],
            "authority_envelope": {
                "max_task_risk": "MEDIUM", "allowed_change_classes": ["implementation"],
                "dependency_policy": "existing-only", "public_api_changes": "require-human",
                "security_changes": "require-human", "data_migration": "require-human",
                "permission_expansion": "require-human", "remote_actions": "forbidden",
            },
            "phase": "SCAFFOLD", "tasks": [task], "questions": [],
        }
        controller = CampaignController(self.root, FakePlanner([proposal]))
        planned = controller.plan(CampaignRequest("Migrate", target="ARCHITECTURE_BASELINE"))
        self.assertEqual(controller.approve("CAMP-001", planned.data["proposal_hash"]).status, "SUCCESS")
        evidence = self.root / ".autodev/runs/V3-RUN/evidence.json"
        evidence.parent.mkdir(parents=True)
        evidence.write_text('{"evidence_id":"V3-EVIDENCE"}\n', encoding="utf-8")
        state_path = self.root / ".autodev/state.json"
        state = json.loads(state_path.read_text())
        state.pop("current_action_id")
        state.pop("pause_requested")
        state["framework_version"] = "3.0.0a1"
        state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        manifest_path = self.root / ".autodev/manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["framework_version"] = "3.0.0a1"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def test_check_apply_and_rollback_preserve_v3_assets_and_refs(self) -> None:
        canonical = self.root / ".autodev"
        before_check = digest_tree(canonical)
        refs = git(self.root, "for-each-ref", "--format=%(refname) %(objectname)", "refs/autodev/campaigns")

        checked = check_v3_migration(self.root)

        self.assertEqual(checked.status, "SUCCESS", checked)
        self.assertEqual(digest_tree(canonical), before_check)
        applied = apply_v3_migration(self.root)
        self.assertEqual(applied.status, "SUCCESS", applied)
        state = json.loads((canonical / "state.json").read_text())
        self.assertIsNone(state["current_action_id"])
        self.assertFalse(state["pause_requested"])
        self.assertEqual(git(self.root, "for-each-ref", "--format=%(refname) %(objectname)", "refs/autodev/campaigns"), refs)
        for relative, expected in checked.data["asset_hashes"].items():
            self.assertEqual(hashlib.sha256((canonical / relative).read_bytes()).hexdigest(), expected)

        rolled_back = rollback_v3_migration(self.root, applied.data["migration_id"])

        self.assertEqual(rolled_back.status, "SUCCESS", rolled_back)
        restored = json.loads((canonical / "state.json").read_text())
        self.assertNotIn("current_action_id", restored)
        self.assertEqual(restored["framework_version"], "3.0.0a1")
        self.assertEqual(git(self.root, "for-each-ref", "--format=%(refname) %(objectname)", "refs/autodev/campaigns"), refs)

    def test_first_v4_action_permanently_blocks_rollback_and_cli_supports_check(self) -> None:
        applied = apply_v3_migration(self.root)
        self.assertEqual(applied.status, "SUCCESS", applied)
        action = ActionController(self.root).get_next_action("CAMP-001")
        self.assertEqual(action.status, "SUCCESS", action)

        refused = rollback_v3_migration(self.root, applied.data["migration_id"])

        self.assertEqual(refused.status, "BLOCKED", refused)
        self.assertIn("first V4 Action", refused.message)
        environment = dict(os.environ, PYTHONPATH=str(SOURCE_ROOT / "src"))
        process = subprocess.run(
            [sys.executable, "-m", "autodev", "migrate", "v3", "--check"],
            cwd=self.root, env=environment, capture_output=True, text=True,
        )
        self.assertEqual(process.returncode, 1)
        self.assertIn("not applicable", process.stdout)

    def test_check_and_apply_preserve_dirty_source_worktree(self) -> None:
        (self.root / "app.py").write_text("VALUE = 'dirty user edit'\n", encoding="utf-8")
        (self.root / "user-note.txt").write_text("untracked user data\n", encoding="utf-8")
        source_before = {
            "app.py": (self.root / "app.py").read_bytes(),
            "user-note.txt": (self.root / "user-note.txt").read_bytes(),
        }
        status_before = git(self.root, "status", "--short", "--", "app.py", "user-note.txt")

        checked = check_v3_migration(self.root)
        applied = apply_v3_migration(self.root)

        self.assertEqual(checked.status, "SUCCESS", checked)
        self.assertEqual(applied.status, "SUCCESS", applied)
        self.assertEqual(
            {name: (self.root / name).read_bytes() for name in source_before}, source_before,
        )
        self.assertEqual(
            git(self.root, "status", "--short", "--", "app.py", "user-note.txt"),
            status_before,
        )

    def test_atomic_swap_fault_preserves_v3_and_apply_retry_succeeds(self) -> None:
        canonical = self.root / ".autodev"
        before = digest_tree(canonical)
        from autodev import _project

        original_replace = _project.os.replace
        failed_once = False

        def fail_staging_install(source: object, destination: object) -> None:
            nonlocal failed_once
            source_path = Path(source)
            destination_path = Path(destination)
            if (
                not failed_once
                and source_path.name.startswith(".autodev.v4-staging-")
                and destination_path == canonical
            ):
                failed_once = True
                raise OSError("injected V4 staging install fault")
            original_replace(source, destination)

        with mock.patch.object(_project.os, "replace", side_effect=fail_staging_install):
            interrupted = apply_v3_migration(self.root)

        self.assertEqual(interrupted.status, "INFRA_FAILURE", interrupted)
        self.assertEqual(digest_tree(canonical), before)
        self.assertEqual(check_v3_migration(self.root).status, "SUCCESS")

        retried = apply_v3_migration(self.root)

        self.assertEqual(retried.status, "SUCCESS", retried)
        state = json.loads((canonical / "state.json").read_text())
        self.assertIsNone(state["current_action_id"])
        self.assertFalse(state["pause_requested"])


if __name__ == "__main__":
    unittest.main()
