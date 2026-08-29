"""Internal access to immutable resources shipped with AutoDev."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import PurePosixPath
from typing import Any


def _parts(relative_path: str) -> tuple[str, ...]:
    path = PurePosixPath(relative_path)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"resource path must be a normalized relative path: {relative_path!r}")
    return path.parts


def _read_text(relative_path: str) -> str:
    """Return a UTF-8 package resource by its path below ``resources/``."""

    resource = files("autodev").joinpath("resources", *_parts(relative_path))
    return resource.read_text(encoding="utf-8")


def _resource_manifest() -> dict[str, Any]:
    """Return the packaged internal resource catalog."""

    manifest = json.loads(_read_text("resource-manifest.json"))
    if not isinstance(manifest, dict):
        raise ValueError("packaged resource manifest must be a JSON object")
    return manifest
