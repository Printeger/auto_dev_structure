from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "src"))

from autodev.engines import AttemptRequest, CodexExecEngine


class CodexEngineTests(unittest.TestCase):
    def request(self, root: Path, executable: Path, *, stop: bool = False, timeout: int = 5) -> tuple[CodexExecEngine, AttemptRequest]:
        schema = root / "schema.json"
        schema.write_text("{}\n", encoding="utf-8")
        stop_file = root / "STOP"
        if stop:
            stop_file.write_text("STOP\n", encoding="utf-8")
        request = AttemptRequest(
            "RUN-1", "TASK-001", "builder", root, "prompt", schema, root / "artifacts",
            idle_timeout=timeout, hard_timeout=timeout, stop_file=stop_file,
        )
        return CodexExecEngine(str(executable)), request

    @staticmethod
    def script(root: Path, body: str) -> Path:
        path = root / "fake-codex"
        path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_command_enforces_codex_controls_and_jsonl_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = self.script(
                root,
                "import json\n"
                "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', "
                "'text': json.dumps({'outcome': 'PASS', 'summary': 'ok'})}}), flush=True)\n",
            )
            engine, request = self.request(root, executable)
            command = engine.command(request)
            exec_index = command.index("exec")
            for global_flag in ("--ask-for-approval", "-c"):
                self.assertLess(command.index(global_flag), exec_index)
            for exec_flag in (
                "--ephemeral", "--ignore-user-config", "--ignore-rules", "--json",
                "--output-schema", "-C",
            ):
                self.assertGreater(command.index(exec_flag), exec_index)
            self.assertIn("never", command)
            self.assertIn('default_permissions=":workspace"', command)
            self.assertNotIn("--sandbox", command)
            self.assertFalse(any("legacy_landlock" in argument for argument in command))
            self.assertFalse(any("sandbox_workspace_write" in argument for argument in command))
            self.assertIn("mcp_servers={}", command)
            self.assertIn("hooks={}", command)
            self.assertIn("--json", command)
            self.assertIn("--output-schema", command)
            with mock.patch.dict(os.environ, {"AUTODEV_LIVE_CODEX": "1"}):
                result = engine.execute(request)
            self.assertEqual(result.status, "SUCCESS", result)
            self.assertEqual(result.proposal["outcome"], "PASS")

    def test_command_uses_explicit_permission_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = self.script(root, "raise SystemExit(0)\n")
            engine, request = self.request(root, executable)
            builder = engine.command(request)
            reviewer = engine.command(AttemptRequest(
                request.run_id, request.task_id, "reviewer", request.workspace,
                request.prompt, request.output_schema, request.artifact_dir,
                permission_profile=":read-only",
            ))
            self.assertIn('default_permissions=":workspace"', builder)
            self.assertIn('default_permissions=":read-only"', reviewer)
            self.assertFalse(any("legacy_landlock" in argument for argument in builder + reviewer))
            self.assertFalse(any(":danger-full-access" in argument for argument in builder + reviewer))

    def test_external_sandbox_is_explicit_and_never_an_automatic_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "model-started"
            executable = self.script(
                root,
                "import sys\n"
                "from pathlib import Path\n"
                "if 'sandbox' in sys.argv[1:]:\n"
                "    print('bwrap bootstrap failed')\n"
                "    raise SystemExit(1)\n"
                f"Path({str(marker)!r}).write_text('started')\n",
            )
            engine, request = self.request(root, executable)
            with mock.patch.dict(os.environ, {"AUTODEV_LIVE_CODEX": "1"}, clear=True):
                regular = engine.execute(request)
            self.assertEqual(regular.status, "INFRA_FAILURE")
            self.assertEqual(regular.failure_class, "bubblewrap_bootstrap_failure")
            self.assertFalse(marker.exists())
            self.assertNotIn('default_permissions=":danger-full-access"', engine.command(request))

            external = AttemptRequest(
                request.run_id, request.task_id, request.role, request.workspace,
                request.prompt, request.output_schema, request.artifact_dir,
                runtime_mode="external-sandbox",
            )
            with mock.patch.dict(os.environ, {"AUTODEV_LIVE_CODEX": "1"}, clear=True):
                rejected = engine.execute(external)
            self.assertEqual(rejected.status, "INFRA_FAILURE")
            self.assertEqual(rejected.failure_class, "external_sandbox_not_confirmed")
            self.assertFalse(marker.exists())
            self.assertIn('default_permissions=":danger-full-access"', engine.command(external))
            with mock.patch.dict(
                os.environ,
                {"AUTODEV_LIVE_CODEX": "1", "AUTODEV_EXTERNAL_SANDBOX": "1"},
                clear=True,
            ):
                confirmed = engine.preflight(
                    root, runtime_mode="external-sandbox",
                )
            self.assertTrue(confirmed["ready"])
            self.assertEqual(confirmed["classification"], "external_sandbox")
            with mock.patch.dict(os.environ, {"AUTODEV_LIVE_CODEX": "1"}, clear=True):
                revoked = engine.preflight(root, runtime_mode="external-sandbox")
            self.assertFalse(revoked["ready"])
            self.assertEqual(revoked["classification"], "external_sandbox_not_confirmed")

    def test_unknown_runtime_mode_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            diagnostic = CodexExecEngine("codex").preflight(
                Path(directory), runtime_mode="invented-backend",
            )
            self.assertFalse(diagnostic["ready"])
            self.assertEqual(diagnostic["classification"], "codex_configuration_error")

    def test_linux_preflight_classifies_runtime_failures_without_exec(self) -> None:
        cases = {
            "legacy_landlock_incompatibility": (
                "permission profiles requiring direct runtime enforcement are incompatible "
                "with --use-legacy-landlock"
            ),
            "bubblewrap_bootstrap_failure": "bubblewrap executable was not found",
            "nested_sandbox_restriction": (
                "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted"
            ),
            "codex_configuration_error": "Error loading configuration: invalid TOML",
            "environment_runtime_failure": "unexpected sandbox helper failure",
        }
        for classification, output in cases.items():
            with self.subTest(classification=classification), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                marker = root / "exec-started"
                executable = self.script(
                    root,
                    "import sys\n"
                    "from pathlib import Path\n"
                    "args = sys.argv[1:]\n"
                    "if 'sandbox' in args:\n"
                    f"    print({output!r})\n"
                    "    raise SystemExit(1)\n"
                    f"Path({str(marker)!r}).write_text('started')\n",
                )
                diagnostic = CodexExecEngine(str(executable)).preflight(root)
                self.assertFalse(diagnostic["ready"])
                self.assertEqual(diagnostic["classification"], classification)
                self.assertFalse(marker.exists())

    def test_probe_parses_the_final_command_shape_and_checks_login(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = self.script(
                root,
                "import sys\n"
                "args = sys.argv[1:]\n"
                "if args == ['login', 'status']:\n"
                "    print('Logged in using ChatGPT')\n"
                "    raise SystemExit(0)\n"
                "if ('--help' in args or '--version' in args) and 'exec' in args:\n"
                "    split = args.index('exec')\n"
                "    before, after = args[:split], args[split + 1:]\n"
                "    valid = all(flag in before for flag in ['--ask-for-approval', '-c'])\n"
                "    valid = valid and all(flag in after for flag in "
                "['--ephemeral', '--ignore-user-config', '--ignore-rules', '--json', '--output-schema', '-C'])\n"
                "    print('final command parsed' if valid else 'invalid option placement')\n"
                "    raise SystemExit(0 if valid else 2)\n"
                "if 'sandbox' in args and '/bin/true' in args:\n"
                "    print('sandbox ready')\n"
                "    raise SystemExit(0)\n"
                "raise SystemExit(2)\n",
            )
            probe = CodexExecEngine(str(executable)).probe()
            self.assertTrue(probe["ready"], probe)
            self.assertTrue(probe["command_parse"]["ready"])
            self.assertTrue(probe["login"]["ready"])
            self.assertTrue(probe["sandbox_preflight"]["ready"])

    def test_probe_rejects_help_text_false_positive_and_login_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parse_failure = self.script(
                root,
                "import sys\n"
                "args = sys.argv[1:]\n"
                "if args in (['--help'], ['exec', '--help']):\n"
                "    print('--json --output-schema --sandbox --ask-for-approval --ephemeral --ignore-user-config')\n"
                "    raise SystemExit(0)\n"
                "if args == ['login', 'status']:\n"
                "    print('Logged in')\n"
                "    raise SystemExit(0)\n"
                "raise SystemExit(2)\n",
            )
            probe = CodexExecEngine(str(parse_failure)).probe()
            self.assertFalse(probe["ready"], probe)
            self.assertFalse(probe["command_parse"]["ready"])

            login_failure = self.script(
                root,
                "import sys\n"
                "args = sys.argv[1:]\n"
                "if args == ['login', 'status']:\n"
                "    print('Not logged in')\n"
                "    raise SystemExit(1)\n"
                "if ('--help' in args or '--version' in args) and 'exec' in args:\n"
                "    print('parsed')\n"
                "    raise SystemExit(0)\n"
                "raise SystemExit(2)\n",
            )
            probe = CodexExecEngine(str(login_failure)).probe()
            self.assertFalse(probe["ready"], probe)
            self.assertTrue(probe["command_parse"]["ready"])
            self.assertFalse(probe["login"]["ready"])

    def test_probe_rejects_a_config_error_that_help_short_circuits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = self.script(
                root,
                "import sys\n"
                "args = sys.argv[1:]\n"
                "if args == ['login', 'status']:\n"
                "    print('Logged in')\n"
                "    raise SystemExit(0)\n"
                "if '--help' in args and 'exec' in args:\n"
                "    print('help')\n"
                "    raise SystemExit(0)\n"
                "if '--version' in args and 'exec' in args:\n"
                "    print('invalid config')\n"
                "    raise SystemExit(2)\n"
                "raise SystemExit(2)\n",
            )
            probe = CodexExecEngine(str(executable)).probe()
            self.assertFalse(probe["ready"], probe)
            self.assertFalse(probe["command_parse"]["ready"])
            self.assertEqual(probe["command_parse"]["config_returncode"], 2)

    def test_execute_fails_closed_without_live_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "started"
            executable = self.script(
                root,
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('started')\n",
            )
            engine, request = self.request(root, executable)
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("AUTODEV_LIVE_CODEX", None)
                result = engine.execute(request)
            self.assertEqual(result.status, "NOT_READY")
            self.assertFalse(marker.exists())

    def test_execute_surfaces_structured_codex_error_on_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = self.script(
                root,
                "import json, sys\n"
                "if 'sandbox' in sys.argv[1:]:\n"
                "    raise SystemExit(0)\n"
                "detail = {'error': {'message': \"Invalid schema: 'uniqueItems' is not permitted.\"}}\n"
                "print(json.dumps({'type': 'error', 'message': json.dumps(detail)}), flush=True)\n"
                "raise SystemExit(1)\n",
            )
            engine, request = self.request(root, executable)
            with mock.patch.dict(os.environ, {"AUTODEV_LIVE_CODEX": "1"}):
                result = engine.execute(request)
            self.assertEqual(result.status, "INFRA_FAILURE")
            self.assertIn("uniqueItems", result.infrastructure_error or "")
            self.assertNotEqual(result.infrastructure_error, "codex exited 1")

    def test_protocol_error_timeout_and_stop_are_structured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad = self.script(root, "print('not-json', flush=True)\n")
            engine, request = self.request(root, bad)
            with mock.patch.dict(os.environ, {"AUTODEV_LIVE_CODEX": "1"}):
                self.assertEqual(engine.execute(request).status, "INFRA_FAILURE")

            slow = self.script(
                root,
                "import sys, time\n"
                "if 'sandbox' in sys.argv[1:]:\n"
                "    raise SystemExit(0)\n"
                "time.sleep(30)\n",
            )
            engine, request = self.request(root, slow, timeout=1)
            with mock.patch.dict(os.environ, {"AUTODEV_LIVE_CODEX": "1"}):
                timed = engine.execute(request)
            self.assertEqual(timed.status, "INFRA_FAILURE")
            self.assertEqual(timed.infrastructure_error, "timeout")

            engine, request = self.request(root, slow, stop=True, timeout=30)
            with mock.patch.dict(os.environ, {"AUTODEV_LIVE_CODEX": "1"}):
                self.assertEqual(engine.execute(request).status, "STOPPED")


if __name__ == "__main__":
    unittest.main()
