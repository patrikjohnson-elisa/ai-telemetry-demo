# AI tool telemetry

One view across AI coding assistants with OpenTelemetry

Note:
About 20 minutes including demos.

---

## The problem

- Several AI tools in daily use
- Each has its own usage view, or none
- No shared answer to "what did I use, and what did it cost?"

Note:

---

## What the tools export

- OpenCode: OTel plugin, OpenInference-style spans
- VS Code Copilot Chat: built-in OTel export, GenAI semantic conventions
- Same idea, different field names

Note:

---

## Architecture

```text
OpenCode ──────── OTLP ──▶ opencode_raw ───────┐
                                               ├──▶ VRL pipelines ──▶ ai_normalized
VS Code Copilot ─ OTLP ──▶ vscode_copilot_raw ─┘

Local history ── backfill scripts ──▶ raw streams
```

Note:
OpenObserve runs locally in Docker.

---

## Raw vs normalized

- Raw streams keep everything, including prompts and responses
- `ai_normalized` keeps identity, timing, model, tokens, cost, tools
- No content in the shared stream

Note:

---

## The canonical schema

`ai_source` · `ai_event_kind` · `ai_model` · `ai_conversation_id`

`ai_input_tokens` · `ai_output_tokens` · `ai_cache_read_tokens` · `ai_cost_usd`

`ai_tool_name` · `ai_duration_ms` · `ai_success`

Note:

---

## Backfilling history

- Both tools keep local history
- Reconstructed into the same spans, marked `backfill=true`
- What can't be recovered: retries, time to first token, original trace IDs

Note:

---

## Demo: raw traces

Note:
One conversation, raw stream vs normalized stream.

---

## Demo: dashboards

Note:

---

## Findings

Note:
Real usage numbers.

---

## Caveats

- Token counts can be double counted across span kinds
- Backfilled VS Code data is per turn, not per inference call
- Some fields only exist for one tool

Note:

---

## Next

- Project and branch per session
- Context window limit and utilization
- Session ratings as telemetry events

Note:

---

## Questions

Note:
