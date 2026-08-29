"""Quality routing and debt acceptance rules used by the run controller."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


_REVIEW_CHANGE_CLASSES = {
    "architecture", "public-interface", "security", "migration",
    "shared-schema", "shared-data", "milestone-integration",
}
_BLOCKING_DEBT_CLASSES = {"security", "data-loss", "acceptance-failure", "public-interface"}


def requires_independent_review(contract: Mapping[str, Any], *, rework_count: int = 0) -> bool:
    return (
        contract.get("risk") == "HIGH"
        or contract.get("quality_mode") == "HARDENING"
        or bool(_REVIEW_CHANGE_CLASSES.intersection(contract.get("change_classes", [])))
        or rework_count >= 2
    )


def validate_debt(
    contract: Mapping[str, Any], debt_items: Sequence[Mapping[str, Any]]
) -> list[str]:
    errors: list[str] = []
    if contract.get("risk") == "HIGH" or contract.get("quality_mode") == "HARDENING":
        errors.append("PASS_WITH_DEBT is limited to LOW/MEDIUM non-HARDENING Tasks")
    if not debt_items:
        errors.append("PASS_WITH_DEBT requires at least one debt item")
    required = {"id", "source_task", "reason", "severity", "module", "fix_before", "classification"}
    seen: set[str] = set()
    for index, item in enumerate(debt_items):
        missing = sorted(required - set(item))
        if missing:
            errors.append(f"debt[{index}] missing: {', '.join(missing)}")
            continue
        debt_id = item["id"]
        if not isinstance(debt_id, str) or not debt_id.strip() or debt_id in seen:
            errors.append(f"debt[{index}] id is empty or duplicated")
        seen.add(str(debt_id))
        if item["source_task"] != contract.get("id"):
            errors.append(f"debt[{index}] source_task mismatch")
        if item["severity"] not in {"LOW", "MEDIUM"}:
            errors.append(f"debt[{index}] severity is blocking")
        if item["classification"] in _BLOCKING_DEBT_CLASSES:
            errors.append(f"debt[{index}] classification cannot be deferred")
        for field in ("reason", "module", "fix_before"):
            if not isinstance(item[field], str) or not item[field].strip():
                errors.append(f"debt[{index}] {field} must be non-empty")
    return errors
