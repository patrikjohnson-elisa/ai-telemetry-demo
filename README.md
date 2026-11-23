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

---

Most of the code and documentation in this repository was written with AI
coding assistants and reviewed by a human.
