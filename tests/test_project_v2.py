from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "src"))

from autodev import Command, ControlPlane
from autodev._project import apply_migration, check_migration, initialize_project, rollback_migration


class ProjectInstallationTests(unittest.TestCase):
    def test_init_installs_only_contracts_state_and_builder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            result = initialize_project(target, "sample")
            self.assertEqual(result.exit_code, 0, result)
            self.assertTrue((target / ".autodev/state.json").is_file())
            self.assertTrue((target / ".codex/agents/autodev-builder.toml").is_file())
            policy_text = (target / ".autodev/policy.json").read_text(encoding="utf-8")
            policy = json.loads(policy_text)
            self.assertEqual(policy["runtime"]["mode"], "codex-sandbox")
            self.assertEqual(policy["runtime"]["build_permission_profile"], ":workspace")
            self.assertEqual(policy["runtime"]["review_permission_profile"], ":read-only")
            self.assertNotIn("use_legacy_landlock", policy_text)
            self.assertFalse((target / "scripts/autodev.py").exists())
            self.assertFalse((target / "tests/test_autodev.py").exists())
            validation = ControlPlane(target).execute(Command("validate"))
            self.assertEqual(validation.status, "SUCCESS", validation)

    def test_init_conflict_preflight_and_merge_preserve_existing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            (target / "docs").mkdir(parents=True)
            requirements = target / "docs/REQUIREMENTS.md"
            requirements.write_text("keep\n", encoding="utf-8")
            blocked = initialize_project(target, "sample")
            self.assertEqual(blocked.exit_code, 2)
            self.assertFalse((target / ".autodev").exists())
            merged = initialize_project(target, "sample", merge=True)
            self.assertEqual(merged.exit_code, 2)
            self.assertEqual(requirements.read_text(encoding="utf-8"), "keep\n")
            self.assertTrue((target / ".autodev/state.json").is_file())


class MigrationTests(unittest.TestCase):
    def _legacy(self, root: Path, status: str = "ACTIVE") -> None:
        (root / ".agent/tasks").mkdir(parents=True)
        (root / ".agent/STATE.json").write_text(json.dumps({
            "project_name": "legacy", "project_status": status,
            "blocker": "choice" if status == "BLOCKED" else None,
            "next_action": "continue",
        }), encoding="utf-8")
        (root / ".agent/tasks/TASK-001.md").write_text("# TASK-001: Legacy task\n", encoding="utf-8")
        (root / ".agent/DEBT.md").write_text(
            "| ID | Severity | Module | Reason deferred | Fix before | Source Task | Status |\n"
            "| --- | --- | --- | --- | --- | --- | --- |\n"
            "| DEBT-001 | LOW | legacy | cleanup | M2 | TASK-001 | OPEN |\n",
            encoding="utf-8",
        )
        (root / "docs").mkdir()
        (root / "docs/REQUIREMENTS.md").write_text(
            "# Requirements\n\n| ID | Priority | Requirement | Acceptance signal | Status |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| REQ-001 | MUST | Legacy behavior. | A check passes. | ACCEPTED |\n",
            encoding="utf-8",
        )

    def test_check_is_read_only_apply_is_valid_and_rollback_is_guarded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._legacy(root)
            before = (root / ".agent/STATE.json").read_bytes()
            check = check_migration(root)
            self.assertEqual(check.status, "SUCCESS", check)
            self.assertEqual((root / ".agent/STATE.json").read_bytes(), before)
            self.assertFalse((root / ".autodev").exists())

            applied = apply_migration(root)
            self.assertEqual(applied.status, "SUCCESS", applied)
            migration_id = applied.data["migration_id"]
            self.assertFalse((root / ".agent").exists())
            self.assertTrue(next(root.glob(".agent.v1-frozen-*")))
            self.assertEqual(ControlPlane(root).execute(Command("validate")).status, "SUCCESS")
            self.assertTrue((root / ".autodev/tasks/TASK-001/contract.json").is_file())
            state = json.loads((root / ".autodev/state.json").read_text())
            self.assertEqual(state["campaigns"]["CAMP-001"]["mode"], "CHANGE")
            contract = json.loads((root / ".autodev/tasks/TASK-001/contract.json").read_text())
            self.assertEqual(contract["campaign_id"], "CAMP-001")
            self.assertEqual(contract["admission"], "HUMAN_APPROVED")
            self.assertEqual(json.loads((root / ".autodev/debt.json").read_text())["items"][0]["id"], "DEBT-001")

            rolled_back = rollback_migration(root, migration_id)
            self.assertEqual(rolled_back.status, "SUCCESS", rolled_back)
            self.assertTrue((root / ".agent/STATE.json").is_file())
            self.assertFalse((root / ".autodev").exists())

    def test_false_complete_becomes_blocked_and_progress_prevents_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._legacy(root, "COMPLETE")
            applied = apply_migration(root)
            self.assertEqual(applied.status, "SUCCESS", applied)
            state_path = root / ".autodev/state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["project_status"], "BLOCKED")
            state["revision"] = 1
            state_path.write_text(json.dumps(state), encoding="utf-8")
            rollback = rollback_migration(root, applied.data["migration_id"])
            self.assertEqual(rollback.status, "BLOCKED")

    def test_modified_framework_copy_blocks_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._legacy(root)
            (root / "scripts").mkdir()
            (root / "scripts/autodev.py").write_text("user modification\n", encoding="utf-8")
            report = check_migration(root)
            self.assertEqual(report.status, "BLOCKED")
            self.assertIn("scripts/autodev.py", report.data["modified_framework_conflicts"])
            self.assertFalse((root / ".autodev").exists())

    def test_git_backed_v1_migration_creates_a_runnable_private_campaign_ref(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._legacy(root)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "legacy"], cwd=root, check=True)

            applied = apply_migration(root)
            self.assertEqual(applied.status, "SUCCESS", applied)
            checkpoint = applied.data["checkpoint"]
            self.assertTrue(checkpoint)
            ref = subprocess.run(
                ["git", "rev-parse", "refs/autodev/campaigns/CAMP-001/current"],
                cwd=root, check=True, capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual(ref, checkpoint)
            state = json.loads((root / ".autodev/state.json").read_text())
            self.assertEqual(state["campaigns"]["CAMP-001"]["checkpoint"], checkpoint)
            rolled_back = rollback_migration(root, applied.data["migration_id"])
            self.assertEqual(rolled_back.status, "SUCCESS", rolled_back)
            missing = subprocess.run(
                ["git", "show-ref", "--verify", "refs/autodev/campaigns/CAMP-001/current"],
                cwd=root, capture_output=True,
            )
            self.assertNotEqual(missing.returncode, 0)


if __name__ == "__main__":
    unittest.main()
