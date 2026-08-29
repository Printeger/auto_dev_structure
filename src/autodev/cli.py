"""Command-line routing for the implemented AutoDev control-plane slice."""

from __future__ import annotations

import argparse
import json
import os
import sys
import platform
import shutil
from datetime import datetime, timezone
from collections.abc import Sequence
from pathlib import Path

from autodev import Command, CommandResult, ControlPlane, __version__
from autodev._project import (
    ProjectOperation,
    apply_migration,
    check_migration,
    initialize_project,
    rollback_migration,
)
from autodev._workspace import _write_json_atomic, git_baseline_status, source_fingerprint
from autodev.engines import CodexExecEngine
from autodev.run_controller import RunController, RunRequest


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _UsageError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="autodev")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("version", help="print the installed AutoDev version")

    doctor = commands.add_parser("doctor", help="probe project and Codex capabilities")
    doctor.add_argument("--json", action="store_true", dest="as_json")

    init = commands.add_parser("init", help="install V2 project contracts and state")
    init.add_argument("target")
    init.add_argument("--name", required=True)
    init.add_argument("--merge", action="store_true")

    migrate = commands.add_parser("migrate", help="migrate explicit V1 state")
    migration_mode = migrate.add_mutually_exclusive_group(required=True)
    migration_mode.add_argument("--check", action="store_true")
    migration_mode.add_argument("--apply", action="store_true")
    migration_mode.add_argument("--rollback", metavar="MIGRATION_ID")

    validate = commands.add_parser("validate", help="validate canonical project contracts")
    validate.add_argument("--ready", action="store_true")
    validate.add_argument("--json", action="store_true", dest="as_json")

    commands.add_parser("activate", help="activate a valid bootstrap project")
    commands.add_parser("complete", help="derive and record project completion")

    status = commands.add_parser("status", help="show canonical project status")
    status.add_argument("--json", action="store_true", dest="as_json")

    task = commands.add_parser("task", help="manage structured Tasks")
    task_commands = task.add_subparsers(dest="task_command", required=True)

    create = task_commands.add_parser("create", help="create a DRAFT Task contract")
    create.add_argument("--id", required=True)
    create.add_argument("--title", required=True)
    create.add_argument("--risk", required=True, choices=("LOW", "MEDIUM", "HIGH"))
    create.add_argument(
        "--quality-mode", required=True, choices=("BUILD", "INTEGRATION", "HARDENING")
    )
    create.add_argument("--requirements", required=True)

    ready = task_commands.add_parser("ready", help="validate and freeze a Task")
    ready.add_argument("task_id")
    show = task_commands.add_parser("show", help="show a Task contract and state")
    show.add_argument("task_id")
    reopen = task_commands.add_parser("reopen", help="reopen a frozen Task")
    reopen.add_argument("task_id")
    reopen.add_argument("--reason", required=True)

    for task_command, help_text in (
        ("defer", "defer a READY Task"), ("block", "block a READY Task"),
        ("unblock", "return a BLOCKED Task to READY"),
    ):
        transition = task_commands.add_parser(task_command, help=help_text)
        transition.add_argument("task_id")

    run = commands.add_parser("run", help="run one Task or an explicit bounded loop")
    run.add_argument("--task")
    run.add_argument("--until", choices=("complete-or-blocked",))
    resume = commands.add_parser("resume", help="resume with a fresh attempt")
    resume.add_argument("--recover-stale", action="store_true")
    commands.add_parser("stop", help="request process-group cancellation")

    checkpoint = commands.add_parser("checkpoint", help="manage source checkpoints")
    checkpoint_commands = checkpoint.add_subparsers(dest="checkpoint_command", required=True)
    checkpoint_commands.add_parser("adopt-existing")

    logs = commands.add_parser("logs", help="show immutable run logs")
    logs.add_argument("--run", required=True, dest="run_id")
    evidence = commands.add_parser("evidence", help="show Task evidence")
    evidence.add_argument("task_id")
    return parser


def _command(arguments: argparse.Namespace) -> Command:
    if arguments.command in {"activate", "complete", "status"}:
        return Command(arguments.command)
    if arguments.command == "validate":
        return Command("validate", {"ready": arguments.ready})
    if arguments.command == "task":
        if arguments.task_command == "create":
            requirement_ids = [item.strip() for item in arguments.requirements.split(",") if item.strip()]
            return Command(
                "task.create",
                {
                    "id": arguments.id,
                    "title": arguments.title,
                    "risk": arguments.risk,
                    "quality_mode": arguments.quality_mode,
                    "requirements": requirement_ids,
                },
            )
        if arguments.task_command == "ready":
            return Command("task.ready", {"id": arguments.task_id})
        if arguments.task_command == "show":
            return Command("task.show", {"id": arguments.task_id})
        if arguments.task_command == "reopen":
            return Command(
                "task.reopen", {"id": arguments.task_id, "reason": arguments.reason}
            )
        targets = {"defer": "DEFERRED", "block": "BLOCKED", "unblock": "READY"}
        if arguments.task_command in targets:
            return Command("task.transition", {"id": arguments.task_id, "to": targets[arguments.task_command]})
    raise AssertionError(f"unhandled parsed command: {arguments}")


def _render_human(command: Command, result: CommandResult) -> str:
    if command.name == "task.show" and result.status == "SUCCESS":
        return json.dumps(result.to_dict()["data"], indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    return result.message + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """Parse, execute, and render one AutoDev command."""

    try:
        arguments = _parser().parse_args(argv)
    except _UsageError as error:
        print(f"autodev: {error}", file=sys.stderr)
        return 1
    if arguments.command == "version":
        print(__version__)
        return 0
    if arguments.command == "doctor":
        try:
            policy = json.loads((Path.cwd() / ".autodev/policy.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            policy = {}
        runtime = {
            "mode": "codex-sandbox",
            "build_permission_profile": ":workspace",
            **policy.get("runtime", {}),
        }
        probe = CodexExecEngine().probe(
            workspace=Path.cwd(),
            permission_profile=runtime["build_permission_profile"],
            runtime_mode=runtime["mode"],
        )
        baseline = git_baseline_status(Path.cwd())
        canonical = ControlPlane(Path.cwd()).execute(Command("validate", {"ready": True}))
        checks = {
            "python": {"ready": sys.version_info >= (3, 11), "version": platform.python_version()},
            "git": {"ready": shutil.which("git") is not None},
            "codex_command_parse": probe.get(
                "command_parse", {"ready": False, "error": probe.get("error", "probe failed")}
            ),
            "codex_login": probe.get(
                "login", {"ready": False, "error": probe.get("error", "login check failed")}
            ),
            "codex_sandbox_preflight": probe.get(
                "sandbox_preflight",
                {"ready": False, "error": probe.get("error", "sandbox preflight failed")},
            ),
            "runtime_policy": {
                "ready": runtime["mode"] in {"codex-sandbox", "external-sandbox"},
                "mode": runtime["mode"],
                "build_permission_profile": runtime["build_permission_profile"],
            },
            "live_authorization": {
                "ready": os.environ.get("AUTODEV_LIVE_CODEX") == "1",
                "required": "AUTODEV_LIVE_CODEX=1",
            },
            "git_head": {
                "ready": baseline["has_head"], "head": baseline["head"],
                "error": baseline["error"] if not baseline["has_head"] else None,
            },
            "clean_baseline": {
                "ready": baseline["clean"], "dirty_paths": baseline["dirty_paths"],
                "error": baseline["error"] if baseline["has_head"] else None,
            },
            "canonical_state": {
                "ready": canonical.status == "SUCCESS",
                "status": canonical.status,
                "message": canonical.message,
            },
        }
        ready = all(item.get("ready", False) for item in checks.values())
        payload = {"ready": ready, "checks": checks}
        if arguments.as_json:
            print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
        else:
            for name, check in checks.items():
                print(f"{name}: {'ready' if check.get('ready') else 'not ready'}")
        return 0 if ready else 2
    if arguments.command in {"run", "resume"} and os.environ.get("AUTODEV_LIVE_CODEX") != "1":
        print("live Codex requires AUTODEV_LIVE_CODEX=1")
        return 2
    operation: ProjectOperation | None = None
    if arguments.command == "init":
        operation = initialize_project(Path(arguments.target), arguments.name, merge=arguments.merge)
    elif arguments.command == "migrate":
        if arguments.check:
            operation = check_migration(Path.cwd())
        elif arguments.apply:
            operation = apply_migration(Path.cwd())
        else:
            operation = rollback_migration(Path.cwd(), arguments.rollback)
    if operation is not None:
        print(operation.message)
        if operation.data:
            print(json.dumps(operation.data, indent=2, sort_keys=True, ensure_ascii=False))
        return operation.exit_code

    if arguments.command in {"run", "resume"}:
        if arguments.command == "resume":
            stop_file = Path.cwd() / ".autodev/STOP"
            stop_file.unlink(missing_ok=True)
            control = ControlPlane(Path.cwd())
            try:
                state = json.loads((Path.cwd() / ".autodev/state.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                print(f"cannot resume: {error}", file=sys.stderr)
                return 5
            if state.get("current_run_id"):
                control.execute(Command("run.finish", {
                    "run_id": state["current_run_id"], "outcome": "INFRA_FAILURE",
                }))
                state = json.loads((Path.cwd() / ".autodev/state.json").read_text(encoding="utf-8"))
            if state.get("project_status") in {"PAUSED", "STOPPED"}:
                activated = control.execute(Command("activate"))
                if activated.status != "SUCCESS":
                    print(activated.message)
                    return activated.exit_code
            request = RunRequest(recover_stale=arguments.recover_stale)
        else:
            request = RunRequest(task_id=arguments.task, until=arguments.until)
        outcome = RunController(Path.cwd(), CodexExecEngine()).run(request)
        print(outcome.message)
        return outcome.exit_code
    if arguments.command == "stop":
        stop_file = Path.cwd() / ".autodev/STOP"
        stop_file.parent.mkdir(parents=True, exist_ok=True)
        stop_file.write_text("STOP\n", encoding="utf-8")
        try:
            state = json.loads((Path.cwd() / ".autodev/state.json").read_text(encoding="utf-8"))
            if state.get("project_status") == "ACTIVE" and state.get("current_run_id") is None:
                ControlPlane(Path.cwd()).execute(Command("project.transition", {"to": "STOPPED"}))
        except (OSError, json.JSONDecodeError):
            pass
        print("STOP requested")
        return 4
    if arguments.command == "checkpoint":
        fingerprint = source_fingerprint(Path.cwd())
        path = Path.cwd() / ".autodev/adopted-source.json"
        _write_json_atomic(path, {"adopted_at": datetime.now(timezone.utc).isoformat(), **fingerprint.to_dict()})
        print(f"adopted existing source: {fingerprint.digest}")
        return 0
    if arguments.command == "logs":
        if not arguments.run_id.replace("-", "").isalnum():
            print("invalid run id", file=sys.stderr)
            return 1
        run_dir = Path.cwd() / ".autodev/runs" / arguments.run_id
        if not run_dir.is_dir():
            print("unknown run", file=sys.stderr)
            return 1
        for path in sorted(run_dir.rglob("*")):
            if path.is_file() and path.suffix in {".json", ".jsonl", ".log"}:
                print(f"== {path.relative_to(run_dir)} ==")
                print(path.read_text(encoding="utf-8", errors="replace"), end="")
        return 0
    if arguments.command == "evidence":
        found = []
        for path in sorted((Path.cwd() / ".autodev/runs").glob("*/evidence.json")):
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("task_id") == arguments.task_id:
                found.append(value)
        print(json.dumps(found, indent=2, sort_keys=True, ensure_ascii=False))
        return 0 if found else 1

    command = _command(arguments)
    result = ControlPlane(Path.cwd()).execute(command)
    if getattr(arguments, "as_json", False):
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True, ensure_ascii=False))
    else:
        print(_render_human(command, result), end="")
    return result.exit_code
