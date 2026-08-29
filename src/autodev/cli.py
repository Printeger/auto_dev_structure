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
    apply_v2_migration,
    apply_v3_migration,
    check_migration,
    check_v2_migration,
    check_v3_migration,
    initialize_project,
    rollback_migration,
    rollback_v2_migration,
    rollback_v3_migration,
)
from autodev._workspace import _write_json_atomic, git_baseline_status, source_fingerprint
from autodev.engines import CodexExecEngine
from autodev.engines.app_server import AppServerCodexEngine, CodexExecPlanner, HybridPlanner
from autodev.campaign import CampaignController, CampaignRequest
from autodev.human import (
    AutoResolvingHumanInteraction, PersistentHumanInteraction, TTYHumanInteraction,
)
from autodev.reporting import render_report
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

    init = commands.add_parser("init", help="install AutoDev project contracts and state")
    init.add_argument("target")
    init.add_argument("--name", required=True)
    init.add_argument("--merge", action="store_true")

    migrate = commands.add_parser("migrate", help="migrate explicit V1, V2, or V3 state")
    migrate.add_argument("migration_version", nargs="?", choices=("v2", "v3"))
    migration_mode = migrate.add_mutually_exclusive_group(required=True)
    migration_mode.add_argument("--check", action="store_true")
    migration_mode.add_argument("--apply", action="store_true")
    migration_mode.add_argument("--rollback", metavar="MIGRATION_ID")
    migrate.add_argument("--adopt-source")

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
    run.add_argument("--until", choices=("complete-or-blocked", "target-or-blocked"))
    resume = commands.add_parser("resume", help="resume with a fresh attempt")
    resume.add_argument("--recover-stale", action="store_true")
    resume.add_argument("--campaign")
    resume.add_argument("--until", choices=("target-or-blocked", "complete-or-blocked"))
    commands.add_parser("stop", help="request process-group cancellation")

    checkpoint = commands.add_parser("checkpoint", help="manage source checkpoints")
    checkpoint_commands = checkpoint.add_subparsers(dest="checkpoint_command", required=True)
    checkpoint_commands.add_parser("adopt-existing")

    logs = commands.add_parser("logs", help="show immutable run logs")
    logs.add_argument("--run", required=True, dest="run_id")
    evidence = commands.add_parser("evidence", help="show Task evidence")
    evidence.add_argument("task_id")

    start = commands.add_parser("start", help="plan, approve, and run a Campaign")
    idea = start.add_mutually_exclusive_group(required=True)
    idea.add_argument("--idea")
    idea.add_argument("--idea-file")
    start.add_argument("--mode", default="staged", choices=("change", "staged", "critical"))
    start.add_argument(
        "--target", default="working-mvp",
        choices=("change-complete", "architecture-baseline", "working-mvp", "integrated-system", "release-candidate"),
    )

    campaign = commands.add_parser("campaign", help="manage V3 Campaigns")
    campaign_commands = campaign.add_subparsers(dest="campaign_command", required=True)
    plan = campaign_commands.add_parser("plan")
    plan_idea = plan.add_mutually_exclusive_group(required=True)
    plan_idea.add_argument("--idea")
    plan_idea.add_argument("--idea-file")
    plan.add_argument("--mode", default="staged", choices=("change", "staged", "critical"))
    plan.add_argument(
        "--target", default="working-mvp",
        choices=("change-complete", "architecture-baseline", "working-mvp", "integrated-system", "release-candidate"),
    )
    approve = campaign_commands.add_parser("approve")
    approve.add_argument("campaign_id")
    approve.add_argument("--proposal-hash", required=True)
    campaign_start = campaign_commands.add_parser("start")
    campaign_start.add_argument("campaign_id")
    campaign_status = campaign_commands.add_parser("status")
    campaign_status.add_argument("campaign_id")
    answer = campaign_commands.add_parser("answer")
    answer.add_argument("campaign_id")
    answer.add_argument("--request", required=True, dest="request_id")
    answer.add_argument("--answer", action="append", required=True, dest="answers")
    retarget = campaign_commands.add_parser("retarget")
    retarget.add_argument("campaign_id")
    retarget.add_argument(
        "--target", required=True,
        choices=("architecture-baseline", "working-mvp", "integrated-system", "release-candidate"),
    )
    for name in ("materialize", "archive"):
        subcommand = campaign_commands.add_parser(name)
        subcommand.add_argument("campaign_id")

    report = commands.add_parser("report", help="derive a Markdown report from canonical evidence")
    report.add_argument("report_kind", choices=("phase", "requirements", "release"))
    report.add_argument("--campaign")
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


def _campaign_request(arguments: argparse.Namespace) -> CampaignRequest:
    if arguments.idea_file:
        idea = Path(arguments.idea_file).read_text(encoding="utf-8")
    else:
        idea = arguments.idea
    mode = arguments.mode.upper()
    target = arguments.target.replace("-", "_").upper()
    if mode == "CHANGE" and arguments.target == "working-mvp":
        target = "CHANGE_COMPLETE"
    return CampaignRequest(idea=idea, mode=mode, target=target)


def _interaction(root: Path):
    persistent = PersistentHumanInteraction(root)
    if sys.stdin.isatty():
        return AutoResolvingHumanInteraction(TTYHumanInteraction())
    return AutoResolvingHumanInteraction(persistent)


def _planner(root: Path, interaction: object | None = None):
    interaction = interaction or _interaction(root)
    return HybridPlanner(
        AppServerCodexEngine(
            interaction,
            progress=lambda message: print(message, file=sys.stderr, flush=True),
        ),
        CodexExecPlanner(),
    )


def _print_campaign(outcome: object) -> int:
    print(outcome.message)
    data = dict(outcome.data)
    if outcome.campaign_id:
        data.setdefault("campaign_id", outcome.campaign_id)
    if data:
        print(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False))
    return outcome.exit_code


def _confirm_start_approval(outcome: object) -> bool:
    """Show the one frozen proposal and require an explicit interactive approval."""

    _print_campaign(outcome)
    if not sys.stdin.isatty():
        print(
            "autodev start requires an interactive approval; use campaign plan and "
            "campaign approve in non-interactive environments",
            file=sys.stderr,
        )
        return False
    print("Approve this Campaign proposal? [y/N] ", end="", flush=True)
    return sys.stdin.readline().strip().lower() in {"y", "yes"}


def main(argv: Sequence[str] | None = None) -> int:
    """Parse, execute, and render one AutoDev command."""

    try:
        arguments = _parser().parse_args(argv)
    except _UsageError as error:
        print(f"autodev: {error}", file=sys.stderr)
        return 1
    requested_until = getattr(arguments, "until", None)
    if requested_until == "complete-or-blocked" and arguments.command in {"run", "resume"}:
        print(
            "warning: complete-or-blocked is deprecated; use target-or-blocked",
            file=sys.stderr,
        )
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
        app_server = AppServerCodexEngine(PersistentHumanInteraction(Path.cwd())).probe()
        checks["app_server_interaction"] = {
            "ready": True,
            "mode": app_server.get("mode", "fallback"),
            "native": bool(app_server.get("ready")),
            "request_user_input": bool(app_server.get("request_user_input")),
            "error": app_server.get("error"),
        }
        ready = all(item.get("ready", False) for item in checks.values())
        payload = {"ready": ready, "checks": checks}
        if arguments.as_json:
            print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
        else:
            for name, check in checks.items():
                print(f"{name}: {'ready' if check.get('ready') else 'not ready'}")
        return 0 if ready else 2
    campaign_live = arguments.command == "start" or (
        arguments.command == "campaign" and arguments.campaign_command in {"plan", "start", "answer"}
    )
    if (arguments.command in {"run", "resume"} or campaign_live) and os.environ.get("AUTODEV_LIVE_CODEX") != "1":
        print("live Codex requires AUTODEV_LIVE_CODEX=1")
        return 2
    operation: ProjectOperation | None = None
    if arguments.command == "init":
        operation = initialize_project(Path(arguments.target), arguments.name, merge=arguments.merge)
    elif arguments.command == "migrate":
        if arguments.migration_version == "v3":
            if arguments.check:
                operation = check_v3_migration(Path.cwd())
            elif arguments.apply:
                operation = apply_v3_migration(Path.cwd())
            else:
                operation = rollback_v3_migration(Path.cwd(), arguments.rollback)
        elif arguments.migration_version == "v2":
            if arguments.check:
                operation = check_v2_migration(Path.cwd())
            elif arguments.apply:
                operation = apply_v2_migration(Path.cwd(), adopt_source=arguments.adopt_source)
            else:
                operation = rollback_v2_migration(Path.cwd(), arguments.rollback)
        elif arguments.check:
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

    if arguments.command == "report":
        try:
            print(render_report(Path.cwd(), arguments.report_kind, campaign_id=arguments.campaign), end="")
            return 0
        except (OSError, json.JSONDecodeError, ValueError) as error:
            print(f"cannot render report: {error}", file=sys.stderr)
            return 1

    if arguments.command in {"start", "campaign"}:
        interaction = _interaction(Path.cwd()) if (
            arguments.command == "start"
            or arguments.campaign_command in {"plan", "start", "answer"}
        ) else None
        planner = _planner(Path.cwd(), interaction) if interaction is not None else None
        controller = CampaignController(Path.cwd(), planner, interaction)
        if arguments.command == "start":
            print("AutoDev: starting a fresh read-only Planner...", file=sys.stderr, flush=True)
            planned = controller.plan(_campaign_request(arguments))
            if planned.status != "SUCCESS":
                return _print_campaign(planned)
            if not _confirm_start_approval(planned):
                return 2
            approved = controller.approve(planned.campaign_id or "", planned.data["proposal_hash"])
            if approved.status != "SUCCESS":
                return _print_campaign(approved)
            print("AutoDev: proposal approved; executing the Campaign...", file=sys.stderr, flush=True)
            outcome = controller.run_until_target_or_blocked(
                planned.campaign_id or "", CodexExecEngine(), reviewer_engine=CodexExecEngine(),
            )
            return _print_campaign(outcome)
        if arguments.campaign_command == "plan":
            print("AutoDev: starting a fresh read-only Planner...", file=sys.stderr, flush=True)
            return _print_campaign(controller.plan(_campaign_request(arguments)))
        if arguments.campaign_command == "approve":
            return _print_campaign(controller.approve(arguments.campaign_id, arguments.proposal_hash))
        if arguments.campaign_command == "status":
            return _print_campaign(controller.status(arguments.campaign_id))
        if arguments.campaign_command == "retarget":
            return _print_campaign(controller.retarget(
                arguments.campaign_id, arguments.target.replace("-", "_").upper(),
            ))
        if arguments.campaign_command == "materialize":
            return _print_campaign(controller.materialize(arguments.campaign_id))
        if arguments.campaign_command == "archive":
            return _print_campaign(controller.archive(arguments.campaign_id))
        if arguments.campaign_command == "answer":
            parsed_answers: dict[str, list[str]] = {}
            for item in arguments.answers:
                if "=" not in item:
                    print("--answer must be QUESTION_ID=VALUE", file=sys.stderr)
                    return 1
                key, value = item.split("=", 1)
                parsed_answers.setdefault(key, []).append(value)
            if any(
                value.strip().lower() == "revise batch"
                for values in parsed_answers.values() for value in values
            ):
                print(
                    "AutoDev: starting a fresh read-only Planner for the revised batch...",
                    file=sys.stderr, flush=True,
                )
            return _print_campaign(controller.answer(
                arguments.campaign_id, arguments.request_id, parsed_answers,
            ))
        if arguments.campaign_command == "start":
            print(
                "AutoDev: executing Campaign Tasks; Codex runs may take several minutes...",
                file=sys.stderr, flush=True,
            )
            return _print_campaign(controller.run_until_target_or_blocked(
                arguments.campaign_id, CodexExecEngine(), reviewer_engine=CodexExecEngine(),
            ))

    if arguments.command in {"run", "resume"}:
        if arguments.command == "resume" and arguments.campaign:
            print(
                "AutoDev: resuming Campaign Tasks; Codex runs may take several minutes...",
                file=sys.stderr, flush=True,
            )
            interaction = _interaction(Path.cwd())
            controller = CampaignController(
                Path.cwd(), _planner(Path.cwd(), interaction), interaction,
            )
            return _print_campaign(controller.run_until_target_or_blocked(
                arguments.campaign, CodexExecEngine(), reviewer_engine=CodexExecEngine(),
            ))
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
            legacy_until = "complete-or-blocked" if arguments.until == "target-or-blocked" else arguments.until
            request = RunRequest(recover_stale=arguments.recover_stale, until=legacy_until)
        else:
            legacy_until = "complete-or-blocked" if arguments.until == "target-or-blocked" else arguments.until
            request = RunRequest(task_id=arguments.task, until=legacy_until)
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
