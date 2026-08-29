"""Private cumulative Git checkpoints and safe campaign materialization."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from autodev._workspace import SourceFingerprint, _write_json_atomic, source_fingerprint


class CampaignWorkspaceError(RuntimeError):
    """Base class for fail-closed campaign workspace failures."""


class CheckpointConflict(CampaignWorkspaceError):
    """The private campaign ref moved concurrently."""


class MaterializationConflict(CampaignWorkspaceError):
    """The user's source no longer matches the recorded write-back source."""


@dataclass(frozen=True, slots=True)
class CheckpointResult:
    campaign_id: str
    task_id: str
    run_id: str
    base_commit: str
    commit: str
    tree: str
    journal_path: Path


@dataclass(frozen=True, slots=True)
class MaterializationResult:
    campaign_id: str
    from_commit: str
    to_commit: str
    patch_sha256: str
    applied: bool
    journal_path: Path


def _run_git(
    root: Path,
    *arguments: str,
    input_bytes: bytes | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *arguments], cwd=root, input=input_bytes, check=False,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )


def _git_value(root: Path, *arguments: str) -> str:
    result = _run_git(root, *arguments)
    if result.returncode:
        raise CampaignWorkspaceError(
            result.stderr.decode("utf-8", errors="replace").strip()
            or f"git {' '.join(arguments)} failed"
        )
    return result.stdout.decode("utf-8", errors="replace").strip()


def _fingerprint_from(value: dict[str, Any]) -> SourceFingerprint:
    return SourceFingerprint(str(value["digest"]), str(value["head"]), int(value["files"]))


class CampaignWorkspace:
    """Own one private campaign ref without moving the user's branch.

    A checkpoint is deliberately split into ref update and canonical-state
    acknowledgement.  The journal makes either crash window observable and
    recoverable instead of guessing from the worktree.
    """

    def __init__(self, project_root: Path, campaign_id: str) -> None:
        if not campaign_id.startswith("CAMP-") or not campaign_id[5:].isdigit():
            raise ValueError("campaign_id must match CAMP-NNN")
        self.root = project_root.resolve()
        self.campaign_id = campaign_id
        self.ref_name = f"refs/autodev/campaigns/{campaign_id}/current"
        self.canonical = self.root / ".autodev"
        self.campaign_dir = self.canonical / "campaigns" / campaign_id
        self.journals = self.campaign_dir / "checkpoint-journal"

    def initialize(self, base: str = "HEAD") -> str:
        """Create the campaign ref exactly once and record its source baseline."""

        commit = _git_value(self.root, "rev-parse", "--verify", f"{base}^{{commit}}")
        fingerprint = source_fingerprint(self.root)
        self.campaign_dir.mkdir(parents=True, exist_ok=True)
        baseline_path = self.campaign_dir / "workspace-baseline.json"
        if baseline_path.exists():
            recorded = json.loads(baseline_path.read_text(encoding="utf-8"))
            existing = _git_value(self.root, "rev-parse", "--verify", self.ref_name)
            if recorded.get("initial_commit") != commit and existing != commit:
                raise CheckpointConflict("campaign already has a different baseline")
            return str(recorded["initial_commit"])
        created = _run_git(self.root, "update-ref", self.ref_name, commit, "")
        if created.returncode:
            raise CheckpointConflict(
                created.stderr.decode("utf-8", errors="replace").strip()
                or "campaign ref already exists"
            )
        _write_json_atomic(
            baseline_path,
            {
                "campaign_id": self.campaign_id,
                "initial_commit": commit,
                "last_materialized_commit": commit,
                "source_fingerprint": fingerprint.to_dict(),
            },
        )
        return commit

    @property
    def current_commit(self) -> str:
        return _git_value(self.root, "rev-parse", "--verify", self.ref_name)

    def create_task_workspace(self, run_id: str) -> Path:
        """Create a detached worktree from the current cumulative checkpoint."""

        base = self.current_commit
        path = self.canonical / "workspaces" / run_id
        if path.exists():
            raise CampaignWorkspaceError(f"workspace already exists: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        result = _run_git(self.root, "worktree", "add", "--detach", str(path), base)
        if result.returncode:
            raise CampaignWorkspaceError(result.stderr.decode(errors="replace").strip())
        _write_json_atomic(
            self.campaign_dir / "workspaces" / f"{run_id}.json",
            {"campaign_id": self.campaign_id, "run_id": run_id, "base_commit": base, "path": str(path)},
        )
        return path

    def remove_task_workspace(self, path: Path) -> None:
        if not path.exists():
            return
        result = _run_git(self.root, "worktree", "remove", "--force", str(path.resolve()))
        if result.returncode:
            raise CampaignWorkspaceError(result.stderr.decode(errors="replace").strip())
        _run_git(self.root, "worktree", "prune")

    def checkpoint(self, workspace: Path, *, task_id: str, run_id: str) -> CheckpointResult:
        """Commit a Task tree and compare-and-swap the private campaign ref."""

        workspace = workspace.resolve()
        base = _git_value(workspace, "rev-parse", "--verify", "HEAD")
        added = _run_git(workspace, "add", "-A", "--", ".")
        if added.returncode:
            raise CampaignWorkspaceError(added.stderr.decode(errors="replace").strip())
        tree = _git_value(workspace, "write-tree")
        base_tree = _git_value(workspace, "rev-parse", f"{base}^{{tree}}")
        if tree == base_tree:
            raise CampaignWorkspaceError("Task produced no checkpoint changes")
        commit_env = dict(os.environ)
        commit_env.update({
            "GIT_AUTHOR_NAME": "AutoDev",
            "GIT_AUTHOR_EMAIL": "autodev@local.invalid",
            "GIT_COMMITTER_NAME": "AutoDev",
            "GIT_COMMITTER_EMAIL": "autodev@local.invalid",
        })
        committed = _run_git(
            workspace, "commit-tree", tree, "-p", base,
            input_bytes=f"AutoDev {self.campaign_id} {task_id}\n".encode(), env=commit_env,
        )
        if committed.returncode:
            raise CampaignWorkspaceError(committed.stderr.decode(errors="replace").strip())
        commit = committed.stdout.decode().strip()
        journal_path = self.journals / f"{run_id}.json"
        journal = {
            "schema_version": 1,
            "campaign_id": self.campaign_id,
            "task_id": task_id,
            "run_id": run_id,
            "base_commit": base,
            "commit": commit,
            "tree": tree,
            "phase": "PREPARED",
            "canonical_revision": None,
        }
        _write_json_atomic(journal_path, journal)
        updated = _run_git(self.root, "update-ref", self.ref_name, commit, base)
        if updated.returncode:
            journal["phase"] = "CONFLICT"
            journal["error"] = updated.stderr.decode("utf-8", errors="replace").strip()
            _write_json_atomic(journal_path, journal)
            raise CheckpointConflict(journal["error"] or "campaign ref compare-and-swap failed")
        journal["phase"] = "REF_UPDATED"
        _write_json_atomic(journal_path, journal)
        return CheckpointResult(
            self.campaign_id, task_id, run_id, base, commit, tree, journal_path,
        )

    def finalize_checkpoint(self, checkpoint: CheckpointResult, *, canonical_revision: int) -> None:
        """Acknowledge that canonical state now names the ref checkpoint."""

        journal = json.loads(checkpoint.journal_path.read_text(encoding="utf-8"))
        if journal.get("commit") != checkpoint.commit or self.current_commit != checkpoint.commit:
            raise CheckpointConflict("cannot finalize a checkpoint that is not current")
        if journal.get("phase") not in {"REF_UPDATED", "COMMITTED"}:
            raise CheckpointConflict(f"checkpoint journal is {journal.get('phase')}")
        journal["phase"] = "COMMITTED"
        journal["canonical_revision"] = canonical_revision
        _write_json_atomic(checkpoint.journal_path, journal)

    def recover_checkpoints(
        self,
        record: Callable[[dict[str, Any]], int] | None = None,
    ) -> list[dict[str, Any]]:
        """Finish provable journal states; reject divergent refs."""

        recovered: list[dict[str, Any]] = []
        if not self.journals.is_dir():
            return recovered
        for path in sorted(self.journals.glob("*.json")):
            journal = json.loads(path.read_text(encoding="utf-8"))
            if journal.get("phase") in {"COMMITTED", "CONFLICT"}:
                continue
            current = self.current_commit
            base, commit = journal.get("base_commit"), journal.get("commit")
            if current == base and journal.get("phase") == "PREPARED":
                updated = _run_git(self.root, "update-ref", self.ref_name, str(commit), str(base))
                if updated.returncode:
                    raise CheckpointConflict("checkpoint recovery lost the ref race")
                current = str(commit)
                journal["phase"] = "REF_UPDATED"
                _write_json_atomic(path, journal)
            if current != commit:
                raise CheckpointConflict(
                    f"journal {path.name} cannot be proven against current ref {current}"
                )
            if record is not None:
                journal["canonical_revision"] = record(journal)
                journal["phase"] = "COMMITTED"
                _write_json_atomic(path, journal)
            recovered.append(journal)
        return recovered

    def materialize(self, *, from_commit: str | None = None) -> MaterializationResult:
        """Apply one binary diff to the user's unchanged source worktree."""

        baseline_path = self.campaign_dir / "workspace-baseline.json"
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        source = from_commit or str(baseline["last_materialized_commit"])
        target = self.current_commit
        expected = _fingerprint_from(baseline["source_fingerprint"])
        actual = source_fingerprint(self.root)
        if actual.digest != expected.digest:
            raise MaterializationConflict(
                f"source changed: expected {expected.digest}, found {actual.digest}"
            )
        diff = _run_git(
            self.root, "diff", "--binary", "--full-index", source, target, "--",
        )
        if diff.returncode:
            raise CampaignWorkspaceError(diff.stderr.decode(errors="replace").strip())
        patch = diff.stdout
        patch_hash = hashlib.sha256(patch).hexdigest()
        journal_path = self.campaign_dir / "materialization-journal.json"
        journal = {
            "schema_version": 1,
            "campaign_id": self.campaign_id,
            "from_commit": source,
            "to_commit": target,
            "patch_sha256": patch_hash,
            "source_fingerprint": expected.to_dict(),
            "phase": "PREPARED",
        }
        _write_json_atomic(journal_path, journal)
        if patch:
            checked = _run_git(self.root, "apply", "--check", "--binary", "-", input_bytes=patch)
            if checked.returncode:
                raise MaterializationConflict(checked.stderr.decode(errors="replace").strip())
            applied = _run_git(self.root, "apply", "--binary", "-", input_bytes=patch)
            if applied.returncode:
                raise CampaignWorkspaceError(applied.stderr.decode(errors="replace").strip())
        journal["phase"] = "APPLIED"
        _write_json_atomic(journal_path, journal)
        new_fingerprint = source_fingerprint(self.root)
        baseline["last_materialized_commit"] = target
        baseline["source_fingerprint"] = new_fingerprint.to_dict()
        _write_json_atomic(baseline_path, baseline)
        journal["phase"] = "COMMITTED"
        journal["result_fingerprint"] = new_fingerprint.to_dict()
        _write_json_atomic(journal_path, journal)
        return MaterializationResult(
            self.campaign_id, source, target, patch_hash, bool(patch), journal_path,
        )

    def archive(self, *, results_materialized: bool, has_child_dependencies: bool = False) -> None:
        if not results_materialized:
            raise CampaignWorkspaceError("campaign results must be materialized before archive")
        if has_child_dependencies:
            raise CampaignWorkspaceError("campaign has child campaign dependencies")
        exists = _run_git(self.root, "show-ref", "--verify", "--quiet", self.ref_name)
        if exists.returncode == 1:
            return
        if exists.returncode:
            raise CheckpointConflict(exists.stderr.decode(errors="replace").strip())
        current = self.current_commit
        deleted = _run_git(self.root, "update-ref", "-d", self.ref_name, current)
        if deleted.returncode:
            raise CheckpointConflict(deleted.stderr.decode(errors="replace").strip())
