from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "src"))

from autodev._project import initialize_project
from autodev.campaign import CampaignController, CampaignRequest


TOOL_NAMES = {
    "inspect_project",
    "initialize_project",
    "propose_campaign",
    "approve_campaign",
    "campaign_status",
    "campaign_continue",
    "pause_campaign",
    "answer_blocker",
    "retarget_campaign",
    "materialize_campaign",
    "get_next_action",
    "submit_action_result",
}


def git(root: Path, *arguments: str) -> None:
    process = subprocess.run(
        ["git", *arguments], cwd=root, text=True, capture_output=True, check=False,
    )
    if process.returncode:
        raise AssertionError(process.stderr)


def task(*, task_id: str = "TASK-001", risk: str = "MEDIUM") -> dict[str, object]:
    return {
        "id": task_id,
        "title": "Implement the requirement",
        "objective": "Change app.py.",
        "requirements": ["REQ-001"],
        "dependencies": [],
        "priority": "MUST",
        "blocking": True,
        "risk": risk,
        "quality_mode": "BUILD",
        "change_classes": ["implementation"],
        "allowed_paths": ["app.py"],
        "out_of_scope": [],
        "acceptance_criteria": [{"id": "AC-001", "description": "The behavior works."}],
        "validation_commands": [
            {"argv": ["python3", "-c", "print('ok')"], "cwd": ".", "timeout": 60},
        ],
        "prohibited_actions": ["commit", "push", "publish", "deploy"],
    }


def proposal(*, risk: str = "MEDIUM", phase: str = "IMPLEMENT") -> dict[str, object]:
    return {
        "requirements": [{
            "id": "REQ-001",
            "priority": "MUST",
            "statement": "Provide behavior.",
            "acceptance_signal": "Tests pass.",
        }],
        "authority_envelope": {
            "max_task_risk": "MEDIUM",
            "allowed_change_classes": [
                "implementation", "test", "documentation", "architecture",
                "internal-interface", "shared-internal-data",
            ],
            "dependency_policy": "existing-only",
            "public_api_changes": "require-human",
            "security_changes": "require-human",
            "data_migration": "require-human",
            "permission_expansion": "require-human",
            "remote_actions": "forbidden",
        },
        "phase": phase,
        "tasks": [task(risk=risk)],
        "questions": [],
    }


class StructuredProposalCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "test@example.com")
        git(self.root, "config", "user.name", "Test")
        self.assertEqual(initialize_project(self.root, "mcp-test").status, "SUCCESS")
        (self.root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "base")

    def test_structured_proposal_needs_no_planner(self) -> None:
        controller = CampaignController(self.root)
        outcome = controller.propose_structured(
            CampaignRequest("Change the behavior", mode="CHANGE", target="CHANGE_COMPLETE"),
            proposal(),
        )

        self.assertEqual(outcome.status, "SUCCESS", outcome)
        self.assertEqual(outcome.campaign_id, "CAMP-001")
        self.assertFalse(hasattr(controller, "_mcp_planner"))


try:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
except ImportError:
    ClientSession = None  # type: ignore[assignment]


@unittest.skipIf(ClientSession is None, "mcp>=2.1 is not installed in this interpreter")
class MCPStdioTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "test@example.com")
        git(self.root, "config", "user.name", "Test")
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        self.codex_sentinel = self.root / "codex-was-started"
        fake_codex = fake_bin / "codex"
        fake_codex.write_text(
            "#!/usr/bin/env python3\n"
            "from pathlib import Path\n"
            f"Path({str(self.codex_sentinel)!r}).write_text('started')\n",
            encoding="utf-8",
        )
        fake_codex.chmod(0o755)
        env = {
            "PYTHONPATH": str(SOURCE_ROOT / "src"),
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        }
        self.server_parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "autodev.mcp_server", "--stdio"],
            env=env,
        )

    @asynccontextmanager
    async def session(self):
        async with stdio_client(self.server_parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    async def test_tool_list_is_exact_strict_and_annotated(self) -> None:
        async with self.session() as session:
            listed = await session.list_tools()
            for name in TOOL_NAMES:
                invalid = await session.call_tool(name, {})
                self.assertTrue(invalid.is_error, name)
        tools = {item.name: item for item in listed.tools}
        self.assertEqual(set(tools), TOOL_NAMES)
        for item in tools.values():
            self.assertIn("project_root", item.input_schema["required"])
            self.assertFalse(item.input_schema["additionalProperties"])
            self.assertFalse(item.output_schema["additionalProperties"])
            self.assertIsNotNone(item.annotations)
            self.assertFalse(item.annotations.open_world_hint)
        self.assertTrue(tools["inspect_project"].annotations.read_only_hint)
        self.assertTrue(tools["campaign_status"].annotations.read_only_hint)
        self.assertFalse(tools["get_next_action"].annotations.read_only_hint)
        self.assertTrue(tools["materialize_campaign"].annotations.destructive_hint)

    async def test_initialize_inspect_propose_approve_and_security_errors(self) -> None:
        async with self.session() as session:
            initialized = await session.call_tool("initialize_project", {
                "project_root": str(self.root), "name": "mcp-flow", "merge": False,
            })
            self.assertFalse(initialized.is_error, initialized)
            git(self.root, "add", ".")
            git(self.root, "commit", "-qm", "base")

            inspected = await session.call_tool("inspect_project", {
                "project_root": str(self.root),
            })
            self.assertFalse(inspected.is_error, inspected)
            self.assertTrue(inspected.structured_content["data"]["initialized"])

            proposed = await session.call_tool("propose_campaign", {
                "project_root": str(self.root),
                "idea": "Change the behavior",
                "development_strategy": "CHANGE",
                "target": "CHANGE_COMPLETE",
                "autonomy": "HUMAN_ON_BLOCKED",
                "proposal": proposal(),
            })
            self.assertFalse(proposed.is_error, proposed)
            campaign_id = proposed.structured_content["campaign_id"]
            proposal_hash = proposed.structured_content["data"]["proposal_hash"]

            unconfirmed = await session.call_tool("approve_campaign", {
                "project_root": str(self.root),
                "campaign_id": campaign_id,
                "proposal_hash": proposal_hash,
                "proposal_and_authority_confirmed": False,
            })
            self.assertTrue(unconfirmed.is_error)
            confirmed = await session.call_tool("approve_campaign", {
                "project_root": str(self.root),
                "campaign_id": campaign_id,
                "proposal_hash": proposal_hash,
                "proposal_and_authority_confirmed": True,
            })
            self.assertFalse(confirmed.is_error, confirmed)

            injected = await session.call_tool("inspect_project", {
                "project_root": f"{self.root}; touch /tmp/autodev-injected",
            })
            self.assertTrue(injected.is_error)
            extra = await session.call_tool("inspect_project", {
                "project_root": str(self.root), "unexpected": True,
            })
            self.assertTrue(extra.is_error)
            self.assertFalse(self.codex_sentinel.exists())

    async def test_pause_resume_and_action_progression(self) -> None:
        async with self.session() as session:
            await session.call_tool("initialize_project", {
                "project_root": str(self.root), "name": "mcp-flow", "merge": False,
            })
            (self.root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            git(self.root, "add", ".")
            git(self.root, "commit", "-qm", "base")
            proposed = await session.call_tool("propose_campaign", {
                "project_root": str(self.root), "idea": "Change", "development_strategy": "STAGED",
                "target": "ARCHITECTURE_BASELINE", "autonomy": "HUMAN_ON_BLOCKED",
                "proposal": proposal(phase="SCAFFOLD"),
            })
            campaign_id = proposed.structured_content["campaign_id"]
            await session.call_tool("approve_campaign", {
                "project_root": str(self.root), "campaign_id": campaign_id,
                "proposal_hash": proposed.structured_content["data"]["proposal_hash"],
                "proposal_and_authority_confirmed": True,
            })
            next_action = await session.call_tool("get_next_action", {
                "project_root": str(self.root), "campaign_id": campaign_id,
            })
            action = next_action.structured_content["action"]
            self.assertEqual(action["type"], "EXECUTE_TASK")
            Path(action["workspace"]).joinpath("app.py").write_text("VALUE = 2\n", encoding="utf-8")
            paused = await session.call_tool("pause_campaign", {
                "project_root": str(self.root), "campaign_id": campaign_id,
            })
            self.assertFalse(paused.is_error, paused)
            result = {
                "action_id": action["id"],
                "canonical_revision": action["canonical_revision"],
                "outcome": "PASS",
                "summary": "Implemented and verified.",
                "data": {},
                "findings": [],
                "blocker": None,
                "next_action": None,
            }
            completed = await session.call_tool("submit_action_result", {
                "project_root": str(self.root), "action_id": action["id"], "result": result,
            })
            self.assertFalse(completed.is_error, completed)
            self.assertIsNotNone(completed.structured_content["action"], completed)
            self.assertEqual(completed.structured_content["action"]["type"], "PAUSED")
            continued = await session.call_tool("campaign_continue", {
                "project_root": str(self.root), "campaign_id": campaign_id,
            })
            self.assertFalse(continued.is_error, continued)
            target = await session.call_tool("get_next_action", {
                "project_root": str(self.root), "campaign_id": campaign_id,
            })
            self.assertEqual(target.structured_content["action"]["type"], "TARGET_REACHED")
            materialized = await session.call_tool("materialize_campaign", {
                "project_root": str(self.root), "campaign_id": campaign_id,
            })
            self.assertFalse(materialized.is_error, materialized)
            retargeted = await session.call_tool("retarget_campaign", {
                "project_root": str(self.root), "campaign_id": campaign_id,
                "target": "WORKING_MVP",
            })
            self.assertFalse(retargeted.is_error, retargeted)
            planning = await session.call_tool("get_next_action", {
                "project_root": str(self.root), "campaign_id": campaign_id,
            })
            self.assertEqual(planning.structured_content["action"]["type"], "PLAN_PHASE")
            self.assertFalse(self.codex_sentinel.exists())

    async def test_blocked_campaign_status_and_answer(self) -> None:
        async with self.session() as session:
            await session.call_tool("initialize_project", {
                "project_root": str(self.root), "name": "blocked-flow", "merge": False,
            })
            (self.root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            git(self.root, "add", ".")
            git(self.root, "commit", "-qm", "base")
            proposed = await session.call_tool("propose_campaign", {
                "project_root": str(self.root), "idea": "High risk change",
                "development_strategy": "CHANGE", "target": "CHANGE_COMPLETE",
                "autonomy": "HUMAN_ON_BLOCKED", "proposal": proposal(risk="HIGH"),
            })
            campaign_id = proposed.structured_content["campaign_id"]
            blocked = await session.call_tool("approve_campaign", {
                "project_root": str(self.root), "campaign_id": campaign_id,
                "proposal_hash": proposed.structured_content["data"]["proposal_hash"],
                "proposal_and_authority_confirmed": True,
            })
            self.assertTrue(blocked.is_error, blocked)
            request_id = blocked.structured_content["data"]["request_id"]
            status = await session.call_tool("campaign_status", {
                "project_root": str(self.root), "campaign_id": campaign_id,
            })
            self.assertFalse(status.is_error, status)
            self.assertEqual(status.structured_content["data"]["status"], "WAITING_FOR_HUMAN")
            waiting = await session.call_tool("get_next_action", {
                "project_root": str(self.root), "campaign_id": campaign_id,
            })
            self.assertEqual(waiting.structured_content["action"]["type"], "ASK_HUMAN")
            answered = await session.call_tool("answer_blocker", {
                "project_root": str(self.root), "campaign_id": campaign_id,
                "request_id": request_id, "answers": {"decision": ["Approve exception"]},
            })
            self.assertFalse(answered.is_error, answered)
            self.assertFalse(self.codex_sentinel.exists())


class PluginStructureTests(unittest.TestCase):
    def test_plugin_is_skill_plus_stdio_mcp_only(self) -> None:
        plugin = SOURCE_ROOT / "plugins" / "autodev"
        manifest = json.loads((plugin / ".codex-plugin/plugin.json").read_text())
        self.assertEqual(manifest["name"], "autodev")
        self.assertEqual(manifest["version"], "4.0.0-alpha.1")
        self.assertEqual(manifest["skills"], "./skills/")
        self.assertEqual(manifest["mcpServers"], "./.mcp.json")
        self.assertNotIn("hooks", manifest)
        self.assertNotIn("apps", manifest)
        self.assertFalse((plugin / ".app.json").exists())
        self.assertFalse((plugin / "hooks").exists())
        mcp_config = json.loads((plugin / ".mcp.json").read_text())
        self.assertEqual(mcp_config["mcpServers"]["autodev"]["command"], "autodev-mcp")
        self.assertEqual(mcp_config["mcpServers"]["autodev"]["args"], ["--stdio"])
        marketplace = json.loads((SOURCE_ROOT / ".agents/plugins/marketplace.json").read_text())
        self.assertEqual(marketplace["name"], "personal")
        self.assertEqual(marketplace["interface"]["displayName"], "Personal")
        self.assertEqual(marketplace["plugins"], [{
            "name": "autodev",
            "source": {"source": "local", "path": "./plugins/autodev"},
            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
            "category": "Productivity",
        }])

    def test_skill_is_explicit_and_has_locked_commander_guards(self) -> None:
        skill = (SOURCE_ROOT / "plugins/autodev/skills/autodev/SKILL.md").read_text()
        metadata = (SOURCE_ROOT / "plugins/autodev/skills/autodev/agents/openai.yaml").read_text()
        self.assertIn("allow_implicit_invocation: false", metadata)
        for phrase in (
            "exactly one", "fresh", "at most one Worker", "quality_route",
            "autodev start", "codex exec", "App Server",
        ):
            self.assertIn(phrase, skill)
        lowered = skill.lower()
        self.assertNotIn("execution_backend", lowered)
        self.assertNotIn("managed/native", lowered)


if __name__ == "__main__":
    unittest.main()
