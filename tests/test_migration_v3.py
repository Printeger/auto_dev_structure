from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "src"))

from autodev._project import (
    apply_v2_migration, check_v2_migration, initialize_project, rollback_v2_migration,
)
from autodev._workspace import git_baseline_status, source_fingerprint
from autodev.control_plane import Command, ControlPlane


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(["git", *arguments], cwd=root, capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class V3MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "test@example.com")
        git(self.root, "config", "user.name", "Test")
        self.assertEqual(initialize_project(self.root, "v2").status, "SUCCESS")
        (self.root / "app.txt").write_text("base\n", encoding="utf-8")
        git(self.root, "add", "app.txt", "docs", ".codex")
        git(self.root, "commit", "-qm", "base")

    def test_clean_migration_is_valid_and_rolls_back_before_progress(self) -> None:
        checked = check_v2_migration(self.root)
        self.assertEqual(checked.status, "SUCCESS", checked)
        self.assertIsNone(checked.data["adopt_source_fingerprint"])
        applied = apply_v2_migration(self.root)
        self.assertEqual(applied.status, "SUCCESS", applied)
        self.assertEqual(ControlPlane(self.root).execute(Command("validate")).status, "SUCCESS")
        self.assertTrue((self.root / ".autodev/campaigns/CAMP-001/requirements.json").is_file())
        rolled_back = rollback_v2_migration(self.root, applied.data["migration_id"])
        self.assertEqual(rolled_back.status, "SUCCESS", rolled_back)
        self.assertFalse((self.root / ".autodev/campaigns/CAMP-001").exists())

    def test_dirty_migration_requires_exact_fingerprint_and_adopts_source(self) -> None:
        (self.root / "app.txt").write_text("accepted but uncommitted\n", encoding="utf-8")
        (self.root / "new.txt").write_text("new\n", encoding="utf-8")
        checked = check_v2_migration(self.root)
        fingerprint = checked.data["adopt_source_fingerprint"]
        refused = apply_v2_migration(self.root)
        self.assertEqual(refused.status, "BLOCKED", refused)
        applied = apply_v2_migration(self.root, adopt_source=fingerprint)
        self.assertEqual(applied.status, "SUCCESS", applied)
        checkpoint = applied.data["checkpoint"]
        self.assertEqual(git(self.root, "show", f"{checkpoint}:app.txt"), "accepted but uncommitted")
        self.assertEqual(git(self.root, "show", f"{checkpoint}:new.txt"), "new")

    def test_frozen_migration_backup_is_not_part_of_source_identity(self) -> None:
        before = source_fingerprint(self.root)
        backup = self.root / ".autodev.v2-frozen-example"
        backup.mkdir()
        (backup / "state.json").write_text("{}\n", encoding="utf-8")
        after = source_fingerprint(self.root)
        self.assertEqual(after, before)
        self.assertTrue(git_baseline_status(self.root)["ready"])


if __name__ == "__main__":
    unittest.main()
