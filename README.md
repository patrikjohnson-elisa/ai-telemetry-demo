# AI tool telemetry demo

Track AI coding assistant usage across tools with OpenTelemetry, and store it in
a local [OpenObserve](https://openobserve.ai/) instance.

Each tool exports its own flavour of OTLP traces. They land in source-specific
raw streams and are then normalized into one content-free stream, so usage can
be compared across tools.

Supported sources:

- [OpenCode](https://opencode.ai/), through the
  [`@devtheops/opencode-plugin-otel`](https://www.npmjs.com/package/@devtheops/opencode-plugin-otel) plugin
- GitHub Copilot Chat in VS Code, through its built-in OTel export

<br>

## Architecture

```text
OpenCode ──────── OTLP ──▶ opencode_raw ───────┐
                                               ├──▶ VRL pipelines ──▶ ai_normalized
VS Code Copilot ─ OTLP ──▶ vscode_copilot_raw ─┘

Local history ── backfill scripts ──▶ raw streams
```

The raw streams keep everything each tool sends, including prompts and
responses when content capture is enabled. `ai_normalized` keeps only
identity, timing, model, token, cost, and tool fields.

<br>

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```bash
uv sync
```

<br>

### OpenObserve

```bash
cp .env.example .env   # then set a real password
docker compose up -d
```

The web UI runs at <http://localhost:5080>. The root user is only created when
the data volume is first initialized, so changing `.env` later does not change
the credentials.

<br>

### Exporting live telemetry

Both tools send OTLP over HTTP to `http://localhost:5080/api/default`. The
`stream-name` header picks the raw stream. Create the `Authorization` value
with `echo -n "$OPENOBSERVE_USER:$OPENOBSERVE_PASSWORD" | base64`, or use an
ingestion token from the OpenObserve UI.

OpenCode, in `~/.config/opencode/opencode.jsonc`:

```jsonc
{
  "plugin": [
    [
      "@devtheops/opencode-plugin-otel",
      {
        "enabled": true,
        "endpoint": "http://localhost:5080/api/default",
        "protocol": "http/protobuf",
        "otlpHeaders": "Authorization=Basic <base64>,stream-name=opencode_raw"
      }
    ]
  ]
}
```

VS Code, in user `settings.json`:

```jsonc
{
  "github.copilot.chat.otel.enabled": true,
  "github.copilot.chat.otel.exporterType": "otlp-http",
  "github.copilot.chat.otel.protocol": "http/protobuf",
  "github.copilot.chat.otel.otlpEndpoint": "http://localhost:5080/api/default",
  "github.copilot.chat.otel.headers": {
    "Authorization": "Basic <base64>",
    "stream-name": "vscode_copilot_raw"
  }
}
```

Both tools can also capture prompt and response content (`capturePromptInLogs`
and `github.copilot.chat.otel.captureContent`). It stays in the raw streams
only.

<br>

## Normalization

Real-time OpenObserve pipelines copy each raw span into `ai_normalized` with a
shared `ai_*` schema. See [docs/ai-normalization.md](docs/ai-normalization.md).

```bash
uv run --env-file .env scripts/manage_ai_normalization.py deploy
```

<br>

## Backfilling history

Both tools keep local history that predates live telemetry. The backfill
scripts read it without modifying it and export reconstructed spans to the raw
streams. Records are marked with `backfill=true`.

- OpenCode: [docs/opencode-backfill.md](docs/opencode-backfill.md)
- VS Code Copilot Chat: [docs/vscode-chat-backfill.md](docs/vscode-chat-backfill.md)

The default VS Code storage location is the macOS one. Use `--storage`
elsewhere.

<br>

## Presentation

The slides use [reveal.js](https://revealjs.com/) with Markdown content in
`slides/slides.md`:

```bash
uv run python -m http.server -d slides 8000
```

Open <http://localhost:8000>, and press `S` for speaker notes.

<br>

## Development

```bash
uv run pytest
```

---

Most of the code and documentation in this repository was written with AI
coding assistants and reviewed by a human.
