"""The narrow execution boundary owned by AutoDev."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class AttemptRequest:
    run_id: str
    task_id: str
    role: str
    workspace: Path
    prompt: str
    output_schema: Path
    artifact_dir: Path
    permission_profile: str = ":workspace"
    runtime_mode: str = "codex-sandbox"
    idle_timeout: int = 600
    hard_timeout: int = 2400
    stop_file: Path | None = None


@dataclass(frozen=True, slots=True)
class EngineResult:
    status: str
    proposal: dict[str, Any] = field(default_factory=dict)
    events: tuple[dict[str, Any], ...] = ()
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    infrastructure_error: str | None = None
    failure_class: str | None = None


class ExecutionEngine(Protocol):
    requires_live_authorization: bool

    def preflight(
        self, workspace: Path, *, permission_profile: str = ":workspace",
        runtime_mode: str = "codex-sandbox",
    ) -> dict[str, Any]: ...

    def execute(self, request: AttemptRequest) -> EngineResult: ...
