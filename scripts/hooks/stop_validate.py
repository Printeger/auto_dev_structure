#!/usr/bin/env python3
"""Optional Codex Stop hook: request one state repair when validation fails."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from autodev import validate_project  # noqa: E402


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        _emit({"continue": True, "suppressOutput": True})
        return 0

    if not isinstance(event, dict) or event.get("stop_hook_active") is True:
        _emit({"continue": True, "suppressOutput": True})
        return 0

    errors, _, _ = validate_project(ROOT, ready=False)
    if errors:
        preview = "; ".join(errors[:3])
        if len(errors) > 3:
            preview += f"; and {len(errors) - 3} more"
        _emit(
            {
                "decision": "block",
                "reason": (
                    "Project state is invalid. Fix the state contract once, run "
                    f"`python3 scripts/autodev.py validate`, and then stop again. Errors: {preview}"
                ),
                "suppressOutput": False,
            }
        )
        return 0

    _emit({"continue": True, "suppressOutput": True})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
