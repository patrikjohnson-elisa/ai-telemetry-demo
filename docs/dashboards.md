# Dashboards

OpenObserve dashboards are stored as JSON in `openobserve/dashboards/`, in the
format OpenObserve uses for dashboard export. Deploy them with:

```bash
uv run --env-file .env scripts/manage_ai_normalization.py dashboards
```

Dashboards are matched by title in the `default` folder. Existing ones are
updated, missing ones are created. Variable values already set on the
instance, such as the credit budget or time zone, are kept on update. To
change a dashboard, edit it in the UI, export it, and replace the JSON file.
Export-only fields such as `dashboardId` and `owner` are ignored on deploy.

<br>

## Terms

The dashboards use these terms:

| Term | Meaning |
|---|---|
| AI tool | The coding tool: `opencode`, `vscode_copilot`, or `vscode_copilot background` |
| Agent tool | A tool the agent calls, such as `read_file` or `shell` |
| Model call | One call to a model, an `inference` span |
| Context | Input tokens of a model call: uncached input + cache read + cache write |
| Cache read % of context | Share of the context read from the prompt cache |
| Cache write % of credits | Share of the estimated credits spent writing context to the cache |
| Break | Time since the previous model call in the same conversation |

<br>

## AI tools overview

Usage across AI tools from `ai_normalized`, in five parts:

- Budget: estimated GitHub AI credits this calendar month (UTC) against the
  `credit_budget` dashboard variable, the projected month-end total, and
  credits per day by token type
- Activity: tokens per day by AI tool, and the average context per call per
  day. Days start at midnight in the time zone set in the `timezone` variable
- Most expensive conversations, with their project, average context per call,
  and cache write % of credits. Click a row to open it in the AI conversation
  dashboard
- Per AI tool and per model totals, and a simplistic estimate of what the same
  tokens would cost on a cheaper model
- The most called agent tools

The monthly panels need a time range that covers the start of the month. The
default range is the last 30 days, which misses the first day on the 31st, so
pick the range to match the month when it matters. Change the budget and time
zone in the dashboard settings, under variables.

Token and credit panels only count `ai_event_kind = 'inference'`. Agent spans
in VS Code repeat the token counts of their inference spans, so summing all
spans double counts.

<br>

## AI conversation

One conversation, set in the `conversation` variable:

- Details: AI tool, project, main model, start and last call
- Credits, model calls, average context per call, and cache write % of credits
- Context per call and credits per call, by token type and call number
- Agent tool calls, and every model call in order with the break since the
  previous call

The project is the repo and branch, looked up from `vscode_copilot_raw`. It is
known for VS Code conversations started in a repo. OpenCode telemetry has no
readable project.

<br>

## Cache writes after a break

Each model call sends the whole conversation again. Normally the earlier part
is still in the prompt cache, so the call reads it cheaply and writes only the
new messages. The cache expires after about 5 minutes without use, so the
first call after a longer break writes the whole context again, at 25 times
the price of reading it. The tall cache write bars in the context per call
chart are these calls.

<br>

## AI efficiency

Patterns across conversations, in the selected time range:

- Cache read % of context, cache write % of credits, average context per
  call, and the share of failed agent tool calls
- Credits and context per call by conversation length. Calls are numbered
  from the start of the time range, so conversations that started earlier
  count from there
- Cache read % of context and cache write % of credits per day
- Share of model calls and of credits by context size
- Cache write credits by break length, which shows what the breaks cost
- Agent tool failures

<br>

## Background calls

VS Code makes many small model calls besides chat: next edit suggestions,
conversation titles, progress messages, and similar. They are grouped as
`vscode_copilot background`, identified by `ai_agent`:

- `XtabProvider`
- `copilotLanguageModelWrapper*`
- `progressMessages`, `title`, `intentDetection`, `healApplyPatch`

They are most of VS Code's calls but few tokens, and no credits.

<br>

## Comparable token counts

The tools count input tokens differently:

| Source | `ai_input_tokens` | `ai_total_tokens` |
|---|---|---|
| OpenCode | Uncached input only | input + cache read + cache write + output + reasoning |
| VS Code | Includes cache read and cache write | input + output |

The dashboards use:

- Uncached input: `ai_input_tokens` for OpenCode,
  `ai_input_tokens - ai_cache_read_tokens - ai_cache_write_tokens` for VS Code
- Output: `ai_output_tokens + ai_reasoning_tokens` for OpenCode,
  `ai_output_tokens` for VS Code
- Total tokens, cache reads, and cache writes as is

<br>

## Estimated GitHub AI credits

VS Code chat spans report the GitHub AI credits each call consumed
(`copilot_chat_copilot_usage_nano_aiu`, in billionths of a credit). OpenCode
does not, and its own cost figure does not match GitHub billing.

Credits are therefore estimated from tokens with per-model rates, fitted from
the VS Code spans that report credits. One credit is billed at $0.01.

Credits per million tokens:

| Model | Uncached input | Cache read | Cache write | Output | Fit |
|---|---|---|---|---|---|
| claude-opus-5.5 | 400 | 20 | 500 | 2000 | exact |
| claude-sonnet-5.5 | 240 | 16 | 250 | 960 | approximate |
| claude-haiku-4.5 | 100 | 10 | 125 | 500 | exact |
| gpt-6.1-sol | 250 | 10 | 0 | 1000 | exact |
| gpt-6-luna | 12.5 | 1 | 0 | 50 | exact |
| gpt-5.6-sol | 250 | 25 | 0 | 2000 | approximate |
| gpt-5.6-terra | 250 | 20 | 0 | 1150 | approximate |
| gpt-5.6-luna | 25 | 2 | 0 | 120 | exact |
| gemini-3.8-flash | 75 | 7.5 | 0 | 375 | exact |

Background models count as 0. Calls through other providers, or on models
without a rate, are left out.

To check the estimate, compare it with the credits VS Code reports in
`vscode_copilot_raw`:

```sql
SELECT SUM(CAST(copilot_chat_copilot_usage_nano_aiu AS FLOAT)) / 1000000000.0
FROM "vscode_copilot_raw"
WHERE gen_ai_operation_name = 'chat'
```

Rates change with new models and pricing. Refit them when a new model shows
up.
