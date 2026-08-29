"""Human interaction seam with TTY, persistent, App Server, and fake adapters."""

from __future__ import annotations

import json
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TextIO

from autodev._workspace import _write_json_atomic


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class HumanOption:
    label: str
    description: str


@dataclass(frozen=True, slots=True)
class HumanQuestion:
    id: str
    header: str
    question: str
    options: tuple[HumanOption, ...] = ()
    allow_other: bool = True
    is_secret: bool = False


@dataclass(frozen=True, slots=True)
class HumanRequest:
    campaign_id: str
    questions: tuple[HumanQuestion, ...]
    request_id: str = field(default_factory=lambda: f"HUMAN-{uuid.uuid4().hex[:12]}")
    auto_resolution_ms: int | None = None

    def __post_init__(self) -> None:
        if not 1 <= len(self.questions) <= 3:
            raise ValueError("HumanRequest requires 1-3 questions")
        for question in self.questions:
            if len(question.header) > 12:
                raise ValueError("question header must be 12 characters or fewer")
            if question.options and not 2 <= len(question.options) <= 3:
                raise ValueError("each selectable question requires 2-3 options")


@dataclass(frozen=True, slots=True)
class HumanResponse:
    request_id: str
    answers: Mapping[str, tuple[str, ...]]
    source: str


@dataclass(frozen=True, slots=True)
class Pending:
    request_id: str
    artifact_path: Path
    reason: str


class HumanInteraction(Protocol):
    def request(self, request: HumanRequest) -> HumanResponse | Pending: ...


class FakeHumanInteraction:
    def __init__(self, responses: Sequence[HumanResponse | Pending]) -> None:
        self.responses = list(responses)
        self.requests: list[HumanRequest] = []

    def request(self, request: HumanRequest) -> HumanResponse | Pending:
        self.requests.append(request)
        if not self.responses:
            raise RuntimeError("FakeHumanInteraction has no response")
        return self.responses.pop(0)


class PersistentHumanInteraction:
    """Persist non-secret questions for headless resume and explicit answer."""

    def __init__(self, project_root: Path) -> None:
        self.root = project_root.resolve()

    def _path(self, request: HumanRequest) -> Path:
        return self.root / ".autodev" / "campaigns" / request.campaign_id / "human-requests" / f"{request.request_id}.json"

    @staticmethod
    def _document(request: HumanRequest) -> dict[str, Any]:
        questions = []
        for question in request.questions:
            if question.is_secret:
                questions.append({
                    "id": question.id, "header": "Credential",
                    "question": "Provide the credential through the controlled environment, then resume.",
                    "options": [], "isOther": False, "credential_only": True,
                })
            else:
                questions.append({
                    "id": question.id, "header": question.header, "question": question.question,
                    "options": [
                        {"label": option.label, "description": option.description}
                        for option in question.options
                    ],
                    "isOther": question.allow_other,
                })
        return {
            "$schema": "https://autodev.local/schemas/human-request.schema.json",
            "schema_version": 1, "request_id": request.request_id,
            "campaign_id": request.campaign_id, "questions": questions,
            "status": "PENDING", "created_at": _now(),
        }

    def request(self, request: HumanRequest) -> HumanResponse | Pending:
        path = self._path(request)
        if path.exists():
            document = json.loads(path.read_text(encoding="utf-8"))
            if document.get("status") in {"ANSWERED", "AUTO_RESOLVED"}:
                answers = {
                    key: tuple(value) for key, value in document.get("answers", {}).items()
                }
                return HumanResponse(request.request_id, answers, "persistent")
        else:
            _write_json_atomic(path, self._document(request))
        reason = (
            "credential must be supplied through a controlled environment"
            if any(item.is_secret for item in request.questions)
            else "no interactive TTY is available"
        )
        return Pending(request.request_id, path, reason)

    def answer(self, campaign_id: str, request_id: str, answers: Mapping[str, Sequence[str]]) -> HumanResponse:
        path = self.root / ".autodev" / "campaigns" / campaign_id / "human-requests" / f"{request_id}.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("status") in {"ANSWERED", "AUTO_RESOLVED"}:
            existing = {
                key: tuple(str(item) for item in value)
                for key, value in document.get("answers", {}).items()
            }
            requested = {key: tuple(str(item) for item in value) for key, value in answers.items()}
            if existing != requested:
                raise ValueError("human request was already answered differently")
            return HumanResponse(request_id, existing, "persistent")
        if document.get("status") != "PENDING":
            raise ValueError("human request is not pending")
        known = {question["id"] for question in document["questions"]}
        if any(question.get("credential_only") for question in document["questions"]):
            raise ValueError("credential answers are never persisted")
        if set(answers) != known:
            raise ValueError("answers must cover exactly the pending question IDs")
        normalized = {
            key: [str(item) for item in value if str(item).strip()]
            for key, value in answers.items()
        }
        if any(not value for value in normalized.values()):
            raise ValueError("answers must be non-empty")
        document["answers"] = normalized
        document["status"] = "ANSWERED"
        document["answered_at"] = _now()
        # answered_at is operational metadata; the packaged schema deliberately
        # validates the request envelope before the response is appended.
        _write_json_atomic(path, document)
        return HumanResponse(request_id, {key: tuple(value) for key, value in normalized.items()}, "persistent")


class TTYHumanInteraction:
    def __init__(self, *, input_stream: TextIO | None = None, output_stream: TextIO | None = None) -> None:
        self.input = input_stream or sys.stdin
        self.output = output_stream or sys.stdout

    def request(self, request: HumanRequest) -> HumanResponse | Pending:
        if not getattr(self.input, "isatty", lambda: False)():
            raise RuntimeError("TTYHumanInteraction requires a TTY")
        answers: dict[str, tuple[str, ...]] = {}
        for question in request.questions:
            if question.is_secret:
                raise RuntimeError("secrets must be supplied through a controlled environment")
            self.output.write(f"[{question.header}] {question.question}\n")
            for index, option in enumerate(question.options, 1):
                self.output.write(f"  {index}. {option.label} — {option.description}\n")
            if question.allow_other:
                self.output.write("  Enter free-form text, or an option number.\n")
            self.output.flush()
            raw = self.input.readline().strip()
            if raw.isdigit() and 1 <= int(raw) <= len(question.options):
                answer = question.options[int(raw) - 1].label
            elif question.allow_other and raw:
                answer = raw
            else:
                raise ValueError(f"invalid answer for {question.id}")
            answers[question.id] = (answer,)
        return HumanResponse(request.request_id, answers, "tty")


class AutoResolvingHumanInteraction:
    """Apply the recommended first option after a declared non-blocking timeout."""

    def __init__(self, delegate: HumanInteraction, *, sleeper: Any = time.sleep) -> None:
        self.delegate = delegate
        self.sleeper = sleeper

    def request(self, request: HumanRequest) -> HumanResponse | Pending:
        result = self.delegate.request(request)
        if not isinstance(result, Pending) or request.auto_resolution_ms is None:
            return result
        if any(not question.options or question.is_secret for question in request.questions):
            return result
        self.sleeper(request.auto_resolution_ms / 1000)
        answers = {question.id: (question.options[0].label,) for question in request.questions}
        try:
            document = json.loads(result.artifact_path.read_text(encoding="utf-8"))
            document["answers"] = {key: list(value) for key, value in answers.items()}
            document["status"] = "AUTO_RESOLVED"
            document["answered_at"] = _now()
            _write_json_atomic(result.artifact_path, document)
        except (OSError, json.JSONDecodeError):
            pass
        return HumanResponse(request.request_id, answers, "timeout-default")


def request_from_app_server(params: Mapping[str, Any], campaign_id: str) -> HumanRequest:
    """Translate the versioned app-server request into the stable seam."""

    questions = tuple(
        HumanQuestion(
            id=str(item["id"]), header=str(item["header"]), question=str(item["question"]),
            options=tuple(
                HumanOption(str(option["label"]), str(option["description"]))
                for option in (item.get("options") or [])
            ),
            allow_other=bool(item.get("isOther", False)),
            is_secret=bool(item.get("isSecret", False)),
        )
        for item in params["questions"]
    )
    return HumanRequest(
        campaign_id=campaign_id, questions=questions,
        request_id=str(params.get("itemId") or f"HUMAN-{uuid.uuid4().hex[:12]}"),
        auto_resolution_ms=params.get("autoResolutionMs"),
    )


def app_server_response(response: HumanResponse) -> dict[str, Any]:
    return {
        "answers": {
            question_id: {"answers": list(values)}
            for question_id, values in response.answers.items()
        }
    }
