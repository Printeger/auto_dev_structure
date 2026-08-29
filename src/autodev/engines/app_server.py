"""Experimental Codex App Server Planner with stable exec fallback."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

from autodev._resources import _read_text
from autodev.engines.base import AttemptRequest
from autodev.engines.codex import CodexExecEngine
from autodev.human import (
    HumanInteraction, HumanResponse, Pending, app_server_response, request_from_app_server,
)

if TYPE_CHECKING:
    from autodev.campaign import PlannerRequest


class AppServerUnavailable(RuntimeError):
    pass


class HumanInputPending(RuntimeError):
    def __init__(self, pending: Pending) -> None:
        super().__init__(pending.reason)
        self.pending = pending


class AppServerCodexEngine:
    """Run each Planner call in a fresh app-server process and thread."""

    def __init__(
        self,
        interaction: HumanInteraction,
        executable: str = "codex",
        *,
        process_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        timeout: float = 2400,
        heartbeat_interval: float = 15,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.interaction = interaction
        self.executable = executable
        self.process_factory = process_factory
        self.timeout = timeout
        self.heartbeat_interval = heartbeat_interval
        self.progress = progress

    def probe(self) -> dict[str, Any]:
        try:
            with tempfile.TemporaryDirectory() as directory:
                generated = subprocess.run(
                    [self.executable, "app-server", "generate-json-schema", "--out", directory],
                    capture_output=True, text=True, timeout=20, check=False,
                )
                params = Path(directory) / "ToolRequestUserInputParams.json"
                if generated.returncode or not params.is_file():
                    return {"ready": False, "mode": "fallback", "error": generated.stderr.strip()}
                schema = json.loads(params.read_text(encoding="utf-8"))
                fields = schema.get("properties", {})
                ready = "questions" in fields and "autoResolutionMs" in fields
                return {
                    "ready": ready, "mode": "native" if ready else "fallback",
                    "request_user_input": ready, "experimental_api": True,
                }
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
            return {"ready": False, "mode": "fallback", "error": str(error)}

    @staticmethod
    def _prompt(request: PlannerRequest) -> str:
        contract = {
            "campaign_id": request.campaign_id, "idea": request.idea, "mode": request.mode,
            "target": request.target, "phase": request.phase,
            "approved_requirements": list(request.requirements), "prior_answers": dict(request.answers),
        }
        return (
            "Act as AutoDev's read-only Phase Planner. Inspect the repository but do not edit it. "
            "Do not expand the approved requirements. Return only JSON matching this shape: "
            "{requirements: [...], authority_envelope: {...}, phase: string, tasks: [...], questions: [...]}. "
            "Use request_user_input for at most three directional questions when the host supports it.\n"
            + json.dumps(contract, indent=2, ensure_ascii=False)
        )

    def plan(self, request: PlannerRequest) -> Mapping[str, Any]:
        isolated_home = tempfile.TemporaryDirectory(prefix="autodev-codex-home-")
        isolated_path = Path(isolated_home.name)
        user_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        auth = user_home / "auth.json"
        if auth.is_file():
            try:
                os.symlink(auth, isolated_path / "auth.json")
            except OSError as error:
                isolated_home.cleanup()
                raise AppServerUnavailable(f"cannot isolate Codex authentication: {error}") from error
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(isolated_path)
        try:
            process = self.process_factory(
                [
                    self.executable,
                    "-c", "mcp_servers={}",
                    "-c", "hooks={}",
                    "app-server", "--stdio",
                ], cwd=request.repository, env=environment,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1,
            )
        except OSError as error:
            isolated_home.cleanup()
            raise AppServerUnavailable(str(error)) from error
        if process.stdin is None or process.stdout is None:
            process.kill()
            isolated_home.cleanup()
            raise AppServerUnavailable("app-server stdio is unavailable")

        def send(message: Mapping[str, Any]) -> None:
            process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            process.stdin.flush()

        stdout_lines: queue.Queue[str | None] = queue.Queue()
        stderr_lines: deque[str] = deque(maxlen=200)

        def pump_stdout() -> None:
            try:
                for line in process.stdout or ():
                    stdout_lines.put(line)
            finally:
                stdout_lines.put(None)

        def pump_stderr() -> None:
            for line in process.stderr or ():
                stderr_lines.append(line)

        stdout_thread = threading.Thread(target=pump_stdout, name="autodev-app-stdout", daemon=True)
        stderr_thread = threading.Thread(target=pump_stderr, name="autodev-app-stderr", daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        deadline = time.monotonic() + self.timeout

        def receive() -> dict[str, Any]:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AppServerUnavailable("app-server protocol timed out")
                wait_for = min(remaining, self.heartbeat_interval)
                try:
                    line = stdout_lines.get(timeout=wait_for)
                    break
                except queue.Empty:
                    if self.progress is not None:
                        self.progress("AutoDev: Planner is still working...")
            if line is None:
                detail = "".join(stderr_lines)
                raise AppServerUnavailable(detail.strip() or "app-server closed stdout")
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise AppServerUnavailable(f"invalid app-server JSON: {line[:200]}") from error
            if not isinstance(value, dict):
                raise AppServerUnavailable("app-server message is not an object")
            return value

        try:
            send({
                "method": "initialize", "id": 0,
                "params": {
                    "clientInfo": {"name": "autodev", "title": "AutoDev", "version": "3.0.0a1"},
                    "capabilities": {"experimentalApi": True},
                },
            })
            initialized = receive()
            if initialized.get("id") != 0 or "error" in initialized:
                raise AppServerUnavailable(f"app-server initialize failed: {initialized}")
            send({"method": "initialized", "params": {}})
            send({
                "method": "thread/start", "id": 1,
                "params": {"cwd": str(request.repository), "approvalPolicy": "never", "sandbox": "read-only"},
            })
            thread_id: str | None = None
            thread_model: str | None = None
            while thread_id is None:
                message = receive()
                if message.get("id") == 1:
                    if "error" in message:
                        raise AppServerUnavailable(f"thread/start failed: {message['error']}")
                    thread = message.get("result", {}).get("thread", {})
                    thread_id = thread.get("id")
                    candidate_model = thread.get("model")
                    thread_model = candidate_model if isinstance(candidate_model, str) else None
            turn_params: dict[str, Any] = {
                "threadId": thread_id,
                "input": [{"type": "text", "text": self._prompt(request)}],
                "outputSchema": json.loads(
                    _read_text("schemas/campaign-proposal.schema.json")
                ),
            }
            if thread_model is not None:
                turn_params["collaborationMode"] = {
                    "mode": "plan",
                    "settings": {
                        "model": thread_model,
                        "reasoning_effort": "medium",
                        "developer_instructions": None,
                    },
                }
            send({
                "method": "turn/start", "id": 2,
                "params": turn_params,
            })
            message_text = ""
            turn_id: str | None = None
            while True:
                message = receive()
                if message.get("method") == "item/tool/requestUserInput" and "id" in message:
                    human_request = request_from_app_server(message.get("params", {}), request.campaign_id)
                    response = self.interaction.request(human_request)
                    if isinstance(response, Pending):
                        raise HumanInputPending(response)
                    send({"id": message["id"], "result": app_server_response(response)})
                elif message.get("method") == "item/agentMessage/delta":
                    message_text += str(message.get("params", {}).get("delta", ""))
                elif message.get("method") == "item/completed":
                    item = message.get("params", {}).get("item", {})
                    if item.get("type") == "agentMessage" and item.get("text"):
                        message_text = str(item["text"])
                elif message.get("method") == "turn/started":
                    turn_id = message.get("params", {}).get("turn", {}).get("id")
                elif message.get("method") == "turn/completed":
                    break
                elif message.get("id") == 2 and "error" in message:
                    raise AppServerUnavailable(f"turn/start failed: {message['error']}")
            rendered = message_text.strip()
            if rendered.startswith("```"):
                rendered = rendered.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            try:
                proposal = json.loads(rendered)
            except json.JSONDecodeError as error:
                raise AppServerUnavailable("Planner did not return JSON") from error
            if not isinstance(proposal, dict):
                raise AppServerUnavailable("Planner proposal is not an object")
            return proposal
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)
            isolated_home.cleanup()


class CodexExecPlanner:
    """Stable fresh `codex exec` fallback when app-server probing fails."""

    def __init__(self, executable: str = "codex") -> None:
        self.engine = CodexExecEngine(executable)

    def plan(self, request: PlannerRequest) -> Mapping[str, Any]:
        artifact = request.repository / ".autodev" / "runs" / f"PLANNER-{uuid.uuid4().hex[:12]}"
        artifact.mkdir(parents=True, exist_ok=False)
        schema = artifact / "output-schema.json"
        schema.write_text(_read_text("schemas/campaign-proposal.schema.json"), encoding="utf-8")
        attempt = AttemptRequest(
            artifact.name, "TASK-000", "planner", request.repository,
            AppServerCodexEngine._prompt(request), schema, artifact,
            ":read-only", "codex-sandbox", 600, 2400,
            request.repository / ".autodev" / "STOP",
        )
        result = self.engine.execute(attempt)
        if result.status != "SUCCESS":
            raise AppServerUnavailable(result.infrastructure_error or result.status)
        return result.proposal


class HybridPlanner:
    def __init__(self, native: AppServerCodexEngine, fallback: CodexExecPlanner) -> None:
        self.native = native
        self.fallback = fallback

    def plan(self, request: PlannerRequest) -> Mapping[str, Any]:
        try:
            return self.native.plan(request)
        except HumanInputPending:
            raise
        except AppServerUnavailable:
            return self.fallback.plan(request)
