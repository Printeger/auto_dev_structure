"""Deterministic engine for CI and controller tests."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from autodev.engines.base import AttemptRequest, EngineResult


class FakeCodexRunner:
    requires_live_authorization = False

    def __init__(
        self,
        results: Iterable[EngineResult],
        changes: Iterable[Mapping[str, str | bytes] | Callable[[AttemptRequest], None] | None] = (),
    ) -> None:
        self._results = deque(results)
        self._changes = deque(changes)
        self.requests: list[AttemptRequest] = []

    def preflight(
        self, workspace: Path, *, permission_profile: str = ":workspace",
        runtime_mode: str = "codex-sandbox",
    ) -> dict[str, object]:
        return {
            "ready": True, "classification": "fake_model_free",
            "message": "FakeCodexRunner requires no runtime preflight",
            "returncode": 0, "output": "",
        }

    def execute(self, request: AttemptRequest) -> EngineResult:
        self.requests.append(request)
        if not self._results:
            return EngineResult(
                "INFRA_FAILURE", infrastructure_error="FakeCodexRunner result queue exhausted",
                failure_class="environment_runtime_failure",
            )
        change = self._changes.popleft() if self._changes else None
        if callable(change):
            change(request)
        elif change:
            for relative, content in change.items():
                path = request.workspace / Path(relative)
                path.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(content, bytes):
                    path.write_bytes(content)
                else:
                    path.write_text(content, encoding="utf-8")
        return self._results.popleft()
