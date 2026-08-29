from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "src"))

from autodev._workspace import (
    ConcurrentSourceChange,
    GitWorkspace,
    LockUnavailable,
    PatchPolicyViolation,
    ProjectLock,
    recover_stale_workspaces,
)


def git(root: Path, *args: str) -> None:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    if result.returncode:
        raise AssertionError(result.stderr)


class WorkspaceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "test@example.com")
        git(self.root, "config", "user.name", "Test")
        (self.root / "app.txt").write_text("base\n", encoding="utf-8")
        git(self.root, "add", "app.txt")
        git(self.root, "commit", "-qm", "base")
        for name in ("locks", "workspaces", "runs"):
            (self.root / ".autodev" / name).mkdir(parents=True, exist_ok=True)

    def test_lock_refuses_live_owner_and_recovers_same_host_dead_pid(self) -> None:
        first = ProjectLock(self.root)
        owner = first.acquire()
        with self.assertRaises(LockUnavailable):
            ProjectLock(self.root).acquire(recover_stale=True)
        first.heartbeat()
        self.assertEqual(json.loads((first.path / "owner.json").read_text())["owner_id"], owner["owner_id"])
        first.release()

        stale = ProjectLock(self.root)
        stale.path.mkdir()
        (stale.path / "owner.json").write_text(json.dumps({
            "owner_id": "dead", "hostname": socket.gethostname(), "pid": 2_000_000_000
        }), encoding="utf-8")
        recovered = ProjectLock(self.root)
        recovered.acquire(recover_stale=True)
        self.assertTrue(any(path.name.startswith("lock-recovery-") for path in (self.root / ".autodev/runs").iterdir()))
        recovered.release()

    def test_binary_patch_checkpoint_applies_and_cleans_up(self) -> None:
        workspace = GitWorkspace(self.root, "RUN-001")
        worktree = workspace.create()
        (worktree / "app.txt").write_text("changed\n", encoding="utf-8")
        (worktree / "asset.bin").write_bytes(b"\x00\xff\x10")
        patch, paths = workspace.collect_patch(allowed_paths=("app.txt", "asset.bin"))
        self.assertEqual(paths, ["app.txt", "asset.bin"])
        self.assertIn(b"GIT binary patch", patch)
        checkpoint = workspace.checkpoint(patch, paths)
        self.assertTrue(checkpoint.is_file())
        workspace.cleanup()
        workspace.apply(patch)
        self.assertEqual((self.root / "app.txt").read_text(), "changed\n")
        self.assertEqual((self.root / "asset.bin").read_bytes(), b"\x00\xff\x10")

    def test_path_policy_and_concurrent_source_change_fail_closed(self) -> None:
        workspace = GitWorkspace(self.root, "RUN-002")
        worktree = workspace.create()
        (worktree / "outside.txt").write_text("no\n", encoding="utf-8")
        with self.assertRaises(PatchPolicyViolation):
            workspace.collect_patch(allowed_paths=("app.txt",))
        (worktree / "outside.txt").unlink()
        (worktree / "app.txt").write_text("task\n", encoding="utf-8")
        patch, _ = workspace.collect_patch(allowed_paths=("app.txt",))
        (self.root / "app.txt").write_text("user\n", encoding="utf-8")
        with self.assertRaises(ConcurrentSourceChange):
            workspace.apply(patch)
        workspace.cleanup()
        self.assertEqual((self.root / "app.txt").read_text(), "user\n")

    def test_stale_recovery_preserves_patch_before_removing_worktree(self) -> None:
        workspace = GitWorkspace(self.root, "RUN-STALE")
        worktree = workspace.create()
        (worktree / "app.txt").write_text("recover me\n", encoding="utf-8")
        recovered = recover_stale_workspaces(self.root)
        self.assertTrue(recovered[0]["removed"])
        self.assertFalse(worktree.exists())
        patch = self.root / ".autodev/runs/RUN-STALE/recovered.patch"
        self.assertIn(b"recover me", patch.read_bytes())


if __name__ == "__main__":
    unittest.main()
