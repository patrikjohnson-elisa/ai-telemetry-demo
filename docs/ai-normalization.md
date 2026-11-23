# AI Trace Normalization

OpenObserve receives source-specific raw OTLP data in:

- `opencode_raw`
- `vscode_copilot_raw`

Two real-time pipelines preserve those raw records and write content-free,
canonical trace records to `ai_normalized`.

<br>

## Canonical Fields

The normalized stream retains trace identity, parentage, timing, operation,
service, and status fields. It adds:

```text
ai_schema_version
ai_source
ai_event_kind
ai_model
ai_provider
ai_conversation_id
ai_agent
ai_input_tokens
ai_output_tokens
ai_reasoning_tokens
ai_cache_read_tokens
ai_cache_write_tokens
ai_total_tokens
ai_cost_usd
ai_tool_name
ai_duration_ms
ai_success
```

`ai_event_kind` is one of `agent`, `inference`, `tool`, `embedding`, or
`other`. Usage and cost are populated only where the source span reports them.
Missing values remain absent rather than being replaced with zero.

Prompts, responses, reasoning content, tool arguments/results, span events,
and source-specific namespaces are excluded. Complete records remain in the
raw streams.

<br>

## Management

The connection settings are read from `.env` (see `.env.example`):

```bash
uv run --env-file .env scripts/manage_ai_normalization.py validate
uv run --env-file .env scripts/manage_ai_normalization.py deploy
uv run --env-file .env scripts/manage_ai_normalization.py smoke-test
uv run --env-file .env scripts/manage_ai_normalization.py status
```

The VRL functions live in `openobserve/pipelines/`.

The repeatable smoke test sends one uniquely named synthetic span to each raw stream. It
verifies raw preservation, normalized output, trace/span identity, token and
cost mapping, and that sentinel content does not leak into `ai_normalized`.

Deployment is idempotent: existing functions and pipelines are updated.

OpenObserve allows one real-time pipeline per source stream, so each source has
its own function and fan-out pipeline. Every pipeline has a direct destination
back to its raw stream plus a transformed destination to `ai_normalized`.

Existing records are not processed retroactively. Historical OTLP replay into
the raw streams will pass through these pipelines during ingestion.
