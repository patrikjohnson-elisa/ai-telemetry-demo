#!/usr/bin/env python3
"""Backfill OpenCode's local SQLite history into an OTLP endpoint.

The source database is always opened read-only. Trace and span IDs are derived
from stable OpenCode IDs so dry runs and repeated exports are reproducible.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import requests
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, InstrumentationScope, KeyValue
from opentelemetry.proto.logs.v1.logs_pb2 import LogRecord, ResourceLogs, ScopeLogs, SeverityNumber
from opentelemetry.proto.metrics.v1.metrics_pb2 import (
    AggregationTemporality,
    Gauge,
    Histogram,
    HistogramDataPoint,
    Metric,
    NumberDataPoint,
    ResourceMetrics,
    ScopeMetrics,
    Sum,
)
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span, Status


SCRIPT_VERSION = "1"
PLUGIN_VERSION = "1.5.1"
DEFAULT_DB = Path.home() / ".local/share/opencode/opencode.db"
DEFAULT_ENDPOINT = os.environ.get("OPENOBSERVE_URL", "http://localhost:5080").rstrip("/") + "/api/default"
DEFAULT_USER = os.environ.get("OPENOBSERVE_USER", "root@example.com")
NAMESPACE = b"opencode-otel-history-backfill-v1\0"


def json_load(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {}


def stable_bytes(kind: str, value: str, size: int) -> bytes:
    return hashlib.sha256(NAMESPACE + kind.encode() + b"\0" + value.encode()).digest()[:size]


def trace_id(value: str) -> bytes:
    return stable_bytes("trace", value, 16)


def span_id(value: str) -> bytes:
    return stable_bytes("span", value, 8)


def ms_to_ns(value: int | float | None) -> int:
    return int(value or 0) * 1_000_000


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


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


@dataclass
class SessionRecord:
    id: str
    project_id: str
    parent_id: str | None
    title: str
    version: str
    directory: str
    created_ms: int
    updated_ms: int
    agent: str | None
    model: dict[str, Any]
    metadata: dict[str, Any]


@dataclass
class MessageRecord:
    id: str
    session_id: str
    created_ms: int
    updated_ms: int
    data: dict[str, Any]
    parts: list["PartRecord"] = field(default_factory=list)

    @property
    def role(self) -> str:
        return str(self.data.get("role", "unknown"))

    @property
    def completed_ms(self) -> int | None:
        value = self.data.get("time", {}).get("completed")
        return int(value) if value is not None else None

    def text(self, part_type: str = "text") -> str:
        return "".join(str(part.data.get("text", "")) for part in self.parts if part.data.get("type") == part_type)


@dataclass
class PartRecord:
    id: str
    message_id: str
    session_id: str
    created_ms: int
    updated_ms: int
    data: dict[str, Any]


@dataclass
class History:
    sessions: dict[str, SessionRecord]
    messages: dict[str, MessageRecord]
    parts: list[PartRecord]


@dataclass
class Payloads:
    traces: ExportTraceServiceRequest
    logs: ExportLogsServiceRequest
    metrics: ExportMetricsServiceRequest
    counts: dict[str, int]
    min_ms: int
    max_ms: int


def open_database(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def load_history(
    db: sqlite3.Connection,
    session_ids: Sequence[str] | None,
    created_before_ms: int | None = None,
    created_after_ms: int | None = None,
) -> History:
    clauses: list[str] = []
    params: list[Any] = []
    if session_ids:
        placeholders = ",".join("?" for _ in session_ids)
        clauses.append(f"id IN ({placeholders})")
        params.extend(session_ids)
    if created_before_ms is not None:
        clauses.append("time_created < ?")
        params.append(created_before_ms)
    if created_after_ms is not None:
        clauses.append("time_created >= ?")
        params.append(created_after_ms)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

    sessions: dict[str, SessionRecord] = {}
    for row in db.execute(f"SELECT * FROM session{where} ORDER BY time_created", params):
        sessions[row["id"]] = SessionRecord(
            id=row["id"], project_id=row["project_id"], parent_id=row["parent_id"], title=row["title"],
            version=row["version"], directory=row["directory"], created_ms=row["time_created"],
            updated_ms=row["time_updated"], agent=row["agent"], model=json_load(row["model"]),
            metadata=json_load(row["metadata"]),
        )

    if not sessions:
        return History({}, {}, [])
    ids = list(sessions)
    placeholders = ",".join("?" for _ in ids)
    messages: dict[str, MessageRecord] = {}
    for row in db.execute(
        f"SELECT * FROM message WHERE session_id IN ({placeholders}) ORDER BY time_created,id", ids
    ):
        messages[row["id"]] = MessageRecord(
            id=row["id"], session_id=row["session_id"], created_ms=row["time_created"],
            updated_ms=row["time_updated"], data=json_load(row["data"]),
        )
    parts: list[PartRecord] = []
    for row in db.execute(
        f"SELECT * FROM part WHERE session_id IN ({placeholders}) ORDER BY time_created,id", ids
    ):
        part = PartRecord(
            id=row["id"], message_id=row["message_id"], session_id=row["session_id"],
            created_ms=row["time_created"], updated_ms=row["time_updated"], data=json_load(row["data"]),
        )
        parts.append(part)
        if part.message_id in messages:
            messages[part.message_id].parts.append(part)
    return History(sessions, messages, parts)


def resource() -> Resource:
    return Resource(attributes=attributes({
        "service.name": "opencode",
        "app.version": PLUGIN_VERSION,
        "os.type": sys.platform,
        "host.arch": platform.machine(),
        "backfill": True,
        "backfill.version": SCRIPT_VERSION,
        "backfill.source": "opencode.sqlite",
    }))


def scope() -> InstrumentationScope:
    return InstrumentationScope(name="com.opencode.backfill", version=SCRIPT_VERSION)


def agent_attrs(name: str, agent_type: str) -> dict[str, Any]:
    return {"agent": name, "agent.name": name, "agent.type": agent_type, "gen_ai.agent.name": name}


def common_attrs(session: SessionRecord) -> dict[str, Any]:
    return {
        "project.id": session.project_id,
        "session.id": session.id,
        "gen_ai.conversation.id": session.id,
        "opencode.version": session.version,
        "backfill": True,
        "backfill.version": SCRIPT_VERSION,
    }


def provider_name(provider: str) -> str:
    return {
        "amazon-bedrock": "aws.bedrock", "azure": "azure.ai.openai",
        "azure-cognitive-services": "azure.ai.openai", "google": "gcp.gemini",
        "google-vertex": "gcp.vertex_ai", "google-vertex-anthropic": "gcp.vertex_ai",
        "mistral": "mistral_ai", "xai": "x_ai",
    }.get(provider, provider)


def error_summary(error: Any) -> str:
    if not error:
        return "unknown"
    if isinstance(error, dict):
        name = error.get("name", "Error")
        data = error.get("data")
        if isinstance(data, dict) and data.get("message"):
            return f"{name}: {data['message']}"
        return str(name)
    return str(error)


def add_span(
    target: ScopeSpans, *, trace: bytes, span: bytes, parent: bytes = b"", name: str,
    kind: int, start_ms: int, end_ms: int, attrs: dict[str, Any], error: str | None = None,
) -> None:
    status = Status(code=Status.STATUS_CODE_ERROR if error else Status.STATUS_CODE_OK, message=error or "")
    target.spans.append(Span(
        trace_id=trace, span_id=span, parent_span_id=parent, name=name, kind=kind,
        start_time_unix_nano=ms_to_ns(start_ms), end_time_unix_nano=ms_to_ns(max(start_ms, end_ms)),
        attributes=attributes(attrs), status=status,
    ))


def add_log(
    target: ScopeLogs, *, timestamp_ms: int, body: str, attrs: dict[str, Any], error: bool = False,
) -> None:
    target.log_records.append(LogRecord(
        time_unix_nano=ms_to_ns(timestamp_ms), observed_time_unix_nano=ms_to_ns(timestamp_ms),
        severity_number=SeverityNumber.SEVERITY_NUMBER_ERROR if error else SeverityNumber.SEVERITY_NUMBER_INFO,
        severity_text="ERROR" if error else "INFO", body=any_value(body), attributes=attributes(attrs),
    ))


class MetricBuilder:
    def __init__(self, target: ScopeMetrics, prefix: str):
        self.target = target
        self.prefix = prefix
        self.metrics: dict[tuple[str, str], Metric] = {}

    def _metric(self, name: str, unit: str, kind: str, description: str) -> Metric:
        key = (name, kind)
        if key in self.metrics:
            return self.metrics[key]
        metric = Metric(name=f"{self.prefix}{name}", unit=unit, description=description)
        if kind == "sum":
            metric.sum.CopyFrom(Sum(
                aggregation_temporality=AggregationTemporality.AGGREGATION_TEMPORALITY_DELTA,
                is_monotonic=True,
            ))
        elif kind == "gauge":
            metric.gauge.CopyFrom(Gauge())
        elif kind == "histogram":
            metric.histogram.CopyFrom(Histogram(
                aggregation_temporality=AggregationTemporality.AGGREGATION_TEMPORALITY_DELTA
            ))
        self.target.metrics.append(metric)
        self.metrics[key] = self.target.metrics[-1]
        return self.metrics[key]

    def sum(self, name: str, value: int | float, at_ms: int, attrs: dict[str, Any], unit: str, description: str) -> None:
        metric = self._metric(name, unit, "sum", description)
        point = NumberDataPoint(time_unix_nano=ms_to_ns(at_ms), attributes=attributes(attrs))
        if isinstance(value, int):
            point.as_int = value
        else:
            point.as_double = float(value)
        metric.sum.data_points.append(point)

    def histogram(self, name: str, value: int | float, at_ms: int, attrs: dict[str, Any], unit: str, description: str) -> None:
        metric = self._metric(name, unit, "histogram", description)
        metric.histogram.data_points.append(HistogramDataPoint(
            time_unix_nano=ms_to_ns(at_ms), attributes=attributes(attrs), count=1,
            sum=float(value), bucket_counts=[1], explicit_bounds=[], min=float(value), max=float(value),
        ))


def user_model(message: MessageRecord) -> tuple[str, str]:
    model = message.data.get("model") or {}
    return str(model.get("providerID", "unknown")), str(model.get("modelID", "unknown"))


def assistant_tokens(message: MessageRecord) -> dict[str, int]:
    raw = message.data.get("tokens") or {}
    cache = raw.get("cache") or {}
    return {
        "input": int(raw.get("input") or 0), "output": int(raw.get("output") or 0),
        "reasoning": int(raw.get("reasoning") or 0), "cacheRead": int(cache.get("read") or 0),
        "cacheCreation": int(cache.get("write") or 0),
    }


def build_payloads(history: History, metric_prefix: str) -> Payloads:
    trace_request = ExportTraceServiceRequest()
    rs = trace_request.resource_spans.add()
    rs.resource.CopyFrom(resource())
    ss = rs.scope_spans.add()
    ss.scope.CopyFrom(scope())

    log_request = ExportLogsServiceRequest()
    rl = log_request.resource_logs.add()
    rl.resource.CopyFrom(resource())
    sl = rl.scope_logs.add()
    sl.scope.CopyFrom(scope())

    metric_request = ExportMetricsServiceRequest()
    rm = metric_request.resource_metrics.add()
    rm.resource.CopyFrom(resource())
    sm = rm.scope_metrics.add()
    sm.scope.CopyFrom(scope())
    metrics = MetricBuilder(sm, metric_prefix)

    messages_by_session: dict[str, list[MessageRecord]] = defaultdict(list)
    for message in history.messages.values():
        messages_by_session[message.session_id].append(message)
    for values in messages_by_session.values():
        values.sort(key=lambda item: (item.created_ms, item.id))

    assistant_by_parent: dict[str, list[MessageRecord]] = defaultdict(list)
    for message in history.messages.values():
        if message.role == "assistant":
            assistant_by_parent[str(message.data.get("parentID", ""))].append(message)

    counts: dict[str, int] = defaultdict(int)
    timestamps: list[int] = []

    for session in sorted(history.sessions.values(), key=lambda item: item.created_ms):
        session_messages = messages_by_session[session.id]
        agent_type = "subagent" if session.parent_id else "primary"
        default_agent = session.agent or next((str(m.data.get("agent")) for m in session_messages if m.data.get("agent")), "unknown")
        base = common_attrs(session)
        tool_end_times = [
            int(p.data.get("state", {}).get("time", {}).get("end"))
            for p in history.parts
            if p.session_id == session.id
            and p.data.get("type") == "tool"
            and p.data.get("state", {}).get("time", {}).get("end") is not None
        ]
        session_end = max(
            [session.created_ms]
            + [m.completed_ms or m.created_ms for m in session_messages]
            + tool_end_times
        )

        add_log(sl, timestamp_ms=session.created_ms, body="session.created", attrs={
            "event.name": "session.created", "is_subagent": bool(session.parent_id),
            **agent_attrs("unknown", agent_type), **base,
        })
        metrics.sum("session.count", 1, session.created_ms, {
            **base, "is_subagent": bool(session.parent_id)
        }, "{session}", "Count of opencode sessions started")
        counts["session.created"] += 1
        timestamps.append(session.created_ms)

        session_token_total = 0
        session_cost_total = 0.0
        completed_messages = 0

        for user in (m for m in session_messages if m.role == "user"):
            provider, model = user_model(user)
            agent = str(user.data.get("agent") or default_agent)
            prompt = user.text()
            run_trace = trace_id(f"run:{user.id}")
            run_span = span_id(f"run:{user.id}")
            children = sorted(assistant_by_parent.get(user.id, []), key=lambda item: item.created_ms)
            run_end = max([user.created_ms] + [child.completed_ms or child.created_ms for child in children])
            run_tokens = sum(sum(assistant_tokens(child).values()) for child in children if child.completed_ms)
            run_cost = sum(float(child.data.get("cost") or 0) for child in children if child.completed_ms)
            run_attrs = {
                "openinference.span.kind": "AGENT", "input.value": prompt, "input.mime_type": "text/plain",
                "gen_ai.input.messages": json_text([{"role": "user", "content": prompt}]),
                "model": f"{provider}/{model}", "session.is_subagent": bool(session.parent_id),
                "session.total_tokens": run_tokens, "session.total_cost_usd": run_cost,
                "session.total_messages": sum(1 for child in children if child.completed_ms),
                "backfill.source.user_message_id": user.id, **agent_attrs(agent, agent_type), **base,
            }
            add_span(ss, trace=run_trace, span=run_span, name="opencode.session", kind=Span.SPAN_KIND_INTERNAL,
                     start_ms=user.created_ms, end_ms=run_end, attrs=run_attrs)
            add_log(sl, timestamp_ms=user.created_ms, body="user_prompt", attrs={
                "event.name": "user_prompt", "prompt_length": len(prompt), "prompt": prompt,
                "model": f"{provider}/{model}", "backfill.source.user_message_id": user.id,
                **agent_attrs(agent, agent_type), **base,
            })
            counts["session_spans"] += 1
            counts["user_prompt"] += 1
            timestamps.extend([user.created_ms, run_end])

            for assistant in children:
                if not assistant.completed_ms:
                    counts["assistant_incomplete_skipped"] += 1
                    continue
                assistant_agent = str(assistant.data.get("mode") or assistant.data.get("agent") or agent)
                provider_id = str(assistant.data.get("providerID") or "unknown")
                model_id = str(assistant.data.get("modelID") or "unknown")
                tokens = assistant_tokens(assistant)
                total_tokens = sum(tokens.values())
                output = assistant.text()
                cost = float(assistant.data.get("cost") or 0)
                error = error_summary(assistant.data.get("error")) if assistant.data.get("error") else None
                finish = "error" if error else str(assistant.data.get("finish") or "stop")
                llm_span = span_id(f"llm:{assistant.id}")
                llm_attrs = {
                    "openinference.span.kind": "LLM", "llm.system": provider_id, "llm.provider": provider_id,
                    "gen_ai.provider.name": provider_name(provider_id), "llm.model_name": model_id,
                    "gen_ai.response.model": model_id, "input.value": prompt, "input.mime_type": "text/plain",
                    "output.value": output, "output.mime_type": "text/plain",
                    "gen_ai.input.messages": json_text([{"role": "user", "content": prompt}]),
                    "gen_ai.output.messages": json_text([{"role": "assistant", "content": output}]),
                    "llm.token_count.prompt": tokens["input"], "llm.token_count.completion": tokens["output"],
                    "llm.token_count.completion_details.reasoning": tokens["reasoning"],
                    "llm.token_count.prompt_details.cache_read": tokens["cacheRead"],
                    "llm.token_count.prompt_details.cache_write": tokens["cacheCreation"],
                    "llm.token_count.total": total_tokens, "llm.finish_reason": finish,
                    "llm.cost.total": cost, "cost_usd": cost,
                    "duration_ms": assistant.completed_ms - assistant.created_ms,
                    "backfill.source.assistant_message_id": assistant.id,
                    **agent_attrs(assistant_agent, agent_type), **base,
                }
                add_span(ss, trace=run_trace, span=llm_span, parent=run_span, name="opencode.llm",
                         kind=Span.SPAN_KIND_CLIENT, start_ms=assistant.created_ms,
                         end_ms=assistant.completed_ms, attrs=llm_attrs, error=error)
                api_attrs = {
                    "event.name": "api_error" if error else "api_request", "model": model_id,
                    "provider": provider_id, "gen_ai.provider.name": provider_name(provider_id),
                    "duration_ms": assistant.completed_ms - assistant.created_ms,
                    "backfill.source.assistant_message_id": assistant.id,
                    **agent_attrs(assistant_agent, agent_type), **base,
                }
                if error:
                    api_attrs["error"] = error
                else:
                    api_attrs.update({
                        "cost_usd": cost, "input_tokens": tokens["input"], "output_tokens": tokens["output"],
                        "reasoning_tokens": tokens["reasoning"], "cache_read_tokens": tokens["cacheRead"],
                        "cache_creation_tokens": tokens["cacheCreation"],
                    })
                add_log(sl, timestamp_ms=assistant.created_ms, body="api_error" if error else "api_request",
                        attrs=api_attrs, error=bool(error))
                metric_base = {**base, "model": model_id, "agent": assistant_agent}
                for token_type, value in tokens.items():
                    metrics.sum("token.usage", value, assistant.completed_ms, {**metric_base, "type": token_type},
                                "tokens", "Number of tokens used")
                metrics.sum("cost.usage", cost, assistant.completed_ms, metric_base, "USD", "Cost of the opencode session in USD")
                metrics.sum("message.count", 1, assistant.completed_ms, metric_base, "{message}", "Number of completed assistant messages per session")
                metrics.sum("model.usage", 1, assistant.completed_ms, {**metric_base, "provider": provider_id}, "{request}", "Number of completed assistant messages per model and provider")
                if tokens["cacheRead"] > 0:
                    metrics.sum("cache.count", 1, assistant.completed_ms, {**metric_base, "type": "cacheRead"}, "{request}", "Token cache activity per completed assistant message")
                if tokens["cacheCreation"] > 0:
                    metrics.sum("cache.count", 1, assistant.completed_ms, {**metric_base, "type": "cacheCreation"}, "{request}", "Token cache activity per completed assistant message")
                counts["llm_spans"] += 1
                counts["api_error" if error else "api_request"] += 1
                session_token_total += total_tokens
                session_cost_total += cost
                completed_messages += 1
                timestamps.extend([assistant.created_ms, assistant.completed_ms])

                for part in assistant.parts:
                    if part.data.get("type") != "tool":
                        continue
                    state = part.data.get("state") or {}
                    if state.get("status") not in ("completed", "error"):
                        counts["tool_incomplete_skipped"] += 1
                        continue
                    timing = state.get("time") or {}
                    start_ms = int(timing.get("start") or part.created_ms)
                    end_ms = int(timing.get("end") or start_ms)
                    success = state.get("status") == "completed"
                    tool = str(part.data.get("tool") or "unknown")
                    call_id = str(part.data.get("callID") or part.id)
                    tool_output = str(state.get("output") or "") if success else str(state.get("error") or "unknown")
                    tool_error = None if success else tool_output
                    tool_attrs = {
                        "openinference.span.kind": "TOOL", "tool.id": call_id, "tool.name": tool,
                        "tool.parameters": json_text(state.get("input") or {}),
                        "input.value": json_text(state.get("input") or {}), "input.mime_type": "application/json",
                        "output.value": tool_output, "output.mime_type": "text/plain", "tool.success": success,
                        "tool.result_size_bytes": len(tool_output.encode()), "tool.error": tool_error,
                        "backfill.source.part_id": part.id, **agent_attrs(assistant_agent, agent_type), **base,
                    }
                    add_span(ss, trace=run_trace, span=span_id(f"tool:{part.id}"), parent=llm_span,
                             name=f"opencode.tool.{tool}", kind=Span.SPAN_KIND_INTERNAL,
                             start_ms=start_ms, end_ms=end_ms, attrs=tool_attrs, error=tool_error)
                    tool_log_attrs = {
                        "event.name": "tool_result", "tool_name": tool, "success": success,
                        "duration_ms": end_ms - start_ms, "backfill.source.part_id": part.id,
                        **({"tool_result_size_bytes": len(tool_output.encode())} if success else {"error": tool_error}),
                        **agent_attrs(assistant_agent, agent_type), **base,
                    }
                    add_log(sl, timestamp_ms=start_ms, body="tool_result", attrs=tool_log_attrs, error=not success)
                    metrics.histogram("tool.duration", end_ms - start_ms, end_ms,
                                      {**base, "tool_name": tool, "success": success}, "ms", "Duration of tool executions in milliseconds")
                    counts["tool_spans"] += 1
                    counts["tool_result"] += 1
                    timestamps.extend([start_ms, end_ms])
                    command = str((state.get("input") or {}).get("command") or "")
                    if tool == "bash" and "git commit" in command:
                        add_log(sl, timestamp_ms=start_ms, body="commit", attrs={
                            "event.name": "commit", "backfill.source.part_id": part.id,
                            **agent_attrs(assistant_agent, agent_type), **base,
                        })
                        metrics.sum("commit.count", 1, end_ms, base, "{commit}", "Number of git commits created")
                        counts["commit"] += 1

        add_log(sl, timestamp_ms=session_end, body="session.idle", attrs={
            "event.name": "session.idle", "total_tokens": session_token_total,
            "total_cost_usd": session_cost_total, "total_messages": completed_messages,
            "backfill.estimated_timestamp": True, **agent_attrs(default_agent, agent_type), **base,
        })
        metrics.histogram("session.duration", session_end - session.created_ms, session_end, base, "ms", "Duration of a session from created to idle in milliseconds")
        metrics.histogram("session.token.total", session_token_total, session_end, base, "tokens", "Total tokens consumed per session")
        metrics.histogram("session.cost.total", session_cost_total, session_end, base, "USD", "Total cost per session in USD")
        counts["session.idle"] += 1
        timestamps.append(session_end)

    return Payloads(trace_request, log_request, metric_request, dict(counts), min(timestamps or [0]), max(timestamps or [0]))


def chunks(values: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]


def split_trace_request(request: ExportTraceServiceRequest, batch_size: int) -> Iterator[ExportTraceServiceRequest]:
    spans = list(request.resource_spans[0].scope_spans[0].spans)
    for batch in chunks(spans, batch_size):
        output = ExportTraceServiceRequest()
        rs = output.resource_spans.add()
        rs.resource.CopyFrom(request.resource_spans[0].resource)
        ss = rs.scope_spans.add()
        ss.scope.CopyFrom(request.resource_spans[0].scope_spans[0].scope)
        ss.spans.extend(batch)
        yield output


def split_log_request(request: ExportLogsServiceRequest, batch_size: int) -> Iterator[ExportLogsServiceRequest]:
    records = list(request.resource_logs[0].scope_logs[0].log_records)
    for batch in chunks(records, batch_size):
        output = ExportLogsServiceRequest()
        rl = output.resource_logs.add()
        rl.resource.CopyFrom(request.resource_logs[0].resource)
        sl = rl.scope_logs.add()
        sl.scope.CopyFrom(request.resource_logs[0].scope_logs[0].scope)
        sl.log_records.extend(batch)
        yield output


def post_protobuf(url: str, payload: Any, username: str, password: str, stream: str, timeout: int) -> None:
    response = requests.post(
        url, data=payload.SerializeToString(), auth=(username, password), timeout=timeout,
        headers={"Content-Type": "application/x-protobuf", "stream-name": stream},
    )
    response.raise_for_status()


def export_payloads(args: argparse.Namespace, payloads: Payloads) -> None:
    endpoint = args.endpoint.rstrip("/")
    password = os.environ.get(args.password_env)
    if not password:
        raise SystemExit(f"Set {args.password_env} to the OpenObserve password before exporting")
    for request in split_trace_request(payloads.traces, args.batch_size):
        post_protobuf(f"{endpoint}/v1/traces", request, args.username, password, args.trace_stream, args.timeout)
    for request in split_log_request(payloads.logs, args.batch_size):
        post_protobuf(f"{endpoint}/v1/logs", request, args.username, password, args.log_stream, args.timeout)
    if args.include_metrics:
        post_protobuf(f"{endpoint}/v1/metrics", payloads.metrics, args.username, password, args.metric_stream, args.timeout)


def payload_summary(payloads: Payloads) -> dict[str, Any]:
    trace_count = len(payloads.traces.resource_spans[0].scope_spans[0].spans)
    log_count = len(payloads.logs.resource_logs[0].scope_logs[0].log_records)
    metric_points = 0
    metrics = payloads.metrics.resource_metrics[0].scope_metrics[0].metrics
    for metric in metrics:
        metric_points += len(metric.sum.data_points) + len(metric.gauge.data_points) + len(metric.histogram.data_points)
    return {
        "counts": payloads.counts, "trace_spans": trace_count, "log_records": log_count,
        "metric_series": len(metrics), "metric_points": metric_points,
        "min_timestamp_ms": payloads.min_ms, "max_timestamp_ms": payloads.max_ms,
        "trace_payload_bytes": len(payloads.traces.SerializeToString()),
        "log_payload_bytes": len(payloads.logs.SerializeToString()),
        "metric_payload_bytes": len(payloads.metrics.SerializeToString()),
    }


def inspect_history(history: History) -> dict[str, Any]:
    roles: dict[str, int] = defaultdict(int)
    part_types: dict[str, int] = defaultdict(int)
    completed = 0
    token_totals: dict[str, int] = defaultdict(int)
    total_cost = 0.0
    for message in history.messages.values():
        roles[message.role] += 1
        if message.role == "assistant" and message.completed_ms:
            completed += 1
            for name, value in assistant_tokens(message).items():
                token_totals[name] += value
            total_cost += float(message.data.get("cost") or 0)
    for part in history.parts:
        part_types[str(part.data.get("type", "unknown"))] += 1
    return {
        "sessions": len(history.sessions), "messages": len(history.messages), "parts": len(history.parts),
        "roles": dict(roles), "part_types": dict(part_types), "completed_assistant_messages": completed,
        "token_totals": dict(token_totals), "cost_total_usd": total_cost,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--database", type=Path, default=DEFAULT_DB)
    sub = result.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect", help="Summarize locally stored history without creating OTLP payloads")
    inspect.add_argument("--session-id", action="append")
    inspect.add_argument("--created-before-ms", type=int, help="Include sessions created before this Unix millisecond timestamp")
    inspect.add_argument("--created-after-ms", type=int, help="Include sessions created at or after this Unix millisecond timestamp")
    export = sub.add_parser("export", help="Build OTLP payloads and optionally send them")
    export.add_argument("--session-id", action="append", help="Export only this session; repeat for multiple sessions")
    export.add_argument("--created-before-ms", type=int, help="Include sessions created before this Unix millisecond timestamp")
    export.add_argument("--created-after-ms", type=int, help="Include sessions created at or after this Unix millisecond timestamp")
    export.add_argument("--dry-run", action="store_true", help="Build and validate payloads without sending them")
    export.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    export.add_argument("--username", default=DEFAULT_USER)
    export.add_argument("--password-env", default="OPENOBSERVE_PASSWORD")
    export.add_argument("--trace-stream", default="opencode_raw")
    export.add_argument("--log-stream", default="opencode_raw")
    export.add_argument("--include-metrics", action="store_true", help="Also export reconstructed historical metrics (disabled by default)")
    export.add_argument("--metric-stream", default="default")
    export.add_argument("--metric-prefix", default="opencode.")
    export.add_argument("--batch-size", type=int, default=250)
    export.add_argument("--timeout", type=int, default=60)
    return result


def main() -> int:
    args = parser().parse_args()
    if not args.database.exists():
        raise SystemExit(f"OpenCode database not found: {args.database}")
    with open_database(args.database) as db:
        history = load_history(db, args.session_id, args.created_before_ms, args.created_after_ms)
    if not history.sessions:
        raise SystemExit("No matching sessions found")
    if args.command == "inspect":
        print(json.dumps(inspect_history(history), indent=2, sort_keys=True))
        return 0
    if not args.dry_run and not (args.session_id or args.created_before_ms is not None or args.created_after_ms is not None):
        raise SystemExit(
            "Refusing unbounded export. Specify --session-id, --created-before-ms, or --created-after-ms."
        )
    payloads = build_payloads(history, args.metric_prefix)
    # Parsing serialized output catches malformed protobuf construction before any network request.
    ExportTraceServiceRequest.FromString(payloads.traces.SerializeToString())
    ExportLogsServiceRequest.FromString(payloads.logs.SerializeToString())
    ExportMetricsServiceRequest.FromString(payloads.metrics.SerializeToString())
    summary = payload_summary(payloads)
    summary["destinations"] = {
        "traces": args.trace_stream,
        "logs": args.log_stream,
        "metrics": args.metric_stream if args.include_metrics else None,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    if args.dry_run:
        print("Dry run complete; no data was sent.")
        return 0
    export_payloads(args, payloads)
    print("Export complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
