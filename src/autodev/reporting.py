"""Read-only Markdown views derived from canonical Campaign JSON and evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _state(root: Path) -> dict[str, Any]:
    return json.loads((root / ".autodev/state.json").read_text(encoding="utf-8"))


def _campaign_id(root: Path, requested: str | None) -> str:
    state = _state(root)
    campaign_id = requested or state.get("current_campaign_id")
    if campaign_id is None:
        reached = [
            item for item, record in state.get("campaigns", {}).items()
            if record["status"] in {"TARGET_REACHED", "ARCHIVED"}
        ]
        campaign_id = max(reached) if reached else None
    if campaign_id not in state.get("campaigns", {}):
        raise ValueError("no matching Campaign")
    return campaign_id


def render_report(root: Path, kind: str, *, campaign_id: str | None = None) -> str:
    root = root.resolve()
    selected = _campaign_id(root, campaign_id)
    directory = root / ".autodev" / "campaigns" / selected
    state = _state(root)
    record = state["campaigns"][selected]
    contract = json.loads((directory / "campaign.json").read_text(encoding="utf-8"))
    requirements = json.loads((directory / "requirements.json").read_text(encoding="utf-8"))
    if kind == "requirements":
        lines = [f"# Requirements — {selected}", "", "| ID | Priority | Requirement | Acceptance signal |", "| --- | --- | --- | --- |"]
        lines.extend(
            f"| {item['id']} | {item['priority']} | {item['statement']} | {item['acceptance_signal']} |"
            for item in requirements["requirements"]
        )
        return "\n".join(lines) + "\n"
    summaries = []
    for path in sorted(directory.glob("phase-summary-*.json")):
        summaries.append(json.loads(path.read_text(encoding="utf-8")))
    if kind == "phase":
        lines = [f"# Phase Report — {selected}", "", f"Current status: `{record['status']}`", f"Current phase: `{record['phase']}`", ""]
        for summary in summaries:
            lines.extend([
                f"## {summary['phase']}", "", f"Status: `{summary['status']}`",
                f"Checkpoint: `{summary.get('checkpoint')}`", "",
            ])
        return "\n".join(lines) + "\n"
    if kind == "release":
        evidence = []
        for path in sorted((root / ".autodev/runs").glob("*/evidence.json")):
            item = json.loads(path.read_text(encoding="utf-8"))
            task_path = root / ".autodev/tasks" / str(item.get("task_id")) / "contract.json"
            if task_path.is_file() and json.loads(task_path.read_text()).get("campaign_id") == selected:
                evidence.append(item)
        passed = sum(item.get("outcome") in {"PASS", "PASS_WITH_DEBT"} for item in evidence)
        return (
            f"# Release Report — {selected}\n\n"
            f"Idea: {contract['idea']}\n\n"
            f"Target: `{contract['target']}`\n\n"
            f"Status: `{record['status']}`\n\n"
            f"Accepted attempts: {passed}\n\n"
            f"Private checkpoint: `{record['checkpoint']}`\n"
        )
    raise ValueError("report kind must be phase, requirements, or release")
