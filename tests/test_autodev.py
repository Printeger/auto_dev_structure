from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_SCRIPT = SOURCE_ROOT / "scripts" / "autodev.py"


def run_cli(script: Path, *arguments: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *arguments],
        cwd=cwd or script.parents[1],
        check=False,
        capture_output=True,
        text=True,
    )


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


class InitTests(unittest.TestCase):
    def test_fresh_init_replaces_name_and_excludes_design_background(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            result = run_cli(SOURCE_SCRIPT, "init", str(target), "--name", "sample-app")
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(json.loads((target / ".agent/STATE.json").read_text())["project_name"], "sample-app")
            self.assertIn("sample-app", (target / "README.md").read_text(encoding="utf-8"))
            self.assertFalse((target / "Auto_Dev.md").exists())

            validation = run_cli(target / "scripts/autodev.py", "validate", cwd=target)
            self.assertEqual(validation.returncode, 0, validation.stderr + validation.stdout)
            status = run_cli(target / "scripts/autodev.py", "status", "--json", cwd=target)
            self.assertEqual(status.returncode, 0, status.stderr + status.stdout)
            doctor = run_cli(target / "scripts/autodev.py", "doctor", cwd=target)
            self.assertEqual(doctor.returncode, 0, doctor.stderr + doctor.stdout)
            nested = run_cli(
                target / "scripts/autodev.py",
                "init",
                str(target / "nested"),
                "--name",
                "nested-app",
                cwd=target,
            )
            self.assertEqual(nested.returncode, 0, nested.stderr + nested.stdout)

    def test_default_conflict_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            target.mkdir()
            readme = target / "README.md"
            readme.write_text("keep me\n", encoding="utf-8")
            result = run_cli(SOURCE_SCRIPT, "init", str(target), "--name", "sample-app")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(readme.read_text(encoding="utf-8"), "keep me\n")
            self.assertEqual([path.name for path in target.iterdir()], ["README.md"])

    def test_merge_copies_missing_and_preserves_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            target.mkdir()
            readme = target / "README.md"
            readme.write_text("existing\n", encoding="utf-8")
            result = run_cli(SOURCE_SCRIPT, "init", str(target), "--name", "merged", "--merge")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(readme.read_text(encoding="utf-8"), "existing\n")
            self.assertTrue((target / "AGENTS.md").is_file())
            self.assertEqual(json.loads((target / ".agent/STATE.json").read_text())["project_name"], "merged")


class InitializedProjectTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project"
        result = run_cli(SOURCE_SCRIPT, "init", str(self.root), "--name", "test-project")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.script = self.root / "scripts" / "autodev.py"

    @property
    def state_path(self) -> Path:
        return self.root / ".agent" / "STATE.json"

    def state(self) -> dict[str, object]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def validate(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return run_cli(self.script, "validate", *arguments, cwd=self.root)

    def make_ready(self) -> None:
        replacements = {
            "PROJECT.md": {
                "{{MISSION}}": "Deliver a useful tool.",
                "{{MOTIVATION}}": "Reduce repeated work.",
                "{{DELIVERABLE_1}}": "A tested CLI.",
                "{{SUCCESS_CRITERION_1}}": "All acceptance tests pass.",
                "{{CONSTRAINT_1}}": "Use the standard library.",
                "{{PRIORITY_1}}": "Correctness",
                "{{PRIORITY_2}}": "Safety",
                "{{PRIORITY_3}}": "Speed",
            },
            "docs/REQUIREMENTS.md": {
                "{{REQUIREMENT_1}}": "The CLI validates state.",
                "{{ACCEPTANCE_SIGNAL_1}}": "Validation exits zero.",
            },
            "docs/ARCHITECTURE.md": {
                "{{ARCH_STATUS}}": "FROZEN",
                "{{SYSTEM_CONTEXT}}": "A local command-line workflow.",
                "{{COMPONENT_BOUNDARIES}}": "CLI, contracts, and hooks.",
                "{{DATA_AND_INTERFACES}}": "Versioned JSON and Markdown.",
                "{{QUALITY_ATTRIBUTES}}": "Safe and dependency-free.",
                "{{RE_EVALUATION_TRIGGER_1}}": "The state contract changes.",
            },
            ".agent/ROADMAP.md": {
                "{{MILESTONE_ID}}": "M1",
                "{{MILESTONE_TITLE}}": "Foundation",
                "{{MILESTONE_OUTCOME}}": "Validated workflow.",
                "{{MILESTONE_ENTRY_CRITERIA}}": "Contracts drafted.",
                "{{MILESTONE_EXIT_CRITERIA}}": "Tests pass.",
                "{{MILESTONE_REQUIREMENTS}}": "REQ-001",
                "{{MILESTONE_RISKS}}": "None known.",
                "{{LATER_MILESTONE_1}}": "M2 hardening.",
            },
        }
        for relative, mapping in replacements.items():
            path = self.root / relative
            content = path.read_text(encoding="utf-8")
            for old, new in mapping.items():
                content = content.replace(old, new)
            path.write_text(content, encoding="utf-8")
        state = self.state()
        state.update(
            project_status="ACTIVE",
            current_milestone="M1",
            next_action="Create TASK-001.",
            next_owner="COMMANDER",
            updated_at="2026-08-28T10:00:00+08:00",
        )
        write_json(self.state_path, state)


class ValidationTests(InitializedProjectTestCase):
    def test_bootstrap_state_is_valid_but_not_ready(self) -> None:
        self.assertEqual(self.validate().returncode, 0)
        status = self.validate("--json")
        self.assertEqual(status.returncode, 0)
        self.assertFalse(json.loads(status.stdout)["ready"])
        result = self.validate("--ready", "--json")
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["valid"])
        self.assertFalse(payload["ready"])

    def test_ready_contract_and_active_state_pass(self) -> None:
        self.make_ready()
        result = self.validate("--ready", "--json")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(json.loads(result.stdout)["ready"])

    def test_invalid_json_and_unknown_enum_fail(self) -> None:
        self.state_path.write_text("{bad json", encoding="utf-8")
        self.assertEqual(self.validate().returncode, 1)

        source_state = json.loads((SOURCE_ROOT / ".agent/STATE.json").read_text(encoding="utf-8"))
        source_state["project_name"] = "test-project"
        source_state["quality_mode"] = "QUICK"
        write_json(self.state_path, source_state)
        result = self.validate("--json")
        self.assertEqual(result.returncode, 1)
        self.assertIn("quality_mode", result.stdout)

    def test_missing_task_and_budget_limits_fail(self) -> None:
        state = self.state()
        state["current_task_id"] = "TASK-404"
        state["agent_calls"] = 5
        state["rework_count"] = 3
        write_json(self.state_path, state)
        result = self.validate("--json")
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not exist", result.stdout)
        self.assertIn("exceeds policy maximum", result.stdout)

    def test_blocked_invariants(self) -> None:
        state = self.state()
        state.update(project_status="BLOCKED", blocker=None, next_owner="BUILDER", last_outcome="BLOCKED")
        write_json(self.state_path, state)
        result = self.validate("--json")
        self.assertEqual(result.returncode, 1)
        self.assertIn("BLOCKED requires a non-empty blocker", result.stdout)
        self.assertIn("BLOCKED requires HUMAN", result.stdout)

        state.update(blocker="Need API credentials.", next_owner="HUMAN", next_action="Provide a scoped test credential.")
        write_json(self.state_path, state)
        self.assertEqual(self.validate().returncode, 0)

    def test_complete_invariants(self) -> None:
        task = self.root / ".agent/tasks/TASK-001.md"
        task.write_text("# TASK-001\n", encoding="utf-8")
        state = self.state()
        state.update(project_status="COMPLETE", current_task_id="TASK-001", next_owner="COMMANDER")
        write_json(self.state_path, state)
        result = self.validate("--json")
        self.assertEqual(result.returncode, 1)
        self.assertIn("COMPLETE cannot have", result.stdout)
        self.assertIn("COMPLETE requires NONE", result.stdout)


class TaskAndPromptTests(InitializedProjectTestCase):
    def test_new_task_writes_risk_and_requirements_and_refuses_duplicate(self) -> None:
        result = run_cli(
            self.script,
            "new-task",
            "--id",
            "TASK-001",
            "--title",
            "Validate timestamps",
            "--risk",
            "high",
            "--requirements",
            "REQ-001,REQ-002",
            cwd=self.root,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        task = (self.root / ".agent/tasks/TASK-001.md").read_text(encoding="utf-8")
        self.assertIn("Risk: `HIGH`", task)
        self.assertIn("`REQ-001`", task)
        self.assertIn("`REQ-002`", task)
        duplicate = run_cli(
            self.script,
            "new-task",
            "--id",
            "TASK-001",
            "--title",
            "Duplicate",
            "--risk",
            "LOW",
            cwd=self.root,
        )
        self.assertEqual(duplicate.returncode, 1)

    def test_new_task_rejects_bad_id(self) -> None:
        result = run_cli(
            self.script,
            "new-task",
            "--id",
            "TASK-1",
            "--title",
            "Bad",
            "--risk",
            "LOW",
            cwd=self.root,
        )
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.root / ".agent/tasks/TASK-1.md").exists())

        lowercase = run_cli(
            self.script,
            "new-task",
            "--id",
            "task-001",
            "--title",
            "Bad case",
            "--risk",
            "LOW",
            cwd=self.root,
        )
        self.assertEqual(lowercase.returncode, 1)

    def test_prompt_reflects_bootstrap_state(self) -> None:
        result = run_cli(self.script, "prompt", cwd=self.root)
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=BOOTSTRAP", result.stdout)
        self.assertIn("validate --ready", result.stdout)


class HookTests(InitializedProjectTestCase):
    def run_hook(self, event: dict[str, object]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(self.root / "scripts/hooks/stop_validate.py")],
            cwd=self.root,
            input=json.dumps(event),
            check=False,
            capture_output=True,
            text=True,
        )

    def test_valid_state_allows_stop(self) -> None:
        result = self.run_hook({"stop_hook_active": False})
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["continue"])
        self.assertNotIn("decision", payload)

    def test_invalid_state_blocks_once(self) -> None:
        state = self.state()
        state["project_status"] = "INVALID"
        write_json(self.state_path, state)
        first = json.loads(self.run_hook({"stop_hook_active": False}).stdout)
        self.assertEqual(first["decision"], "block")
        second = json.loads(self.run_hook({"stop_hook_active": True}).stdout)
        self.assertTrue(second["continue"])
        self.assertNotIn("decision", second)


if __name__ == "__main__":
    unittest.main()
