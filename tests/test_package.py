from __future__ import annotations

import contextlib
import io
import json
import sys
import tomllib
import unittest
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "src"))

from autodev import __version__
from autodev._resources import _read_text, _resource_manifest
from autodev.cli import main
from autodev.mcp_server import main as mcp_main


class PackageFoundationTests(unittest.TestCase):
    def test_version_command_reports_prerelease(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(["version"])
        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), f"{__version__}\n")

    def test_v4_package_metadata_declares_both_entry_points_and_dependencies(self) -> None:
        metadata = tomllib.loads((SOURCE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(__version__, "4.0.0a1")
        self.assertEqual(
            metadata["project"]["dependencies"],
            ["jsonschema>=4.26,<5", "mcp>=2.1,<3"],
        )
        self.assertEqual(metadata["project"]["scripts"]["autodev"], "autodev.cli:main")
        self.assertEqual(
            metadata["project"]["scripts"]["autodev-mcp"],
            "autodev.mcp_server:main",
        )

    def test_mcp_entry_point_is_importable_and_fail_closed_until_implemented(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = mcp_main(["--version"])
        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), "4.0.0a1\n")

        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            result = mcp_main(["--stdio"])
        self.assertEqual(result, 2)
        self.assertIn("not available until the V4 MCP slice", error.getvalue())

    def test_manifest_resources_are_loadable(self) -> None:
        manifest = _resource_manifest()
        self.assertEqual(manifest["framework_version"], __version__)
        for relative in [
            *manifest["schemas"], *manifest.get("campaign_schemas", []),
            *manifest["templates"], *manifest.get("migration_manifests", [])
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

    def test_campaign_proposal_uses_codex_strict_object_shapes(self) -> None:
        schema = json.loads(_read_text("schemas/campaign-proposal.schema.json"))

        def assert_strict(value: object) -> None:
            if isinstance(value, dict):
                if value.get("type") == "object" and "properties" in value:
                    self.assertFalse(value.get("additionalProperties", True))
                    self.assertEqual(set(value.get("required", [])), set(value["properties"]))
                for child in value.values():
                    assert_strict(child)
            elif isinstance(value, list):
                for child in value:
                    assert_strict(child)

        assert_strict(schema)

        def keywords(value: object):
            if isinstance(value, dict):
                for key, child in value.items():
                    yield key
                    yield from keywords(child)
            elif isinstance(value, list):
                for child in value:
                    yield from keywords(child)

        self.assertNotIn("uniqueItems", set(keywords(schema)))


if __name__ == "__main__":
    unittest.main()
