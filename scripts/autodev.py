#!/usr/bin/env python3
"""Standard-library tooling for the AutoDev workflow template."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence


EXIT_OK = 0
EXIT_INVALID = 1
EXIT_NOT_READY = 2
MIN_PYTHON = (3, 11)
TASK_ID_RE = re.compile(r"^TASK-[0-9]{3,}$")
REQUIREMENT_ID_RE = re.compile(r"^REQ-[A-Z0-9][A-Z0-9._-]*$")
PLACEHOLDER_RE = re.compile(r"\{\{[A-Z0-9_]+\}\}")
ROOT = Path(__file__).resolve().parents[1]

TEMPLATE_FILES = (
    "README.md",
    "AGENTS.md",
    "PROJECT.md",
    "docs/WORKFLOW.md",
    "docs/REQUIREMENTS.md",
    "docs/ARCHITECTURE.md",
    "docs/DECISIONS.md",
    "docs/decisions/ADR-TEMPLATE.md",
    ".agent/STATE.json",
    ".agent/STATE.schema.json",
    ".agent/POLICY.json",
    ".agent/ROADMAP.md",
    ".agent/DEBT.md",
    ".agent/HANDOFF.md",
    ".agent/tasks/TASK-TEMPLATE.md",
    ".agent/artifacts/.gitignore",
    ".codex/config.toml",
    ".codex/hooks.example.json",
    ".codex/agents/explorer.toml",
    ".codex/agents/builder.toml",
    ".codex/agents/reviewer.toml",
    "scripts/autodev.py",
    "scripts/hooks/stop_validate.py",
    "tests/test_autodev.py",
)

REQUIRED_DIRS = (
    "docs/decisions",
    ".agent/tasks",
    ".agent/artifacts",
    ".codex/agents",
    "scripts/hooks",
)

READY_CONTRACTS = (
    "PROJECT.md",
    "docs/REQUIREMENTS.md",
    "docs/ARCHITECTURE.md",
    ".agent/ROADMAP.md",
)


class ArgumentParser(argparse.ArgumentParser):
    """Use the documented exit code for command-line errors."""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(EXIT_INVALID, f"{self.prog}: error: {message}\n")


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _read_json(path: Path) -> tuple[Any | None, list[str]]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle), []
    except FileNotFoundError:
        return None, [f"missing file: {path.relative_to(path.parents[1]) if len(path.parents) > 1 else path}"]
    except json.JSONDecodeError as exc:
        return None, [f"invalid JSON in {path}: line {exc.lineno}, column {exc.colno}: {exc.msg}"]
    except OSError as exc:
        return None, [f"cannot read {path}: {exc}"]


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False


def _validate_schema(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Validate the intentionally small JSON Schema subset used by STATE.schema.json."""

    errors: list[str] = []
    expected = schema.get("type")
    if expected is not None:
        allowed = expected if isinstance(expected, list) else [expected]
        if not any(_type_matches(value, item) for item in allowed):
            return [f"{path}: expected type {' | '.join(allowed)}, got {type(value).__name__}"]

    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: value {value!r} is not in {schema['enum']!r}")

    if isinstance(value, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                errors.append(f"{path}: missing required property {key!r}")
        properties = schema.get("properties", {})
        for key, child in value.items():
            if key in properties:
                errors.extend(_validate_schema(child, properties[key], f"{path}.{key}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unknown property {key!r}")

    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors.extend(_validate_schema(item, schema["items"], f"{path}[{index}]"))

    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: string is shorter than {schema['minLength']}")
        pattern = schema.get("pattern")
        if pattern and re.search(pattern, value) is None:
            errors.append(f"{path}: value {value!r} does not match {pattern!r}")
        if schema.get("format") == "date-time":
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                errors.append(f"{path}: value {value!r} is not an ISO 8601 date-time")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: value {value} is below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: value {value} exceeds maximum {schema['maximum']}")
    return errors


def _load_policy(root: Path) -> tuple[dict[str, Any] | None, list[str]]:
    raw, errors = _read_json(root / ".agent" / "POLICY.json")
    if errors:
        return None, errors
    if not isinstance(raw, dict):
        return None, [".agent/POLICY.json: expected an object"]
    limits = raw.get("limits")
    if not isinstance(limits, dict):
        return None, [".agent/POLICY.json: missing object 'limits'"]
    for key in ("max_agent_calls_per_task", "max_reworks_per_task"):
        value = limits.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            errors.append(f".agent/POLICY.json: limits.{key} must be a non-negative integer")
    return raw, errors


def validate_project(root: Path = ROOT, ready: bool = False) -> tuple[list[str], list[str], dict[str, Any] | None]:
    """Return state errors, readiness errors, and parsed state without mutating files."""

    root = root.resolve()
    state_path = root / ".agent" / "STATE.json"
    schema_path = root / ".agent" / "STATE.schema.json"
    state, state_errors = _read_json(state_path)
    schema, schema_errors = _read_json(schema_path)
    errors = [*state_errors, *schema_errors]

    if state is not None and not isinstance(state, dict):
        errors.append(".agent/STATE.json: expected an object")
    if schema is not None and not isinstance(schema, dict):
        errors.append(".agent/STATE.schema.json: expected an object")
    if isinstance(state, dict) and isinstance(schema, dict):
        errors.extend(_validate_schema(state, schema))

    policy, policy_errors = _load_policy(root)
    errors.extend(policy_errors)

    if isinstance(state, dict) and isinstance(policy, dict):
        status = state.get("project_status")
        blocker = state.get("blocker")
        next_owner = state.get("next_owner")
        current_task = state.get("current_task_id")
        next_action = state.get("next_action")

        if status == "BLOCKED":
            if not isinstance(blocker, str) or not blocker.strip():
                errors.append("$.blocker: BLOCKED requires a non-empty blocker")
            if next_owner != "HUMAN":
                errors.append("$.next_owner: BLOCKED requires HUMAN")
            if not isinstance(next_action, str) or not next_action.strip():
                errors.append("$.next_action: BLOCKED requires a concrete next action")
        elif blocker is not None:
            errors.append("$.blocker: must be null unless project_status is BLOCKED")

        if status == "COMPLETE":
            if current_task is not None:
                errors.append("$.current_task_id: COMPLETE cannot have a current Task")
            if next_owner != "NONE":
                errors.append("$.next_owner: COMPLETE requires NONE")

        if state.get("last_outcome") == "BLOCKED" and status != "BLOCKED":
            errors.append("$.last_outcome: BLOCKED outcome requires project_status BLOCKED")

        if isinstance(current_task, str):
            task_path = root / ".agent" / "tasks" / f"{current_task}.md"
            if not task_path.is_file():
                errors.append(f"$.current_task_id: Task file does not exist: .agent/tasks/{current_task}.md")

        limits = policy.get("limits", {})
        calls = state.get("agent_calls")
        reworks = state.get("rework_count")
        max_calls = limits.get("max_agent_calls_per_task")
        max_reworks = limits.get("max_reworks_per_task")
        if isinstance(calls, int) and isinstance(max_calls, int) and calls > max_calls:
            errors.append(f"$.agent_calls: {calls} exceeds policy maximum {max_calls}")
        if isinstance(reworks, int) and isinstance(max_reworks, int) and reworks > max_reworks:
            errors.append(f"$.rework_count: {reworks} exceeds policy maximum {max_reworks}")
        if isinstance(calls, int) and isinstance(reworks, int) and reworks > calls:
            errors.append("$.rework_count: cannot exceed agent_calls")

    readiness_errors: list[str] = []
    if ready:
        if not isinstance(state, dict) or state.get("project_status") == "BOOTSTRAP":
            readiness_errors.append("project_status must leave BOOTSTRAP before the project is ready")
        for relative in READY_CONTRACTS:
            path = root / relative
            try:
                content = path.read_text(encoding="utf-8")
            except OSError as exc:
                readiness_errors.append(f"cannot read required contract {relative}: {exc}")
                continue
            markers = sorted(set(PLACEHOLDER_RE.findall(content)))
            if markers:
                readiness_errors.append(f"{relative}: unresolved required placeholders: {', '.join(markers)}")

        requirements_path = root / "docs" / "REQUIREMENTS.md"
        architecture_path = root / "docs" / "ARCHITECTURE.md"
        try:
            requirements = requirements_path.read_text(encoding="utf-8")
            if re.search(r"\bREQ-[A-Z0-9][A-Z0-9._-]*\b", requirements) is None:
                readiness_errors.append("docs/REQUIREMENTS.md: no stable REQ-* ID found")
        except OSError:
            pass
        try:
            architecture = architecture_path.read_text(encoding="utf-8")
            if re.search(r"^## .+ — (FROZEN|PROVISIONAL|OPEN)$", architecture, re.MULTILINE) is None:
                readiness_errors.append("docs/ARCHITECTURE.md: no design section has FROZEN, PROVISIONAL, or OPEN status")
        except OSError:
            pass

    return errors, readiness_errors, state if isinstance(state, dict) else None


def _print_validation(
    errors: list[str], readiness_errors: list[str], as_json: bool, readiness_required: bool
) -> None:
    payload = {
        "valid": not errors,
        "ready": not errors and not readiness_errors,
        "errors": errors,
        "readiness_errors": readiness_errors,
    }
    if as_json:
        print(_json_dump(payload))
        return
    if errors:
        print("Validation failed:")
        for error in errors:
            print(f"- {error}")
    elif readiness_required and readiness_errors:
        print("State is valid, but the project is not ready:")
        for error in readiness_errors:
            print(f"- {error}")
    else:
        print("Validation passed." + (" Project is ready." if readiness_required else ""))


def cmd_validate(args: argparse.Namespace) -> int:
    # Always calculate readiness so JSON status is truthful; only --ready makes
    # readiness failures affect human output and the exit code.
    errors, readiness_errors, _ = validate_project(ROOT, ready=True)
    _print_validation(errors, readiness_errors, args.json, args.ready)
    if errors:
        return EXIT_INVALID
    if args.ready and readiness_errors:
        return EXIT_NOT_READY
    return EXIT_OK


def _template_sources() -> tuple[list[tuple[str, Path]], list[str]]:
    sources: list[tuple[str, Path]] = []
    missing: list[str] = []
    for relative in TEMPLATE_FILES:
        path = ROOT / relative
        if path.is_file():
            sources.append((relative, path))
        else:
            missing.append(relative)
    return sources, missing


def _initialized_state(source: Path, project_name: str) -> str:
    """Create a clean V1 bootstrap state instead of copying this repository's live state."""

    raw = json.loads(source.read_text(encoding="utf-8"))
    raw.update(
        project_name=project_name,
        project_status="BOOTSTRAP",
        quality_mode="BUILD",
        phase="IDLE",
        last_outcome=None,
        current_milestone=None,
        current_task_id=None,
        last_good_commit=None,
        agent_calls=0,
        rework_count=0,
        blocker=None,
        next_action="Complete required project contracts, then run validate --ready.",
        next_owner="COMMANDER",
        updated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )
    return _json_dump(raw) + "\n"


def cmd_init(args: argparse.Namespace) -> int:
    name = args.name.strip()
    if not name or "\n" in name or "\r" in name:
        print("error: --name must be a non-empty single-line value", file=sys.stderr)
        return EXIT_INVALID

    target = Path(args.target).expanduser().resolve()
    sources, missing = _template_sources()
    if missing:
        print("error: template is incomplete:", file=sys.stderr)
        for relative in missing:
            print(f"- {relative}", file=sys.stderr)
        return EXIT_INVALID
    if target.exists() and not target.is_dir():
        print(f"error: target is not a directory: {target}", file=sys.stderr)
        return EXIT_INVALID

    conflicts = [relative for relative, _ in sources if (target / relative).exists()]
    if conflicts and not args.merge:
        print("Initialization stopped before writing because these paths already exist:")
        for relative in conflicts:
            print(f"- {relative}")
        print("Use --merge to copy only missing files; existing files will still not be overwritten.")
        return EXIT_NOT_READY

    copied: list[str] = []
    for relative, source in sources:
        destination = target / relative
        if destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if relative == ".agent/STATE.json":
            content = _initialized_state(source, name)
        else:
            content = source.read_text(encoding="utf-8").replace("{{PROJECT_NAME}}", name)
            if relative == "README.md" and name not in content:
                content = f"<!-- Initialized project: {name} -->\n" + content
        destination.write_text(content, encoding="utf-8")
        shutil.copymode(source, destination)
        copied.append(relative)

    print(f"Initialized {len(copied)} workflow files in {target} for project {name!r}.")
    if conflicts:
        print("Existing files were preserved and require manual review/merge:")
        for relative in conflicts:
            print(f"- {relative}")
        return EXIT_NOT_READY
    return EXIT_OK


def _command_version(command: str, *arguments: str) -> tuple[bool, str]:
    executable = shutil.which(command)
    if not executable:
        return False, "not found on PATH"
    try:
        result = subprocess.run(
            [executable, *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    output = (result.stdout or result.stderr).strip().splitlines()
    return result.returncode == 0, output[0] if output else f"exit {result.returncode}"


def cmd_doctor(_args: argparse.Namespace) -> int:
    checks: list[tuple[str, bool, str]] = []
    checks.append((
        "Python",
        sys.version_info >= MIN_PYTHON,
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro} (requires 3.11+)",
    ))
    for label, command, arguments in (
        ("Git", "git", ("--version",)),
        ("Codex CLI", "codex", ("--version",)),
    ):
        ok, detail = _command_version(command, *arguments)
        checks.append((label, ok, detail))

    try:
        import tomllib

        toml_paths = [ROOT / ".codex" / "config.toml", *(ROOT / ".codex" / "agents").glob("*.toml")]
        toml_errors: list[str] = []
        for path in toml_paths:
            try:
                with path.open("rb") as handle:
                    tomllib.load(handle)
            except (OSError, tomllib.TOMLDecodeError) as exc:
                toml_errors.append(f"{path.relative_to(ROOT)}: {exc}")
        checks.append(("TOML", not toml_errors, "; ".join(toml_errors) or f"parsed {len(toml_paths)} files"))
    except ImportError as exc:  # pragma: no cover - Python 3.11+ always has tomllib
        checks.append(("TOML", False, str(exc)))

    missing_dirs = [relative for relative in REQUIRED_DIRS if not (ROOT / relative).is_dir()]
    checks.append(("Directories", not missing_dirs, ", ".join(missing_dirs) or "workflow structure present"))

    hook, hook_errors = _read_json(ROOT / ".codex" / "hooks.example.json")
    hook_valid = isinstance(hook, dict) and not hook_errors and isinstance(hook.get("hooks"), dict)
    checks.append(("Hook example", hook_valid, "; ".join(hook_errors) or ("valid JSON" if hook_valid else "missing hooks object")))

    state_errors, _, _ = validate_project(ROOT, ready=False)
    checks.append(("State contract", not state_errors, "; ".join(state_errors) or "valid"))

    for label, ok, detail in checks:
        print(f"[{'OK' if ok else 'FAIL'}] {label}: {detail}")
    return EXIT_OK if all(ok for _, ok, _ in checks) else EXIT_INVALID


def cmd_status(args: argparse.Namespace) -> int:
    errors, _, state = validate_project(ROOT, ready=False)
    if errors or state is None:
        if args.json:
            print(_json_dump({"valid": False, "errors": errors}))
        else:
            print("Cannot show trustworthy status because validation failed:")
            for error in errors:
                print(f"- {error}")
        return EXIT_INVALID

    fields = (
        "project_name",
        "project_status",
        "quality_mode",
        "phase",
        "last_outcome",
        "current_milestone",
        "current_task_id",
        "agent_calls",
        "rework_count",
        "blocker",
        "next_action",
        "next_owner",
        "last_good_commit",
        "updated_at",
    )
    summary = {field: state.get(field) for field in fields}
    if args.json:
        print(_json_dump(summary))
    else:
        for field, value in summary.items():
            print(f"{field}: {value if value is not None else '-'}")
    return EXIT_OK


def _parse_requirements(values: Iterable[str]) -> tuple[list[str], list[str]]:
    ids: list[str] = []
    invalid: list[str] = []
    for value in values:
        for item in re.split(r"[\s,]+", value.strip()):
            if not item:
                continue
            normalized = item.upper()
            if REQUIREMENT_ID_RE.fullmatch(normalized):
                if normalized not in ids:
                    ids.append(normalized)
            else:
                invalid.append(item)
    return ids, invalid


def cmd_new_task(args: argparse.Namespace) -> int:
    task_id = args.id
    if not TASK_ID_RE.fullmatch(task_id):
        print("error: Task ID must match TASK-\\d{3,}", file=sys.stderr)
        return EXIT_INVALID
    if not args.title.strip() or "\n" in args.title or "\r" in args.title:
        print("error: --title must be a non-empty single-line value", file=sys.stderr)
        return EXIT_INVALID
    requirement_ids, invalid = _parse_requirements(args.requirements or [])
    if invalid:
        print(f"error: invalid requirement IDs: {', '.join(invalid)}", file=sys.stderr)
        return EXIT_INVALID

    errors, _, state = validate_project(ROOT, ready=False)
    if errors or state is None:
        print("error: current state is invalid; fix it before creating a Task", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return EXIT_INVALID

    destination = ROOT / ".agent" / "tasks" / f"{task_id}.md"
    if destination.exists():
        print(f"error: Task already exists: {destination.relative_to(ROOT)}", file=sys.stderr)
        return EXIT_INVALID

    requirements_line = ", ".join(requirement_ids) if requirement_ids else "none specified"
    requirement_items = "\n".join(f"- `{item}`: define relevant behavior." for item in requirement_ids)
    if not requirement_items:
        requirement_items = "- Add stable `REQ-*` IDs before marking this Task READY."
    milestone = state.get("current_milestone") or "unassigned"
    content = f"""# {task_id}: {args.title.strip()}

- Risk: `{args.risk}`
- Quality mode: `{state.get('quality_mode', 'BUILD')}`
- Requirements: `{requirements_line}`
- Milestone: `{milestone}`
- Status: `DRAFT`

## Objective

Define one measurable outcome.

## Requirements

{requirement_items}

## Inputs

- List only the files, interfaces, ADRs, fixtures, and commands required for this Task.

## In Scope

- Define allowed behavior and modules.

## Out of Scope

- Define adjacent behavior that must not be added.

## Acceptance Criteria

- AC1: Define an observable result.

## Mandatory Tests

- Add exact commands and expected signals.

## Do Not

- Do not commit, push, deploy, mutate remote systems, or expand scope.

## Evidence

- Provide the diff and exact mandatory-test results for acceptance.

## Output Contract

Return exactly these headings:

- `STATUS`
- `FILES_CHANGED`
- `TEST_RESULTS`
- `ACCEPTANCE_EVIDENCE`
- `RISKS`
- `STATE_UPDATE_PROPOSAL`
"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    print(f"Created {destination.relative_to(ROOT)}")
    return EXIT_OK


def cmd_prompt(_args: argparse.Namespace) -> int:
    errors, _, state = validate_project(ROOT, ready=False)
    if errors or state is None:
        print("Cannot generate a Commander prompt from invalid state:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return EXIT_INVALID

    status = state["project_status"]
    task = state.get("current_task_id")
    milestone = state.get("current_milestone")
    if status == "BOOTSTRAP":
        objective = (
            "Complete the required placeholders in PROJECT.md, docs/REQUIREMENTS.md, "
            "docs/ARCHITECTURE.md, and .agent/ROADMAP.md. Then propose a valid transition "
            "from BOOTSTRAP and verify it with `python3 scripts/autodev.py validate --ready`."
        )
    elif status == "BLOCKED":
        objective = (
            f"Do not resume implementation. Present the blocker and request only the minimum human action needed: "
            f"{state.get('blocker')}. Next action: {state.get('next_action')}."
        )
    elif status == "COMPLETE":
        objective = "Verify the COMPLETE invariants and summarize final evidence. Do not select new work."
    elif task:
        objective = f"Resume {task} from `.agent/tasks/{task}.md` and execute the next action below."
    else:
        objective = "Select the smallest ready vertical Task for the current milestone; do not implement before its contract is READY."

    print(
        f"""Act as Commander for {state['project_name']}.

Read in order:
1. AGENTS.md
2. PROJECT.md
3. .agent/STATE.json and .agent/POLICY.json
4. The relevant section of .agent/ROADMAP.md
5. {f'.agent/tasks/{task}.md' if task else 'No current Task; inspect only candidate requirement IDs needed to select one.'}
6. Only the referenced requirements, architecture sections, ADRs, code, tests, and current diff

Current state: status={status}, quality={state['quality_mode']}, phase={state['phase']}, milestone={milestone or '-'}, task={task or '-'}.
Last outcome: {state.get('last_outcome') or '-'}.
Next owner/action: {state.get('next_owner')} — {state.get('next_action') or '-'}.

Objective:
{objective}

Follow the risk gates and budgets in AGENTS.md and POLICY.json. Use only one Builder at a time. Explorer and Reviewer are read-only. Validate every state transition. Do not commit, push, deploy, mutate remote systems, or start unattended Codex loops without explicit user authorization. End with evidence, the resulting outcome, and the exact next state/action.
"""
    )
    return EXIT_OK


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(description="Manage the persisted AutoDev workflow state.")
    subparsers = parser.add_subparsers(dest="command", required=True, parser_class=ArgumentParser)

    init_parser = subparsers.add_parser("init", help="copy the workflow into a target project")
    init_parser.add_argument("target")
    init_parser.add_argument("--name", required=True)
    init_parser.add_argument("--merge", action="store_true")
    init_parser.set_defaults(handler=cmd_init)

    doctor_parser = subparsers.add_parser("doctor", help="check local tools and workflow structure")
    doctor_parser.set_defaults(handler=cmd_doctor)

    validate_parser = subparsers.add_parser("validate", help="validate state and cross-file invariants")
    validate_parser.add_argument("--ready", action="store_true")
    validate_parser.add_argument("--json", action="store_true")
    validate_parser.set_defaults(handler=cmd_validate)

    status_parser = subparsers.add_parser("status", help="show current validated state")
    status_parser.add_argument("--json", action="store_true")
    status_parser.set_defaults(handler=cmd_status)

    task_parser = subparsers.add_parser("new-task", help="create a non-overwriting Task contract")
    task_parser.add_argument("--id", required=True)
    task_parser.add_argument("--title", required=True)
    task_parser.add_argument("--risk", required=True, type=str.upper, choices=("LOW", "MEDIUM", "HIGH"))
    task_parser.add_argument("--requirements", nargs="*", default=[])
    task_parser.set_defaults(handler=cmd_new_task)

    prompt_parser = subparsers.add_parser("prompt", help="generate a Commander start/resume prompt")
    prompt_parser.set_defaults(handler=cmd_prompt)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except BrokenPipeError:
        return EXIT_OK
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INVALID


if __name__ == "__main__":
    raise SystemExit(main())
