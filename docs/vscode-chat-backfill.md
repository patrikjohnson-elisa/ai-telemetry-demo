# VS Code Chat OTEL Reconstruction

`scripts/backfill_vscode_chat_otel.py` reads VS Code's local `workspaceStorage`
without modifying it and reconstructs missing Copilot chat telemetry.

It supports both storage generations:

- `chatSessions/*.json` snapshots
- `chatSessions/*.jsonl` journals (`kind: 0` base, `kind: 1` replace,
  `kind: 2` append)

Each stored request becomes one aggregate `chat <model>` inference span.
Exact transcript tool timing is emitted as child `execute_tool <name>` spans
when `GitHub.copilot-chat/transcripts/<session>.jsonl` exists.

<br>

## Fidelity

Reconstructed records are marked:

```text
backfill=true
backfill.source=vscode_workspace_storage
backfill.fidelity=turn_aggregate
```

The persisted token values summarize a user turn. They do not recreate every
provider inference round made during an agent loop. Copilot credits are stored
as `copilot_chat.copilot_usage_nano_aiu`; they are not represented as USD cost.

<br>

## Safety

- Live exports require an explicit session, request, or time filter.
- The script queries both `default` and `vscode_copilot_raw` for overlapping
  chat spans and refuses export when overlap is detected.
- `--allow-overlap` exists for deliberate recovery only.
- The default destination is `vscode_copilot_raw`; its active OpenObserve
  pipeline writes content-free canonical copies to `ai_normalized`.

<br>

## Commands

```bash
uv run scripts/backfill_vscode_chat_otel.py inspect \
  --session-id <chat-session-id>

uv run --env-file .env scripts/backfill_vscode_chat_otel.py export \
  --dry-run \
  --request-id <request-id>
```

Use a dedicated test stream for the first import:

```bash
uv run --env-file .env scripts/backfill_vscode_chat_otel.py export \
  --request-id <request-id> \
  --trace-stream vscode-chat-backfill-test
```

The test stream is not attached to the normalization pipeline, so manual
verification should use its raw fields. Compare the imported spans, token
counts, and timestamps with the chat in VS Code. Production imports into
`vscode_copilot_raw` flow to `ai_normalized` automatically.

A typical use is recovering turns from a period when live export was not
running. Use `inspect` to find the affected sessions and requests, and import
one turn to the test stream before importing whole sessions.
