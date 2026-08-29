#!/usr/bin/env python3
"""Run one authorized Codex BUILD + LOW smoke in a disposable Git project."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


EXAMPLE_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = EXAMPLE_ROOT.parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
sys.path.insert(0, str(SOURCE_ROOT))

from autodev import Command, ControlPlane, __version__  # noqa: E402


def _command(
    argv: list[str], *, cwd: Path, environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv, cwd=cwd, env=environment, check=False,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {argv!r}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _cli(root: Path, *arguments: str, live: bool = False) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("AUTODEV_LIVE_CODEX", None)
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = str(SOURCE_ROOT) + (os.pathsep + existing if existing else "")
    if live:
        environment["AUTODEV_LIVE_CODEX"] = "1"
    return _command([sys.executable, "-m", "autodev", *arguments], cwd=root, environment=environment)


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return _command(["git", *arguments], cwd=root)


def _sha256(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _non_runtime_changes(root: Path) -> list[str]:
    output = _git(root, "status", "--porcelain=v1", "--untracked-files=all").stdout
    paths = [line[3:] for line in output.splitlines() if len(line) >= 4]
    return sorted(path for path in paths if Path(path).parts[0] != ".autodev")


def _prepare_project(root: Path) -> None:
    shutil.copytree(EXAMPLE_ROOT / "fixture", root, dirs_exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "AutoDev smoke")
    _git(root, "config", "user.email", "autodev-smoke@example.invalid")
    _cli(root, "init", str(root), "--name", "build-low-greeting")
    policy_path = root / ".autodev/policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["runner"]["infrastructure_retries"] = 0
    policy_path.write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (root / "docs/REQUIREMENTS.md").write_text(
        "# Requirements\n\n"
        "| ID | Priority | Requirement | Acceptance signal | Status |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| REQ-001 | MUST | Build a validated greeting. | Fixed unit tests pass. | ACCEPTED |\n",
        encoding="utf-8",
    )
    _git(root, "add", ".gitignore", ".codex", "docs", "greeting.py", "test_greeting.py")
    _git(root, "commit", "-qm", "greeting smoke baseline")
    control = ControlPlane(root)
    activated = control.execute(Command("activate"))
    if activated.status != "SUCCESS":
        raise RuntimeError(f"activation failed: {activated.to_dict()}")
    created = control.execute(Command("task.create", {
        "id": "TASK-001",
        "title": "Implement build_greeting",
        "risk": "LOW",
        "quality_mode": "BUILD",
        "requirements": ["REQ-001"],
    }))
    if created.status != "SUCCESS":
        raise RuntimeError(f"task creation failed: {created.to_dict()}")
    contract_path = root / ".autodev/tasks/TASK-001/contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract.update(
        objective=(
            "Implement build_greeting(name) in greeting.py so it returns 'Hello, <name>!', "
            "trims surrounding whitespace, and raises ValueError for an all-whitespace name."
        ),
        change_classes=["implementation"],
        allowed_paths=["greeting.py"],
        out_of_scope=["Changing tests", "Adding dependencies", "Changing AutoDev state"],
        acceptance_criteria=[
            {"id": "AC-001", "description": "Ada returns Hello, Ada!"},
            {"id": "AC-002", "description": "Surrounding name whitespace is removed"},
            {"id": "AC-003", "description": "An all-whitespace name raises ValueError"},
        ],
        validation_commands=[{
            "argv": ["python3", "-m", "unittest", "-v"], "cwd": ".", "timeout": 60,
        }],
        prohibited_actions=[
            "Modify test_greeting.py", "Modify .autodev", "Commit", "Push",
            "Publish", "Use network access",
        ],
    )
    contract_path.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    ready = control.execute(Command("task.ready", {"id": "TASK-001"}))
    if ready.status != "SUCCESS":
        raise RuntimeError(f"task freeze failed: {ready.to_dict()}")


def _run(root: Path) -> dict[str, Any]:
    baseline_head = _git(root, "rev-parse", "HEAD").stdout.strip()
    codex_version = _command(["codex", "--version"], cwd=root).stdout.strip()
    run = _cli(root, "run", "--task", "TASK-001", live=True)
    state = json.loads((root / ".autodev/state.json").read_text(encoding="utf-8"))
    if state["tasks"]["TASK-001"]["status"] != "ACCEPTED":
        raise RuntimeError(f"Task was not accepted after run: {state['tasks']['TASK-001']}")
    changed_paths = _non_runtime_changes(root)
    if changed_paths != ["greeting.py"]:
        raise RuntimeError(f"unexpected changed paths: {changed_paths}")
    run_dirs = sorted(path for path in (root / ".autodev/runs").iterdir() if path.name.startswith("RUN-"))
    if len(run_dirs) != 1:
        raise RuntimeError(f"expected one run directory, found {[path.name for path in run_dirs]}")
    run_dir = run_dirs[0]
    builder_attempts = sorted(path.relative_to(run_dir).as_posix() for path in run_dir.rglob("attempt-01"))
    retries = sorted(path.relative_to(run_dir).as_posix() for path in run_dir.rglob("infra-retry-*"))
    reviewer_attempts = sorted(path.relative_to(run_dir).as_posix() for path in run_dir.rglob("review-*"))
    if len(builder_attempts) != 1 or retries or reviewer_attempts:
        raise RuntimeError(
            f"unexpected routing: builders={builder_attempts}, retries={retries}, reviewers={reviewer_attempts}"
        )
    evidence = json.loads((run_dir / "evidence.json").read_text(encoding="utf-8"))
    if evidence["review_hash"] is not None:
        raise RuntimeError("BUILD + LOW evidence unexpectedly contains a Reviewer hash")

    validation = _command([sys.executable, "-m", "unittest", "-v"], cwd=root)
    attempt_paths_before_completion = sorted(
        path.relative_to(root).as_posix()
        for pattern in ("attempt-*", "review-*")
        for path in (root / ".autodev/runs").rglob(pattern)
    )
    control = ControlPlane(root)
    full_evidence_id = f"FULL-{_sha256(validation.stdout + validation.stderr)}"
    recorded = control.execute(Command(
        "validation.record", {"passed": True, "evidence_id": full_evidence_id},
    ))
    if recorded.status != "SUCCESS":
        raise RuntimeError(f"full validation record failed: {recorded.to_dict()}")
    _cli(root, "complete")
    attempt_paths_after_completion = sorted(
        path.relative_to(root).as_posix()
        for pattern in ("attempt-*", "review-*")
        for path in (root / ".autodev/runs").rglob(pattern)
    )
    if attempt_paths_after_completion != attempt_paths_before_completion:
        raise RuntimeError("completion gate unexpectedly created a Codex attempt")
    final_state = json.loads((root / ".autodev/state.json").read_text(encoding="utf-8"))
    if final_state["project_status"] != "COMPLETE":
        raise RuntimeError(f"project did not complete: {final_state['project_status']}")

    return {
        "example": "build-low-greeting",
        "framework_version": __version__,
        "codex_version": codex_version,
        "live_authorization": "AUTODEV_LIVE_CODEX=1",
        "runtime_mode": ":workspace via Codex sandbox",
        "baseline_head": baseline_head,
        "run_exit_code": run.returncode,
        "task_status": final_state["tasks"]["TASK-001"]["status"],
        "project_status": final_state["project_status"],
        "changed_paths": changed_paths,
        "builder_attempts": len(builder_attempts),
        "reviewer_attempts": len(reviewer_attempts),
        "completion_created_attempt": False,
        "validation": {
            "argv": [sys.executable, "-m", "unittest", "-v"],
            "returncode": validation.returncode,
            "output_sha256": _sha256(validation.stdout + validation.stderr),
        },
        "evidence": {
            key: evidence.get(key)
            for key in (
                "evidence_id", "contract_hash", "proposal_hash", "diff_hash",
                "review_hash", "checkpoint_id", "validations",
            )
        },
    }


def _failure_summary(root: Path) -> dict[str, Any]:
    state_path = root / ".autodev/state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    run_dirs = sorted((root / ".autodev/runs").glob("RUN-*"))
    evidence_path = run_dirs[-1] / "evidence.json" if run_dirs else None
    evidence = (
        json.loads(evidence_path.read_text(encoding="utf-8"))
        if evidence_path is not None and evidence_path.is_file() else {}
    )
    attempts = [path for run_dir in run_dirs for path in run_dir.rglob("attempt-01")]
    retries = [path for run_dir in run_dirs for path in run_dir.rglob("infra-retry-*")]
    reviewers = [path for run_dir in run_dirs for path in run_dir.rglob("review-*")]
    task = state.get("tasks", {}).get("TASK-001", {})
    return {
        "example": "build-low-greeting",
        "framework_version": __version__,
        "codex_version": _command(["codex", "--version"], cwd=root).stdout.strip(),
        "live_authorization": "AUTODEV_LIVE_CODEX=1",
        "runtime_mode": ":workspace via Codex sandbox",
        "result": state.get("last_outcome", "SETUP_FAILURE"),
        "project_status": state.get("project_status"),
        "task_status": task.get("status"),
        "changed_paths": _non_runtime_changes(root) if (root / ".git").exists() else [],
        "builder_attempts": len(attempts) + len(retries),
        "infrastructure_retries": len(retries),
        "reviewer_attempts": len(reviewers),
        "completion_gate_run": False,
        "validation": {
            "argv": ["python3", "-m", "unittest", "-v"], "status": "NOT_RUN",
        },
        "evidence": {
            key: evidence.get(key)
            for key in (
                "evidence_id", "contract_hash", "proposal_hash", "diff_hash", "review_hash",
            )
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="keep the temporary project on success")
    arguments = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="autodev-build-low-greeting-"))
    try:
        _prepare_project(root)
        result = _run(root)
    except Exception as error:
        try:
            summary = _failure_summary(root)
            summary_path = root / "smoke-failure-summary.json"
            summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(json.dumps(summary, indent=2, sort_keys=True), file=sys.stderr)
            print(f"Sanitized failure summary: {summary_path}", file=sys.stderr)
        except Exception as summary_error:
            print(f"Could not create sanitized failure summary: {summary_error}", file=sys.stderr)
        print(f"SMOKE FAILED: {error}", file=sys.stderr)
        print(f"Preserved temporary project: {root}", file=sys.stderr)
        print(f"Attempt logs: {root / '.autodev/runs'}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    if arguments.keep:
        print(f"Kept temporary project: {root}")
    else:
        shutil.rmtree(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
