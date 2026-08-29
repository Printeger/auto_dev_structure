"""Supervised non-interactive Codex CLI adapter."""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from autodev.engines.base import AttemptRequest, EngineResult


class CodexExecEngine:
    requires_live_authorization = True

    def __init__(self, executable: str = "codex") -> None:
        self.executable = executable
        self._preflight_cache: dict[tuple[Path, str, str], dict[str, Any]] = {}

    @staticmethod
    def _permission_override(permission_profile: str) -> str:
        return f"default_permissions={json.dumps(permission_profile)}"

    def _command(
        self, *, permission_profile: str, output_schema: Path, workspace: Path,
        final_argument: str,
    ) -> list[str]:
        command = [
            self.executable,
            "--ask-for-approval", "never",
            "-c", self._permission_override(permission_profile),
            "-c", "mcp_servers={}",
            "-c", "hooks={}",
        ]
        command.extend([
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--json",
            "--output-schema", str(output_schema),
            "-C", str(workspace),
            final_argument,
        ])
        return command

    def command(self, request: AttemptRequest) -> list[str]:
        if request.runtime_mode not in {"codex-sandbox", "external-sandbox"}:
            raise ValueError(f"unsupported runtime mode: {request.runtime_mode}")
        permission_profile = (
            ":danger-full-access"
            if request.runtime_mode == "external-sandbox"
            else request.permission_profile
        )
        return self._command(
            permission_profile=permission_profile,
            output_schema=request.output_schema,
            workspace=request.workspace,
            final_argument=request.prompt,
        )

    @staticmethod
    def _classify_runtime_failure(output: str) -> str:
        lowered = output.lower()
        if "legacy" in lowered and "landlock" in lowered and (
            "direct runtime enforcement" in lowered or "incompatible" in lowered
        ):
            return "legacy_landlock_incompatibility"
        if (
            "rtm_newaddr" in lowered
            or ("operation not permitted" in lowered and any(
                term in lowered for term in ("namespace", "bwrap", "bubblewrap", "loopback")
            ))
            or "nested sandbox" in lowered
        ):
            return "nested_sandbox_restriction"
        if any(term in lowered for term in (
            "bwrap", "bubblewrap", "user namespace", "unprivileged userns", "apparmor",
        )):
            return "bubblewrap_bootstrap_failure"
        if any(term in lowered for term in (
            "configuration", "config.toml", "invalid toml", "unknown option",
            "unrecognized option", "unrecognized subcommand", "usage:",
        )):
            return "codex_configuration_error"
        return "environment_runtime_failure"

    @classmethod
    def _structured_failure_detail(cls, events: list[dict[str, Any]]) -> str | None:
        def message(value: Any) -> str | None:
            if isinstance(value, dict):
                nested = value.get("error")
                if nested is not None:
                    detail = message(nested)
                    if detail:
                        return detail
                direct = value.get("message")
                if direct is not None:
                    return message(direct)
            elif isinstance(value, str):
                rendered = value.strip()
                if not rendered:
                    return None
                try:
                    decoded = json.loads(rendered)
                except json.JSONDecodeError:
                    return rendered
                return message(decoded) or rendered
            return None

        for event in reversed(events):
            if event.get("type") in {"error", "turn.failed"}:
                detail = message(event)
                if detail:
                    return detail[:2000]
        return None

    def preflight(
        self, workspace: Path, *, permission_profile: str = ":workspace",
        runtime_mode: str = "codex-sandbox",
    ) -> dict[str, Any]:
        workspace = workspace.resolve()
        key = (workspace, permission_profile, runtime_mode)
        if runtime_mode not in {"codex-sandbox", "external-sandbox"}:
            return {
                "ready": False,
                "classification": "codex_configuration_error",
                "message": f"unsupported runtime mode: {runtime_mode}",
                "returncode": None,
                "output": "",
            }
        if runtime_mode == "external-sandbox":
            ready = os.environ.get("AUTODEV_EXTERNAL_SANDBOX") == "1"
            diagnostic = {
                "ready": ready,
                "classification": "external_sandbox" if ready else "external_sandbox_not_confirmed",
                "message": (
                    "trusted external sandbox explicitly confirmed"
                    if ready else "external-sandbox requires AUTODEV_EXTERNAL_SANDBOX=1"
                ),
                "returncode": None,
                "output": "",
            }
            return diagnostic
        if key in self._preflight_cache:
            return self._preflight_cache[key]
        if not sys.platform.startswith("linux"):
            diagnostic = {
                "ready": True, "classification": "not_applicable",
                "message": "Linux sandbox preflight is not applicable", "returncode": 0,
                "output": "",
            }
            self._preflight_cache[key] = diagnostic
            return diagnostic
        command = [
            self.executable,
            "-c", self._permission_override(permission_profile),
            "-C", str(workspace),
            "sandbox", "--", "/bin/true",
        ]
        try:
            result = subprocess.run(
                command, cwd=workspace, check=False, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, timeout=15,
            )
        except OSError as error:
            diagnostic = {
                "ready": False, "classification": "codex_configuration_error",
                "message": str(error), "returncode": None, "output": str(error),
            }
            return diagnostic
        except subprocess.TimeoutExpired as error:
            return {
                "ready": False, "classification": "environment_runtime_failure",
                "message": f"Codex Linux sandbox preflight timed out: {error}",
                "returncode": None, "output": str(error),
            }
        output = result.stdout[-2000:]
        ready = result.returncode == 0
        diagnostic = {
            "ready": ready,
            "classification": "ready" if ready else self._classify_runtime_failure(output),
            "message": "Codex Linux sandbox preflight passed" if ready else output.strip(),
            "returncode": result.returncode,
            "output": output,
            "command": "codex sandbox -- /bin/true",
        }
        if ready:
            self._preflight_cache[key] = diagnostic
        return diagnostic

    def probe(
        self, *, workspace: Path | None = None,
        permission_profile: str = ":workspace", runtime_mode: str = "codex-sandbox",
    ) -> dict[str, Any]:
        workspace = (workspace or Path.cwd()).resolve()
        try:
            parse_result = subprocess.run(
                self._command(
                    permission_profile=permission_profile, output_schema=Path(os.devnull),
                    workspace=workspace, final_argument="--help",
                ),
                check=False,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=15,
            )
            config_result = subprocess.run(
                self._command(
                    permission_profile=permission_profile, output_schema=Path(os.devnull),
                    workspace=workspace, final_argument="--version",
                ),
                check=False,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=15,
            )
            login_result = subprocess.run(
                [self.executable, "login", "status"], check=False,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            failed = {"ready": False, "error": str(error)}
            return {
                "ready": False, "command_parse": failed, "login": failed,
                "sandbox_preflight": failed,
            }
        command_parse = {
            "ready": parse_result.returncode == 0 and config_result.returncode == 0,
            "returncode": parse_result.returncode,
            "config_returncode": config_result.returncode,
            "output": (parse_result.stdout + "\n" + config_result.stdout)[-2000:],
        }
        login = {
            "ready": login_result.returncode == 0,
            "returncode": login_result.returncode,
            "output": login_result.stdout[-500:],
        }
        sandbox_preflight = self.preflight(
            workspace, permission_profile=permission_profile, runtime_mode=runtime_mode,
        )
        return {
            "ready": command_parse["ready"] and login["ready"] and sandbox_preflight["ready"],
            "command_parse": command_parse,
            "login": login,
            "sandbox_preflight": sandbox_preflight,
        }

    @staticmethod
    def _proposal(events: list[dict[str, Any]]) -> dict[str, Any]:
        for event in reversed(events):
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            candidates = [
                event.get("proposal"), event.get("result"), event.get("output"),
                item.get("text"), item.get("content"), event.get("message"),
            ]
            for candidate in candidates:
                if isinstance(candidate, dict) and "outcome" in candidate:
                    return candidate
                if isinstance(candidate, str):
                    try:
                        parsed = json.loads(candidate)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(parsed, dict) and "outcome" in parsed:
                        return parsed
        return {}

    def execute(self, request: AttemptRequest) -> EngineResult:
        if os.environ.get("AUTODEV_LIVE_CODEX") != "1":
            return EngineResult(
                "NOT_READY",
                infrastructure_error="live Codex requires AUTODEV_LIVE_CODEX=1",
            )
        preflight = self.preflight(
            request.workspace, permission_profile=request.permission_profile,
            runtime_mode=request.runtime_mode,
        )
        if not preflight["ready"]:
            return EngineResult(
                "INFRA_FAILURE", infrastructure_error=preflight["message"],
                failure_class=preflight["classification"],
            )
        request.artifact_dir.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                self.command(request), cwd=request.workspace, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=False, start_new_session=True,
            )
        except OSError as error:
            return EngineResult(
                "INFRA_FAILURE", infrastructure_error=str(error),
                failure_class="codex_configuration_error",
            )
        assert process.stdout is not None and process.stderr is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        last_activity = started
        terminal_status: str | None = None
        while process.poll() is None:
            now = time.monotonic()
            if request.stop_file and request.stop_file.exists():
                terminal_status = "STOPPED"
                break
            if now - started > request.hard_timeout:
                terminal_status = "INFRA_FAILURE"
                break
            if now - last_activity > request.idle_timeout:
                terminal_status = "INFRA_FAILURE"
                break
            for key, _ in selector.select(timeout=0.25):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if chunk:
                    buffers[key.data].extend(chunk)
                    last_activity = time.monotonic()
        if terminal_status is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGINT)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        remaining_out, remaining_err = process.communicate()
        buffers["stdout"].extend(remaining_out or b"")
        buffers["stderr"].extend(remaining_err or b"")
        stdout = buffers["stdout"].decode("utf-8", errors="replace")
        stderr = buffers["stderr"].decode("utf-8", errors="replace")
        (request.artifact_dir / "events.jsonl").write_text(stdout, encoding="utf-8")
        (request.artifact_dir / "stderr.log").write_text(stderr, encoding="utf-8")
        events: list[dict[str, Any]] = []
        protocol_errors: list[str] = []
        for line in stdout.splitlines():
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    events.append(value)
            except json.JSONDecodeError as error:
                protocol_errors.append(str(error))
        duration = time.monotonic() - started
        if terminal_status == "STOPPED":
            return EngineResult(
                "STOPPED", events=tuple(events), stdout=stdout, stderr=stderr,
                duration_seconds=duration,
            )
        if terminal_status or process.returncode != 0:
            failure_detail = self._structured_failure_detail(events)
            return EngineResult(
                "INFRA_FAILURE", events=tuple(events), stdout=stdout, stderr=stderr,
                duration_seconds=duration,
                infrastructure_error=(
                    "timeout" if terminal_status
                    else failure_detail or f"codex exited {process.returncode}"
                ),
                failure_class=self._classify_runtime_failure(stdout + "\n" + stderr),
            )
        proposal = self._proposal(events)
        if not proposal or protocol_errors:
            return EngineResult(
                "INFRA_FAILURE", events=tuple(events), stdout=stdout, stderr=stderr,
                duration_seconds=duration, infrastructure_error="invalid Codex JSONL/proposal protocol",
            )
        return EngineResult("SUCCESS", proposal, tuple(events), stdout, stderr, duration)
