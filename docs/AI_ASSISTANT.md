# AI Reservation Assistant

HERD can answer free-text questions about a specific reservation by calling the configured LLM provider. The assistant lives on the reservation detail modal as the **AI Assistant** tab and is grounded in live reservation data via a tool-use loop. By default it is read-only; when `AI_WRITE_TOOLS_ENABLED=true` it can also propose and schedule config changes through the existing apply pipeline (every change requires a user confirmation step before any real apply runs).

This document covers the multi-turn chat surface (iteration 4): the user can ask a question, the model answers, and follow-up turns continue the same conversation with full history. The endpoint URL is unchanged from earlier iterations; the request shape gains an optional `conversation_id` field and the response always returns the conversation id so the next request can pass it back.

## Prerequisites

- An AI provider must be configured: for `AI_PROVIDER=anthropic` that means `AI_API_KEY` is set; for `AI_PROVIDER=openai_compat` that means `AI_BASE_URL` points at a running endpoint with tool-call support enabled. The frontend reads `GET /api/ai/status`; when the provider is unconfigured that endpoint reports `{"enabled": false}` and the AI Assistant tab is hidden entirely. The endpoint itself returns 503 in that state. See [ENV_VARS.md](ENV_VARS.md) for the env-var reference plus vLLM tool-call parser flags.
- Local models drive the assistant well only at sufficient scale. Multi-turn tool calling is the hardest surface to serve; the 30B+ class (e.g. Qwen 3 family, Llama 3.3 70B) is the realistic floor. Smaller models work for one-shot topology generation but degrade visibly on multi-turn assistant flows.
- You must own the reservation. The assistant calls `GET /api/reservations/{id}` with your JWT and inherits ownership-based access (non-owners get 404, matching the public reservations endpoint).

## The flow

1. Open the reservations page and click any reservation to open its detail modal.
2. Click the **AI Assistant** tab. (Hidden if the API key is not configured.)
3. Type a question (up to 4000 characters) and press Enter (Shift+Enter for a newline) or click **Send**. Example prompts:
   - "What devices are reserved and how are they connected?"
   - "What is the current config of my firewall?"
   - "Did the last apply on switch-a succeed?"
   - "Is there an L2 path between my firewall and my client?"
4. The orchestrator renders a thin seed (reservation metadata + device list) once per conversation, persists it as the opening message, runs the tool-use loop for the current turn, and returns a grounded answer.
5. Send a follow-up question to continue the same conversation. The chat thread shows alternating user and assistant bubbles; each assistant turn has a collapsible tool-call panel with names, argument summaries, and durations. Click **Start new conversation** to clear the thread and begin fresh.

The conversation id is persisted in your browser's `sessionStorage` keyed by reservation, so reopening the modal in the same browser session resumes where you left off. Conversations idle longer than `ASSISTANT_CONVERSATION_TTL_HOURS` (default 24) are expired by a background sweeper; once expired, the next request automatically starts a new conversation.

The frontend chat UI is gated behind `VITE_AI_CHAT_ENABLED` (build-time flag, default off). When the flag is off, the legacy single-shot UI renders instead and each request is independent.

## What the assistant can see

The opening seed sent to the model is intentionally narrow:

- **Reservation**: id, status, start_time, end_time, topology_id, topology_type, purpose, owner_name
- **Per device (one line each)**: id, name, template_name, status

For everything else, the model calls one of seven read-only tools. Each tool's HTTP call carries your JWT, so existing RBAC and device visibility apply exactly as if you made the call yourself.

| Tool | Backing endpoint | Returns |
|---|---|---|
| `get_device` | `GET /api/inventory/devices/{id}` | Device detail; password-typed `field_data` keys stripped using the template definition |
| `get_device_ports` | `GET /api/inventory/devices/{id}/ports` | Port list; password-typed per-port fields stripped |
| `get_device_current_config` | `GET /api/inventory/devices/{id}/config-versions?limit=1` then the version detail | Latest config payload plus metadata, or `{"has_config": false}` |
| `get_device_config_schema` | inventory's `GET /api/inventory/drivers/{id}/config-schema` proxy (driver-published schema sourced from execution), with the in-process registry as fallback | The config schema the device's `config_payload` must satisfy: `{connection_type, schema, allowed_keys, source}`. `source` is `driver` when the device's driver publishes its own `config_schema()` (the real accepted shape), `registry` when it falls back to the generic connection-type vocabulary, or `none` when no schema applies. The lookup fails open to the registry, so a broken or unreachable published schema never breaks the tool. |
| `list_device_config_history` | `GET /api/inventory/devices/{id}/config-versions?limit=N` | Recent version metadata (newest first), no payloads |
| `find_path` | `POST /api/cabling/pathfind` | `{reachable, hop_count, paths}` between two devices in your reservation |
| `list_executions_for_reservation` | `GET /api/execution/runs?reservation_id={your reservation}&...` | Recent execution runs for this reservation (reservation_id injected server-side, never accepted from the model) |

The list of executions tool requires a small change in the execution service: previously `/api/execution/runs` was admin-only; iter 2 opens it to non-admin callers when a `reservation_id` filter is supplied AND they own that reservation (verified via a cross-service call to the reservations service with the caller's JWT).

The model is told to call tools only when the seed cannot answer the question. Simple questions ("when does my reservation end?") return in zero tool calls; deeper diagnostic questions ("why did my last apply fail?") call one to a handful.

## Endpoint

`POST /api/ai/reservations/{id}/assistant`

Request (first turn):

```json
{ "question": "What is the current configuration of switch-a?" }
```

Request (follow-up turn, passing back the id from the prior response):

```json
{
  "question": "And on switch-b?",
  "conversation_id": "1f1a85b8-7d20-4c54-9d3b-9f6c2d4e1a7e"
}
```

Response:

```json
{
  "answer": "switch-a is running config version 7 (applied 2026-05-19 by Jordan). VLAN 100 is configured on ports Gig0/1-12, ...",
  "model": "claude-sonnet-4-6",
  "input_tokens": 1284,
  "output_tokens": 162,
  "stop_reason": "end_turn",
  "tool_calls": [
    { "name": "get_device", "arguments_summary": "device_id=...", "duration_ms": 47, "error": null },
    { "name": "get_device_current_config", "arguments_summary": "device_id=...", "duration_ms": 91, "error": null }
  ],
  "tool_iterations": 2,
  "conversation_id": "1f1a85b8-7d20-4c54-9d3b-9f6c2d4e1a7e"
}
```

The response's `conversation_id` is always non-null (the route either created a new conversation or echoed the one supplied in the request). Pass it back on the next request to continue the same thread.

Errors:

- 401: missing or invalid bearer token
- 404: reservation does not exist or caller does not own it; **or** a `conversation_id` was supplied that does not exist, was created by a different user, or belongs to a different reservation (404 not 403 to avoid leaking existence)
- 422: question empty or longer than 4000 characters
- 502: the LLM call failed or returned no text content
- 503: no AI provider configured (`AI_API_KEY` blank under `AI_PROVIDER=anthropic`, or `AI_BASE_URL` blank under `AI_PROVIDER=openai_compat`)
- 504: assistant or seed gather exceeded its deadline (90s overall by default; configurable)

Note: the iter-1 413 response (rendered context exceeded a size ceiling) no longer exists. Per-tool-result truncation handles oversized payloads instead, with a `... [truncated: N chars omitted]` marker appended to the affected tool result.

## Streaming endpoint (SSE)

`POST /api/ai/reservations/{id}/assistant/stream`

Same request body, auth, ownership, quota, and persistence as the buffered endpoint above; the difference is the response is a `text/event-stream` (Server-Sent Events) so the answer renders as it is produced instead of arriving in one payload. Setup failures (401, 404, 422, 503, quota 429) still surface as real HTTP status codes before the stream opens; a failure after streaming has begun arrives as an `error` event, since the HTTP status is already committed.

Event types (each frame is `event: <type>` then `data: <json>`):

- `status`: a progress signal, `{ "message": "analyzing" | "running tools", "tools": ["get_device_ports", ...], "interim": false }`. `interim` is `true` on the status that follows a tool turn's narration text, the client's cue to discard the provisional tokens streamed so far in that turn before the tools run.
- `token`: one chunk of the final answer text, `{ "text": "..." }`. Only real answer text is streamed; a reasoning model's internal thinking is dropped.
- `done`: the fully-assembled turn, carrying the same JSON object the buffered endpoint returns (`answer`, `model`, token counts, `stop_reason`, `tool_calls`, `tool_iterations`, `conversation_id`, `pending_apply`).
- `error`: `{ "message": "..." }` for a failure after the stream opened (timeout or LLM failure).

The conversation is persisted after the stream completes, so a `conversation_id` from a streamed `done` event can be passed to either endpoint to continue the thread. The buffered endpoint remains available; streaming is opt-in per request by calling the `/stream` path.

## Privacy and safety

- The question text is **not** logged. The per-request structured log entry records reservation id, model, token counts, stop reason, question length, `tool_iterations`, and `tool_call_count`, never the question content itself.
- Per-tool-result bodies are **not** logged. Tool call names and short argument summaries are logged for observability; the actual payload returned to the model is never written to logs.
- The system prompt is static. The seed and every tool result are sent in the user message wrapped in XML-style tags with explicit instructions that the contents are untrusted data and any embedded instructions should be ignored.
- Password-typed template fields are stripped server-side before any device or port payload reaches the model. If the template definition cannot be fetched, the entire `field_data` block is dropped (closed-by-default).
- The reservation id is captured in the dispatcher at construction time and injected into the `list_executions_for_reservation` tool server-side. The tool's input schema does not accept a `reservation_id` argument, so the model has no way to peek into other reservations even if it tried.
- The assistant is read-only by default. When `AI_WRITE_TOOLS_ENABLED=true`, it can also propose and schedule config changes routed through the existing apply pipeline; every write is a dry-run proposal that you must confirm in the UI before any real apply runs. With the flag off, the read-only tools above are the only ones advertised, and the write tools are additionally refused at the dispatch boundary: even if a model emits a write-tool call by name, the orchestrator rejects it, so the assistant cannot write to inventory, cabling, reservations, or execution.

## Limits

| Setting | Default | Env var |
|---|---|---|
| Question length | 4000 chars | (hardcoded) |
| Tool iterations per request | 8 | `ASSISTANT_MAX_TOOL_ITERATIONS` |
| Per-tool-result size cap | 8000 chars (truncated above) | `ASSISTANT_TOOL_RESULT_CHAR_CAP` |
| Overall route deadline | 90 seconds | `ASSISTANT_OVERALL_DEADLINE_S` |
| Per-model-call deadline | 20 seconds | `ASSISTANT_PER_CALL_TIMEOUT_S` |
| Seed gather deadline | 30 seconds | (hardcoded) |
| Per-hop HTTP timeout | 15 seconds | (hardcoded) |
| Inventory fetch concurrency | 8 parallel device fetches | (hardcoded) |
| Conversation idle TTL | 24 hours | `ASSISTANT_CONVERSATION_TTL_HOURS` |
| Hard turn cap per conversation | 40 (user + assistant + tool messages combined; seed pinned) | `ASSISTANT_MAX_TURNS` |
| History token budget per conversation | 60,000 input tokens (chars/4 estimate) | `ASSISTANT_HISTORY_TOKEN_BUDGET` |
| Per-user daily token quota | disabled (0) | `AI_DAILY_TOKEN_QUOTA` |
| Sweeper interval | 3600 seconds | `ASSISTANT_SWEEPER_INTERVAL_SECONDS` |

When the iteration cap is hit, the orchestrator makes one final call with `tool_choice={"type": "none"}` and a nudge ("you have exhausted your tool budget; answer with what you know"), and returns the resulting text as a normal 200 response. The user always gets an answer, even if a degraded one.

When either the turn cap or the token budget is exceeded, the repository evicts the oldest user+assistant pair (and any tool-result echo between them) until both bounds are satisfied. The position-0 seed message is pinned and never evicted; without it the model loses its grounding.

The per-conversation budget above is separate from the optional per-user daily quota. `AI_DAILY_TOKEN_QUOTA` (default 0, disabled) caps the input + output tokens one user can spend per UTC day across all AI features (generation, the assistant, and template-identity suggestions). When the running daily total reaches the cap, the next billable call is rejected with HTTP 429 and a `{limit, used, remaining, reset_at}` body before the provider is called; the total resets on the UTC day boundary. `GET /api/ai/quota` returns the caller's current usage. See [ENV_VARS.md](ENV_VARS.md) for the full description.

## Future iterations

- Web/docs lookup tools for vendor reference material.
- Streaming responses (current responses are returned as a single completed turn).
- Optional cross-reservation conversation memory for users who own multiple related reservations.
