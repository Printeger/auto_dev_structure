from __future__ import annotations

import subprocess
import json
import sys
import tempfile
import unittest
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "src"))

from autodev.campaign_workspace import CampaignWorkspace, CheckpointConflict
from autodev.campaign_workspace import MaterializationConflict


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=root, capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class CampaignWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "test@example.com")
        git(self.root, "config", "user.name", "Test")
        (self.root / "value.txt").write_text("base\n", encoding="utf-8")
        git(self.root, "add", "value.txt")
        git(self.root, "commit", "-qm", "base")

    def test_tasks_accumulate_privately_then_materialize_once(self) -> None:
        campaign = CampaignWorkspace(self.root, "CAMP-001")
        baseline = campaign.initialize()

        first = campaign.create_task_workspace("RUN-001")
        (first / "value.txt").write_text("one\n", encoding="utf-8")
        checkpoint_one = campaign.checkpoint(first, task_id="TASK-001", run_id="RUN-001")
        campaign.finalize_checkpoint(checkpoint_one, canonical_revision=1)
        campaign.remove_task_workspace(first)

        second = campaign.create_task_workspace("RUN-002")
        self.assertEqual((second / "value.txt").read_text(encoding="utf-8"), "one\n")
        (second / "second.txt").write_text("two\n", encoding="utf-8")
        checkpoint_two = campaign.checkpoint(second, task_id="TASK-002", run_id="RUN-002")
        campaign.finalize_checkpoint(checkpoint_two, canonical_revision=2)
        campaign.remove_task_workspace(second)

        self.assertEqual((self.root / "value.txt").read_text(encoding="utf-8"), "base\n")
        self.assertFalse((self.root / "second.txt").exists())
        materialized = campaign.materialize(from_commit=baseline)
        self.assertTrue(materialized.applied)
        self.assertEqual((self.root / "value.txt").read_text(encoding="utf-8"), "one\n")
        self.assertEqual((self.root / "second.txt").read_text(encoding="utf-8"), "two\n")

    def test_checkpoint_compare_and_swap_fails_closed(self) -> None:
        campaign = CampaignWorkspace(self.root, "CAMP-001")
        campaign.initialize()
        worktree = campaign.create_task_workspace("RUN-001")
        (worktree / "value.txt").write_text("task\n", encoding="utf-8")
        git(self.root, "update-ref", campaign.ref_name, "HEAD")
        # Advance the ref behind the workspace's back.
        tree = git(self.root, "rev-parse", "HEAD^{tree}")
        rival = subprocess.run(
            ["git", "commit-tree", tree, "-p", "HEAD", "-m", "rival"],
            cwd=self.root, input="", capture_output=True, text=True, check=True,
        ).stdout.strip()
        git(self.root, "update-ref", campaign.ref_name, rival, git(self.root, "rev-parse", "HEAD"))

        with self.assertRaises(CheckpointConflict):
            campaign.checkpoint(worktree, task_id="TASK-001", run_id="RUN-001")

    def test_journal_recovery_finishes_only_a_provable_half_checkpoint(self) -> None:
        campaign = CampaignWorkspace(self.root, "CAMP-001")
        campaign.initialize()
        worktree = campaign.create_task_workspace("RUN-001")
        (worktree / "value.txt").write_text("task\n", encoding="utf-8")
        checkpoint = campaign.checkpoint(worktree, task_id="TASK-001", run_id="RUN-001")
        journal = json.loads(checkpoint.journal_path.read_text())
        git(self.root, "update-ref", campaign.ref_name, checkpoint.base_commit, checkpoint.commit)
        journal["phase"] = "PREPARED"
        checkpoint.journal_path.write_text(json.dumps(journal), encoding="utf-8")
        recorded: list[str] = []

        recovered = campaign.recover_checkpoints(
            lambda item: recorded.append(item["commit"]) or 7,
        )
        self.assertEqual(recorded, [checkpoint.commit])
        self.assertEqual(campaign.current_commit, checkpoint.commit)
        self.assertEqual(recovered[0]["canonical_revision"], 7)
        self.assertEqual(json.loads(checkpoint.journal_path.read_text())["phase"], "COMMITTED")

    def test_materialization_refuses_a_concurrent_user_edit(self) -> None:
        campaign = CampaignWorkspace(self.root, "CAMP-001")
        baseline = campaign.initialize()
        worktree = campaign.create_task_workspace("RUN-001")
        (worktree / "value.txt").write_text("task\n", encoding="utf-8")
        checkpoint = campaign.checkpoint(worktree, task_id="TASK-001", run_id="RUN-001")
        campaign.finalize_checkpoint(checkpoint, canonical_revision=1)
        campaign.remove_task_workspace(worktree)
        (self.root / "value.txt").write_text("user\n", encoding="utf-8")
        with self.assertRaises(MaterializationConflict):
            campaign.materialize(from_commit=baseline)
        self.assertEqual((self.root / "value.txt").read_text(), "user\n")


if __name__ == "__main__":
    unittest.main()
