#!/usr/bin/env python3
"""Gate an installed wheel's plugin layout and exact stdio console command."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


TOOL_NAMES = {
    "inspect_project", "initialize_project", "propose_campaign", "approve_campaign",
    "campaign_status", "campaign_continue", "pause_campaign", "answer_blocker",
    "retarget_campaign", "materialize_campaign", "get_next_action", "submit_action_result",
}
TOOL_ANNOTATIONS = {
    "inspect_project": (True, False, True, False),
    "initialize_project": (False, False, True, False),
    "propose_campaign": (False, False, False, False),
    "approve_campaign": (False, False, False, False),
    "campaign_status": (True, False, True, False),
    "campaign_continue": (False, False, False, False),
    "pause_campaign": (False, False, False, False),
    "answer_blocker": (False, True, True, False),
    "retarget_campaign": (False, True, True, False),
    "materialize_campaign": (False, True, False, False),
    "get_next_action": (False, True, True, False),
    "submit_action_result": (False, True, True, False),
}


def installed_layout_smoke(repository: Path) -> dict[str, object]:
    plugin_source = repository / "plugins" / "autodev"
    marketplace_source = repository / ".agents" / "plugins" / "marketplace.json"
    with tempfile.TemporaryDirectory() as directory:
        layout = Path(directory)
        plugin = layout / "plugins" / "autodev"
        plugin.parent.mkdir(parents=True)
        shutil.copytree(plugin_source, plugin)
        marketplace_path = layout / ".agents" / "plugins" / "marketplace.json"
        marketplace_path.parent.mkdir(parents=True)
        shutil.copy2(marketplace_source, marketplace_path)
        marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
        entry = marketplace["plugins"][0]
        if entry["source"] != {"source": "local", "path": "./plugins/autodev"}:
            raise RuntimeError("temporary marketplace source is not the AutoDev plugin")
        manifest = json.loads((plugin / ".codex-plugin" / "plugin.json").read_text())
        if manifest.get("version") != "4.0.0-alpha.1":
            raise RuntimeError("temporary plugin has the wrong version")
        if "hooks" in manifest or "apps" in manifest or (plugin / ".app.json").exists():
            raise RuntimeError("temporary plugin contains an out-of-scope component")
        return json.loads((plugin / ".mcp.json").read_text(encoding="utf-8"))


async def stdio_smoke(config: dict[str, object]) -> None:
    server = config["mcpServers"]["autodev"]  # type: ignore[index]
    command = str(server["command"])  # type: ignore[index]
    arguments = [str(item) for item in server["args"]]  # type: ignore[index]
    if (command, arguments) != ("autodev-mcp", ["--stdio"]):
        raise RuntimeError("plugin MCP command does not match the installed console entry point")
    server_environment = dict(os.environ)
    server_environment["PATH"] = os.pathsep.join((
        str(Path(sys.executable).parent), server_environment.get("PATH", ""),
    ))
    executable = shutil.which(command, path=server_environment["PATH"])
    if executable is None:
        raise RuntimeError("autodev-mcp is not installed in the test environment")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        parameters = StdioServerParameters(command=command, args=arguments, env=server_environment)
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                if {tool.name for tool in listed.tools} != TOOL_NAMES:
                    raise RuntimeError("installed stdio server published the wrong tool inventory")
                for tool in listed.tools:
                    annotation = tool.annotations
                    actual = (
                        annotation.read_only_hint,
                        annotation.destructive_hint,
                        annotation.idempotent_hint,
                        annotation.open_world_hint,
                    )
                    if actual != TOOL_ANNOTATIONS[tool.name]:
                        raise RuntimeError(f"installed {tool.name} published incorrect annotations")
                initialized = await session.call_tool("initialize_project", {
                    "project_root": str(root), "name": "installed-smoke", "merge": False,
                })
                if initialized.is_error:
                    raise RuntimeError(f"installed initialize_project failed: {initialized}")
                inspected = await session.call_tool("inspect_project", {"project_root": str(root)})
                if inspected.is_error or not inspected.structured_content["data"]["initialized"]:
                    raise RuntimeError(f"installed inspect_project failed: {inspected}")
                for name in TOOL_NAMES:
                    invalid = await session.call_tool(name, {})
                    if not invalid.is_error:
                        raise RuntimeError(f"installed {name} accepted invalid arguments")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repository", type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    args = parser.parse_args()
    config = installed_layout_smoke(args.repository.resolve())
    asyncio.run(stdio_smoke(config))
    print("Installed plugin layout and autodev-mcp stdio smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
