# OpenCode OTEL History Backfill

`scripts/backfill_opencode_otel.py` converts OpenCode's local SQLite history
into the traces, logs, and metrics emitted by `@devtheops/opencode-plugin-otel`.

The source database is opened with SQLite `mode=ro` and `PRAGMA query_only=ON`.
The script does not alter OpenCode history.

<br>

## Signals

- `opencode.session` agent span for each user turn
- `opencode.llm` client span for each completed assistant message
- `opencode.tool.<name>` span for each completed or failed tool call
- `session.created`, `user_prompt`, `api_request`, `api_error`, `tool_result`,
  `commit`, and estimated `session.idle` log events
- Plugin-compatible session, token, cost, cache, message, model, tool-duration,
  commit, and session-total metrics

All records include `backfill=true`, `backfill.version=1`, and stable OpenCode
source IDs. Trace and span IDs are deterministically derived from those IDs.

The script does not synthesize unavailable retries, permission decisions,
time-to-first-token, original trace IDs, or intermediate session-diff events.
`session.idle` timestamps are estimated from the final stored activity.

<br>

## Inspect And Dry Run

```bash
uv run scripts/backfill_opencode_otel.py inspect

uv run scripts/backfill_opencode_otel.py export \
  --dry-run \
  --session-id ses_example
```

Sessions can also be bounded by their creation timestamp:

```bash
uv run scripts/backfill_opencode_otel.py export \
  --dry-run \
  --created-before-ms <unix-ms>
```

This is useful for excluding sessions already covered by live telemetry.

Dry runs construct, serialize, and parse every protobuf payload without making
network requests.

<br>

## Isolated Test Export

Export a single session to separate streams first. Connection settings are
read from `.env`:

```bash
uv run --env-file .env scripts/backfill_opencode_otel.py export \
  --session-id ses_example \
  --trace-stream opencode-backfill-test \
  --log-stream opencode-backfill-test \
  --include-metrics \
  --metric-prefix opencode_backfill_test.
```

OpenObserve stores periods in metric names as underscores, so the test metric
prefix appears as `opencode_backfill_test_*`.

<br>

## Full Export

Verify the isolated test manually first. Then export everything created before
live telemetry was enabled, using the earliest live event as the exclusive
boundary:

```bash
uv run --env-file .env scripts/backfill_opencode_otel.py export \
  --created-before-ms <first-live-event-unix-ms>
```

Sessions are selected by creation time, so a session that was still active
when live telemetry started is exported in full and partly duplicated.

The exporter defaults to `opencode_raw` for traces and logs, so the
normalization pipeline also writes canonical copies to `ai_normalized`.
Historical metrics are disabled unless `--include-metrics` is explicitly
supplied. Re-running an export can duplicate records; deterministic IDs and
source attributes make duplicates identifiable but do not make OpenObserve
ingestion idempotent.

Non-dry-run exports require an explicit session or time selection. An unbounded
export is rejected.

<br>

## Timestamp Requirements

Span start/end times, log event times, and metric point times come from the
historical SQLite records and are encoded as OTLP Unix nanoseconds. OpenObserve
therefore uses historical `_timestamp` values while `_o2_ingest_ts` reflects
the actual import time.

OpenObserve must have `ZO_INGEST_ALLOWED_UPTO` large enough for the oldest
record before exporting.
