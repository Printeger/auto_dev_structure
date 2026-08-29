from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "src"))

from autodev.human import (
    AutoResolvingHumanInteraction,
    HumanOption,
    HumanQuestion,
    HumanRequest,
    Pending,
    PersistentHumanInteraction,
    app_server_response,
    request_from_app_server,
    FakeHumanInteraction,
    HumanResponse,
)
from autodev.campaign import PlannerRequest
from autodev.engines.app_server import AppServerCodexEngine


class HumanInteractionTests(unittest.TestCase):
    def test_app_server_request_and_response_preserve_protocol_shape(self) -> None:
        params = {
            "threadId": "thread", "turnId": "turn", "itemId": "item-1",
            "autoResolutionMs": 60000,
            "questions": [{
                "id": "scope", "header": "Scope", "question": "Choose scope",
                "isOther": True, "isSecret": False,
                "options": [
                    {"label": "Narrow", "description": "Keep the current scope."},
                    {"label": "Broad", "description": "Expand it."},
                ],
            }],
        }
        request = request_from_app_server(params, "CAMP-001")
        self.assertEqual(request.auto_resolution_ms, 60000)
        self.assertTrue(request.questions[0].allow_other)
        with tempfile.TemporaryDirectory() as directory:
            interaction = PersistentHumanInteraction(Path(directory))
            pending = interaction.request(request)
            self.assertIsInstance(pending, Pending)
            response = interaction.answer("CAMP-001", "item-1", {"scope": ["custom scope"]})
        self.assertEqual(
            app_server_response(response),
            {"answers": {"scope": {"answers": ["custom scope"]}}},
        )

    def test_timeout_uses_first_recommended_option_and_persists_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = HumanRequest(
                "CAMP-001",
                (HumanQuestion("mode", "Mode", "Choose", (
                    HumanOption("Staged", "Recommended flow."),
                    HumanOption("Critical", "More gates."),
                )),),
                request_id="HUMAN-001", auto_resolution_ms=60000,
            )
            waited: list[float] = []
            interaction = AutoResolvingHumanInteraction(
                PersistentHumanInteraction(Path(directory)), sleeper=waited.append,
            )
            response = interaction.request(request)
            self.assertEqual(waited, [60.0])
            self.assertEqual(response.answers["mode"], ("Staged",))
            artifact = Path(directory) / ".autodev/campaigns/CAMP-001/human-requests/HUMAN-001.json"
            self.assertEqual(json.loads(artifact.read_text())["status"], "AUTO_RESOLVED")

    def test_secret_request_is_sanitized_and_cannot_be_answered_in_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            interaction = PersistentHumanInteraction(Path(directory))
            request = HumanRequest(
                "CAMP-001", (HumanQuestion(
                    "token", "Token", "Paste the production token", is_secret=True,
                ),), request_id="HUMAN-SECRET",
            )
            pending = interaction.request(request)
            content = pending.artifact_path.read_text(encoding="utf-8")
            self.assertNotIn("production token", content)
            with self.assertRaises(ValueError):
                interaction.answer("CAMP-001", "HUMAN-SECRET", {"token": ["secret-value"]})
            self.assertNotIn("secret-value", pending.artifact_path.read_text(encoding="utf-8"))

    def test_app_server_planner_opts_into_experimental_input_and_completes_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "fake-app-server"
            proposal = {
                "requirements": [{"id": "REQ-001"}], "authority_envelope": {},
                "phase": "SCAFFOLD", "tasks": [], "questions": [],
            }
            completed_event = {
                "method": "item/completed",
                "params": {"item": {"type": "agentMessage", "text": json.dumps(proposal)}},
            }
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "assert 'mcp_servers={}' in sys.argv\n"
                "assert 'hooks={}' in sys.argv\n"
                "assert os.environ.get('CODEX_HOME')\n"
                "def read(): return json.loads(sys.stdin.readline())\n"
                "def send(v): print(json.dumps(v), flush=True)\n"
                "init = read()\n"
                "assert init['params']['capabilities']['experimentalApi'] is True\n"
                "send({'id': 0, 'result': {'userAgent': 'fake'}})\n"
                "read(); start = read(); send({'id': 1, 'result': {'thread': {'id': 'thr', 'model': 'test-model'}}})\n"
                "turn = read()\n"
                "assert turn['params']['collaborationMode']['mode'] == 'plan'\n"
                "assert turn['params']['collaborationMode']['settings']['model'] == 'test-model'\n"
                "assert turn['params']['outputSchema']['title'].startswith('AutoDev')\n"
                "send({'method': 'item/tool/requestUserInput', 'id': 99, 'params': {"
                "'threadId': 'thr', 'turnId': 'turn', 'itemId': 'item', 'autoResolutionMs': None,"
                "'questions': [{'id':'scope','header':'Scope','question':'Choose?',"
                "'options':[{'label':'Narrow','description':'Keep it.'},{'label':'Broad','description':'Expand.'}],"
                "'isOther': True, 'isSecret': False}]}})\n"
                "answer = read(); assert answer['result']['answers']['scope']['answers'] == ['Narrow']\n"
                f"send({completed_event!r})\n"
                "send({'method':'turn/completed','params':{'turn':{'id':'turn','status':'completed'}}})\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            interaction = FakeHumanInteraction([
                HumanResponse("item", {"scope": ("Narrow",)}, "fake"),
            ])
            engine = AppServerCodexEngine(interaction, str(executable), timeout=10)
            result = engine.plan(PlannerRequest(
                "CAMP-001", "Build", "STAGED", "ARCHITECTURE_BASELINE", "SCAFFOLD", root,
            ))
            self.assertEqual(result["phase"], "SCAFFOLD")
            self.assertEqual(interaction.requests[0].questions[0].id, "scope")

    def test_app_server_reads_coalesced_jsonl_from_python_buffer_without_deadlock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "coalesced-app-server"
            proposal = {
                "requirements": [], "authority_envelope": {},
                "phase": "SCAFFOLD", "tasks": [], "questions": [],
            }
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys, time\n"
                "def read(): return json.loads(sys.stdin.readline())\n"
                "def line(v): return json.dumps(v) + '\\n'\n"
                "read(); print(line({'id':0,'result':{}}), end='', flush=True)\n"
                "read(); read(); print(line({'id':1,'result':{'thread':{'id':'thr'}}}), end='', flush=True)\n"
                "read()\n"
                f"proposal = {proposal!r}\n"
                "time.sleep(0.15)\n"
                "messages = (\n"
                "  line({'method':'item/completed','params':{'item':{'type':'agentMessage','text':json.dumps(proposal)}}}) +\n"
                "  line({'method':'turn/completed','params':{'turn':{'id':'turn','status':'completed'}}})\n"
                ")\n"
                "os.write(sys.stdout.fileno(), messages.encode())\n"
                "time.sleep(2)\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            progress: list[str] = []
            engine = AppServerCodexEngine(
                FakeHumanInteraction([]), str(executable), timeout=0.5,
                heartbeat_interval=0.05, progress=progress.append,
            )
            result = engine.plan(PlannerRequest(
                "CAMP-001", "Build", "STAGED", "ARCHITECTURE_BASELINE", "SCAFFOLD", root,
            ))
            self.assertEqual(result["phase"], "SCAFFOLD")
            self.assertTrue(any("working" in item.lower() for item in progress))


if __name__ == "__main__":
    unittest.main()
