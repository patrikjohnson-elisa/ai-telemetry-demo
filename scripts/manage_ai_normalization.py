#!/usr/bin/env python3
"""Validate and deploy source-specific OpenObserve AI normalization pipelines."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, InstrumentationScope, KeyValue
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import Span, Status


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_URL = os.environ.get("OPENOBSERVE_URL", "http://localhost:5080")
DEFAULT_USER = os.environ.get("OPENOBSERVE_USER", "root@example.com")
DEFAULT_ORG = "default"
DASHBOARD_DIR = REPO_ROOT / "openobserve/dashboards"
DASHBOARD_FOLDER = "default"
EXPORT_ONLY_DASHBOARD_KEYS = ("dashboardId", "owner", "created", "updatedAt")
FORBIDDEN_NORMALIZED_PREFIXES = (
    "copilot_chat_", "github_copilot_", "llm_", "session_", "tool_",
    "gen_ai_input_", "gen_ai_output_", "gen_ai_system_", "gen_ai_tool_call_",
)

SOURCES = {
    "opencode": {
        "stream": "opencode_raw",
        "function": "normalize_opencode",
        "vrl": REPO_ROOT / "openobserve/pipelines/normalize_opencode.vrl",
        "pipeline": "normalize_opencode_traces",
        "input_id": "04a9a296-6cbf-44e5-bd3a-1fe8fa534001",
        "raw_id": "04a9a296-6cbf-44e5-bd3a-1fe8fa534002",
        "function_id": "04a9a296-6cbf-44e5-bd3a-1fe8fa534003",
        "normalized_id": "04a9a296-6cbf-44e5-bd3a-1fe8fa534004",
    },
    "vscode_copilot": {
        "stream": "vscode_copilot_raw",
        "function": "normalize_vscode_copilot",
        "vrl": REPO_ROOT / "openobserve/pipelines/normalize_vscode_copilot.vrl",
        "pipeline": "normalize_vscode_copilot_traces",
        "input_id": "d5189874-9eb9-4052-b2f4-ac52fc7a4001",
        "raw_id": "d5189874-9eb9-4052-b2f4-ac52fc7a4002",
        "function_id": "d5189874-9eb9-4052-b2f4-ac52fc7a4003",
        "normalized_id": "d5189874-9eb9-4052-b2f4-ac52fc7a4004",
    },
}

def otel_value(value: Any) -> AnyValue:
    result = AnyValue()
    if isinstance(value, bool):
        result.bool_value = value
    elif isinstance(value, int):
        result.int_value = value
    elif isinstance(value, float):
        result.double_value = value
    else:
        result.string_value = str(value)
    return result


def otel_attributes(values: dict[str, Any]) -> list[KeyValue]:
    return [KeyValue(key=key, value=otel_value(value)) for key, value in values.items()]


class OpenObserve:
    def __init__(self, url: str, org: str, username: str, password: str):
        self.url = url.rstrip("/")
        self.org = org
        self.session = requests.Session()
        self.session.auth = (username, password)

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.session.request(method, f"{self.url}/api/{self.org}{path}", timeout=60, **kwargs)
        if not response.ok:
            raise RuntimeError(f"{method} {path} failed ({response.status_code}): {response.text}")
        return response.json() if response.content else {}

    def recent_spans(self, stream: str, size: int = 20) -> list[dict[str, Any]]:
        now_us = int(__import__("time").time() * 1_000_000)
        body = {"query": {
            "sql": f'SELECT * FROM "{stream}" ORDER BY _timestamp DESC LIMIT {size}',
            "start_time": now_us - 7 * 24 * 60 * 60 * 1_000_000,
            "end_time": now_us + 60 * 60 * 1_000_000,
            "from": 0,
            "size": size,
        }}
        return self.request("POST", "/_search?type=traces", json=body).get("hits", [])

    def search_spans(self, stream: str, operation: str) -> list[dict[str, Any]]:
        now_us = int(__import__("time").time() * 1_000_000)
        body = {"query": {
            "sql": f'SELECT * FROM "{stream}" WHERE operation_name = \'{operation}\' ORDER BY _timestamp DESC LIMIT 20',
            "start_time": now_us - 60 * 60 * 1_000_000,
            "end_time": now_us + 60 * 60 * 1_000_000,
            "from": 0,
            "size": 20,
        }}
        return self.request("POST", "/_search?type=traces", json=body).get("hits", [])

    def send_smoke_span(self, source_name: str, source: dict[str, str], operation: str) -> None:
        now_ns = __import__("time").time_ns()
        request = ExportTraceServiceRequest()
        resource_spans = request.resource_spans.add()
        resource_spans.resource.CopyFrom(Resource(attributes=otel_attributes({
            "service.name": "opencode" if source_name == "opencode" else "copilot-chat",
        })))
        scope_spans = resource_spans.scope_spans.add()
        scope_spans.scope.CopyFrom(InstrumentationScope(name="ai-normalization-smoke", version="1"))
        common = {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": "smoke-provider",
            "gen_ai.response.model": "smoke-model",
            "gen_ai.agent.name": "smoke-agent",
            "gen_ai.conversation.id": f"smoke-{source_name}",
        }
        if source_name == "opencode":
            source_attrs = {
                "openinference.span.kind": "LLM",
                "session.id": "smoke-opencode",
                "llm.model_name": "smoke-model",
                "llm.provider": "smoke-provider",
                "llm.token_count.prompt": 101,
                "llm.token_count.completion": 23,
                "llm.token_count.completion_details.reasoning": 7,
                "llm.token_count.prompt_details.cache_read": 80,
                "llm.token_count.prompt_details.cache_write": 3,
                "llm.token_count.total": 214,
                "llm.cost.total": 0.0123,
                "input.value": "NORMALIZATION_SMOKE_CONTENT_MUST_NOT_LEAK",
            }
        else:
            source_attrs = {
                "gen_ai.usage.input_tokens": 101,
                "gen_ai.usage.output_tokens": 23,
                "gen_ai.usage.reasoning_tokens": 7,
                "gen_ai.usage.cache_read_input_tokens": 80,
                "gen_ai.usage.cache_creation_input_tokens": 3,
                "gen_ai.usage.total_tokens": 124,
                "gen_ai.usage.cost": 0.0123,
                "copilot_chat.user_request": "NORMALIZATION_SMOKE_CONTENT_MUST_NOT_LEAK",
            }
        scope_spans.spans.append(Span(
            trace_id=__import__("hashlib").sha256(f"smoke-trace-{source_name}".encode()).digest()[:16],
            span_id=__import__("hashlib").sha256(f"smoke-span-{source_name}".encode()).digest()[:8],
            name=operation,
            kind=Span.SPAN_KIND_CLIENT,
            start_time_unix_nano=now_ns,
            end_time_unix_nano=now_ns + 5_000_000,
            attributes=otel_attributes({**common, **source_attrs}),
            status=Status(code=Status.STATUS_CODE_OK),
        ))
        response = self.session.post(
            f"{self.url}/api/{self.org}/v1/traces",
            data=request.SerializeToString(), timeout=60,
            headers={"Content-Type": "application/x-protobuf", "stream-name": source["stream"]},
        )
        if not response.ok:
            raise RuntimeError(f"Smoke OTLP export failed ({response.status_code}): {response.text}")

    def test_function(self, function: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = self.request("POST", "/functions/test", json={
            "function": function, "events": events, "trans_type": 0,
        })
        failures = [item for item in result.get("results", []) if item.get("message")]
        if failures:
            raise RuntimeError(f"VRL validation failed: {json.dumps(failures, indent=2)}")
        return [item["event"] for item in result.get("results", [])]


def function_text(source: dict[str, str]) -> str:
    return Path(source["vrl"]).read_text()


def validate_source(client: OpenObserve, name: str, source: dict[str, str]) -> dict[str, Any]:
    samples = client.recent_spans(source["stream"])
    if not samples:
        raise RuntimeError(f'No recent spans found in {source["stream"]}')
    output = client.test_function(function_text(source), samples)
    if len(output) != len(samples):
        raise RuntimeError(f"{name}: function returned {len(output)} of {len(samples)} records")
    required = {"_timestamp", "trace_id", "span_id", "ai_schema_version", "ai_source", "ai_event_kind"}
    for event in output:
        missing = required - event.keys()
        if missing:
            raise RuntimeError(f"{name}: normalized event is missing {sorted(missing)}")
        forbidden = [key for key in event if key.startswith(FORBIDDEN_NORMALIZED_PREFIXES)]
        if forbidden:
            raise RuntimeError(f"{name}: normalized event leaked content/source fields: {forbidden}")
    return {
        "source": name,
        "input_records": len(samples),
        "output_records": len(output),
        "event_kinds": sorted({event["ai_event_kind"] for event in output}),
        "fields": sorted({key for event in output for key in event}),
    }


def function_payload(source: dict[str, str]) -> dict[str, Any]:
    return {
        "name": source["function"],
        "function": function_text(source),
        "numArgs": 0,
        "transType": 0,
    }


def stream_node(node_id: str, io_type: str, stream: str, x: int, y: int) -> dict[str, Any]:
    return {
        "id": node_id, "io_type": io_type, "position": {"x": x, "y": y},
        "data": {"node_type": "stream", "org_id": DEFAULT_ORG,
                 "stream_name": stream, "stream_type": "traces"},
    }


def pipeline_payload(source: dict[str, str]) -> dict[str, Any]:
    input_id = source["input_id"]
    raw_id = source["raw_id"]
    function_id = source["function_id"]
    normalized_id = source["normalized_id"]
    return {
        "name": source["pipeline"],
        "description": f'Preserve {source["stream"]} and write content-free canonical AI spans to ai_normalized.',
        "enabled": True,
        "source": {"source_type": "realtime"},
        "nodes": [
            stream_node(input_id, "input", source["stream"], 100, 200),
            stream_node(raw_id, "output", source["stream"], 500, 100),
            {
                "id": function_id, "io_type": "default", "position": {"x": 300, "y": 300},
                "data": {"node_type": "function", "name": source["function"], "after_flatten": True},
            },
            stream_node(normalized_id, "output", "ai_normalized", 500, 300),
        ],
        "edges": [
            {"id": f"e{input_id}-{raw_id}", "source": input_id, "target": raw_id},
            {"id": f"e{input_id}-{function_id}", "source": input_id, "target": function_id},
            {"id": f"e{function_id}-{normalized_id}", "source": function_id, "target": normalized_id},
        ],
    }


def deploy(client: OpenObserve) -> None:
    existing_functions = {item["name"] for item in client.request("GET", "/functions").get("list", [])}
    for source in SOURCES.values():
        payload = function_payload(source)
        if source["function"] in existing_functions:
            client.request("PUT", f'/functions/{source["function"]}', json=payload)
            print(f'Updated function {source["function"]}')
        else:
            client.request("POST", "/functions", json=payload)
            print(f'Created function {source["function"]}')

    existing_pipelines = {
        item["name"]: item for item in client.request("GET", "/pipelines").get("list", [])
    }
    for source in SOURCES.values():
        payload = pipeline_payload(source)
        existing = existing_pipelines.get(source["pipeline"])
        if existing:
            payload.update({"pipeline_id": existing["pipeline_id"], "version": existing["version"], "org": client.org})
            client.request("PUT", "/pipelines", json=payload)
            print(f'Updated pipeline {source["pipeline"]}')
        else:
            client.request("POST", "/pipelines", json=payload)
            print(f'Created pipeline {source["pipeline"]}')


def keep_variable_values(dashboard: dict[str, Any], current: dict[str, Any]) -> None:
    """Keep variable values set on the instance, such as a budget or time zone."""
    current_values = {
        variable["name"]: variable.get("value")
        for variable in current.get("variables", {}).get("list", [])
    }
    for variable in dashboard.get("variables", {}).get("list", []):
        if variable["name"] in current_values:
            variable["value"] = current_values[variable["name"]]


def deploy_dashboards(client: OpenObserve) -> None:
    listed = client.request("GET", f"/dashboards?folder={DASHBOARD_FOLDER}").get("dashboards", [])
    existing = {item["title"]: item for item in listed}

    for path in sorted(DASHBOARD_DIR.glob("*.json")):
        dashboard = json.loads(path.read_text())
        for key in EXPORT_ONLY_DASHBOARD_KEYS:
            dashboard.pop(key, None)

        match = existing.get(dashboard["title"])
        if match:
            current = client.request("GET", f'/dashboards/{match["dashboard_id"]}')["v8"]
            keep_variable_values(dashboard, current)
            dashboard["dashboardId"] = match["dashboard_id"]
            client.request(
                "PUT", f'/dashboards/{match["dashboard_id"]}?folder={DASHBOARD_FOLDER}&hash={match["hash"]}',
                json=dashboard,
            )
            print(f'Updated dashboard {dashboard["title"]}')
        else:
            client.request("POST", f"/dashboards?folder={DASHBOARD_FOLDER}", json=dashboard)
            print(f'Created dashboard {dashboard["title"]}')


def status(client: OpenObserve) -> dict[str, Any]:
    functions = client.request("GET", "/functions").get("list", [])
    pipelines = client.request("GET", "/pipelines").get("list", [])
    return {
        "functions": [item for item in functions if item.get("name") in {s["function"] for s in SOURCES.values()}],
        "pipelines": [item for item in pipelines if item.get("name") in {s["pipeline"] for s in SOURCES.values()}],
    }


def smoke_test(client: OpenObserve) -> dict[str, Any]:
    run_id = str(__import__("time").time_ns())
    operations = {
        name: f"ai-normalization.smoke.{name}.{run_id}"
        for name in SOURCES
    }
    for name, source in SOURCES.items():
        client.send_smoke_span(name, source, operations[name])

    results: list[dict[str, Any]] = []
    for name, source in SOURCES.items():
        operation = operations[name]
        raw = client.search_spans(source["stream"], operation)
        normalized = client.search_spans("ai_normalized", operation)
        if len(raw) != 1 or len(normalized) != 1:
            raise RuntimeError(
                f"{name}: expected one raw and normalized smoke span, got raw={len(raw)}, normalized={len(normalized)}"
            )
        raw_record = raw[0]
        record = normalized[0]
        leaked = [key for key, value in record.items() if "NORMALIZATION_SMOKE_CONTENT" in str(value)]
        if leaked:
            raise RuntimeError(f"{name}: normalized content leaked through fields {leaked}")
        expected_total = 214 if name == "opencode" else 124
        expected = {
            "ai_source": name,
            "ai_event_kind": "inference",
            "ai_model": "smoke-model",
            "ai_provider": "smoke-provider",
            "ai_input_tokens": 101,
            "ai_output_tokens": 23,
            "ai_reasoning_tokens": 7,
            "ai_cache_read_tokens": 80,
            "ai_cache_write_tokens": 3,
            "ai_total_tokens": expected_total,
            "ai_cost_usd": 0.0123,
        }
        mismatches = {
            key: {"expected": value, "actual": record.get(key)}
            for key, value in expected.items()
            if str(record.get(key)) != str(value)
        }
        if mismatches:
            raise RuntimeError(f"{name}: normalized values differ: {json.dumps(mismatches, indent=2)}")
        results.append({
            "source": name,
            "operation_name": operation,
            "raw_stream": source["stream"],
            "raw_records": len(raw),
            "normalized_records": len(normalized),
            "trace_id_preserved": raw_record.get("trace_id") == record.get("trace_id"),
            "span_id_preserved": raw_record.get("span_id") == record.get("span_id"),
            "normalized": {key: record.get(key) for key in expected},
        })
    return {"smoke_test": "passed", "results": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "deploy", "dashboards", "status", "smoke-test"))
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--org", default=DEFAULT_ORG)
    parser.add_argument("--username", default=DEFAULT_USER)
    parser.add_argument("--password-env", default="OPENOBSERVE_PASSWORD")
    args = parser.parse_args()
    password = os.environ.get(args.password_env)
    if not password:
        raise SystemExit(f"Set {args.password_env} before running this command")
    client = OpenObserve(args.url, args.org, args.username, password)
    if args.command == "validate":
        print(json.dumps([validate_source(client, name, source) for name, source in SOURCES.items()], indent=2))
    elif args.command == "deploy":
        for name, source in SOURCES.items():
            print(json.dumps(validate_source(client, name, source), indent=2))
        deploy(client)
        print(json.dumps(status(client), indent=2))
    elif args.command == "dashboards":
        deploy_dashboards(client)
    elif args.command == "status":
        print(json.dumps(status(client), indent=2))
    else:
        print(json.dumps(smoke_test(client), indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (requests.RequestException, RuntimeError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1)
