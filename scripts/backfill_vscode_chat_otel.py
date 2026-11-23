#!/usr/bin/env python3
"""Reconstruct VS Code Copilot chat traces from local workspaceStorage.

The source files are read-only. Each stored user request becomes one aggregate
inference span; transcript tool events become child spans when available.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence
from urllib.parse import unquote, urlparse

import requests
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, InstrumentationScope, KeyValue
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span, Status


SCRIPT_VERSION = "1"
DEFAULT_STORAGE = Path.home() / "Library/Application Support/Code/User/workspaceStorage"
DEFAULT_ENDPOINT = os.environ.get("OPENOBSERVE_URL", "http://localhost:5080").rstrip("/") + "/api/default"
DEFAULT_USER = os.environ.get("OPENOBSERVE_USER", "root@example.com")
NAMESPACE = b"vscode-chat-otel-history-backfill-v1\0"


def stable_bytes(kind: str, value: str, size: int) -> bytes:
    return hashlib.sha256(NAMESPACE + kind.encode() + b"\0" + value.encode()).digest()[:size]


def trace_id(value: str) -> bytes:
    return stable_bytes("trace", value, 16)


def span_id(value: str) -> bytes:
    return stable_bytes("span", value, 8)


def ms_to_ns(value: int | float | None) -> int:
    return int(value or 0) * 1_000_000


def iso_to_ms(value: str | None) -> int | None:
    if not value:
        return None
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def any_value(value: Any) -> AnyValue:
    result = AnyValue()
    if isinstance(value, bool):
        result.bool_value = value
    elif isinstance(value, int):
        result.int_value = value
    elif isinstance(value, float):
        result.double_value = value
    elif isinstance(value, bytes):
        result.bytes_value = value
    elif value is not None:
        result.string_value = str(value)
    return result


def attributes(values: dict[str, Any]) -> list[KeyValue]:
    return [KeyValue(key=key, value=any_value(value)) for key, value in values.items() if value is not None]


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def set_path(root: Any, path: list[Any], value: Any, append: bool = False) -> None:
    if not path:
        raise ValueError("empty mutation path")
    current = root
    for index, key in enumerate(path[:-1]):
        next_key = path[index + 1]
        if isinstance(key, int):
            if not isinstance(current, list):
                raise ValueError(f"expected list at {path[:index]}")
            while len(current) <= key:
                current.append([] if isinstance(next_key, int) else {})
            if current[key] is None:
                current[key] = [] if isinstance(next_key, int) else {}
            current = current[key]
        else:
            if not isinstance(current, dict):
                raise ValueError(f"expected object at {path[:index]}")
            if key not in current or current[key] is None:
                current[key] = [] if isinstance(next_key, int) else {}
            current = current[key]

    key = path[-1]
    if append and value is None:
        new_value = None
    elif append:
        additions = value if isinstance(value, list) else [value]
        if isinstance(key, int):
            while len(current) <= key:
                current.append(None)
            if not isinstance(current[key], list):
                current[key] = []
            current[key].extend(copy.deepcopy(additions))
            return
        new_value = current.get(key)
        if not isinstance(new_value, list):
            new_value = []
        new_value.extend(copy.deepcopy(additions))
    else:
        new_value = copy.deepcopy(value)

    if isinstance(key, int):
        while len(current) <= key:
            current.append(None)
        current[key] = new_value
    else:
        current[key] = new_value


def load_session_file(path: Path) -> dict[str, Any]:
    if path.stat().st_size == 0:
        raise ValueError("empty file")
    if path.suffix == ".json":
        parsed = json.loads(path.read_text())
        if not isinstance(parsed, dict):
            raise ValueError("snapshot root is not an object")
        return parsed

    state: dict[str, Any] | None = None
    with path.open() as handle:
        for line_number, line in enumerate(handle, 1):
            parsed = json.loads(line)
            records = parsed if isinstance(parsed, list) else [parsed]
            for record in records:
                kind = record.get("kind")
                if kind == 0:
                    state = copy.deepcopy(record["v"])
                elif state is None:
                    raise ValueError(f"line {line_number}: mutation before base state")
                elif kind == 1:
                    set_path(state, record["k"], record.get("v"))
                elif kind == 2:
                    set_path(state, record["k"], record.get("v"), append=True)
                else:
                    raise ValueError(f"line {line_number}: unsupported mutation kind {kind}")
    if state is None:
        raise ValueError("missing base state")
    return state


def workspace_path(workspace_dir: Path) -> str | None:
    path = workspace_dir / "workspace.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        raw = data.get("folder") or data.get("workspace")
        if not raw:
            return None
        parsed = urlparse(raw)
        return unquote(parsed.path) if parsed.scheme == "file" else raw
    except (OSError, json.JSONDecodeError):
        return None


def session_title(workspace_dir: Path, session_id: str) -> str | None:
    database = workspace_dir / "state.vscdb"
    if not database.exists():
        return None
    try:
        uri = f"file:{database.resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            row = connection.execute(
                "SELECT value FROM ItemTable WHERE key='chat.ChatSessionStore.index'"
            ).fetchone()
        if not row:
            return None
        return (json.loads(row[0]).get("entries", {}).get(session_id) or {}).get("title")
    except (sqlite3.Error, json.JSONDecodeError, OSError):
        return None


@dataclass
class ToolEvent:
    call_id: str
    name: str
    start_ms: int
    end_ms: int
    arguments: Any
    success: bool | None


@dataclass
class ChatRequest:
    session_id: str
    request_id: str
    start_ms: int
    end_ms: int
    model: str
    prompt: str
    response: str
    prompt_tokens: int | None
    completion_tokens: int | None
    copilot_credits: float | None
    elapsed_ms: int | None
    first_progress_ms: int | None
    agent_name: str
    agent_mode: str | None
    workspace: str | None
    workspace_hash: str
    title: str | None
    tools: list[ToolEvent] = field(default_factory=list)


@dataclass
class Inventory:
    requests: list[ChatRequest]
    sessions_seen: int
    invalid_files: list[dict[str, str]]


def response_text(request: dict[str, Any]) -> str:
    result: list[str] = []
    for item in request.get("response") or []:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            value = item.get("value")
            if isinstance(value, str):
                result.append(value)
            content = item.get("content")
            if isinstance(content, dict) and isinstance(content.get("value"), str):
                result.append(content["value"])
    if result:
        return "".join(result)
    rounds = (((request.get("result") or {}).get("metadata") or {}).get("toolCallRounds") or [])
    for round_data in reversed(rounds):
        if isinstance(round_data, dict) and isinstance(round_data.get("response"), str):
            return round_data["response"]
    return ""


def transcript_tools(path: Path) -> list[ToolEvent]:
    if not path.exists():
        return []
    pending: dict[str, dict[str, Any]] = {}
    completed: list[ToolEvent] = []
    with path.open() as handle:
        for line in handle:
            event = json.loads(line)
            data = event.get("data") or {}
            event_type = event.get("type")
            timestamp = iso_to_ms(event.get("timestamp"))
            if timestamp is None:
                continue
            if event_type == "tool.execution_start":
                call_id = str(data.get("toolCallId") or event.get("id"))
                pending[call_id] = {
                    "call_id": call_id,
                    "name": str(data.get("toolName") or "unknown"),
                    "start_ms": timestamp,
                    "arguments": data.get("arguments") or {},
                }
            elif event_type == "tool.execution_complete":
                call_id = str(data.get("toolCallId") or "")
                start = pending.pop(call_id, None)
                if start:
                    completed.append(ToolEvent(
                        **start, end_ms=timestamp,
                        success=data.get("success") if isinstance(data.get("success"), bool) else None,
                    ))
    return completed


def inventory(storage: Path) -> Inventory:
    requests: list[ChatRequest] = []
    invalid: list[dict[str, str]] = []
    sessions_seen = 0
    for path in sorted(storage.glob("*/chatSessions/*")):
        if path.suffix not in (".json", ".jsonl"):
            continue
        try:
            session = load_session_file(path)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            invalid.append({"path": str(path), "error": str(error)})
            continue
        sessions_seen += 1
        session_id = str(session.get("sessionId") or path.stem)
        workspace_dir = path.parents[1]
        workspace = workspace_path(workspace_dir)
        title = session_title(workspace_dir, session_id)
        stored = session.get("requests") or []
        tools = transcript_tools(workspace_dir / "GitHub.copilot-chat/transcripts" / f"{session_id}.jsonl")
        request_starts = [int(item.get("timestamp") or item.get("responseTimestamp") or 0) for item in stored]
        for index, item in enumerate(stored):
            start_ms = request_starts[index]
            if not start_ms:
                continue
            elapsed = item.get("elapsedMs") if isinstance(item.get("elapsedMs"), int) else None
            completed = (item.get("modelState") or {}).get("completedAt")
            end_ms = int(completed or (start_ms + elapsed if elapsed else item.get("responseTimestamp") or start_ms))
            next_start = request_starts[index + 1] if index + 1 < len(request_starts) else 2**63 - 1
            request_tools = [tool for tool in tools if start_ms <= tool.start_ms < next_start]
            result_timings = (item.get("result") or {}).get("timings") or {}
            prompt = ((item.get("message") or {}).get("text") or "")
            requests.append(ChatRequest(
                session_id=session_id,
                request_id=str(item.get("requestId") or f"{session_id}:{index}"),
                start_ms=start_ms,
                end_ms=max(start_ms, end_ms, *(tool.end_ms for tool in request_tools)),
                model=str(item.get("modelId") or "unknown").removeprefix("copilot/"),
                prompt=prompt,
                response=response_text(item),
                prompt_tokens=item.get("promptTokens") if isinstance(item.get("promptTokens"), int) else None,
                completion_tokens=item.get("completionTokens") if isinstance(item.get("completionTokens"), int) else None,
                copilot_credits=float(item["copilotCredits"]) if isinstance(item.get("copilotCredits"), (int, float)) else None,
                elapsed_ms=elapsed,
                first_progress_ms=result_timings.get("firstProgress") if isinstance(result_timings.get("firstProgress"), int) else None,
                agent_name=str((item.get("agent") or {}).get("fullName") or "GitHub Copilot"),
                agent_mode=(item.get("modeInfo") or {}).get("telemetryModeName") or (item.get("modeInfo") or {}).get("modeName") or (item.get("modeInfo") or {}).get("kind"),
                workspace=workspace,
                workspace_hash=workspace_dir.name,
                title=title,
                tools=request_tools,
            ))
    return Inventory(sorted(requests, key=lambda item: (item.start_ms, item.request_id)), sessions_seen, invalid)


def selected_requests(items: Sequence[ChatRequest], args: argparse.Namespace) -> list[ChatRequest]:
    selected = list(items)
    if args.session_id:
        ids = set(args.session_id)
        selected = [item for item in selected if item.session_id in ids]
    if args.request_id:
        ids = set(args.request_id)
        selected = [item for item in selected if item.request_id in ids]
    if args.started_after_ms is not None:
        selected = [item for item in selected if item.start_ms >= args.started_after_ms]
    if args.started_before_ms is not None:
        selected = [item for item in selected if item.start_ms < args.started_before_ms]
    if args.max_requests is not None:
        selected = selected[:args.max_requests]
    return selected


def resource() -> Resource:
    return Resource(attributes=attributes({
        "service.name": "copilot-chat",
        "backfill": True,
        "backfill.version": SCRIPT_VERSION,
        "backfill.source": "vscode_workspace_storage",
        "backfill.fidelity": "turn_aggregate",
    }))


def build_traces(items: Sequence[ChatRequest]) -> ExportTraceServiceRequest:
    request = ExportTraceServiceRequest()
    resource_spans = request.resource_spans.add()
    resource_spans.resource.CopyFrom(resource())
    scope_spans = resource_spans.scope_spans.add()
    scope_spans.scope.CopyFrom(InstrumentationScope(name="vscode.chat.backfill", version=SCRIPT_VERSION))
    for item in items:
        trace = trace_id(f"request:{item.request_id}")
        parent = span_id(f"request:{item.request_id}")
        total_tokens = None
        if item.prompt_tokens is not None or item.completion_tokens is not None:
            total_tokens = (item.prompt_tokens or 0) + (item.completion_tokens or 0)
        span_attrs = {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": "github",
            "gen_ai.request.model": item.model,
            "gen_ai.response.model": item.model,
            "gen_ai.conversation.id": item.session_id,
            "gen_ai.agent.name": item.agent_name,
            "gen_ai.input.messages": json_text([{"role": "user", "content": item.prompt}]),
            "gen_ai.output.messages": json_text([{"role": "assistant", "content": item.response}]),
            "gen_ai.usage.input_tokens": item.prompt_tokens,
            "gen_ai.usage.output_tokens": item.completion_tokens,
            "gen_ai.usage.total_tokens": total_tokens,
            "copilot_chat.copilot_usage_nano_aiu": int(item.copilot_credits * 1_000_000_000) if item.copilot_credits is not None else None,
            "copilot_chat.time_to_first_token": item.first_progress_ms,
            "copilot_chat.chat_session_id": item.session_id,
            "copilot_chat.session_id": item.session_id,
            "copilot_chat.user_request": item.prompt,
            "github_copilot.agent_type": item.agent_mode,
            "service.session.id": item.session_id,
            "vscode.workspace": item.workspace,
            "vscode.workspace_hash": item.workspace_hash,
            "vscode.chat.title": item.title,
            "backfill": True,
            "backfill.version": SCRIPT_VERSION,
            "backfill.source": "vscode_workspace_storage",
            "backfill.fidelity": "turn_aggregate",
            "backfill.source_request_id": item.request_id,
            "backfill.tool_count": len(item.tools),
        }
        scope_spans.spans.append(Span(
            trace_id=trace, span_id=parent,
            name=f"chat {item.model}", kind=Span.SPAN_KIND_CLIENT,
            start_time_unix_nano=ms_to_ns(item.start_ms),
            end_time_unix_nano=ms_to_ns(item.end_ms),
            attributes=attributes(span_attrs),
            status=Status(code=Status.STATUS_CODE_OK),
        ))
        for tool in item.tools:
            tool_attrs = {
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": tool.name,
                "gen_ai.tool.call.id": tool.call_id,
                "gen_ai.tool.call.arguments": json_text(tool.arguments),
                "gen_ai.conversation.id": item.session_id,
                "gen_ai.agent.name": item.agent_name,
                "tool.success": tool.success,
                "backfill": True,
                "backfill.version": SCRIPT_VERSION,
                "backfill.source": "vscode_workspace_storage_transcript",
                "backfill.fidelity": "exact_tool_timing",
                "backfill.source_request_id": item.request_id,
            }
            error = tool.success is False
            scope_spans.spans.append(Span(
                trace_id=trace,
                span_id=span_id(f"tool:{item.request_id}:{tool.call_id}"),
                parent_span_id=parent,
                name=f"execute_tool {tool.name}", kind=Span.SPAN_KIND_INTERNAL,
                start_time_unix_nano=ms_to_ns(tool.start_ms),
                end_time_unix_nano=ms_to_ns(tool.end_ms),
                attributes=attributes(tool_attrs),
                status=Status(code=Status.STATUS_CODE_ERROR if error else Status.STATUS_CODE_OK),
            ))
    return request


def chunks(values: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]


def split_trace_request(request: ExportTraceServiceRequest, batch_size: int) -> Iterator[ExportTraceServiceRequest]:
    spans = list(request.resource_spans[0].scope_spans[0].spans)
    for batch in chunks(spans, batch_size):
        output = ExportTraceServiceRequest()
        resource_spans = output.resource_spans.add()
        resource_spans.resource.CopyFrom(request.resource_spans[0].resource)
        scope_spans = resource_spans.scope_spans.add()
        scope_spans.scope.CopyFrom(request.resource_spans[0].scope_spans[0].scope)
        scope_spans.spans.extend(batch)
        yield output


def openobserve_search(args: argparse.Namespace, stream: str, items: Sequence[ChatRequest]) -> list[dict[str, Any]]:
    if not items:
        return []
    password = os.environ.get(args.password_env)
    if not password:
        return []
    session_ids = sorted({item.session_id for item in items})
    quoted = ",".join("'" + value.replace("'", "''") + "'" for value in session_ids)
    sql = (
        f'SELECT gen_ai_conversation_id, _timestamp, span_id FROM "{stream}" '
        f"WHERE gen_ai_operation_name = 'chat' AND gen_ai_conversation_id IN ({quoted})"
    )
    body = {"query": {
        "sql": sql,
        "start_time": (min(item.start_ms for item in items) - 60_000) * 1000,
        "end_time": (max(item.end_ms for item in items) + 60_000) * 1000,
        "from": 0, "size": 10000,
    }}
    response = requests.post(
        f"{args.endpoint.rstrip('/')}/_search?type=traces",
        auth=(args.username, password), json=body, timeout=args.timeout,
    )
    response.raise_for_status()
    return response.json().get("hits", [])


def overlaps(args: argparse.Namespace, items: Sequence[ChatRequest]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for stream in args.overlap_stream:
        for hit in openobserve_search(args, stream, items):
            timestamp_ms = int(hit["_timestamp"]) // 1000
            for item in items:
                if item.session_id == hit.get("gen_ai_conversation_id") and item.start_ms - 5_000 <= timestamp_ms <= item.end_ms + 30_000:
                    found.append({
                        "stream": stream, "session_id": item.session_id,
                        "request_id": item.request_id, "local_start_ms": item.start_ms,
                        "otel_timestamp_ms": timestamp_ms, "span_id": hit.get("span_id"),
                    })
                    break
    return found


def summary(data: Inventory, selected: Sequence[ChatRequest], overlap: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {
        "sessions_seen": data.sessions_seen,
        "invalid_files": data.invalid_files,
        "requests_available": len(data.requests),
        "selected_requests": len(selected),
        "selected_sessions": len({item.session_id for item in selected}),
        "selected_tools": sum(len(item.tools) for item in selected),
        "selected_prompt_tokens": sum(item.prompt_tokens or 0 for item in selected),
        "selected_completion_tokens": sum(item.completion_tokens or 0 for item in selected),
        "selected_copilot_credits": sum(item.copilot_credits or 0 for item in selected),
        "selected_min_ms": min((item.start_ms for item in selected), default=None),
        "selected_max_ms": max((item.end_ms for item in selected), default=None),
        "overlap_count": len(overlap),
        "overlaps": list(overlap[:20]),
    }


def export(args: argparse.Namespace, payload: ExportTraceServiceRequest) -> None:
    password = os.environ.get(args.password_env)
    if not password:
        raise SystemExit(f"Set {args.password_env} before exporting")
    for batch in split_trace_request(payload, args.batch_size):
        response = requests.post(
            f"{args.endpoint.rstrip('/')}/v1/traces",
            auth=(args.username, password), data=batch.SerializeToString(), timeout=args.timeout,
            headers={"Content-Type": "application/x-protobuf", "stream-name": args.trace_stream},
        )
        response.raise_for_status()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("command", choices=("inspect", "export"))
    result.add_argument("--storage", type=Path, default=DEFAULT_STORAGE)
    result.add_argument("--session-id", action="append")
    result.add_argument("--request-id", action="append")
    result.add_argument("--started-after-ms", type=int)
    result.add_argument("--started-before-ms", type=int)
    result.add_argument("--max-requests", type=int)
    result.add_argument("--dry-run", action="store_true")
    result.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    result.add_argument("--username", default=DEFAULT_USER)
    result.add_argument("--password-env", default="OPENOBSERVE_PASSWORD")
    result.add_argument("--trace-stream", default="vscode_copilot_raw")
    result.add_argument("--overlap-stream", action="append", default=["default", "vscode_copilot_raw"])
    result.add_argument("--allow-overlap", action="store_true")
    result.add_argument("--batch-size", type=int, default=250)
    result.add_argument("--timeout", type=int, default=120)
    return result


def main() -> int:
    args = parser().parse_args()
    data = inventory(args.storage)
    selected = selected_requests(data.requests, args)
    if not selected:
        raise SystemExit("No matching requests found")
    overlap = overlaps(args, selected)
    payload = build_traces(selected)
    ExportTraceServiceRequest.FromString(payload.SerializeToString())
    result = summary(data, selected, overlap)
    result["trace_spans"] = len(payload.resource_spans[0].scope_spans[0].spans)
    result["trace_payload_bytes"] = len(payload.SerializeToString())
    result["destination"] = args.trace_stream
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.command == "inspect" or args.dry_run:
        print("Inspection/dry run complete; no data was sent.")
        return 0
    if overlap and not args.allow_overlap:
        raise SystemExit("Refusing export because overlapping OTEL chat spans were found")
    if not (args.session_id or args.request_id or args.started_after_ms is not None or args.started_before_ms is not None):
        raise SystemExit("Refusing unbounded export; specify a session, request, or time range")
    export(args, payload)
    print("Export complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
