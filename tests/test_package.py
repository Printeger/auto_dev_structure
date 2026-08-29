from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "src"))

from autodev import __version__
from autodev._resources import _read_text, _resource_manifest
from autodev.cli import main


class PackageFoundationTests(unittest.TestCase):
    def test_version_command_reports_prerelease(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(["version"])
        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), f"{__version__}\n")

    def test_manifest_resources_are_loadable(self) -> None:
        manifest = _resource_manifest()
        self.assertEqual(manifest["framework_version"], __version__)
        for relative in [
            *manifest["schemas"], *manifest["templates"], *manifest.get("migration_manifests", [])
        ]:
            self.assertTrue(_read_text(relative))

        builder = _read_text("templates/.codex/agents/autodev-builder.toml")
        self.assertIn('name = "autodev-builder"', builder)

    def test_resource_manifest_is_valid_json(self) -> None:
        raw = _read_text("resource-manifest.json")
        self.assertEqual(json.loads(raw), _resource_manifest())

    def test_resource_paths_cannot_escape_package(self) -> None:
        with self.assertRaises(ValueError):
            _read_text("../__init__.py")

    def test_attempt_proposal_uses_codex_strict_object_shapes(self) -> None:
        schema = json.loads(_read_text("schemas/attempt-proposal.schema.json"))
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        debt_item = schema["properties"]["debt_items"]["items"]
        self.assertFalse(debt_item["additionalProperties"])
        self.assertEqual(set(debt_item["required"]), set(debt_item["properties"]))


if __name__ == "__main__":
    unittest.main()
