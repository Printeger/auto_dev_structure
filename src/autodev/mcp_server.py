"""Strict local stdio MCP transport for the Codex-native AutoDev workflow."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from autodev import __version__
from autodev._project import initialize_project as core_initialize_project
from autodev._resources import _read_text
from autodev.action import ActionController
from autodev.campaign import CampaignController, CampaignRequest
from autodev.control_plane import Command, ControlPlane


TOOL_NAMES = (
    "inspect_project",
    "initialize_project",
    "propose_campaign",
    "approve_campaign",
    "campaign_status",
    "campaign_continue",
    "pause_campaign",
    "answer_blocker",
    "retarget_campaign",
    "materialize_campaign",
    "get_next_action",
    "submit_action_result",
)

_STATUSES = ["SUCCESS", "INVALID", "NOT_READY", "BLOCKED", "STOPPED", "INFRA_FAILURE"]
_EXIT_CODES = {status: index for index, status in enumerate(_STATUSES)}


def _object(
    properties: Mapping[str, Any], required: Sequence[str],
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


_PROJECT_ROOT = {
    "type": "string", "minLength": 1,
    "description": "Absolute local path to the project root; never interpreted by a shell.",
}
_CAMPAIGN_ID = {"type": "string", "pattern": "^CAMP-[0-9]{3,}$"}
_ACTION_ID = {"type": "string", "pattern": "^ACTION-[0-9a-f]{32}$"}
_PROPOSAL_SCHEMA = json.loads(_read_text("schemas/campaign-proposal.schema.json"))
_ACTION_SCHEMA = json.loads(_read_text("schemas/action.schema.json"))
_ACTION_RESULT_SCHEMA = json.loads(_read_text("schemas/action-result.schema.json"))
_CAMPAIGN_RECORD_SCHEMA = _object(
    {
        "status": {"enum": [
            "PROPOSED", "ACTIVE", "WAITING_FOR_HUMAN", "TARGET_REACHED",
            "ARCHIVED", "CANCELLED",
        ]},
        "phase": {"enum": [
            "SCAFFOLD", "IMPLEMENT", "COMPONENT_VERIFY", "INTEGRATE", "HARDEN",
            "TARGET_REACHED",
        ]},
        "mode": {"enum": ["CHANGE", "STAGED", "CRITICAL"]},
        "target": {"enum": [
            "CHANGE_COMPLETE", "ARCHITECTURE_BASELINE", "WORKING_MVP",
            "INTEGRATED_SYSTEM", "RELEASE_CANDIDATE",
        ]},
        "proposal_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "checkpoint": {"type": ["string", "null"], "pattern": "^[0-9a-f]{40,64}$"},
        "last_materialized_checkpoint": {
            "type": ["string", "null"], "pattern": "^[0-9a-f]{40,64}$",
        },
        "approved_at": {"type": ["string", "null"], "format": "date-time"},
    },
    [
        "status", "phase", "mode", "target", "proposal_hash", "checkpoint",
        "last_materialized_checkpoint", "approved_at",
    ],
)
_HUMAN_OPTION_SCHEMA = _object(
    {"label": {"type": "string"}, "description": {"type": "string"}},
    ["label", "description"],
)
_HUMAN_QUESTION_SCHEMA = _object(
    {
        "id": {"type": "string"},
        "header": {"type": "string"},
        "question": {"type": "string"},
        "options": {"type": "array", "items": _HUMAN_OPTION_SCHEMA},
        "isOther": {"type": "boolean"},
        "isSecret": {"type": "boolean"},
        "credential_only": {"type": "boolean"},
    },
    ["id", "header", "question", "options", "isOther"],
)
_ANSWER_CONTRACT_SCHEMA = _object(
    {
        "request_id": {"type": "string"},
        "question_ids": {"type": "array", "items": {"type": "string"}},
        "answers_must_cover_exactly": {"type": "array", "items": {"type": "string"}},
    },
    ["request_id", "question_ids", "answers_must_cover_exactly"],
)
_ACTION_CONTEXT_SCHEMA = _object(
    {
        "run_id": {"type": "string"},
        "campaign_ref": {"type": "string"},
        "requirements_ref": {"type": "string"},
        "requirements_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "purpose": {"enum": ["PHASE_PLAN", "PHASE_REPAIR"]},
        "source_fingerprint": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "contract_ref": {"type": "string"},
        "contract_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "worker_action_id": _ACTION_ID,
        "patch_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "attempt_ref": {"type": "string"},
        "failure_ref": {"type": "string"},
        "phase_context_ref": {"type": "string"},
        "phase_flow_ref": {"type": "string"},
        "scope": {"const": "PHASE"},
        "campaign": _CAMPAIGN_RECORD_SCHEMA,
        "blocker": {"type": ["string", "null"]},
        "next_action": {"type": ["string", "null"]},
        "request_id": {"type": "string"},
        "questions": {"type": "array", "items": _HUMAN_QUESTION_SCHEMA},
        "answer_contract": _ANSWER_CONTRACT_SCHEMA,
    },
    [],
)
_MCP_ACTION_SCHEMA = _object(
    {**_ACTION_SCHEMA["properties"], "context": _ACTION_CONTEXT_SCHEMA},
    _ACTION_SCHEMA["required"],
)
_VALIDATION_SCHEMA = _object(
    {
        "index": {"type": "integer"},
        "argv": {"type": "array", "items": {"type": "string"}},
        "cwd": {"type": "string"},
        "returncode": {"type": "integer"},
        "timed_out": {"type": "boolean"},
        "stdout": {"type": "string"},
        "stderr": {"type": "string"},
        "duration_seconds": {"type": "number"},
    },
    [
        "index", "argv", "cwd", "returncode", "timed_out", "stdout", "stderr",
        "duration_seconds",
    ],
)
_INSPECT_SUMMARY_SCHEMA = _object(
    {
        "project_name": {"type": ["string", "null"]},
        "project_status": {"type": ["string", "null"]},
        "revision": {"type": ["integer", "null"]},
        "current_action_id": {"type": ["string", "null"]},
        "pause_requested": {"type": ["boolean", "null"]},
        "next_owner": {"type": ["string", "null"]},
        "next_action": {"type": ["string", "null"]},
    },
    [
        "project_name", "project_status", "revision", "current_action_id",
        "pause_requested", "next_owner", "next_action",
    ],
)
_INSPECT_VALIDATION_SCHEMA = _object(
    {
        "$schema": {"type": "string"},
        "schema_version": {"const": 1},
        "status": {"enum": _STATUSES},
        "exit_code": {"type": "integer"},
        "message": {"type": "string"},
        "revision": {"type": ["integer", "null"]},
        "data": _object({
            "errors": {"type": "array", "items": {"type": "string"}},
            "valid": {"type": "boolean"},
            "ready": {"type": "boolean"},
            "revision": {"type": "integer"},
            "project_status": {"type": "string"},
            "requirements": {
                "type": "array",
                "items": _object(
                    {
                        "id": {"type": "string"},
                        "priority": {"enum": ["MUST", "SHOULD", "COULD"]},
                        "status": {"type": "string"},
                        "acceptance_signal": {"type": "string"},
                    },
                    ["id", "priority", "status", "acceptance_signal"],
                ),
            },
        }, []),
    },
    ["$schema", "schema_version", "status", "exit_code", "message", "revision", "data"],
)


def _data_schema(*names: str) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "error_code": {"type": "string"},
        "errors": {"type": "array", "items": {"type": "string"}},
    }
    available = {
        "initialized": {"type": "boolean"},
        "project_root": {"type": "string"},
        "validation": _INSPECT_VALIDATION_SCHEMA,
        "summary": _INSPECT_SUMMARY_SCHEMA,
        "campaign_ids": {"type": "array", "items": _CAMPAIGN_ID},
        "copied": {"type": "array", "items": {"type": "string"}},
        "conflicts": {"type": "array", "items": {"type": "string"}},
        "proposal_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "questions": {"type": "array", "items": _HUMAN_QUESTION_SCHEMA},
        "proposal": json.loads(_read_text("schemas/campaign.schema.json")),
        "checkpoint": {"type": ["string", "null"]},
        "campaign_id": _CAMPAIGN_ID,
        "task_ids": {"type": "array", "items": {"type": "string"}},
        "admission": {"enum": ["AUTO_ADMITTED", "HUMAN_APPROVED"]},
        "human_approval_request_id": {"type": ["string", "null"]},
        "human_approvable": {"type": "boolean"},
        "request_id": {"type": "string"},
        "artifact": {"type": "string"},
        "graceful": {"type": "boolean"},
        "cleared_action_id": {"type": ["string", "null"]},
        "patch_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "from": {"type": "string"},
        "to": {"type": "string"},
        "action_id": _ACTION_ID,
        "terminal_type": {"enum": ["ASK_HUMAN", "PAUSED", "TARGET_REACHED"]},
        "incomplete_tasks": {"type": "array", "items": {"type": "string"}},
        "validations": {"type": "array", "items": _VALIDATION_SCHEMA},
        "findings": {"type": "array", "items": {"type": "string"}},
    }
    available.update(_CAMPAIGN_RECORD_SCHEMA["properties"])
    properties.update({name: available[name] for name in names})
    return _object(properties, [])


def _output_schemas() -> dict[str, dict[str, Any]]:
    data_fields = {
        "inspect_project": ("initialized", "project_root", "validation", "summary", "campaign_ids"),
        "initialize_project": ("copied", "conflicts"),
        "propose_campaign": ("proposal_hash", "questions", "proposal"),
        "approve_campaign": (
            "checkpoint", "campaign_id", "task_ids", "admission",
            "human_approval_request_id", "human_approvable", "request_id", "artifact",
        ),
        "campaign_status": tuple(_CAMPAIGN_RECORD_SCHEMA["properties"]),
        "campaign_continue": ("campaign_id", "cleared_action_id"),
        "pause_campaign": ("campaign_id", "graceful"),
        "answer_blocker": (
            "campaign_id", "task_ids", "admission", "human_approval_request_id",
            "questions", "request_id", "artifact", "checkpoint", "patch_sha256", "from", "to",
        ),
        "retarget_campaign": ("campaign_id", "from", "to", "action_id", "terminal_type"),
        "materialize_campaign": ("campaign_id", "checkpoint", "patch_sha256", "action_id", "terminal_type"),
        "get_next_action": ("incomplete_tasks", "validations", "findings"),
        "submit_action_result": ("validations", "findings", "incomplete_tasks"),
    }
    schemas: dict[str, dict[str, Any]] = {}
    for name, fields in data_fields.items():
        schema = _object(
            {
                "status": {"enum": _STATUSES},
                "exit_code": {"type": "integer", "minimum": 0, "maximum": 5},
                "message": {"type": "string"},
                "campaign_id": {"anyOf": [_CAMPAIGN_ID, {"type": "null"}]},
                "action": {"anyOf": [_MCP_ACTION_SCHEMA, {"type": "null"}]},
                "data": _data_schema(*fields),
            },
            ["status", "exit_code", "message", "campaign_id", "action", "data"],
        )
        schema["$id"] = f"https://autodev.local/mcp/{name}-output.schema.json"
        schemas[name] = schema
    return schemas


def _schemas() -> dict[str, dict[str, Any]]:
    common_campaign = {"project_root": _PROJECT_ROOT, "campaign_id": _CAMPAIGN_ID}
    return {
        "inspect_project": _object({"project_root": _PROJECT_ROOT}, ["project_root"]),
        "initialize_project": _object(
            {
                "project_root": _PROJECT_ROOT,
                "name": {"type": "string", "minLength": 1, "maxLength": 200},
                "merge": {"type": "boolean", "default": False},
            },
            ["project_root", "name", "merge"],
        ),
        "propose_campaign": _object(
            {
                "project_root": _PROJECT_ROOT,
                "idea": {"type": "string", "minLength": 1, "maxLength": 20000},
                "development_strategy": {"enum": ["CHANGE", "STAGED", "CRITICAL"]},
                "target": {"enum": [
                    "CHANGE_COMPLETE", "ARCHITECTURE_BASELINE", "WORKING_MVP",
                    "INTEGRATED_SYSTEM", "RELEASE_CANDIDATE",
                ]},
                "autonomy": {"enum": ["HUMAN_ON_BLOCKED"]},
                "proposal": _PROPOSAL_SCHEMA,
                "parent_campaign_id": {"anyOf": [_CAMPAIGN_ID, {"type": "null"}], "default": None},
                "source_checkpoint": {"type": ["string", "null"], "default": None},
            },
            [
                "project_root", "idea", "development_strategy", "target", "autonomy",
                "proposal",
            ],
        ),
        "approve_campaign": _object(
            {
                **common_campaign,
                "proposal_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "proposal_and_authority_confirmed": {"type": "boolean"},
            },
            [
                "project_root", "campaign_id", "proposal_hash",
                "proposal_and_authority_confirmed",
            ],
        ),
        "campaign_status": _object(common_campaign, ["project_root", "campaign_id"]),
        "campaign_continue": _object(common_campaign, ["project_root", "campaign_id"]),
        "pause_campaign": _object(common_campaign, ["project_root", "campaign_id"]),
        "answer_blocker": _object(
            {
                **common_campaign,
                "request_id": {"type": "string", "minLength": 1},
                "answers": {
                    "type": "object",
                    "minProperties": 1,
                    "additionalProperties": {
                        "type": "array", "minItems": 1, "maxItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                },
            },
            ["project_root", "campaign_id", "request_id", "answers"],
        ),
        "retarget_campaign": _object(
            {
                **common_campaign,
                "target": {"enum": [
                    "ARCHITECTURE_BASELINE", "WORKING_MVP", "INTEGRATED_SYSTEM",
                    "RELEASE_CANDIDATE",
                ]},
            },
            ["project_root", "campaign_id", "target"],
        ),
        "materialize_campaign": _object(common_campaign, ["project_root", "campaign_id"]),
        "get_next_action": _object(common_campaign, ["project_root", "campaign_id"]),
        "submit_action_result": _object(
            {
                "project_root": _PROJECT_ROOT,
                "action_id": _ACTION_ID,
                "result": _ACTION_RESULT_SCHEMA,
            },
            ["project_root", "action_id", "result"],
        ),
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _response(
    status: str,
    message: str,
    *,
    campaign_id: str | None = None,
    action: Mapping[str, Any] | None = None,
    data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "exit_code": _EXIT_CODES[status],
        "message": message,
        "campaign_id": campaign_id,
        "action": _jsonable(action) if action is not None else None,
        "data": _jsonable(data or {}),
    }


def _from_outcome(outcome: Any) -> dict[str, Any]:
    return _response(
        outcome.status,
        outcome.message,
        campaign_id=getattr(outcome, "campaign_id", None),
        action=getattr(outcome, "action", None),
        data=getattr(outcome, "data", {}),
    )


def _resolve_root(raw: Any, *, may_create: bool = False) -> Path:
    if not isinstance(raw, str) or not raw or "\x00" in raw or any(char in raw for char in "\r\n"):
        raise ValueError("project_root must be a non-empty single-line path")
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError("project_root must be an absolute path")
    resolved = path.resolve(strict=False)
    if may_create:
        parent = resolved.parent
        if not parent.is_dir():
            raise ValueError("project_root parent must be an existing directory")
    elif not resolved.is_dir():
        raise ValueError("project_root must be an existing directory")
    return resolved


def _validate_arguments(name: str, arguments: Any) -> list[str]:
    errors = Draft202012Validator(_schemas()[name]).iter_errors(arguments)
    return sorted(
        f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in errors
    )


def _dispatch(name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    try:
        root = _resolve_root(arguments["project_root"], may_create=name == "initialize_project")
    except (KeyError, OSError, ValueError) as error:
        return _response("INVALID", f"invalid project_root: {error}", data={"error_code": "INVALID_ROOT"})

    try:
        if name == "inspect_project":
            canonical = root / ".autodev"
            if not canonical.is_dir():
                return _response(
                    "SUCCESS", "project is not initialized",
                    data={"initialized": False, "project_root": str(root)},
                )
            validation = ControlPlane(root).execute(Command("validate"))
            data = {
                "initialized": True,
                "project_root": str(root),
                "validation": validation.to_dict(),
            }
            state_path = canonical / "state.json"
            if state_path.is_file():
                state = json.loads(state_path.read_text(encoding="utf-8"))
                data["summary"] = {
                    key: state.get(key) for key in (
                        "project_name", "project_status", "revision", "current_action_id",
                        "pause_requested", "next_owner", "next_action",
                    )
                }
                data["campaign_ids"] = sorted(state.get("campaigns", {}))
            return _response(validation.status, validation.message, data=data)
        if name == "initialize_project":
            return _from_outcome(core_initialize_project(
                root, str(arguments["name"]), merge=bool(arguments["merge"]),
            ))

        if not (root / ".autodev" / "state.json").is_file():
            return _response(
                "NOT_READY", "project is not initialized",
                data={"error_code": "PROJECT_NOT_INITIALIZED"},
            )
        campaign = CampaignController(root)
        if name == "propose_campaign":
            request = CampaignRequest(
                str(arguments["idea"]),
                mode=str(arguments["development_strategy"]),
                target=str(arguments["target"]),
                autonomy=str(arguments["autonomy"]),
                parent_campaign_id=arguments.get("parent_campaign_id"),
                source_checkpoint=arguments.get("source_checkpoint"),
            )
            return _from_outcome(campaign.propose_structured(request, arguments["proposal"]))
        if name == "submit_action_result":
            return _from_outcome(ActionController(root).submit_action_result(
                str(arguments["action_id"]), arguments["result"],
            ))
        campaign_id = str(arguments["campaign_id"])
        if name == "approve_campaign":
            if arguments["proposal_and_authority_confirmed"] is not True:
                return _response(
                    "NOT_READY",
                    "Proposal and Authority Envelope must be confirmed together before approval",
                    campaign_id=campaign_id,
                    data={"error_code": "CONFIRMATION_REQUIRED"},
                )
            return _from_outcome(campaign.approve(campaign_id, str(arguments["proposal_hash"])))
        if name == "campaign_status":
            return _from_outcome(campaign.status(campaign_id))
        if name == "campaign_continue":
            return _from_outcome(ControlPlane(root).execute(Command(
                "action.continue", {"campaign_id": campaign_id},
            ))) | {"campaign_id": campaign_id}
        if name == "pause_campaign":
            return _from_outcome(ControlPlane(root).execute(Command(
                "action.pause", {"campaign_id": campaign_id},
            ))) | {"campaign_id": campaign_id}
        if name == "answer_blocker":
            return _from_outcome(campaign.answer(
                campaign_id, str(arguments["request_id"]), arguments["answers"],
            ))
        if name == "retarget_campaign":
            return _from_outcome(campaign.retarget(campaign_id, str(arguments["target"])))
        if name == "materialize_campaign":
            return _from_outcome(campaign.materialize(campaign_id))
        if name == "get_next_action":
            return _from_outcome(ActionController(root).get_next_action(campaign_id))
        return _response("INVALID", f"unknown tool: {name}", data={"error_code": "UNKNOWN_TOOL"})
    except (KeyError, OSError, json.JSONDecodeError, ValueError) as error:
        return _response(
            "INFRA_FAILURE", f"Core operation failed: {error}",
            campaign_id=str(arguments.get("campaign_id")) if arguments.get("campaign_id") else None,
            data={"error_code": "CORE_FAILURE"},
        )


def create_server() -> Any:
    """Create the official low-level MCP server with strict published schemas."""

    from mcp.server.lowlevel import Server
    from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations

    schemas = _schemas()
    output_schemas = _output_schemas()
    annotations = {
        "inspect_project": ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
        ),
        "initialize_project": ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False,
        ),
        "propose_campaign": ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False,
        ),
        "approve_campaign": ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False,
        ),
        "campaign_status": ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
        ),
        "campaign_continue": ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False,
        ),
        "pause_campaign": ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False,
        ),
        "answer_blocker": ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False,
        ),
        "retarget_campaign": ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False,
        ),
        "materialize_campaign": ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False,
        ),
        "get_next_action": ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False,
        ),
        "submit_action_result": ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False,
        ),
    }
    descriptions = {
        "inspect_project": "Inspect AutoDev initialization and canonical summary without writing.",
        "initialize_project": "Initialize AutoDev contracts and canonical state at a local project root.",
        "propose_campaign": "Persist the current Commander's structured Campaign Proposal without starting a Planner.",
        "approve_campaign": "Approve one confirmed Proposal and Authority Envelope and create its Campaign workspace.",
        "campaign_status": "Read one Campaign's canonical status.",
        "campaign_continue": "Resume a gracefully paused Campaign.",
        "pause_campaign": "Request a graceful pause after any pending Action completes.",
        "answer_blocker": "Record answers for a pending human request through Core.",
        "retarget_campaign": "Extend a reached Campaign to a later maturity target.",
        "materialize_campaign": "Safely apply a reached Campaign checkpoint to the source worktree.",
        "get_next_action": "Get or recover the single persistent next Action chosen by Core.",
        "submit_action_result": "Submit one strict untrusted Action result for Core verification.",
    }
    tools = [
        Tool(
            name=name,
            description=descriptions[name],
            inputSchema=schemas[name],
            outputSchema=output_schemas[name],
            annotations=annotations[name],
        )
        for name in TOOL_NAMES
    ]

    async def list_tools(_context: Any, _params: Any) -> Any:
        return ListToolsResult(tools=tools)

    async def call_tool(_context: Any, params: Any) -> Any:
        name = params.name
        arguments = params.arguments or {}
        if name not in schemas:
            result = _response("INVALID", f"unknown tool: {name}", data={"error_code": "UNKNOWN_TOOL"})
        else:
            errors = _validate_arguments(name, arguments)
            result = (
                _response(
                    "INVALID", "invalid tool arguments",
                    data={"error_code": "INVALID_ARGUMENT", "errors": errors},
                )
                if errors else _dispatch(name, arguments)
            )
        output_errors = sorted(
            f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
            for error in Draft202012Validator(output_schemas[name]).iter_errors(result)
        ) if name in output_schemas else []
        if output_errors:
            result = _response(
                "INFRA_FAILURE", "Core result violated the MCP output contract",
                data={"error_code": "OUTPUT_CONTRACT", "errors": output_errors},
            )
        encoded = json.dumps(result, sort_keys=True, ensure_ascii=False)
        return CallToolResult(
            content=[TextContent(type="text", text=encoded)],
            structuredContent=result,
            isError=result["status"] != "SUCCESS",
        )

    return Server(
        "autodev",
        version=__version__,
        description="Local Core-only control surface for Codex-native AutoDev Campaigns.",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def _serve_stdio() -> None:
    from mcp.server.stdio import stdio_server

    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autodev-mcp")
    parser.add_argument("--stdio", action="store_true", help="serve MCP over standard I/O")
    parser.add_argument("--version", action="store_true", help="print the package version")
    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
        return 0
    if not args.stdio:
        parser.print_help()
        return 0
    try:
        asyncio.run(_serve_stdio())
    except ModuleNotFoundError as error:
        if error.name == "mcp" or (error.name or "").startswith("mcp."):
            print("autodev-mcp requires mcp>=2.1,<3", file=sys.stderr)
            return 2
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
