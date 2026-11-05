# SPEC: Retail Analyst Agent (prototype)

Version 1.0, 2026-11-04. This is the build spec for the OpsFleet assignment
("Design and Build a Data Analysis Chat Assistant"). The HLD for production lives
in `docs/ARCHITECTURE.md`; this file says exactly what the prototype does, so the
code can be checked against it.

## 1. Goal

A CLI chat agent for non-technical executives. They ask questions about
`bigquery-public-data.thelook_ecommerce` in plain English, discuss the answers,
and ask for reports with action items. The prototype must fully implement four
requirements (Safety & PII masking, High-stakes oversight, Resilience,
Observability) and show the shape of the rest (golden bucket, learning loop,
persona management) in a minimal but real way.

## 2. Scope

In scope for the prototype:

- Chat loop with tool calling: schema lookup, SQL over BigQuery, report library,
  preference memory.
- Per-user data scope on products, enforced in SQL, not in the prompt.
- PII never reaches the model output: column deny-list at SQL level plus a
  regex mask on the final text.
- Input guardrail: analysis questions only; off-topic and prompt-injection
  attempts are refused politely.
- Saved reports with a strict delete confirmation flow (preview, typed `yes`,
  own reports only).
- Self-correction on SQL errors and empty results, with hard caps on retries
  and query cost.
- Fallback model and backoff on LLM API errors; friendly error when BigQuery
  or the LLM is down.
- Structured traces per user turn, metrics summary and trace deep-dive from
  the CLI.
- Persona file hot-reloaded every turn, editable without redeploy.
- Golden "trios" (question, SQL, report) retrieved by lexical similarity and
  injected into the prompt; new candidates appended when a report is saved.
- Runs with `pip` or with Docker. Tests run without network.

Out of scope (design only, in `docs/ARCHITECTURE.md`): vector database for the
golden bucket, human review queue for golden candidates, multi-user web UI,
OpenTelemetry export, eval pipeline in CI, chart generation, email delivery,
web search.

## 3. Users and data scope

`config/users.yaml` defines the demo users:

```yaml
users:
  alice: { name: "Alice", role: "Brand Manager", brands: ["Calvin Klein", "Tommy Hilfiger", "Jones New York"] }
  bob:   { name: "Bob",   role: "Brand Manager", brands: ["Diesel", "Carhartt", "True Religion"] }
  ceo:   { name: "Dana",  role: "CEO",           brands: [] }
```

Brand is the only access dimension (client's answer, 2026-11-04): a manager owns one
or more `products.brand` values, an empty list means everything (CEO). The user is
chosen with `--user` at start. There is no login in the prototype; in production the
identity and the brand list come from the JWT issued by SSO.

Scope enforcement (`scope.py`):

- Every SQL the model writes is parsed with `sqlglot` (BigQuery dialect).
- Only a single `SELECT` (or `WITH ... SELECT`) is allowed. Anything else is
  rejected before it reaches BigQuery.
- Every reference to `products`, `order_items`, `orders`, `users` is replaced by
  a filtered subquery, keeping the alias the model used:
  - `products` -> rows whose `brand` is in the user's list (all rows for the CEO).
  - `order_items` -> rows whose `product_id` is in scoped products.
  - `orders` -> rows that have at least one scoped order item; for a brand manager
    `num_of_item` is recounted over scoped items only (no volume leak from mixed orders).
  - `users` -> customers who bought at least one scoped product, and only the safe
    columns (see 4).
- Rejected before BigQuery: other datasets/projects, `INFORMATION_SCHEMA`,
  `FOR SYSTEM_TIME AS OF`, `TABLESAMPLE`, a CTE named like a base table, more than
  one statement. Set operations, subqueries and UNNEST are rewritten like the rest.
- `LIMIT 200` is added when the query has no limit.
- The rewritten SQL is what runs, and it is what the trace stores.

## 4. PII

PII columns in `users`: `first_name`, `last_name`, `email`, `street_address`,
`postal_code`, `latitude`, `longitude`. They are never selectable: the `users`
subquery projects only `id, age, gender, state, city, country, traffic_source,
created_at`. Any explicit reference to a PII column fails parsing with a clear
error that is fed back to the model so it can rewrite the query.

Second layer (`pii.py`): the final assistant text and every tool result are
passed through regex masks for emails, phone numbers, street addresses
(`123 Main St`), 13-19 digit card-like numbers and latitude/longitude pairs.
Masked tokens become `[email]`, `[phone]`, `[address]`, `[card]`, `[coordinates]`.

## 5. Input guardrail

`guard.py` runs before the agent loop:

1. Rule layer: regex list for known injection phrases ("ignore previous
   instructions", "reveal your system prompt", "DROP TABLE", "act as") and for
   obvious off-topic requests. Match -> refuse without an LLM call.
2. Model layer: one short call to the cheap model that returns `ALLOW` or
   `DENY:<reason>`. Only analysis questions about the retail data, follow-ups,
   and report-library requests are allowed.
3. If the classifier call itself fails, the rule layer result stands and the
   trace records `guard=rules_only`.
4. Replies of up to 3 words, and library/preference requests of up to 12 words,
   skip the classifier. Longer messages always go through it, even when they
   mention reports. The guard is one extra model call on most questions (no
   thinking budget trick: the free-tier models reject it, so the verdict gets a
   1024-token output budget and an empty answer counts as "classifier unavailable").
5. The classifier is advisory: when it is unavailable the rules decide and the
   trace records `rules_only`; `GUARD_FAIL_CLOSED=true` refuses instead.
6. The PII rules fire only on people's identifiers tied to customers/users
   ("customer names", "emails of our top customers"); "product names bought by
   customers in Texas" is allowed.
7. The persona file and stored preferences are data: a value that matches an
   injection rule is dropped with a warning and never reaches the prompt.

Refusals are polite, one sentence, and traced with reason.

## 6. Agent loop

`agent.py`, one loop per user message:

1. Build system prompt: `prompts/system.md` + `prompts/persona.md` (re-read if
   the file changed) + user card (name, role, scope, known preferences) + table
   schema summary + up to 3 golden trios retrieved for the message.
2. Call the LLM with the conversation history (bounded to the last 30
   messages) and tool declarations.
3. If the response has function calls, run each tool, append results, loop.
   At most 6 LLM calls per turn (client asked for 2-5 per query); the last call runs
   without tools so the agent answers with what it has.
4. Final text is PII-masked and printed. Trace is closed.

Tools:

| Tool | Purpose | Notes |
|---|---|---|
| `get_schema(table?)` | Column list and description for one or all tables | Cached in memory. |
| `run_sql(sql, purpose)` | Execute scoped SQL, return rows (max 200) and stats | Self-correction loop in 7. |
| `save_report(title, markdown)` | Save a report to the library | Also files a golden candidate. |
| `list_reports(mentioning?, this_conversation?)` | List own reports | |
| `request_delete_reports(mentioning?, this_conversation?)` | Preview matching own reports and ask for confirmation | Never deletes. |
| `remember_preference(key, value)` | Store a user preference | e.g. `format=table`. |

Report backstop: if the user asked for a report and the final answer looks like one
(heading or action items) but the model skipped `save_report`, the agent saves it
anyway, appends "Saved to your report library as #N" and records a
`save_report(auto)` span. Deterministic code, not a second model call.

Delete flow: `request_delete_reports` returns `{needs_confirmation: true,
confirmation_id, reports: [...]}` and the pending confirmation is stored in the
`pending_deletes` table. The loop stops: `Agent.handle()` returns
`status=needs_confirmation` with the preview, the CLI prints the list and asks
the user to type `yes`, then calls `Agent.resume(approved)`, which deletes only
reports owned by the current user (or cancels) and continues the same turn and
trace with the tool result. A new message while a confirmation is open cancels
it. Confirmation ids expire after 5 minutes and are single use. Reports belonging
to other users are never listed or deleted. (A `confirm` callback is still
accepted for scripted use.)

Conversation log: every message (user, model with its raw content, tool result)
is written to the `messages` table, so `analyst chat --conversation <id>` continues
an earlier conversation and an agent can be rebuilt per request.

## 7. Resilience

`run_sql`:

- Parse and scope rewrite. Parse error -> return `{error, hint}`; no BigQuery
  call.
- Dry run first. If estimated bytes > `MAX_BYTES_PER_QUERY` (default 2 GB) ->
  return `{error: "too expensive", bytes, hint}`.
- Execute with timeout `QUERY_TIMEOUT_S` (default 60). SQL errors (bad column,
  syntax, GROUP BY) -> `{error, hint}` back to the model. Service errors (503,
  auth, timeout, quota) -> `DataUnavailable`: the turn ends with the friendly
  message and no attempt is charged.
- Empty result -> `{rows: [], row_count: 0, hint: "no rows, check filters or
  date range"}`; it counts as a failed attempt.
- Timeout -> the BigQuery job is cancelled and the model gets `{error: "query
  cancelled after 60s", hint: "narrow ..."}`, counted as a failed attempt.
- Per turn: at most `MAX_SQL_ATTEMPTS` (3) failed attempts. The fourth attempt
  returns `{error: "attempt budget exhausted", hint: "explain to the user what
  could not be computed"}`.

LLM calls (`llm.py`):

- Retry on 429 and 5xx once, honouring the server retry hint (max 15 s), so a
  rate-limited turn never stalls for minutes.
- Then fallback to `FALLBACK_MODEL` once (also on 404 = wrong model name), with
  the same history: Gemini 3 thought signatures replay across the two models
  (verified live; a function call without its signature is rejected with 400).
- 400/401/403 raise `LLMUnavailable` at once: a second model would repeat them.
- Then raise `LLMUnavailable`. The CLI prints one friendly line, the trace is
  marked `error`, the session continues.
- BigQuery client errors on connect raise `DataUnavailable` with the same
  handling.

Cost caps: `MAX_LLM_CALLS_PER_TURN` (6), `MAX_SQL_ATTEMPTS` (3),
`MAX_BYTES_PER_QUERY`, `LIMIT 200` on rows, result rows truncated to 50 for the
model context, and per-user daily budgets `DAILY_TOKEN_BUDGET` /
`DAILY_BYTES_BUDGET` (summed from today's traces; a turn over budget is refused
before any model call, `guard_result=deny/budget`).

## 8. Observability

`tracing.py` records one trace per user turn in SQLite (`traces` table) and the
full span list in `data/traces.jsonl`:

- Trace: `trace_id, conversation_id, user, started_at, duration_ms, status
  (ok|refused|error), llm_calls, prompt_tokens, completion_tokens, tool_calls,
  sql_attempts, sql_failures, bytes_billed, retries, fallback_used, guard_result,
  error, user_message, assistant_message`.
- Spans: `prompt` (the full system prompt of the turn, with hash), `llm` (model,
  tokens, latency, retries, fallback used, full text, tool calls with args), `tool` (name,
  args, result summary, latency), `sql` (original sql, scoped sql, bytes,
  rows, error), `guard`.

CLI: `analyst stats` (counts, error rate, p50/p95 latency, tokens, bytes,
self-correction rate), `analyst traces --last N`, `analyst trace <id>` (full
message correspondence and spans). Logs are JSON lines, so they can be shipped
to any log store. Production path: the same spans exported with OpenTelemetry
to Langfuse/Cloud Trace (design doc).

## 9. Persona and preferences

- `prompts/persona.md` is read every turn when its mtime changes. A non-developer
  edits the file (or, in production, the prompt record in Langfuse / a Sheet).
  `analyst persona` prints the active text.
- Preferences table `(user, key, value, updated_at)`. The model stores them with
  `remember_preference` when the user states one ("I prefer tables"), and they
  are injected into the user card next turn. `/prefs` shows them.

## 10. Golden bucket

`golden/trios.jsonl` holds the approved trios `{id, question, sql, report, note, status,
tags}`: the full trio as the assignment defines it (question, SQL, analyst report) plus a
short `note` (max 600 chars), which is what the prompt receives together with the
question and SQL (full reports would cost thousands of tokens per turn). Eight seeded
trios cover revenue over time, top customers, product comparison, state gaps, churn,
up-to-date revenue by product, return rates and the "what data do we have" question;
all of them dry-run under a brand scope. `golden.py` ranks them against the user
message with BM25 and injects the top 3. `save_report` files a `candidate` trio
(question, every SQL run in the turn in order, the report, author, trace id) into
`data/golden_candidates.jsonl`; candidates are never injected. `analyst golden list`
shows both sets, `analyst golden approve <id> [--note "..."]` scopes and dry-runs each
query of the candidate against BigQuery (`--no-check` to skip) and moves it into the
approved file. Deletes of reports
are logged in the `deletions` table (user, trace id, report ids, titles). Production: embeddings + vector store, reviewer queue,
nightly refresh (design doc).

## 11. CLI

```
analyst chat --user alice            # start chatting (installed by `pip install -e .`)
analyst chat --user alice --conversation 1a2b3c4d   # continue an earlier conversation
analyst stats | traces | trace <id>  # observability
analyst reports --user alice         # report library
analyst golden list|approve <id> [--note "..."]
analyst persona
```

In-chat commands: `/help`, `/reports`, `/prefs`, `/trace` (last trace id),
`/persona`, `/quit`.

Configuration via `.env`: `GEMINI_API_KEY`, `GOOGLE_CLOUD_PROJECT`,
`ANALYST_MODEL` (default `gemini-3.8-flash`), `ANALYST_FALLBACK_MODEL`
(`gemini-3.5-flash-lite`), `ANALYST_GUARD_MODEL`, `ANALYST_JUDGE_MODEL`,
`MAX_BYTES_PER_QUERY`, `QUERY_TIMEOUT_S`, `DAILY_TOKEN_BUDGET`, `DAILY_BYTES_BUDGET`,
`GUARD_FAIL_CLOSED`, `ANALYST_DATA_DIR`.

## 12. Tests and evals

- `pytest` with fakes (`FakeLLM`, `FakeBigQuery`), no network: scope rewrite,
  PII deny and mask, guard rules, delete confirmation flow, SQL self-correction,
  attempt budget, LLM fallback, trace content.
- `evals/questions.jsonl` + `evals/run_evals.py`: runs real questions through
  the agent, checks status, tables used, rows returned, report saved, PII-free text,
  ground truth where a `reference_sql` is given (the reference query runs under the
  same user's scope and its values must appear in the answer, numbers within 2%),
  and asks a separate judge model (`ANALYST_JUDGE_MODEL`) to score intent match.
  The last run is committed as `evals/last_run.json` with the answers and trace ids.
- Dependencies are pinned (`requirements.txt`); `requirements-dev.txt` adds pytest.

## 13. Done when

- `pip install -r requirements.txt && pip install -e . && analyst chat --user alice`
  answers "top 5 products by revenue last month" with a scoped query.
- `bob` cannot see Calvin Klein numbers even when asking for the brand by name.
- "Delete all reports from this conversation" shows a preview and deletes only
  after `yes`.
- A deliberately broken question still ends with a readable answer, and the
  trace shows the retries.
- `analyst stats` shows the numbers above.
- `docker compose run analyst` does the same.
- `pytest` passes.
