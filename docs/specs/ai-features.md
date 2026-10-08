# AI features specification

| | |
|---|---|
| Area prefix | `AI` (used in rule identifiers, for example `AI-GEN-1`) |
| Verified at | commit `9562b88e` (`v0.6.0-91-g9562b88e`), 2026-10-06; written against `b505402f`, and no file this document cites changed between the two (the ai-orchestrator code is unchanged since `fd589e50`) |
| Owning services | ai-orchestrator (`services/ai-orchestrator/`), and the frontend surfaces that call it |
| Other services involved | inventory (templates, devices, ports, config versions, apply jobs), cabling (pathfinding, topologies, validation, forks), reservations (reservation reads, reservation create, the purpose sweep that calls this service), execution (`POST /execute`, execution runs, recipe package validation), auth (JWT only), the configured LLM provider (external) |
| Design records | [ADR 0005](../design/0005-ai-recipe-authoring.md), [ADR 0012](../design/0012-network-element-objects.md), [ADR 0013](../design/0013-lab-purpose-classification.md), [ADR 0015](../design/0015-assistant-docs-lookup.md); ADR 0006 for port choice ([0006](../design/0006-fork-reconcile-and-as-built.md)) |
| Related guides | [AI_GENERATE.md](../AI_GENERATE.md), [AI_ASSISTANT.md](../AI_ASSISTANT.md), [AI_PROVIDERS.md](../AI_PROVIDERS.md), [AI_RECIPES.md](../AI_RECIPES.md), [AI_PURPOSE_CLASSIFICATION.md](../AI_PURPOSE_CLASSIFICATION.md), [ENV_VARS.md](../ENV_VARS.md), [ROLES.md](../ROLES.md) |

All API paths below are the ai-orchestrator service's own paths. Through the gateway they
are prefixed with `/api/ai` (for example `POST /api/ai/generate`).

## 1. Purpose

The AI features let a person describe what they want in plain words and have HERD do the
tedious part: propose a lab topology built from devices that are actually free and cabled
together, answer questions about a running reservation (and, when an operator allows it,
prepare a configuration change for the user to review), draft a hypervisor recipe for an
administrator to review, suggest a hardware identity for a device template, and suggest why
a reservation exists. A model never commits anything on its own: a human accepts a
proposal, confirms an apply, or approves an upload. The area does not provision wiring,
apply configuration, store topologies, or book reservations itself; it asks the owning
services, with the caller's own token, and those services decide.

## 2. Actors and permissions

The endpoint matrix is in [ROLES.md](../ROLES.md). The rules that go beyond role are
numbered rules in section 8; section 5 names the caller condition for each route. An
unauthenticated caller gets 401 on every route except `GET /status`, `GET /health`, and
`GET /version`, and except where a default-off feature flag answers first (AI-RECIPE-3,
AI-PURPOSE-1).

| Actor | May | May not |
|---|---|---|
| User | Read the AI status; generate a topology proposal from devices they can see; commit a proposal (upstream services check their JWT); talk to the assistant about a reservation they own; read their own quota; ask for a purpose preview when the feature is on | Suggest a template identity; draft recipes; read the usage report; use the assistant on another user's reservation |
| Admin | Everything a user may; suggest a template identity; draft, refine, and read recipe drafts when the feature is on; read the usage report | Use the assistant on another user's reservation (the seed read answers 404, AI-CONV-1) |
| Superadmin | Same as admin | Same as admin |
| Another service (internal token) | Ask for an end-of-reservation purpose classification (section 7) | Any user-facing route |

## 3. Concepts and data

| Concept | Meaning | Owner | Stored in |
|---|---|---|---|
| Provider | The configured LLM backend, `anthropic` or `openai_compat`, built per request behind the `LLMProvider` protocol | ai-orchestrator | not stored; settings |
| Proposal | A model's topology suggestion: roles over template names, edges between roles, optional network elements, each role resolved to a concrete inventory device | ai-orchestrator | not stored; returned to the browser |
| Commit | Turning an accepted proposal into a cabling topology and a reservation | cabling and reservations | their own tables; `topology_id` and `reservation_id` are bare ids |
| Conversation | One multi-turn assistant thread, scoped to one user and one reservation. `reservation_id` and `user_id` are bare ids with no foreign key | ai-orchestrator | `assistant_conversations` (`AssistantConversation` in `services/ai-orchestrator/app/models/conversation.py`) |
| Message | One stored turn part: `USER`, `ASSISTANT`, or `TOOL` content blocks, replayed verbatim to the provider | ai-orchestrator | `assistant_messages` (`AssistantMessage`) |
| Side effect | A durable write a write tool made during a turn (`config_version_created` or `scheduled_apply`) | ai-orchestrator (in memory per turn); the write itself is inventory's | `ToolDispatcher.side_effects`, not persisted |
| Daily usage | Input and output tokens one user spent on one UTC day, plus cache counters | ai-orchestrator | `ai_usage` (`AIUsage` in `services/ai-orchestrator/app/models/ai_usage.py`) |
| Recipe draft | An AI-drafted hypervisor driver package awaiting admin review, with its validation report | ai-orchestrator | `recipe_drafts` (`RecipeDraft` in `services/ai-orchestrator/app/models/recipe_draft.py`) |
| Documentation source | A named read-only corpus (`herd-manual` or an operator directory) or the allowlisted web | ai-orchestrator | files on disk; an in-memory index |
| Purpose classification | A probability distribution over the caller's purpose categories plus a rationale | reservations stores it (`reservations.md`) | `reservations.purpose_suggestion` |

## 4. State model

None.

Nothing in this area carries a status column or guards a status write. A conversation is
created, appended to, trimmed (AI-CONV-8), and deleted by the sweeper (AI-CONV-9); a recipe
draft carries a `valid` flag that each drafting run recomputes (AI-RECIPE-9); the status
route's construction probe is a cache, not a state (AI-PROV-7). Reservation status belongs
to `reservations.md`.

## 5. API surface

| Method | Path | Who may call | Success | Rules |
|---|---|---|---|---|
| GET | `/status` | anyone, no token | 200 | AI-PROV-5, AI-PROV-6, AI-PROV-7, AI-PROV-8 |
| POST | `/generate` | any signed-in user | 200 | AI-PROV-2, AI-GEN-1 to AI-GEN-16, AI-UPLOAD-1 to AI-UPLOAD-7, AI-RESOLVE-1 to AI-RESOLVE-15, AI-QUOTA-2 |
| POST | `/commit` | any signed-in user; upstream services apply their own rules to the caller's JWT | 200 | AI-COMMIT-1 to AI-COMMIT-19 |
| POST | `/reservations/{id}/assistant` | the reservation's owner (first turn); the conversation's creator on the same reservation (later turns) | 200 | AI-PROV-2, AI-CONV-1 to AI-CONV-12, AI-LOOP-1 to AI-LOOP-8, AI-TURN-1 to AI-TURN-8, AI-TOOL-1 to AI-TOOL-11, AI-WRITE-1 to AI-WRITE-3 |
| POST | `/reservations/{id}/assistant/stream` | as above | 200 (`text/event-stream`) | AI-PROV-2, AI-CONV-1 to AI-CONV-12, AI-LOOP-1 to AI-LOOP-8, AI-STREAM-1 to AI-STREAM-7, AI-TOOL-1 to AI-TOOL-11, AI-WRITE-1 to AI-WRITE-3 |
| POST | `/templates/suggest-identity` | admin or superadmin | 200 | AI-PROV-2, AI-IDENT-1 to AI-IDENT-4, AI-QUOTA-2 |
| GET | `/quota` | any signed-in user (own usage) | 200 | AI-QUOTA-7 |
| GET | `/usage` | admin or superadmin | 200 | AI-QUOTA-8 |
| POST | `/recipes/draft` | admin or superadmin, feature on | 200 | AI-RECIPE-1 to AI-RECIPE-9, AI-RECIPE-12 to AI-RECIPE-14 |
| POST | `/recipes/draft/{id}/refine` | admin or superadmin, feature on | 200 | AI-RECIPE-1 to AI-RECIPE-4, AI-RECIPE-8 to AI-RECIPE-11, AI-RECIPE-13, AI-RECIPE-14 |
| GET | `/recipes/draft/{id}` | admin or superadmin, feature on | 200 | AI-RECIPE-1 to AI-RECIPE-3, AI-RECIPE-10, AI-RECIPE-11, AI-RECIPE-12 |
| POST | `/classify-purpose/preview` | any signed-in user, feature on | 200 | AI-PURPOSE-1, AI-PURPOSE-3, AI-PURPOSE-4, AI-PURPOSE-5, AI-PURPOSE-7, AI-PURPOSE-10 to AI-PURPOSE-13 |

`GET /health` and `GET /version` are the service-wide health and build routes
(`operations-and-observability.md`). The request and response bodies are the pydantic
models in `services/ai-orchestrator/app/schemas/` and the two route-local models in
`services/ai-orchestrator/app/routes/recipes.py` and
`services/ai-orchestrator/app/routes/template_identity.py`.

## 6. Events

None. This area publishes no event and consumes none.

## 7. Internal API

| Method | Path | Auth | Caller | Answers | Rules |
|---|---|---|---|---|---|
| POST | `/internal/classify-purpose` | `X-Internal-Token` | reservations (the purpose sweep and the Classify now route) | `PurposeClassification` with `pass` = `end` | AI-PURPOSE-1, AI-PURPOSE-2, AI-PURPOSE-3, AI-PURPOSE-4, AI-PURPOSE-6 to AI-PURPOSE-13 |

How reservations reads each answer (feature off, transient, timeout, attempt counting) is
specified in `reservations.md` (RES-PURPOSE-5 to RES-PURPOSE-8 and RES-PURPOSE-13).

## 8. Features

### 8.1 Provider layer and the AI status

**What it does.** An operator points HERD at either Anthropic's API (or a local endpoint
that speaks its format) or any OpenAI-compatible endpoint. Every page asks one
unauthenticated status route whether AI is usable and hides the AI buttons when it is not.

**Surfaces.** Settings in `services/ai-orchestrator/app/config.py`; the client in
`services/ai-orchestrator/app/services/ai_client.py`; providers under
`services/ai-orchestrator/app/services/providers/`; route `GET /status`; frontend hook
`useAIStatus` in `frontend/src/api/ai.ts` (cached five minutes).

**Rules.**

- **AI-PROV-1.** The provider counts as configured when `AI_PROVIDER` is `anthropic` and
  `AI_API_KEY` or `AI_BASE_URL` is non-empty, or `AI_PROVIDER` is `openai_compat` and
  `AI_BASE_URL` is non-empty; any other provider value counts as unconfigured. \
  Enforced in: `services/ai-orchestrator/app/services/ai_client.py` (`ai_is_configured`) \
  Pinned by: `services/ai-orchestrator/tests/test_ai_is_configured.py` (`test_anthropic_configured_with_key_only`, `test_anthropic_configured_with_base_url_only`, `test_anthropic_unconfigured_when_key_and_base_url_blank`, `test_openai_compat_configured_with_base_url_only`, `test_openai_compat_unconfigured_without_base_url`); `services/ai-orchestrator/tests/test_status.py` (`test_status_disabled_for_unknown_provider`)
- **AI-PROV-2.** Every route that calls the model (generate, both assistant routes,
  suggest-identity, recipe draft and refine, both purpose routes) answers 503
  `AI orchestrator is not configured` when the provider is unconfigured, before any
  provider call. \
  Enforced in: `services/ai-orchestrator/app/routes/generate.py` (`generate`); `services/ai-orchestrator/app/routes/reservation_assistant.py` (`reservation_assistant`, `reservation_assistant_stream`); `services/ai-orchestrator/app/routes/template_identity.py` (`suggest_identity`); `services/ai-orchestrator/app/routes/recipes.py` (`_run_authoring`); `services/ai-orchestrator/app/routes/purpose_classification.py` (`_gate_before_gather`) \
  Pinned by: `services/ai-orchestrator/tests/test_generate.py` (`test_generate_503_when_key_blank`); `services/ai-orchestrator/tests/test_reservation_assistant.py` (`test_503_when_api_key_blank`, `test_stream_503_when_api_key_blank`); `services/ai-orchestrator/tests/test_template_identity_route.py` (`test_suggest_identity_503_when_key_blank`); `services/ai-orchestrator/tests/test_recipes_routes.py` (`test_unconfigured_503_pinned`); `services/ai-orchestrator/tests/test_purpose_classification_routes.py` (`test_preview_503_when_unconfigured`, `test_internal_503_when_unconfigured`)
- **AI-PROV-3.** The provider is constructed per request by a dependency that runs before
  the route's own check; an unrecognized provider value or a constructor that raises (for
  example a missing `AI_CA_CERT` file) answers the same 503 detail, never 500, and the
  exception text is only logged. \
  Enforced in: `services/ai-orchestrator/app/services/ai_client.py` (`get_ai_client`, `_build_provider`) \
  Pinned by: `services/ai-orchestrator/tests/test_ai_is_configured.py` (`test_get_ai_client_503_on_unknown_provider`, `test_get_ai_client_503_on_construction_failure`); `services/ai-orchestrator/tests/test_generate.py` (`test_generate_503_on_unknown_provider`, `test_generate_503_on_construction_failure`); `services/ai-orchestrator/tests/test_reservation_assistant.py` (`test_503_on_unknown_provider`)
- **AI-PROV-4.** A provider error that is not already an `AIError` is wrapped as `AIError`
  by the client, which every route maps to a fixed 502 detail. \
  Enforced in: `services/ai-orchestrator/app/services/ai_client.py` (`_call_provider`) \
  Pinned by: `services/ai-orchestrator/tests/test_ai_client.py` (`test_propose_topology_wraps_provider_exceptions_as_aierror`, `test_tool_loop_wraps_provider_sdk_error_as_aierror`)
- **AI-PROV-5.** `GET /status` needs no token and answers exactly the keys `enabled`,
  `provider`, `model`, `recipe_authoring`, `degraded`, `reason`, and
  `purpose_classification`. \
  Enforced in: `services/ai-orchestrator/app/main.py` (`ai_status`) \
  Pinned by: `tests/integration/test_ai_provider_wiring.py` (`test_ai_status_is_unauthenticated`, `test_ai_status_payload_shape_is_stable`)
- **AI-PROV-6.** When the settings look configured, the status route probes provider
  construction; a failed probe reports `enabled: false`, `degraded: true`, and `reason`
  set to the exception's class name only, never its message. Unconfigured settings report
  `enabled: false` and `degraded: false`. \
  Enforced in: `services/ai-orchestrator/app/main.py` (`ai_status`); `services/ai-orchestrator/app/services/ai_client.py` (`_ProviderConstructionCache`, `get_provider_construction_status`) \
  Pinned by: `services/ai-orchestrator/tests/test_status.py` (`test_status_degraded_when_construction_raises`, `test_status_construction_failure_reason_is_real_exception_class`, `test_status_disabled_when_anthropic_key_blank`)
- **AI-PROV-7.** The probe result is cached for 30 seconds on a monotonic clock, so a
  broken or repaired provider shows on the status route within that window. \
  Enforced in: `services/ai-orchestrator/app/services/ai_client.py` (`_ProviderConstructionCache`) \
  Pinned by: `services/ai-orchestrator/tests/test_status.py` (`test_construction_cache_reuses_result_within_ttl`, `test_construction_cache_reconstructs_after_ttl_expires`, `test_status_construction_success_after_previous_failure_reports_ok`)
- **AI-PROV-8.** `recipe_authoring` reports `AI_RECIPE_AUTHORING_ENABLED` as set;
  `purpose_classification` is true only when the provider is enabled and not degraded and
  `AI_PURPOSE_CLASSIFICATION_ENABLED` is set. \
  Enforced in: `services/ai-orchestrator/app/main.py` (`ai_status`) \
  Pinned by: `services/ai-orchestrator/tests/test_status.py` (`test_status_purpose_classification_true_only_when_flag_and_enabled`, `test_status_purpose_classification_false_when_unconfigured_even_with_flag_on`); `services/ai-orchestrator/tests/test_recipes_routes.py` (`test_status_reports_recipe_authoring_flag`)
- **AI-PROV-9.** Streaming is optional per provider: the anthropic provider streams; the
  openai_compat provider has no `call_stream`, so the streaming assistant runs the
  buffered loop and emits the finished answer as tokens followed by `done`. \
  Enforced in: `services/ai-orchestrator/app/services/ai_client.py` (`answer_reservation_question_streaming`); `services/ai-orchestrator/app/services/providers/anthropic_provider.py` (`call_stream`) \
  Pinned by: `services/ai-orchestrator/tests/test_ai_client.py` (`test_streaming_falls_back_when_provider_lacks_call_stream`)
- **AI-PROV-10.** Both providers turn a connection, DNS, TLS, or transport failure into
  `AIProviderUnavailableError` (routes answer 503 `AI provider is unreachable`), while an
  API error from a live endpoint (auth, 4xx, 5xx) stays an ordinary error. \
  Enforced in: `services/ai-orchestrator/app/services/providers/anthropic_provider.py` (`call`, `call_stream`); `services/ai-orchestrator/app/services/providers/openai_provider.py` (`call`) \
  Pinned by: `services/ai-orchestrator/tests/test_anthropic_provider.py` (`test_call_maps_connection_error_to_unavailable`, `test_call_maps_raw_httpx2_transport_error_to_unavailable`, `test_call_stream_maps_connection_error_to_unavailable`, `test_call_propagates_sdk_exceptions`); `services/ai-orchestrator/tests/test_openai_provider.py` (`test_call_maps_connection_error_to_unavailable`, `test_call_maps_raw_httpx_transport_error_to_unavailable`, `test_call_propagates_sdk_exceptions`)
- **AI-PROV-11.** A provider call given a timeout that it exceeds raises `AIError` with the
  text `AI call exceeded <N>s`. \
  Enforced in: `services/ai-orchestrator/app/services/providers/anthropic_provider.py` (`call`); `services/ai-orchestrator/app/services/providers/openai_provider.py` (`call`) \
  Pinned by: `services/ai-orchestrator/tests/test_anthropic_provider.py` (`test_call_raises_aierror_on_timeout`); `services/ai-orchestrator/tests/test_openai_provider.py` (`test_call_raises_aierror_on_timeout`)
- **AI-PROV-12.** TLS to the provider: with `AI_CA_CERT` set, verification uses that bundle
  and stays on; otherwise `AI_TLS_VERIFY=false` disables verification; otherwise the SDK
  default applies. Both providers follow this order. \
  Enforced in: `services/ai-orchestrator/app/services/providers/anthropic_provider.py` (`_build_anthropic_http_client`); `services/ai-orchestrator/app/services/providers/openai_provider.py` (`_build_http_client`) \
  Pinned by: `services/ai-orchestrator/tests/test_anthropic_provider.py` (`test_build_http_client_ca_cert_verifies_against_bundle_and_takes_precedence`, `test_build_http_client_verify_tls_false_returns_non_verifying_client`, `test_build_http_client_default_returns_none`); `services/ai-orchestrator/tests/test_openai_provider.py` (`test_ca_cert_verifies_against_bundle_and_takes_precedence`, `test_verify_tls_false_injects_non_verifying_http_client`)
- **AI-PROV-13.** The openai_compat provider sends a blank `AI_API_KEY` as the placeholder
  `EMPTY`. \
  Enforced in: `services/ai-orchestrator/app/services/providers/openai_provider.py` (`OpenAICompatProvider`) \
  Pinned by: `services/ai-orchestrator/tests/test_openai_provider.py` (`test_blank_api_key_becomes_empty_placeholder`)
- **AI-PROV-14.** Stop reasons are normalized to `end_turn`, `tool_use`, `max_tokens`,
  `stop_sequence`, or `other`; on openai_compat, `stop` is `end_turn`, `tool_calls` and
  `function_call` are `tool_use`, and `length` is `max_tokens`. \
  Enforced in: `services/ai-orchestrator/app/services/providers/anthropic_provider.py` (`_stop_reason_from_anthropic`); `services/ai-orchestrator/app/services/providers/openai_provider.py` (`_stop_reason_from_openai`) \
  Pinned by: `services/ai-orchestrator/tests/test_anthropic_provider.py` (`test_stop_reason_normalization`); `services/ai-orchestrator/tests/test_openai_provider.py` (`test_stop_reason_normalization`)
- **AI-PROV-15.** The anthropic stream forwards only non-empty text deltas (a reasoning
  model's thinking is dropped) and ends with the same assembled response the buffered call
  would return. \
  Enforced in: `services/ai-orchestrator/app/services/providers/anthropic_provider.py` (`call_stream`) \
  Pinned by: `services/ai-orchestrator/tests/test_anthropic_provider.py` (`test_call_stream_drops_thinking_deltas`, `test_call_stream_skips_empty_text_deltas`, `test_call_stream_done_carries_tool_use_for_loop`)
- **AI-PROV-16.** The anthropic provider marks the system prompt as an ephemeral prompt
  cache block and reports cache read and write tokens separately from input tokens; the
  openai_compat provider reports zero for both. \
  Enforced in: `services/ai-orchestrator/app/services/providers/anthropic_provider.py` (`_system_to_anthropic`, `_response_from_anthropic`) \
  Pinned by: `services/ai-orchestrator/tests/test_anthropic_provider.py` (`test_call_sets_cache_control_on_system_block`, `test_response_from_anthropic_reads_cache_token_fields`, `test_response_from_anthropic_cache_fields_coerce_none_to_zero`)
- **AI-PROV-17.** On openai_compat each tool result becomes its own `role=tool` message,
  and an error result is prefixed with `[tool error] ` because the protocol has no error
  flag. \
  Enforced in: `services/ai-orchestrator/app/services/providers/openai_provider.py` (`_message_to_openai`, `_TOOL_ERROR_SENTINEL`) \
  Pinned by: `services/ai-orchestrator/tests/test_openai_provider.py` (`test_user_with_tool_results_emits_separate_tool_messages`, `test_tool_result_is_error_true_prepends_sentinel`)
- **AI-PROV-18.** On openai_compat, tool-call arguments that are not a JSON object decode to
  an empty object, which the dispatcher then refuses as missing arguments; a response
  with no choices raises `AIError`. \
  Enforced in: `services/ai-orchestrator/app/services/providers/openai_provider.py` (`_safe_json_loads`, `_response_from_openai`) \
  Pinned by: `services/ai-orchestrator/tests/test_openai_provider.py` (`test_safe_json_loads_returns_empty_dict_on_malformed`, `test_safe_json_loads_returns_empty_on_non_dict`, `test_malformed_tool_arguments_surface_as_empty_input`, `test_response_raises_on_empty_choices`)
- **AI-PROV-19.** `ANTHROPIC_API_KEY` is never used as a credential; when it is set and
  `AI_API_KEY` is blank, one warning is logged at startup. \
  Enforced in: `services/ai-orchestrator/app/config.py` (`warn_if_anthropic_api_key_unused`) \
  Pinned by: `services/ai-orchestrator/tests/test_config.py` (`test_warns_when_anthropic_key_set_and_ai_api_key_blank`, `test_no_warning_when_ai_api_key_set`, `test_no_warning_when_anthropic_key_blank`)
- **AI-PROV-20.** The anthropic provider can be built with a blank key (a keyless local
  endpoint named by `AI_BASE_URL`); it hands the SDK the placeholder `EMPTY`. \
  Enforced in: `services/ai-orchestrator/app/services/providers/anthropic_provider.py` (`AnthropicProvider`) \
  Pinned by: `services/ai-orchestrator/tests/test_anthropic_provider.py` (`test_init_blank_key_hands_the_sdk_the_empty_placeholder`)

**Out of scope.** How the gateway routes `/api/ai` and how JWTs are issued
(`identity-and-access.md`). Model choice and backend setup are operator guidance
([AI_PROVIDERS.md](../AI_PROVIDERS.md)).

### 8.2 LLM-driven topology generation

**What it does.** In the topology editor a user describes a lab in plain words, optionally
attaches reference files, and gets back a proposal drawn as dashed ghost nodes: devices
from their own visible inventory, chosen so every drawn connection has a real cable path.

**Surfaces.** User interface `frontend/src/components/topology-editor/AIDialog.tsx`;
route `POST /generate` (multipart form, response `GenerateResponse` in
`services/ai-orchestrator/app/schemas/generate.py`). Device resolution is section 8.3.

**Rules.**

- **AI-GEN-1.** `POST /generate` takes a `prompt` form field of 1 to 20000 characters and
  optional `files`; any signed-in user may call it, and no token answers 401. \
  Enforced in: `services/ai-orchestrator/app/routes/generate.py` (`generate`) \
  Pinned by: `services/ai-orchestrator/tests/test_generate.py` (`test_generate_requires_auth`, `test_generate_rejects_empty_prompt`)
- **AI-GEN-2.** The route checks the provider (503), then the quota (429), then the uploads
  (400), and only then reads the inventory summary, so a refused request makes no inventory
  call. When inventory cannot answer the summary read (a transport error, a non-2xx, or a
  body that is not the expected JSON), the route answers 503
  `Could not read inventory; no topology was generated. Retry the request.`, with no
  upstream text. \
  Enforced in: `services/ai-orchestrator/app/routes/generate.py` (`generate`, `_inventory_provider`); `services/ai-orchestrator/app/services/generator.py` (`INVENTORY_UNAVAILABLE_DETAIL`) \
  Pinned by: `services/ai-orchestrator/tests/test_generate.py` (`test_generate_unconfigured_makes_no_inventory_call`, `test_generate_over_quota_makes_no_inventory_call`, `test_generate_rejected_upload_makes_no_inventory_call`, `test_generate_503_when_inventory_summary_fails`)
- **AI-GEN-3.** The summary is one `GET /templates?template_type=device&limit=500` plus one
  `GET /devices` per template counting `AVAILABLE` `dut_only` devices, all with the
  caller's JWT and a 15 second timeout, so a template the caller cannot see, or beyond the
  first 500, is never offered; a transport error, any non-2xx, or an unreadable body raises
  `InventoryUnavailableError`, which carries the operation, the exception class, and the
  status but never the upstream text. \
  Enforced in: `services/ai-orchestrator/app/services/inventory_client.py` (`fetch_inventory_summary`, `TEMPLATES_PAGE_SIZE`, `InventoryUnavailableError`) \
  Pinned by: `services/ai-orchestrator/tests/test_inventory_client.py` (`test_fetch_inventory_summary_aggregates_counts`, `test_fetch_inventory_summary_forwards_bearer_header`, `test_fetch_inventory_summary_raises_on_templates_5xx`, `test_fetch_inventory_summary_raises_on_devices_5xx`, `test_fetch_inventory_summary_non_json_body_is_inventory_unavailable`)
- **AI-GEN-4.** When no template has an available device, the route answers 409
  `No device templates with available devices in inventory. ...` without calling the
  model. \
  Enforced in: `services/ai-orchestrator/app/services/generator.py` (`generate_topology`) \
  Pinned by: `services/ai-orchestrator/tests/test_generate.py` (`test_generate_409_when_inventory_has_no_templates`, `test_generate_409_when_templates_have_no_available_devices`)
- **AI-GEN-5.** The model is forced to call one tool, `propose_topology`, whose
  `template_name` is an enum of the sorted visible template names (zero-count templates
  included); the prompt lists each template with its available count and, when known, its
  vendor and model. \
  Enforced in: `services/ai-orchestrator/app/services/ai_client.py` (`build_topology_tool`, `propose_topology`, `SYSTEM_PROMPT_TEMPLATE`); `services/ai-orchestrator/app/services/inventory_client.py` (`to_prompt_block`) \
  Pinned by: `services/ai-orchestrator/tests/test_ai_client.py` (`test_propose_topology_forces_tool_choice`); `services/ai-orchestrator/tests/test_inventory_client.py` (`test_summary_prompt_block_sorted`, `test_summary_prompt_block_includes_vendor_model_when_known`)
- **AI-GEN-6.** A proposal carries `purpose`, `devices` (`role`, `template_name`,
  `topology_type` `PHYSICAL` or `CLOUD`, optional `config` limited to `vlan` 1 to 4094,
  `ip`, `hostname`, `description`), `edges` (`source_role`, `target_role`, `layer` L1, L2,
  or L3), optional `elements` (`role`, `element_type` one of `vlan_segment`, `subnet`,
  `external_cloud`, `patch_trunk`, `label`, `attrs` limited to `vlan_id`, `cidr`,
  `description`), and `notes`. \
  Enforced in: `services/ai-orchestrator/app/services/ai_client.py` (`build_topology_tool`); `services/ai-orchestrator/app/schemas/generate.py` (`GenerateResponse`, `ProposedElement`) \
  Pinned by: `services/ai-orchestrator/tests/test_ai_client.py` (`test_build_topology_tool_advertises_elements_with_four_value_enum`, `test_build_topology_tool_elements_attrs_allowlist`); `services/ai-orchestrator/tests/test_generate_schemas.py` (`test_proposed_element_unknown_element_type_is_a_validation_error`, `test_generate_response_elements_defaults_to_empty`)
- **AI-GEN-7.** A tool result that fails the response schema answers 502
  `AI returned a response that did not match the expected schema` at once, with no repair
  attempt. \
  Enforced in: `services/ai-orchestrator/app/services/generator.py` (`generate_topology`, `AI_SCHEMA_VIOLATION_DETAIL`) \
  Pinned by: `services/ai-orchestrator/tests/test_generate.py` (`test_generate_schema_violation_never_leaks_offending_input`)
- **AI-GEN-8.** When the provider returns no `propose_topology` call, the first text block
  that parses as JSON is used; with none, the route answers 502
  `AI returned no usable response`. \
  Enforced in: `services/ai-orchestrator/app/services/ai_client.py` (`propose_topology`); `services/ai-orchestrator/app/services/generator.py` (`AI_NO_USABLE_RESPONSE_DETAIL`) \
  Pinned by: `services/ai-orchestrator/tests/test_ai_client.py` (`test_propose_topology_falls_back_to_text_json_block`, `test_propose_topology_raises_when_no_usable_content`); `services/ai-orchestrator/tests/test_generate.py` (`test_generate_surfaces_ai_error`)
- **AI-GEN-9.** Any other exception from the model call answers 502 `AI call failed`; an
  unreachable provider answers 503 `AI provider is unreachable`. \
  Enforced in: `services/ai-orchestrator/app/services/generator.py` (`generate_topology`, `AI_CALL_FAILED_DETAIL`) \
  Pinned by: `services/ai-orchestrator/tests/test_generate.py` (`test_generate_bare_exception_never_leaks_text`, `test_generate_503_on_provider_unreachable`)
- **AI-GEN-10.** Seven proposal mistakes are refused: a template not in the summary, more
  devices of a template than are available, a role name used twice across devices and
  elements, an edge naming an undefined role, an edge from a role to itself, an edge
  between two elements, and a second edge between the same two device roles in either
  direction. \
  Enforced in: `services/ai-orchestrator/app/services/generator.py` (`_validate_against_inventory`) \
  Pinned by: `services/ai-orchestrator/tests/test_generate.py` (`test_generate_rejects_unknown_template`, `test_generate_rejects_overcommit`, `test_generate_rejects_duplicate_roles`, `test_generate_rejects_duplicate_role_across_device_and_element`, `test_generate_rejects_edge_with_unknown_role`, `test_generate_rejects_self_loop_edge`, `test_generate_rejects_element_to_element_edge`, `test_generate_rejects_duplicate_device_to_device_edge`)
- **AI-GEN-11.** Each such mistake re-prompts the model with the error and the exact list
  of allowed template names, up to `AI_GENERATE_MAX_REPAIRS` times (0 to 5, default 2;
  0 fails on the first mistake); after the last attempt the route answers 502 with the
  mistake's text. \
  Enforced in: `services/ai-orchestrator/app/services/generator.py` (`generate_topology`, `_repair_feedback`); `services/ai-orchestrator/app/config.py` (`Settings`) \
  Pinned by: `services/ai-orchestrator/tests/test_generate.py` (`test_generate_repairs_unknown_template_on_retry`, `test_generate_repairs_self_loop_edge_on_retry`, `test_generate_repair_loop_caps_at_max_repair_attempts_for_element_error`, `test_generate_zero_max_repairs_disables_retry`)
- **AI-GEN-12.** A duplicate device-to-device edge is refused even though the canvas
  supports parallel cables, by decision (issue #827 and the comment in
  `_validate_against_inventory`: AI edges carry no port names, so a repeat is taken as a
  mistake); several devices attaching to one element are allowed. \
  Enforced in: `services/ai-orchestrator/app/services/generator.py` (`_validate_against_inventory`) \
  Pinned by: `services/ai-orchestrator/tests/test_generate.py` (`test_generate_rejects_duplicate_device_to_device_edge`, `test_generate_allows_two_devices_attached_to_the_same_element`, `test_generate_allows_edge_from_device_role_to_element_role`)
- **AI-GEN-13.** A proposal without `elements` is valid and answers `elements: []`. \
  Enforced in: `services/ai-orchestrator/app/schemas/generate.py` (`GenerateResponse`) \
  Pinned by: `services/ai-orchestrator/tests/test_generate.py` (`test_generate_device_only_proposal_defaults_elements_to_empty`)
- **AI-GEN-14.** Token usage is summed over every attempt of one request and recorded once:
  after a successful response, or, when the request fails, as the total the failed
  request's attempts reported (AI-QUOTA-5). \
  Enforced in: `services/ai-orchestrator/app/services/generator.py` (`generate_topology`, `GeneratorError`); `services/ai-orchestrator/app/routes/generate.py` (`generate`) \
  Pinned by: `services/ai-orchestrator/tests/test_generate.py` (`test_generate_records_usage_when_quota_enabled`, `test_generate_failed_after_repairs_meters_every_attempt`)
- **AI-GEN-15.** After resolution, the resolved devices' `topology_type` values must agree
  (a device record without one is not judged). A proposal that mixes types is a repairable
  mistake: the model is re-prompted with one line per type naming its templates, from the
  same `AI_GENERATE_MAX_REPAIRS` budget; after it, the route answers 422
  `{"error": "topology_mixed_types", "groups": [{topology_type, roles, templates}], "message"}`.
  A uniform proposal answers each device's `topology_type` as its resolved device's type.
  The committer still writes `topologyType: "PHYSICAL"` on every device node
  (AI-COMMIT-4). \
  Enforced in: `services/ai-orchestrator/app/services/generator.py` (`_check_uniform_topology_type`, `TopologyMixedTypesError`, `_propose_until_valid`) \
  Pinned by: `services/ai-orchestrator/tests/test_generate.py` (`test_generate_mixed_types_repairs_then_returns_structured_422`, `test_generate_mixed_types_is_repaired_on_retry`, `test_generate_topology_type_follows_the_resolved_devices`)
- **AI-GEN-16.** The response's `file_summaries` lists each extracted file's `filename`,
  character count, and `truncated` flag. \
  Enforced in: `services/ai-orchestrator/app/services/generator.py` (`generate_topology`) \
  Pinned by: `services/ai-orchestrator/tests/test_generate.py` (`test_generate_forwards_file_context_to_ai`)
- **AI-UPLOAD-1.** More than `UPLOAD_MAX_FILES` (default 5) file parts answers 400
  `Too many files: ...` before any part is read. \
  Enforced in: `services/ai-orchestrator/app/routes/generate.py` (`_read_uploads`) \
  Pinned by: `services/ai-orchestrator/tests/test_generate.py` (`test_read_uploads_rejects_too_many_files_without_reading_any`)
- **AI-UPLOAD-2.** Each part is read in 64 KiB chunks; the read stops with 400 at the chunk
  that takes the part past `UPLOAD_MAX_FILE_BYTES` (default 5 MiB) or the request past
  that size times the file cap. \
  Enforced in: `services/ai-orchestrator/app/routes/generate.py` (`_read_uploads`, `_UPLOAD_READ_CHUNK_BYTES`) \
  Pinned by: `services/ai-orchestrator/tests/test_generate.py` (`test_read_uploads_aborts_streaming_read_past_the_per_file_cap`, `test_generate_rejects_oversized_upload`)
- **AI-UPLOAD-3.** A part with no filename or no bytes is skipped silently. \
  Enforced in: `services/ai-orchestrator/app/routes/generate.py` (`_read_uploads`) \
  Pinned by: `services/ai-orchestrator/tests/test_generate.py` (`test_read_uploads_skips_nameless_and_empty_parts`)
- **AI-UPLOAD-4.** Only `.pdf`, `.txt`, `.md`, `.json`, `.xml`, `.tgz`, and `.tar.gz` are
  accepted; any other extension answers 400 naming the accepted list. \
  Enforced in: `services/ai-orchestrator/app/services/extractor.py` (`SUPPORTED_EXTENSIONS`, `_extract_one`) \
  Pinned by: `services/ai-orchestrator/tests/test_generate.py` (`test_generate_rejects_unsupported_file_extension`); `services/ai-orchestrator/tests/test_extractor.py` (`test_extract_rejects_unsupported_extension`)
- **AI-UPLOAD-5.** PDF text comes from pdfplumber (an unparsable PDF answers 400); JSON is
  pretty-printed with sorted keys, or passed raw when invalid; a tar.gz yields only its
  text members (`.txt`, `.md`, `.json`, `.xml`, `.log`, `.cfg`, `.conf`, `.yaml`, `.yml`)
  no larger than the per-file cap; text decodes as UTF-8 with a Latin-1 fallback. \
  Enforced in: `services/ai-orchestrator/app/services/extractor.py` (`_extract_pdf`, `_extract_tgz`, `_extract_one`, `_decode_text`) \
  Pinned by: `services/ai-orchestrator/tests/test_extractor.py` (`test_extract_json_reformats_valid_json`, `test_extract_json_falls_back_to_raw_on_invalid`, `test_extract_tgz_pulls_text_members`, `test_extract_pdf_raises_for_garbage`); `services/ai-orchestrator/tests/test_extractor_branches.py` (`test_tgz_skips_oversized_member`, `test_decode_text_falls_back_to_latin1`)
- **AI-UPLOAD-6.** All files together yield at most `UPLOAD_MAX_EXTRACTED_CHARS` (default
  80000) characters; a file that is cut, or reached after the budget is spent, is marked
  `truncated`. \
  Enforced in: `services/ai-orchestrator/app/services/extractor.py` (`extract_files`) \
  Pinned by: `services/ai-orchestrator/tests/test_extractor.py` (`test_extract_truncates_when_over_char_budget`, `test_extract_second_file_marked_truncated_when_budget_exhausted`)
- **AI-UPLOAD-7.** Extracted text goes to the model ahead of the prompt, framed as
  untrusted context; with no files the prompt is sent alone. \
  Enforced in: `services/ai-orchestrator/app/services/ai_client.py` (`propose_topology`) \
  Pinned by: `services/ai-orchestrator/tests/test_ai_client.py` (`test_propose_topology_prepends_file_context_when_present`, `test_propose_topology_omits_file_context_wrapper_when_absent`)

**Out of scope.** Choosing ports: the proposal never names a port for a device-to-device
edge (section 8.3). Saving the canvas as the user's own topology, which the topology editor
does (`topology.md`).

### 8.3 Cabling-aware device resolution

**What it does.** The model names roles and templates; the orchestrator picks real devices
for them, and only devices the lab's cabling can actually connect for every proposed link.
When no such choice exists, the model gets one more chance; after that the user is told
which connections the lab cannot make.

**Surfaces.** `services/ai-orchestrator/app/services/generator.py` (`_resolve_devices`),
the pure search in `services/ai-orchestrator/app/services/resolver.py`, and cabling's
`POST /pathfind/batch` through `services/ai-orchestrator/app/services/cabling_client.py`.

**Rules.**

- **AI-RESOLVE-1.** For each distinct proposed template the resolver fetches up to
  max(number of roles, `AI_RESOLVER_CANDIDATES_PER_TEMPLATE`) `AVAILABLE` `dut_only`
  devices with the caller's JWT, in inventory order. \
  Enforced in: `services/ai-orchestrator/app/services/generator.py` (`_resolve_devices`); `services/ai-orchestrator/app/services/inventory_client.py` (`fetch_available_devices`) \
  Pinned by: `services/ai-orchestrator/tests/test_generate.py` (`test_generate_fetches_the_configured_candidate_count`, `test_generate_candidate_cap_above_availability_is_not_an_inventory_shift`)
- **AI-RESOLVE-2.** Fewer devices than roles, or a template no longer in the summary,
  answers 409 `Inventory shifted ...`; it is not repaired. \
  Enforced in: `services/ai-orchestrator/app/services/generator.py` (`_resolve_devices`) \
  Pinned by: `services/ai-orchestrator/tests/test_generate.py` (`test_generate_returns_409_when_inventory_shifts`)
- **AI-RESOLVE-3.** A transport error, a non-2xx, or an unreadable body during the
  candidate fetch answers 503
  `Could not read inventory; no topology was generated. Retry the request.`; it is not
  repaired. \
  Enforced in: `services/ai-orchestrator/app/services/inventory_client.py` (`fetch_available_devices`, `InventoryUnavailableError`); `services/ai-orchestrator/app/services/generator.py` (`_resolve_devices`, `INVENTORY_UNAVAILABLE_DETAIL`) \
  Pinned by: `services/ai-orchestrator/tests/test_generate.py` (`test_generate_503_when_inventory_fails_during_candidate_fetch`); `services/ai-orchestrator/tests/test_inventory_client.py` (`test_fetch_available_devices_raises_on_5xx`, `test_fetch_available_devices_transport_error_is_inventory_unavailable`)
- **AI-RESOLVE-4.** Only edges whose two ends are device roles constrain the choice; an
  edge touching an element is ignored here, and no pathfind call is made when no
  device-to-device edge remains. \
  Enforced in: `services/ai-orchestrator/app/services/resolver.py` (`device_edges`); `services/ai-orchestrator/app/services/generator.py` (`_resolve_devices`) \
  Pinned by: `services/ai-orchestrator/tests/test_generate.py` (`test_generate_ignores_element_edges_in_the_feasibility_check`); `services/ai-orchestrator/tests/test_resolver.py` (`test_element_role_edges_are_ignored`)
- **AI-RESOLVE-5.** The pairs asked about are every unordered pair of two different
  candidates across an edge's two roles, each pair once. \
  Enforced in: `services/ai-orchestrator/app/services/resolver.py` (`candidate_pairs`, `pair_key`) \
  Pinned by: `services/ai-orchestrator/tests/test_resolver.py` (`test_candidate_pairs_are_deduplicated_and_order_independent`)
- **AI-RESOLVE-6.** The pathfind call fails closed: a transport error, a non-200, a
  non-JSON body, or a body without a `results` list stops generation with 503
  `Could not verify cabling paths; no topology was generated. Retry the request.`;
  reachability is never assumed. \
  Enforced in: `services/ai-orchestrator/app/services/cabling_client.py` (`fetch_pathfind_batch`, `CablingUnavailableError`); `services/ai-orchestrator/app/services/generator.py` (`CABLING_UNAVAILABLE_DETAIL`) \
  Pinned by: `services/ai-orchestrator/tests/test_generate.py` (`test_generate_503_when_pathfind_is_unavailable`)
- **AI-RESOLVE-7.** Pairs are sent in chunks of 200 per request with the caller's JWT and a
  20 second timeout. \
  Enforced in: `services/ai-orchestrator/app/services/cabling_client.py` (`PATHFIND_BATCH_CHUNK`, `PATHFIND_TIMEOUT_SECONDS`) \
  Pinned by: `services/ai-orchestrator/tests/test_generate.py` (`test_pathfind_batch_chunks_at_200_with_the_callers_jwt`)
- **AI-RESOLVE-8.** A pair result carrying `error`, or without `reachable: true`, counts as
  not reachable. Cabling answers a pair naming a device the caller cannot see with the
  same per-pair error as an unknown device (`topology.md`). \
  Enforced in: `services/ai-orchestrator/app/services/resolver.py` (`read_pathfind_results`) \
  Pinned by: `services/ai-orchestrator/tests/test_resolver.py` (`test_read_pathfind_results_extracts_reachability`)
- **AI-RESOLVE-9.** The search is deterministic: roles in order of fewest candidates, then
  most edges, then role name; candidates in inventory order; the first assignment where
  every edge lands on a reachable pair wins. \
  Enforced in: `services/ai-orchestrator/app/services/resolver.py` (`plan_assignment`) \
  Pinned by: `services/ai-orchestrator/tests/test_resolver.py` (`test_search_is_deterministic_for_the_same_input`, `test_backtracking_finds_the_reachable_pair_first_n_would_miss`); `services/ai-orchestrator/tests/test_generate.py` (`test_generate_resolves_to_the_reachable_devices_not_the_first_listed`)
- **AI-RESOLVE-10.** No device is given to two roles. \
  Enforced in: `services/ai-orchestrator/app/services/resolver.py` (`plan_assignment`, `_consistent`) \
  Pinned by: `services/ai-orchestrator/tests/test_resolver.py` (`test_two_roles_of_one_template_get_distinct_devices`)
- **AI-RESOLVE-11.** The resolver judges reachability only, never per-port capacity, by
  decision: port choice stays with cabling's fork-save resolver, and a stricter rule than
  the validator it pre-empts refused topologies HERD accepts (module docstring of
  `services/ai-orchestrator/app/services/resolver.py`; ADR 0006; issue #531). \
  Enforced in: `services/ai-orchestrator/app/services/resolver.py` (`plan_assignment`) \
  Pinned by: `services/ai-orchestrator/tests/test_resolver.py` (`test_hub_role_with_multiple_edges_may_reuse_one_reachable_device`)
- **AI-RESOLVE-12.** An edge with no reachable candidate pair at all is reported before any
  search runs, naming that edge. \
  Enforced in: `services/ai-orchestrator/app/services/resolver.py` (`plan_assignment`, `_edge_has_feasible_pair`) \
  Pinned by: `services/ai-orchestrator/tests/test_resolver.py` (`test_unreachable_edge_is_reported_with_no_candidate_pair`)
- **AI-RESOLVE-13.** The search stops after `AI_RESOLVER_MAX_SEARCH_STEPS` candidate trials
  (default 5000) and is then treated as infeasible, blaming the edges of the deepest role
  it could not place. \
  Enforced in: `services/ai-orchestrator/app/services/resolver.py` (`plan_assignment`) \
  Pinned by: `services/ai-orchestrator/tests/test_resolver.py` (`test_step_budget_exhaustion_reports_the_unsatisfied_edges`)
- **AI-RESOLVE-14.** An infeasible proposal is a repairable mistake: the model is
  re-prompted with one line per template pair that has no cabled path, from the same
  `AI_GENERATE_MAX_REPAIRS` budget; after it, the route answers 422
  `{"error": "topology_unconnectable", "pairs": [{source_role, target_role, source_template, target_template}], "message"}`.
  By decision a proposal is never returned flagged and no edge is dropped
  (`TopologyUnconnectableError` docstring; issue #828). \
  Enforced in: `services/ai-orchestrator/app/services/generator.py` (`TopologyUnconnectableError`, `_unconnectable_error`, `_unconnectable_repair_feedback`, `generate_topology`) \
  Pinned by: `services/ai-orchestrator/tests/test_generate.py` (`test_generate_unconnectable_repairs_then_returns_structured_422`)
- **AI-RESOLVE-15.** Each proposed device's `device` field holds the inventory device
  record the search chose. \
  Enforced in: `services/ai-orchestrator/app/services/generator.py` (`_resolve_devices`) \
  Pinned by: `services/ai-orchestrator/tests/test_generate.py` (`test_generate_returns_proposal_when_ai_succeeds`)

**Out of scope.** What makes two devices reachable, and the batch pathfinder's own limits
(`topology.md`).

### 8.4 Committing a proposal

**What it does.** When the user accepts a proposal, HERD creates a new topology from it,
checks that every connection can really be wired, and books a reservation for its devices;
optionally it then pushes each device's suggested configuration.

**Surfaces.** User interface `frontend/src/components/topology-editor/AICommitDialog.tsx`;
route `POST /commit` (body `CommitRequest`); the flow in
`services/ai-orchestrator/app/services/committer.py` (`commit_proposal`).

**Rules.**

- **AI-COMMIT-1.** Any signed-in user may commit; every upstream call (inventory, cabling,
  reservations, execution) carries the caller's JWT, so those services' own permission
  and visibility rules decide. No token answers 401. \
  Enforced in: `services/ai-orchestrator/app/routes/commit.py` (`commit`); `services/ai-orchestrator/app/services/committer.py` (`commit_proposal`) \
  Pinned by: `services/ai-orchestrator/tests/test_commit.py` (`test_commit_requires_auth`, `test_commit_forwards_user_jwt_to_upstream`)
- **AI-COMMIT-2.** The body needs `topology_name` (1 to 200 characters), `start_time`,
  `end_time` strictly after it, at least one device, and a `purpose` of at most 500
  characters; otherwise 422. \
  Enforced in: `services/ai-orchestrator/app/schemas/generate.py` (`CommitRequest`, `end_after_start`, `devices_not_empty`) \
  Pinned by: `services/ai-orchestrator/tests/test_commit.py` (`test_commit_rejects_missing_devices`, `test_commit_rejects_end_before_start`); `tests/integration/test_ai_commit_route.py` (`test_commit_missing_topology_name_rejected`)
- **AI-COMMIT-3.** Before any write, every device's `config` is validated against the
  `connection_type` the request supplies; a missing config passes, and a config with no or
  an unregistered connection type, an unknown key, or a bad value answers 422 with nothing
  written. The schema registry itself is `device-configuration.md`. \
  Enforced in: `services/ai-orchestrator/app/services/committer.py` (`commit_proposal`); `services/ai-orchestrator/app/services/config_validator.py` (`validate_device_config`) \
  Pinned by: `services/ai-orchestrator/tests/test_commit.py` (`test_commit_rejects_config_without_connection_type`, `test_commit_rejects_config_on_unsupported_connection_type`, `test_commit_rejects_unknown_config_key`, `test_commit_rejects_out_of_range_vlan`, `test_commit_allows_no_config_on_unknown_connection_type`)
- **AI-COMMIT-4.** The canvas has one `deviceNode` per device whose `data` is
  `{device: {id}, label: <role>, topologyType: "PHYSICAL"}`, placed at the request's
  position or in a row; each device-to-device edge is `{id, source, target, data: {layer}}`;
  `selectedEdgeLayer` is `L2`. \
  Enforced in: `services/ai-orchestrator/app/services/committer.py` (`_build_canvas_data`) \
  Pinned by: `services/ai-orchestrator/tests/test_committer_error_paths.py` (`test_build_canvas_device_to_device_path_is_byte_for_byte_unchanged`); `services/ai-orchestrator/tests/test_commit.py` (`test_commit_happy_path_creates_topology_and_reservation`)
- **AI-COMMIT-5.** Each element becomes a `networkElementNode` in a second row; each
  device-to-element edge becomes an edge from the device carrying the first of that
  device's ports, in natural name order, not already taken by another attachment of the
  same device. \
  Enforced in: `services/ai-orchestrator/app/services/committer.py` (`_build_canvas_data`, `_select_element_port`, `_natural_port_key`) \
  Pinned by: `services/ai-orchestrator/tests/test_committer_error_paths.py` (`test_build_canvas_element_node_shape`, `test_build_canvas_attachment_edge_has_device_as_source_with_chosen_port`, `test_build_canvas_two_attachments_from_one_device_get_distinct_ports`); `services/ai-orchestrator/tests/test_commit.py` (`test_commit_with_element_produces_canvas_with_element_and_attachment`)
- **AI-COMMIT-6.** The port lookup answers "no ports" on a 404 or another 4xx, and the
  attachment is then dropped with a warning; a 5xx answers 503
  `Failed to fetch ports for device <id>: inventory answered HTTP <status>` and a transport
  error 503 `Failed to fetch ports for device <id>`, before any topology is created, with no
  upstream text. \
  Enforced in: `services/ai-orchestrator/app/services/committer.py` (`_fetch_device_ports`) \
  Pinned by: `services/ai-orchestrator/tests/test_committer_error_paths.py` (`test_fetch_device_ports_404_is_treated_as_no_ports`, `test_fetch_device_ports_5xx_raises_commit_error`, `test_fetch_device_ports_transport_failure_raises_commit_error`, `test_build_canvas_device_with_no_ports_skips_attachment_with_warning`); `services/ai-orchestrator/tests/test_commit.py` (`test_commit_aborts_with_503_when_ports_fetch_hits_5xx`)
- **AI-COMMIT-7.** An edge naming an unknown role, or joining two elements, is dropped
  silently. \
  Enforced in: `services/ai-orchestrator/app/services/committer.py` (`_build_canvas_data`) \
  Pinned by: `services/ai-orchestrator/tests/test_committer_error_paths.py` (`test_build_canvas_skips_edge_with_unknown_role`)
- **AI-COMMIT-8.** The order is: create the topology (`POST /topologies {name}`), save the
  canvas, validate it, create the reservation (`POST /` with `device_ids`, `topology_id`,
  `purpose`, `start_time`, `end_time`). A refused topology create is relayed with its
  status as `Failed to create topology: <detail>`, and there is nothing to roll back. \
  Enforced in: `services/ai-orchestrator/app/services/committer.py` (`commit_proposal`, `_create_topology`, `_create_reservation`) \
  Pinned by: `services/ai-orchestrator/tests/test_commit.py` (`test_commit_happy_path_creates_topology_and_reservation`, `test_commit_surfaces_topology_create_failure_without_rollback`)
- **AI-COMMIT-9.** The wireability check calls cabling's user-facing
  `POST /topologies/{id}/validate` and fails closed: a transport error, a 5xx, a 200 that is
  not JSON, or a 200 without a boolean `valid` answers 503
  (`Failed to validate topology wireability: ...` naming which, with the status for a 5xx
  and `cabling unreachable` for a transport error, never upstream text); another 4xx is
  relayed; only `valid: true` proceeds. What validate checks is `topology.md`. \
  Enforced in: `services/ai-orchestrator/app/services/committer.py` (`_validate_topology_wireable`) \
  Pinned by: `services/ai-orchestrator/tests/test_commit.py` (`test_commit_validate_5xx_fails_closed_with_503`, `test_commit_validate_transport_failure_fails_closed_with_503`); `services/ai-orchestrator/tests/test_committer_error_paths.py` (`test_commit_validate_non_json_200_body_fails_closed_with_503`, `test_commit_validate_body_missing_valid_key_fails_closed_with_503`)
- **AI-COMMIT-10.** `valid: false` answers 422
  `{"error": "topology_unwireable", "invalid_edges": [{edge_id, source_role, target_role, reason}], "message"}`,
  naming devices by proposal role (an unknown id stays the id, a missing one is
  `unknown`). \
  Enforced in: `services/ai-orchestrator/app/services/committer.py` (`_validate_topology_wireable`) \
  Pinned by: `services/ai-orchestrator/tests/test_commit.py` (`test_commit_rejects_unwireable_topology_with_structured_detail`)
- **AI-COMMIT-11.** When the canvas save, the check, or the reservation create fails, the
  new topology is deleted and the failure re-raised; a refused reservation is relayed with
  reservations' status as `Failed to create reservation: <detail>`. \
  Enforced in: `services/ai-orchestrator/app/services/committer.py` (`commit_proposal`) \
  Pinned by: `services/ai-orchestrator/tests/test_commit.py` (`test_commit_rolls_back_topology_when_canvas_save_fails`, `test_commit_rolls_back_topology_when_reservation_fails`, `test_commit_rejects_unwireable_topology_with_structured_detail`)
- **AI-COMMIT-12.** Any other exception in those three steps also deletes the topology and
  answers 502 `Unexpected upstream failure (<ExceptionClass>)`; the exception is logged. \
  Enforced in: `services/ai-orchestrator/app/services/committer.py` (`commit_proposal`) \
  Pinned by: `services/ai-orchestrator/tests/test_committer_error_paths.py` (`test_commit_unexpected_error_rolls_back_and_wraps_502`)
- **AI-COMMIT-13.** The rollback delete never raises; a refusal (status 300 or above, for
  example 409 `topology_in_use` when the reservation did land despite the error) keeps the
  topology and logs `rollback_topology_delete_refused`. The delete guard itself is
  `topology.md`. \
  Enforced in: `services/ai-orchestrator/app/services/committer.py` (`_delete_topology`) \
  Pinned by: `services/ai-orchestrator/tests/test_committer_error_paths.py` (`test_delete_topology_swallows_exception`, `test_delete_topology_logs_a_refused_rollback`, `test_delete_topology_success_logs_nothing`)
- **AI-COMMIT-14.** With `apply_configs` false no execution call is made and
  `config_results` is empty. \
  Enforced in: `services/ai-orchestrator/app/services/committer.py` (`commit_proposal`) \
  Pinned by: `services/ai-orchestrator/tests/test_commit.py` (`test_commit_skips_execution_when_apply_configs_false`)
- **AI-COMMIT-15.** With `apply_configs` true, each device without a config is `skipped`,
  and each with one gets execution `POST /execute` with `action: configure`, the
  reservation id, and the config as `method_kwargs`; no `dry_run` flag is sent, so this is
  a real apply. Who may run it is execution's rule (`device-configuration.md`). \
  Enforced in: `services/ai-orchestrator/app/services/committer.py` (`_apply_configs`) \
  Pinned by: `services/ai-orchestrator/tests/test_commit.py` (`test_commit_apply_configs_calls_execution_per_device`)
- **AI-COMMIT-16.** A config push never fails or rolls back the commit: a transport error
  records `failed` with `request failed (<ExceptionClass>)`; a 4xx or 5xx records `failed` with the
  detail, except that a 409 `driver_cannot_configure` or `device_has_no_driver` records its
  structured `message`. \
  Enforced in: `services/ai-orchestrator/app/services/committer.py` (`_apply_configs`, `_structured_detail`) \
  Pinned by: `services/ai-orchestrator/tests/test_commit.py` (`test_commit_apply_configs_records_failure_without_rollback`); `services/ai-orchestrator/tests/test_committer_error_paths.py` (`test_apply_configs_records_request_exception_as_failed`, `test_apply_configs_non_success_status_marks_failed`, `test_apply_configs_driver_cannot_configure_reports_plain_message`, `test_apply_configs_device_has_no_driver_reports_plain_message`, `test_apply_configs_other_409_falls_back_to_generic_detail`); `tests/integration/test_execution_configure_gate.py` (`test_ai_commit_apply_configs_refuses_l3_device_via_execution_gate`)
- **AI-COMMIT-17.** A 2xx push counts as `success` only when its body's `status` is
  `SUCCESS`; a body without a status, or not JSON, counts as `failed`. \
  Enforced in: `services/ai-orchestrator/app/services/committer.py` (`_apply_configs`) \
  Pinned by: `services/ai-orchestrator/tests/test_committer_error_paths.py` (`test_apply_configs_handles_non_json_success_body`)
- **AI-COMMIT-18.** Commit makes no model call and is not gated on provider configuration
  or the token quota. \
  Enforced in: `services/ai-orchestrator/app/routes/commit.py` (`commit`) \
  Pinned by: `tests/integration/test_ai_commit_route.py` (`test_commit_does_not_gate_on_ai_provider`)
- **AI-COMMIT-19.** A successful commit answers `{topology_id, reservation_id, config_results}`. \
  Enforced in: `services/ai-orchestrator/app/services/committer.py` (`commit_proposal`) \
  Pinned by: `services/ai-orchestrator/tests/test_commit.py` (`test_commit_happy_path_creates_topology_and_reservation`)

**Out of scope.** The reservation's own create rules, including the second topology
validation reservations runs (`reservations.md`, RES-TOPO-3). The configure gate's rule
(`device-configuration.md`); this area only maps its 409s.

### 8.5 Reservation assistant

**What it does.** On a reservation they own, a user can ask questions in plain words and
get answers grounded in live data the assistant fetches with the user's own permissions.
When an operator turns on write tools, the assistant can also draft a configuration change
and schedule a dry run of it; the user reviews the captured commands and confirms before
anything real is applied.

**Surfaces.** User interface `frontend/src/components/reservations/AIAssistantTab.tsx`
(and the default `AIAssistantTabLegacy.tsx`, section 8.12); routes
`POST /reservations/{id}/assistant` and `POST /reservations/{id}/assistant/stream`; tools
in `services/ai-orchestrator/app/services/tools.py`; persistence in
`services/ai-orchestrator/app/services/conversation_repo.py`; background work
`conversation_sweeper_loop` every `ASSISTANT_SWEEPER_INTERVAL_SECONDS`.

**Rules.**

- **AI-CONV-1.** A turn with no `conversation_id` reads the reservation through
  reservations `GET /{id}` and each of its devices through inventory `GET /devices/{id}`
  with the caller's JWT (8 at a time, 15 seconds per call, 30 seconds in all). Because
  that read is owner-only for every role (`reservations.md`, RES-VIEW-2), a non-owner,
  admin or superadmin included, gets 404 `Reservation not found`; a gather past its
  deadline answers 504. \
  Enforced in: `services/ai-orchestrator/app/services/reservation_context.py` (`gather_reservation_seed`, `_gather_seed_inner`); `services/ai-orchestrator/app/routes/reservation_assistant.py` (`get_reservation_seed_dep`) \
  Pinned by: `services/ai-orchestrator/tests/test_reservation_assistant_coverage.py` (`test_seed_dep_maps_not_found_to_404`, `test_seed_dep_maps_deadline_to_504`); `services/ai-orchestrator/tests/test_reservation_context.py` (`test_seed_gather_raises_when_reservation_not_found`, `test_seed_gather_respects_deadline`); `tests/integration/test_ai_assistant_tools.py` (`test_assistant_reservation_404_for_non_owner`)
- **AI-CONV-2.** The seed carries the reservation's `id`, `status`, `start_time`,
  `end_time`, `topology_id`, `topology_type`, `purpose`, and `owner_name`, and each
  device's `id`, `name`, `template_name`, `template_vendor`, `template_model`, and
  `status`; a device inventory answers 404 for is left out. It is stored with the first
  question as the position-0 user message. \
  Enforced in: `services/ai-orchestrator/app/services/reservation_context.py` (`_SEED_RESERVATION_FIELDS`, `_SEED_DEVICE_FIELDS`, `render_seed_block`); `services/ai-orchestrator/app/services/conversation_repo.py` (`create`, `set_seed_with_question`) \
  Pinned by: `services/ai-orchestrator/tests/test_reservation_context.py` (`test_seed_gather_returns_thin_bundle`, `test_seed_gather_skips_missing_devices`, `test_render_seed_emits_xml_blocks_without_topology`); `services/ai-orchestrator/tests/test_conversation_repo.py` (`test_create_persists_seed_as_first_user_message`, `test_set_seed_with_question_wraps_question_in_first_message`)
- **AI-CONV-3.** A transport error, any other non-2xx, or a body that is not JSON from
  reservations or inventory during the seed read answers 503
  `Could not read the reservation or its devices; retry the request.`, with no upstream
  text. \
  Enforced in: `services/ai-orchestrator/app/services/reservation_context.py` (`_get_json`, `ContextUpstreamUnavailableError`); `services/ai-orchestrator/app/routes/reservation_assistant.py` (`get_reservation_seed_dep`, `RESERVATION_CONTEXT_UNAVAILABLE_DETAIL`) \
  Pinned by: `services/ai-orchestrator/tests/test_reservation_context.py` (`test_seed_gather_reservations_non_2xx_is_upstream_unavailable`, `test_seed_gather_transport_error_is_upstream_unavailable`, `test_seed_gather_device_5xx_is_upstream_unavailable`, `test_seed_gather_non_json_reservation_body_is_upstream_unavailable`); `services/ai-orchestrator/tests/test_reservation_assistant_coverage.py` (`test_seed_dep_maps_upstream_unavailable_to_503`)
- **AI-CONV-4.** A turn with a `conversation_id` requires a conversation created by this
  caller for this reservation id; anything else, including another user's conversation,
  answers 404 `Conversation not found`. \
  Enforced in: `services/ai-orchestrator/app/services/conversation_repo.py` (`get_or_404`); `services/ai-orchestrator/app/routes/reservation_assistant.py` (`_prepare_turn`) \
  Pinned by: `services/ai-orchestrator/tests/test_reservation_assistant.py` (`test_second_turn_with_unknown_conversation_id_returns_404`, `test_second_turn_with_other_users_conversation_id_returns_404`, `test_stream_unknown_conversation_id_returns_404`, `test_stream_other_users_conversation_id_returns_404`); `services/ai-orchestrator/tests/test_conversation_repo.py` (`test_get_or_404_returns_none_for_wrong_reservation`)
- **AI-CONV-5.** A later turn reads neither the reservation nor its devices again. No
  turn, first or later, is refused because of the reservation's status; each tool call is
  decided by the owning service (AI-TOOL-2). \
  Enforced in: `services/ai-orchestrator/app/routes/reservation_assistant.py` (`_prepare_turn`) \
  Pinned by: `services/ai-orchestrator/tests/test_reservation_assistant.py` (`test_later_turn_reads_neither_the_reservation_nor_its_devices`)
- **AI-CONV-6.** A turn is one transaction: the new user message is only flushed, and is
  committed together with the reply; a turn that fails without a side effect leaves no
  conversation row or trailing user message behind. \
  Enforced in: `services/ai-orchestrator/app/routes/reservation_assistant.py` (`_prepare_turn`, `_persist_turn`) \
  Pinned by: `services/ai-orchestrator/tests/test_reservation_assistant.py` (`test_buffered_failure_leaves_no_orphan_and_next_turn_succeeds`, `test_stream_failure_leaves_no_orphan_and_next_turn_succeeds`, `test_stream_no_answer_leaves_no_orphan`)
- **AI-CONV-7.** Each loop iteration is stored as an `ASSISTANT` message with every block
  the model returned and, when tools ran, one `TOOL` message with their results; the next
  turn replays them unchanged, and an unknown stored block type raises. \
  Enforced in: `services/ai-orchestrator/app/services/conversation_repo.py` (`append_assistant_turn`, `load_messages`, `_json_to_block`) \
  Pinned by: `services/ai-orchestrator/tests/test_conversation_repo.py` (`test_append_assistant_turn_stores_assistant_and_tool_result_in_order`, `test_load_messages_rehydrates_all_block_types`, `test_json_to_block_raises_on_unknown_type`)
- **AI-CONV-8.** After each stored turn, while the conversation holds more than
  `ASSISTANT_MAX_TURNS` messages (the seed counted) or more than
  `ASSISTANT_HISTORY_TOKEN_BUDGET` estimated tokens (characters divided by 4), the oldest
  user message after the seed is deleted together with the assistant and tool messages
  that follow it; the seed is never deleted. \
  Enforced in: `services/ai-orchestrator/app/services/conversation_repo.py` (`evict_to_budget`, `_estimate_tokens_in_blocks`) \
  Pinned by: `services/ai-orchestrator/tests/test_conversation_repo.py` (`test_evict_to_budget_drops_oldest_pair_when_over_turn_cap`, `test_evict_to_budget_respects_token_budget`)
- **AI-CONV-9.** Every `ASSISTANT_SWEEPER_INTERVAL_SECONDS` the sweeper deletes every
  conversation, with its messages, whose `last_used_at` is older than
  `ASSISTANT_CONVERSATION_TTL_HOURS`; a failed cycle is logged and the loop continues.
  The delete re-applies the cutoff, so a conversation used during the cycle survives. \
  Enforced in: `services/ai-orchestrator/app/services/conversation_repo.py` (`expire_idle`); `services/ai-orchestrator/app/tasks/conversation_sweeper.py` (`conversation_sweeper_loop`) \
  Pinned by: `services/ai-orchestrator/tests/test_conversation_repo.py` (`test_expire_idle_deletes_old_conversations_and_keeps_recent`, `test_expire_idle_custom_ttl_setting_moves_the_cutoff`); `services/ai-orchestrator/tests/test_conversation_sweeper.py` (`test_run_sweeper_cycle_deletes_idle_conversations`, `test_loop_swallows_cycle_exception_and_keeps_running`); `services/ai-orchestrator/tests/test_transcript_retention.py` (`test_a_conversation_used_during_the_lookups_is_not_deleted`)
- **AI-CONV-13.** While `AI_PURPOSE_CLASSIFICATION_ENABLED` and
  `AI_PURPOSE_INCLUDE_TRANSCRIPTS` are both on, the sweeper keeps an idle conversation
  whose reservation is not terminal, or is terminal with `purpose_classification_pending`
  true (requested and no suggestion yet), so the end pass can read the transcript
  (AI-PURPOSE-6). It asks reservations `GET /internal/{id}` with the internal token once
  per reservation per cycle, 8 at a time, after its read transaction ends, and fails
  closed: a transport error, a missing token, a non-200 other than 404, a malformed body,
  or an unknown status keeps the conversation and logs
  `conversation_retention_lookup_failed`; a 404 releases it. With either flag off no
  lookup is made and AI-CONV-9 applies unchanged (issue #1039). \
  Enforced in: `services/ai-orchestrator/app/services/conversation_repo.py` (`expire_idle`); `services/ai-orchestrator/app/services/transcript_retention.py` (`reservation_keeps_transcript`, `transcripts_owed_to_classifier`) \
  Pinned by: `services/ai-orchestrator/tests/test_transcript_retention.py` (`test_a_live_reservation_keeps_its_transcript`, `test_a_terminal_reservation_awaiting_classification_keeps_its_transcript`, `test_a_terminal_reservation_already_classified_releases_its_transcript`, `test_an_unknown_reservation_releases_its_transcript`, `test_an_unclear_answer_keeps_the_transcript`, `test_an_unreachable_reservations_service_keeps_the_transcript`, `test_a_missing_internal_token_keeps_the_transcript`, `test_the_sweep_keeps_only_what_the_classifier_still_owes`, `test_the_sweep_asks_once_per_reservation`, `test_without_a_transcript_reader_the_plain_ttl_applies_with_no_lookup`, `test_a_failed_lookup_is_logged_by_reason_and_status`)
- **AI-CONV-10.** `ASSISTANT_CONVERSATION_TTL_HOURS` of 0 or less is refused at startup. \
  Enforced in: `services/ai-orchestrator/app/config.py` (`_validate_assistant_conversation_ttl_hours`) \
  Pinned by: `services/ai-orchestrator/tests/test_config.py` (`test_zero_or_negative_ttl_hours_rejected`)
- **AI-CONV-11.** `question` is 1 to 4000 characters; otherwise 422. \
  Enforced in: `services/ai-orchestrator/app/schemas/assistant.py` (`AssistantRequest`) \
  Pinned by: `services/ai-orchestrator/tests/test_reservation_assistant.py` (`test_empty_question_rejected`, `test_oversize_question_rejected`)
- **AI-CONV-12.** The quota is checked before the seed read or any conversation work; an
  over-quota caller gets 429 and the model is not called. \
  Enforced in: `services/ai-orchestrator/app/routes/reservation_assistant.py` (`_prepare_turn`) \
  Pinned by: `services/ai-orchestrator/tests/test_reservation_assistant.py` (`test_over_quota_returns_429_without_calling_ai`, `test_stream_over_quota_returns_429_without_calling_ai`)
- **AI-LOOP-1.** Each loop call sends the system prompt, the history, the active tools,
  automatic tool choice, and the per-call timeout `ASSISTANT_PER_CALL_TIMEOUT_S`. \
  Enforced in: `services/ai-orchestrator/app/services/ai_client.py` (`answer_reservation_question_with_tools`) \
  Pinned by: `services/ai-orchestrator/tests/test_ai_client.py` (`test_tool_loop_passes_timeout_to_provider`, `test_tool_loop_uses_auto_tool_choice_during_loop`)
- **AI-LOOP-2.** The loop continues only while the stop reason is `tool_use` and the reply
  holds at least one tool call; all tool calls of one reply are dispatched concurrently and
  their results returned in order under their call ids. \
  Enforced in: `services/ai-orchestrator/app/services/ai_client.py` (`answer_reservation_question_with_tools`) \
  Pinned by: `services/ai-orchestrator/tests/test_ai_client.py` (`test_tool_loop_one_tool_call_then_text`, `test_tool_loop_parallel_tool_calls_in_one_turn`, `test_tool_loop_exits_when_stop_reason_is_max_tokens_with_text`); `services/ai-orchestrator/tests/test_provider_round_trip.py` (`test_parallel_tool_calls_preserve_each_id_in_order`, `test_tool_use_id_round_trips_into_next_turn`)
- **AI-LOOP-3.** After `ASSISTANT_MAX_TOOL_ITERATIONS` tool rounds, one more call is made
  with no tools, the base system prompt, and an added "exhausted your tool budget" user
  message; its text is the answer. \
  Enforced in: `services/ai-orchestrator/app/services/ai_client.py` (`answer_reservation_question_with_tools`, `answer_reservation_question_streaming`, `RESERVATION_ASSISTANT_TOOL_SYSTEM_PROMPT`) \
  Pinned by: `services/ai-orchestrator/tests/test_ai_client.py` (`test_tool_loop_iteration_cap_forces_final_answer_without_tools`, `test_streaming_iteration_cap_forces_buffered_final_answer`); `services/ai-orchestrator/tests/test_provider_round_trip.py` (`test_loop_drops_tools_in_iteration_cap_followup_call`); `services/ai-orchestrator/tests/test_reservation_assistant.py` (`test_iteration_cap_exceeded_still_returns_200`)
- **AI-LOOP-4.** That final call returning no text raises `AIError`. \
  Enforced in: `services/ai-orchestrator/app/services/ai_client.py` (`answer_reservation_question_with_tools`, `answer_reservation_question_streaming`) \
  Pinned by: `services/ai-orchestrator/tests/test_ai_client.py` (`test_tool_loop_iteration_cap_with_no_text_raises_aierror`, `test_streaming_iteration_cap_no_text_raises_aierror`)
- **AI-LOOP-5.** A reply that ends the loop with no text raises `AIError`
  (`AI returned no text content`) when no tool was dispatched in this turn; when at least
  one was (even one that errored), the answer becomes `NO_SUMMARY_FALLBACK_ANSWER`, stored
  as a text block, and `ai_assistant_no_text_after_tools` is logged. \
  Enforced in: `services/ai-orchestrator/app/services/ai_client.py` (`answer_reservation_question_with_tools`, `answer_reservation_question_streaming`, `NO_SUMMARY_FALLBACK_ANSWER`) \
  Pinned by: `services/ai-orchestrator/tests/test_reservation_assistant.py` (`test_buffered_no_text_exact_error_wording`, `test_buffered_no_text_after_tool_dispatch_returns_fallback_and_persists`); `services/ai-orchestrator/tests/test_ai_client.py` (`test_tool_loop_no_text_after_successful_tool_returns_fallback`, `test_tool_loop_no_text_after_failed_tool_returns_fallback`, `test_tool_loop_no_text_after_tools_logs_warning`, `test_streaming_no_text_after_successful_tool_returns_fallback`, `test_streaming_empty_final_text_raises`)
- **AI-LOOP-6.** The system prompt gains the documentation-tools section only when a
  documentation source is enabled (AI-DOCS-2). \
  Enforced in: `services/ai-orchestrator/app/services/ai_client.py` (`reservation_assistant_system_prompt`, `RESERVATION_ASSISTANT_DOCS_TOOLS_PROMPT`) \
  Pinned by: `services/ai-orchestrator/tests/test_docs_tools.py` (`test_system_prompt_mentions_the_docs_tools_only_when_enabled`)
- **AI-LOOP-7.** The system prompt gains the write-tools section only when
  `AI_WRITE_TOOLS_ENABLED` is set. \
  Enforced in: `services/ai-orchestrator/app/services/ai_client.py` (`reservation_assistant_system_prompt`, `RESERVATION_ASSISTANT_WRITE_TOOLS_PROMPT`) \
  Pinned by: `services/ai-orchestrator/tests/test_assistant_system_prompt.py` (`test_system_prompt_has_the_write_tools_section_only_when_enabled`)
- **AI-LOOP-8.** The loop appends finished iterations and token counts to objects the route
  passed in, so a later failure still sees what completed. \
  Enforced in: `services/ai-orchestrator/app/services/ai_client.py` (`answer_reservation_question_with_tools`, `answer_reservation_question_streaming`) \
  Pinned by: `services/ai-orchestrator/tests/test_ai_client.py` (`test_with_tools_segments_and_usage_reflect_partial_progress_on_raise`, `test_streaming_segments_and_usage_reflect_partial_progress_on_raise`)
- **AI-TURN-1.** A finished buffered turn answers 200 with `answer`, `model` (the
  configured `AI_MODEL`), `input_tokens`, `output_tokens`, `stop_reason`, `tool_calls`,
  `tool_iterations`, `conversation_id`, `pending_apply`, and `incomplete: null`. \
  Enforced in: `services/ai-orchestrator/app/routes/reservation_assistant.py` (`reservation_assistant`); `services/ai-orchestrator/app/schemas/assistant.py` (`AssistantResponse`) \
  Pinned by: `services/ai-orchestrator/tests/test_reservation_assistant.py` (`test_happy_path_returns_answer`, `test_response_includes_tool_calls_array`, `test_first_turn_creates_conversation_and_returns_id`, `test_second_turn_with_provided_conversation_id_appends_to_existing`)
- **AI-TURN-2.** `tool_calls` lists every attempted call, refused and failed ones included,
  each with `name`, an argument summary of at most 80 characters, `duration_ms`, and
  `error`. \
  Enforced in: `services/ai-orchestrator/app/services/tools.py` (`dispatch`, `_summarise_args`) \
  Pinned by: `services/ai-orchestrator/tests/test_reservation_assistant.py` (`test_response_propagates_tool_error_in_summary`); `services/ai-orchestrator/tests/test_tools.py` (`test_dispatch_records_duration_and_args_summary`)
- **AI-TURN-3.** `pending_apply` is built from the most recent `scheduled_apply` side effect
  of the turn and is null when there is none; a created config version never sets it. \
  Enforced in: `services/ai-orchestrator/app/routes/reservation_assistant.py` (`_pending_apply_from_side_effects`) \
  Pinned by: `services/ai-orchestrator/tests/test_reservation_assistant_coverage.py` (`test_persist_turn_surfaces_pending_apply_from_side_effect`, `test_persist_turn_no_side_effect_returns_none`, `test_buffered_route_surfaces_pending_apply`); `services/ai-orchestrator/tests/test_reservation_assistant.py` (`test_buffered_timeout_after_propose_persists_turn_without_pending_apply`)
- **AI-TURN-4.** `ASSISTANT_OVERALL_DEADLINE_S` bounds the model-and-tool loop only;
  saving a finished turn runs outside it, at most once. \
  Enforced in: `services/ai-orchestrator/app/routes/reservation_assistant.py` (`reservation_assistant`, `reservation_assistant_stream`) \
  Pinned by: `services/ai-orchestrator/tests/test_reservation_assistant.py` (`test_buffered_deadline_during_persist_does_not_interrupt_or_duplicate`, `test_buffered_deadline_during_persist_with_side_effect_persists_once`, `test_stream_deadline_during_persist_emits_done_not_error`)
- **AI-TURN-5.** A buffered turn that fails before any write side effect is rolled back and
  answers 504 `Assistant did not respond within <N>s` on the deadline, 503
  `AI provider is unreachable`, or 502 `Assistant call failed` for any other `AIError`;
  provider text never reaches the client. \
  Enforced in: `services/ai-orchestrator/app/routes/reservation_assistant.py` (`reservation_assistant`) \
  Pinned by: `services/ai-orchestrator/tests/test_reservation_assistant.py` (`test_overall_timeout_returns_504`, `test_buffered_provider_unreachable_returns_503`, `test_ai_error_returns_502`, `test_buffered_incomplete_turn_with_no_side_effect_still_rolls_back`); `services/ai-orchestrator/tests/test_reservation_assistant_coverage.py` (`test_buffered_route_ai_error_maps_to_502_with_generic_detail`)
- **AI-TURN-6.** The same failures after a write tool recorded a side effect keep the turn:
  the finished iterations plus a closing assistant message
  (`INCOMPLETE_AFTER_TOOLS_ANSWER`) are committed, and the route answers 200 with
  `stop_reason: "incomplete"`, `incomplete` set to `timeout`, `provider_unavailable`, or
  `ai_error`, and the real `tool_calls` and `pending_apply`. \
  Enforced in: `services/ai-orchestrator/app/routes/reservation_assistant.py` (`_finalize_incomplete_turn`, `INCOMPLETE_REASON_TIMEOUT`, `INCOMPLETE_REASON_PROVIDER_UNAVAILABLE`, `INCOMPLETE_REASON_AI_ERROR`) \
  Pinned by: `services/ai-orchestrator/tests/test_reservation_assistant.py` (`test_buffered_incomplete_turn_with_side_effect_persists_and_returns_200`, `test_buffered_timeout_after_propose_persists_turn_without_pending_apply`, `test_buffered_incomplete_turn_next_turn_replays_without_error`, `test_incomplete_log_carries_reason_as_its_own_key`)
- **AI-TURN-7.** The closing message also names, under
  `Actions that landed before the failure:`, each side effect whose tool call never
  reached a stored iteration, matching by tool name and ignoring calls whose result was an
  error. \
  Enforced in: `services/ai-orchestrator/app/routes/reservation_assistant.py` (`_closing_incomplete_text`, `INCOMPLETE_LANDED_ACTIONS_HEADER`) \
  Pinned by: `services/ai-orchestrator/tests/test_reservation_assistant.py` (`test_buffered_mid_dispatch_gap_names_landed_tool_in_closing_message`, `test_closing_text_names_landed_call_when_earlier_same_tool_call_errored`, `test_closing_text_omits_call_recorded_by_successful_tool_use`, `test_closing_text_uses_kind_fallback_name_for_config_version_created`)
- **AI-TURN-8.** A finished turn's tokens are recorded after the commit. \
  Enforced in: `services/ai-orchestrator/app/routes/reservation_assistant.py` (`_persist_turn`) \
  Pinned by: `services/ai-orchestrator/tests/test_reservation_assistant.py` (`test_assistant_records_usage_when_quota_enabled`); `services/ai-orchestrator/tests/test_reservation_assistant_coverage.py` (`test_buffered_route_records_usage_against_quota`)
- **AI-STREAM-1.** The streaming route does the token, provider, quota, conversation, and
  seed checks before the stream opens, so their failures are ordinary HTTP statuses. \
  Enforced in: `services/ai-orchestrator/app/routes/reservation_assistant.py` (`reservation_assistant_stream`) \
  Pinned by: `services/ai-orchestrator/tests/test_reservation_assistant.py` (`test_stream_requires_auth`, `test_stream_503_when_api_key_blank`, `test_stream_over_quota_returns_429_without_calling_ai`, `test_stream_unknown_conversation_id_returns_404`)
- **AI-STREAM-2.** Frames are `event: status` with `{message, tools, interim}` (`analyzing`
  or `running tools`), `event: token` with `{text}`, `event: done` with the same body as
  the buffered route, and `event: error` with `{message}`; the response sends
  `Cache-Control: no-cache` and `X-Accel-Buffering: no`. \
  Enforced in: `services/ai-orchestrator/app/routes/reservation_assistant.py` (`reservation_assistant_stream`, `_sse`) \
  Pinned by: `services/ai-orchestrator/tests/test_reservation_assistant.py` (`test_stream_emits_status_tokens_then_done`, `test_stream_status_frame_carries_interim_flag`); `services/ai-orchestrator/tests/test_reservation_assistant_coverage.py` (`test_sse_frames_event_and_json_data`, `test_stream_route_emits_status_token_done`)
- **AI-STREAM-3.** Text streams as it arrives; when a reply that streamed text turns out to
  call tools, the next `status` frame has `interim: true`, telling the client to discard
  that text. \
  Enforced in: `services/ai-orchestrator/app/services/ai_client.py` (`answer_reservation_question_streaming`) \
  Pinned by: `services/ai-orchestrator/tests/test_ai_client.py` (`test_streaming_tool_turn_marks_interim_then_final_answer`)
- **AI-STREAM-4.** Every streamed turn ends in exactly one `done` or `error` frame. Without
  a side effect: the deadline gives `error` `Assistant did not respond within <N>s`, an
  unreachable provider `AI provider is unreachable`, another `AIError`
  `Assistant call failed`, and a stream that ends without a result
  `Assistant produced no answer`, each after a rollback; with a side effect the turn ends
  in `done` carrying `incomplete` (AI-TURN-6). \
  Enforced in: `services/ai-orchestrator/app/routes/reservation_assistant.py` (`reservation_assistant_stream`) \
  Pinned by: `services/ai-orchestrator/tests/test_reservation_assistant.py` (`test_stream_emits_error_event_on_ai_failure`, `test_stream_provider_unreachable_emits_error_event`, `test_stream_overall_timeout_emits_error_and_leaves_no_orphan`, `test_stream_incomplete_turn_with_side_effect_returns_done_event`); `services/ai-orchestrator/tests/test_reservation_assistant_coverage.py` (`test_stream_route_no_done_event_emits_error_no_answer`); `services/ai-orchestrator/tests/test_reservation_assistant_stream_deadline.py` (`test_stalled_client_with_no_tool_gets_one_timeout_error`, `test_stalled_client_with_landed_tool_still_gets_one_done`)
- **AI-STREAM-5.** The deadline is one absolute instant that bounds only the wait for the
  next event; no frame is yielded inside a timeout scope, time spent handing a frame to a
  slow client counts against the deadline, and the inner generator is closed on every
  exit. \
  Enforced in: `services/ai-orchestrator/app/routes/reservation_assistant.py` (`reservation_assistant_stream`, `_STREAM_END`) \
  Pinned by: `services/ai-orchestrator/tests/test_reservation_assistant_stream_deadline.py` (`test_stalled_client_with_landed_tool_still_gets_one_done`, `test_consumer_leaving_at_a_yield_closes_inner_generator_without_leaks`); `tests/unit/test_no_yield_inside_cancel_scope.py` (`test_no_service_app_yields_inside_a_cancel_scope`)
- **AI-STREAM-6.** Any other exception, raised by the loop, by saving a finished turn (a
  database error, for example), or by one of the handlers above, still ends the stream in
  exactly one frame: `error` `Assistant call failed` after a rollback, with the exception
  logged and never sent. An exception from the loop after a write landed keeps the turn and
  ends in `done` with `incomplete: "ai_error"` (AI-TURN-6). \
  Enforced in: `services/ai-orchestrator/app/routes/reservation_assistant.py` (`reservation_assistant_stream`, `ASSISTANT_CALL_FAILED_DETAIL`) \
  Pinned by: `services/ai-orchestrator/tests/test_reservation_assistant_stream_terminal.py` (`test_unexpected_exception_mid_stream_ends_in_one_error_frame`, `test_unexpected_exception_rolls_back_and_next_turn_succeeds`, `test_unexpected_exception_after_a_landed_write_keeps_the_turn`, `test_persist_failure_after_a_finished_answer_ends_in_one_error_frame`, `test_failure_inside_a_handler_still_ends_in_one_error_frame`)
- **AI-STREAM-7.** The turn is saved before its `done` frame is sent, so a streamed
  `conversation_id` can continue on either route. \
  Enforced in: `services/ai-orchestrator/app/routes/reservation_assistant.py` (`reservation_assistant_stream`, `_persist_turn`) \
  Pinned by: `services/ai-orchestrator/tests/test_reservation_assistant.py` (`test_stream_persists_conversation`)
- **AI-TOOL-1.** Seven read tools are always offered: `get_device`, `get_device_ports`,
  `get_device_current_config`, `list_device_config_history`, `find_path`,
  `get_device_config_schema`, and `list_executions_for_reservation`. \
  Enforced in: `services/ai-orchestrator/app/services/tools.py` (`TOOL_DEFINITIONS`, `get_active_tool_definitions`) \
  Pinned by: `services/ai-orchestrator/tests/test_write_tools.py` (`test_baseline_tool_count_unchanged`); `services/ai-orchestrator/tests/test_tools.py` (`test_tool_definitions_have_required_fields`, `test_get_device_config_schema_in_active_tool_set_unconditionally`)
- **AI-TOOL-2.** Every tool call runs with the caller's JWT (15 seconds per HTTP call), so
  the owning service's visibility and permission checks apply; the dispatcher never
  raises: an unknown tool, a refusal, an HTTP error, or any other exception becomes an
  `is_error` result the model can read. \
  Enforced in: `services/ai-orchestrator/app/services/tools.py` (`dispatch`, `_auth_headers`) \
  Pinned by: `services/ai-orchestrator/tests/test_tools.py` (`test_dispatcher_forwards_bearer_token`, `test_dispatch_unknown_tool_returns_is_error`, `test_dispatcher_handles_httpx_failure`)
- **AI-TOOL-11.** A device-id argument (`device_id`, or `source_device_id` and
  `target_device_id` on `find_path`) must name one of the reservation's own devices, for
  read and write tools alike. The check runs at the dispatch boundary before the handler,
  so a device outside the reservation is refused with the tool error
  `<argument> is not a device of this reservation` and no call reaches inventory, cabling,
  or execution. The device list is reservations `GET /{id}` read with the caller's JWT
  once per turn, on the first call that carries a device argument; it fails closed: a
  non-200 answer, a transport error, or a body without a list of UUIDs under `device_ids`
  refuses every device-scoped call with `the reservation's device list could not be read`,
  and a failed read is retried on the next call rather than cached. A malformed id is
  refused by AI-TOOL-10 before the read; a call with no device argument (for example
  `list_executions_for_reservation` with no filter) reads nothing (issue #1054). \
  Enforced in: `services/ai-orchestrator/app/services/tools.py` (`dispatch`, `_enforce_device_scope`, `_fetch_reservation_devices`, `DEVICE_ID_ARGUMENTS`) \
  Pinned by: `services/ai-orchestrator/tests/test_tools_device_scope.py` (`test_every_device_id_property_is_scoped`, `test_a_device_outside_the_reservation_is_refused_before_any_downstream_call`, `test_a_device_of_the_reservation_reaches_the_handler`, `test_find_path_refuses_when_only_the_target_is_outside`, `test_the_device_list_is_read_with_the_callers_jwt_once_per_turn`, `test_an_unreadable_device_list_fails_closed`, `test_a_failed_device_list_read_is_retried_on_the_next_call`, `test_list_executions_without_a_device_filter_reads_no_device_list`, `test_a_refusal_is_logged_by_tool_and_argument_only`)
- **AI-TOOL-3.** A tool result is serialized to JSON and cut at
  `ASSISTANT_TOOL_RESULT_CHAR_CAP` characters with a `... [truncated: N chars omitted]`
  marker. \
  Enforced in: `services/ai-orchestrator/app/services/tools.py` (`dispatch`) \
  Pinned by: `services/ai-orchestrator/tests/test_tools.py` (`test_tool_result_truncation`)
- **AI-TOOL-4.** `get_device` and `get_device_ports` drop `field_data` keys the template
  marks as password fields; when the template cannot be read, `field_data` is emptied
  entirely. \
  Enforced in: `services/ai-orchestrator/app/services/tools.py` (`_strip_password_fields`, `_password_keys_for_template`) \
  Pinned by: `services/ai-orchestrator/tests/test_tools.py` (`test_get_device_strips_password_field_data`, `test_get_device_closed_by_default_when_template_unfetchable`, `test_get_device_ports_strips_passwords_using_port_template_cache`)
- **AI-TOOL-5.** `get_device_current_config` returns the newest config version with its
  payload as inventory stores it, or `{has_config: false}` when there is none. \
  Enforced in: `services/ai-orchestrator/app/services/tools.py` (`_tool_get_device_current_config`) \
  Pinned by: `services/ai-orchestrator/tests/test_tools.py` (`test_get_device_current_config_two_hop_returns_payload`, `test_get_device_current_config_no_versions_returns_has_config_false`, `test_get_device_current_config_missing_version_id_returns_tool_error`)
- **AI-TOOL-6.** `list_device_config_history` takes `limit` 1 to 50, default 10. \
  Enforced in: `services/ai-orchestrator/app/services/tools.py` (`_tool_list_device_config_history`) \
  Pinned by: `services/ai-orchestrator/tests/test_tools.py` (`test_list_device_config_history_passes_limit`, `test_list_device_config_history_rejects_bad_limit`)
- **AI-TOOL-7.** `get_device_config_schema` answers the driver's published schema
  (`source: driver`) through inventory's config-schema proxy when there is one, else the
  connection type's registry schema (`registry`), else `schema: null` (`none`); any proxy
  failure falls back to the registry, and answers are cached per turn. \
  Enforced in: `services/ai-orchestrator/app/services/tools.py` (`_tool_get_device_config_schema`, `_fetch_published_schema_for_driver`) \
  Pinned by: `services/ai-orchestrator/tests/test_tools.py` (`test_get_device_config_schema_returns_driver_published_schema`, `test_get_device_config_schema_falls_back_to_registry_when_source_not_driver`, `test_get_device_config_schema_falls_back_to_registry_when_proxy_errors`, `test_get_device_config_schema_null_schema_for_unregistered_connection_type`, `test_get_device_config_schema_caches_per_dispatcher`)
- **AI-TOOL-8.** `list_executions_for_reservation` always queries execution `GET /runs`
  for the route's reservation id, which the model cannot supply; it accepts `device_id`,
  `status`, and `limit` 1 to 100 (default 20), and maps a 403 to a plain tool error. \
  Enforced in: `services/ai-orchestrator/app/services/tools.py` (`_tool_list_executions_for_reservation`) \
  Pinned by: `services/ai-orchestrator/tests/test_tools.py` (`test_list_executions_schema_has_no_reservation_id_property`, `test_list_executions_injects_reservation_id_ignoring_model_input`, `test_list_executions_forwards_device_id_and_status_filters`, `test_list_executions_translates_403_to_friendly_tool_error`)
- **AI-TOOL-9.** `find_path` calls cabling `POST /pathfind` and maps a 404 to a tool error. \
  Enforced in: `services/ai-orchestrator/app/services/tools.py` (`_tool_find_path`) \
  Pinned by: `services/ai-orchestrator/tests/test_tools.py` (`test_find_path_posts_both_uuids`)
- **AI-TOOL-10.** A device or version argument that is missing or not a UUID is refused as
  a tool error before any HTTP call. \
  Enforced in: `services/ai-orchestrator/app/services/tools.py` (`_require_uuid`) \
  Pinned by: `services/ai-orchestrator/tests/test_tools.py` (`test_get_device_rejects_non_uuid_arg`)
- **AI-WRITE-1.** `propose_config_change` and `schedule_config_apply` are offered only when
  `AI_WRITE_TOOLS_ENABLED` is set, and a call to either while it is off is refused at
  dispatch (`write tools are disabled`) even if the model names it; read tools are
  unaffected. \
  Enforced in: `services/ai-orchestrator/app/services/tools.py` (`get_active_tool_definitions`, `dispatch`, `WRITE_TOOL_NAMES`) \
  Pinned by: `services/ai-orchestrator/tests/test_write_tools.py` (`test_get_active_tool_definitions_flag_off_omits_write_tools`, `test_get_active_tool_definitions_flag_on_includes_write_tools`, `test_propose_config_change_blocked_when_flag_off`, `test_schedule_config_apply_blocked_when_flag_off`, `test_read_only_tool_dispatches_when_flag_off`)
- **AI-WRITE-2.** `propose_config_change` refuses a payload containing any of the
  template's password field keys at any depth, and refuses outright when the template
  cannot be read; otherwise it creates a config version through inventory with the
  description cut to 500 characters. A 422 comes back as a soft
  `{validation: errors, detail}` result and a 403 as a tool error; success records a
  `config_version_created` side effect. \
  Enforced in: `services/ai-orchestrator/app/services/tools.py` (`_tool_propose_config_change`, `_flatten_password_keys_present`) \
  Pinned by: `services/ai-orchestrator/tests/test_write_tools.py` (`test_propose_config_change_success`, `test_propose_config_change_password_field_rejected`, `test_propose_config_change_403_becomes_tool_error`, `test_propose_config_change_422_returns_soft_validation_errors`, `test_propose_config_change_success_records_one_config_version_side_effect`, `test_propose_config_change_failure_records_no_side_effect`, `test_flatten_password_keys_finds_nested`)
- **AI-WRITE-3.** `schedule_config_apply` always schedules a dry run, whatever the
  arguments say, `delay_seconds` (5 to 600, default 30) from now and tagged with the
  route's reservation id; a 403, 409, or 422 becomes a tool error carrying the detail (a
  structured detail's `message`), and only success records a `scheduled_apply` side
  effect. Confirming a dry run into a real apply is a separate non-AI route
  (`device-configuration.md`). \
  Enforced in: `services/ai-orchestrator/app/services/tools.py` (`_tool_schedule_config_apply`) \
  Pinned by: `services/ai-orchestrator/tests/test_write_tools.py` (`test_schedule_config_apply_defaults_dry_run_true`, `test_schedule_config_apply_ignores_attacker_supplied_dry_run_false`, `test_schedule_config_apply_delay_seconds_clamped_to_range`, `test_schedule_config_apply_records_side_effect`, `test_schedule_config_apply_422_from_inventory_bubbles_up`, `test_schedule_config_apply_no_side_effect_on_failure`); `services/ai-orchestrator/tests/test_tools_error_branches.py` (`test_schedule_config_apply_409_structured_detail_surfaces_message`)

**Out of scope.** Who may create a config version or schedule an apply: inventory's rule,
which admits a device `manage` grant or an active reservation of the device through
`user_has_manage_or_owns_active_reservation` in `services/common/herd_common/acl.py`
(`identity-and-access.md`, `device-configuration.md`). Who may list execution runs
(`device-configuration.md`, CFG-RUN-1).

### 8.6 Assistant documentation lookup

**What it does.** The assistant can search and read the HERD manual, any reference
directories an operator mounts, and, when an operator allows it, a fixed list of https
documentation sites, instead of answering such questions from memory.

**Surfaces.** Tools `search_docs` and `read_doc` in
`services/ai-orchestrator/app/services/tools.py`; sources and indexing in
`services/ai-orchestrator/app/services/docs_sources.py`; the web fetch in
`services/ai-orchestrator/app/services/docs_web.py`.

**Rules.**

- **AI-DOCS-1.** The sources are `herd-manual` (`/app/docs/manual`, when
  `AI_DOCS_MANUAL_ENABLED` is set and the directory exists) plus each
  `AI_DOCS_CORPUS_DIRS` entry `name=/absolute/path` whose name is lowercase letters,
  digits, `_` or `-` (at most 64 characters), is not `web`, is not a duplicate, and whose
  directory is readable; a bad entry is logged once and skipped, never fatal. \
  Enforced in: `services/ai-orchestrator/app/services/docs_sources.py` (`build_source_registry`, `_SOURCE_NAME_RE`, `_warn_once`) \
  Pinned by: `services/ai-orchestrator/tests/test_docs_sources.py` (`test_registry_carries_the_manual_when_its_root_exists`, `test_registry_drops_the_manual_when_disabled`, `test_registry_parses_operator_corpora`, `test_registry_skips_a_missing_directory_and_logs_it_once`, `test_registry_skips_malformed_and_relative_entries`, `test_registry_refuses_an_entry_named_web`)
- **AI-DOCS-2.** The two tools are offered only when a corpus source is enabled, or web is
  enabled with at least one valid prefix; otherwise a call to either is refused at
  dispatch (`documentation tools are disabled`). \
  Enforced in: `services/ai-orchestrator/app/services/tools.py` (`docs_tools_enabled`, `dispatch`) \
  Pinned by: `services/ai-orchestrator/tests/test_docs_tools.py` (`test_docs_tools_are_absent_when_no_source_is_enabled`, `test_docs_tools_are_advertised_with_the_manual_alone`, `test_docs_tools_are_advertised_for_web_only`, `test_web_enabled_with_no_prefixes_is_not_a_source`, `test_dispatch_refuses_docs_tools_when_every_source_is_disabled`)
- **AI-DOCS-3.** `search_docs` needs a non-empty query, refuses the `web` source and an
  unknown source, and returns at most 10 hits `{source, path, title, snippet, score}`
  ordered by score, then source name, then path. \
  Enforced in: `services/ai-orchestrator/app/services/tools.py` (`_tool_search_docs`); `services/ai-orchestrator/app/services/docs_sources.py` (`search`, `MAX_SEARCH_RESULTS`) \
  Pinned by: `services/ai-orchestrator/tests/test_docs_tools.py` (`test_search_docs_requires_a_query`, `test_search_docs_refuses_the_web_source`, `test_dispatch_refuses_an_unknown_source`); `services/ai-orchestrator/tests/test_docs_sources.py` (`test_search_caps_results_at_ten`, `test_search_is_deterministic_and_breaks_ties_by_path`, `test_search_can_be_restricted_to_one_source`)
- **AI-DOCS-4.** The score is the summed term frequency of the query's tokens (stop words
  dropped from the query only), with a title hit counting three times, scaled by the
  share of query tokens the page covers and divided by the square root of the page's
  token count; a page scoring 0 is not returned. \
  Enforced in: `services/ai-orchestrator/app/services/docs_sources.py` (`score_document`, `query_tokens`) \
  Pinned by: `services/ai-orchestrator/tests/test_docs_sources.py` (`test_search_ranks_the_more_relevant_document_first`, `test_search_returns_nothing_for_a_query_with_no_overlap`)
- **AI-DOCS-5.** Only `.md`, `.txt`, and `.html` files with no hidden path component and at
  most 2 MiB are indexed; an index is rebuilt on the first lookup after
  `AI_DOCS_INDEX_TTL_SECONDS`. The index applies the read rule of AI-DOCS-6: an entry
  whose resolved path leaves the source root (a symlink pointing out of the corpus) is
  never indexed, so search never returns its title or a snippet (issue #1055). \
  Enforced in: `services/ai-orchestrator/app/services/docs_sources.py` (`_is_indexable`, `_read_file`, `get_index`, `_build_index`, `resolve_in_root`) \
  Pinned by: `services/ai-orchestrator/tests/test_docs_sources.py` (`test_search_ignores_non_text_and_hidden_files`, `test_index_is_rebuilt_after_the_ttl_expires`, `test_search_skips_a_symlink_that_leaves_the_root`, `test_search_keeps_a_symlink_that_stays_inside_the_root`)
- **AI-DOCS-6.** A corpus `read_doc` path is resolved under the source root with symlinks
  followed; an absolute path, a traversal, a symlink leaving the root, a hidden or
  non-text file, and a missing file all answer the same `document not found` refusal. \
  Enforced in: `services/ai-orchestrator/app/services/docs_sources.py` (`resolve_in_root`, `read_document`) \
  Pinned by: `services/ai-orchestrator/tests/test_docs_sources.py` (`test_read_document_refuses_a_traversal_path`, `test_read_document_refuses_an_absolute_path`, `test_read_document_refuses_a_symlink_escape`, `test_read_document_refuses_a_non_text_extension`, `test_read_document_follows_a_symlink_that_stays_inside_the_root`); `services/ai-orchestrator/tests/test_docs_tools.py` (`test_read_doc_refuses_a_traversal_path`)
- **AI-DOCS-7.** `read_doc` returns one window of max(512, 0.7 times the result cap minus
  200) characters from a non-negative `offset`, with `next_offset` null at the end. \
  Enforced in: `services/ai-orchestrator/app/services/tools.py` (`_doc_window`, `_tool_read_doc`); `services/ai-orchestrator/app/services/docs_sources.py` (`read_document`) \
  Pinned by: `services/ai-orchestrator/tests/test_docs_tools.py` (`test_read_doc_window_is_derived_from_the_result_cap`, `test_read_doc_pages_through_a_long_page`, `test_read_doc_refuses_a_negative_offset`); `services/ai-orchestrator/tests/test_docs_sources.py` (`test_read_document_pages_until_next_offset_is_null`, `test_read_document_past_the_end_returns_empty_text`)
- **AI-DOCS-8.** HTML becomes plain text with `script`, `style`, `noscript`, `template`, and
  `svg` content dropped and block elements on their own lines; the title is the `title`
  element, else the first `h1`, else the file name (markdown: the first line). \
  Enforced in: `services/ai-orchestrator/app/services/docs_sources.py` (`html_to_text`, `_TextExtractor`, `document_text_and_title`) \
  Pinned by: `services/ai-orchestrator/tests/test_docs_sources.py` (`test_html_to_text_drops_script_and_style_and_separates_blocks`, `test_html_title_falls_back_to_the_first_heading`, `test_markdown_title_comes_from_the_first_heading`, `test_html_to_text_on_a_real_manual_page`)
- **AI-DOCS-9.** `read_doc` with source `web` is refused at dispatch while
  `AI_DOCS_WEB_ENABLED` is off, before any fetch code runs. \
  Enforced in: `services/ai-orchestrator/app/services/tools.py` (`_tool_read_doc`) \
  Pinned by: `services/ai-orchestrator/tests/test_docs_tools.py` (`test_dispatch_refuses_a_web_read_while_web_is_disabled`, `test_the_dispatch_gate_refuses_web_before_the_fetch_helper_runs`)
- **AI-DOCS-10.** A web URL must be https with no user information; it is normalized
  (lowercase host, default port dropped, query and fragment dropped, and `.` and `..`
  path segments resolved, literal or percent-encoded, issue #1055) and must start with a
  normalized allowed prefix, a bare-host prefix getting a trailing slash. The normalized
  URL is the one requested, so the path that matched is the path fetched. \
  Enforced in: `services/ai-orchestrator/app/services/docs_web.py` (`normalize_url`, `remove_dot_segments`, `normalized_prefixes`, `match_prefix`) \
  Pinned by: `services/ai-orchestrator/tests/test_docs_web.py` (`test_normalize_url_canonicalizes`, `test_normalize_url_refuses_non_https_and_userinfo`, `test_prefix_match_refuses_lookalikes`, `test_a_bare_host_prefix_gets_a_trailing_slash`, `test_fetch_refuses_a_url_outside_the_allowlist`, `test_fetch_refuses_with_an_empty_allowlist`, `test_fetch_refuses_a_dot_segment_path_that_leaves_the_prefix`, `test_fetch_requests_the_normalized_path_that_was_matched`)
- **AI-DOCS-11.** Every address the host resolves to must be public: loopback, private,
  shared address space (100.64.0.0/10, issue #1055), link-local, multicast, unspecified, and reserved addresses, their IPv4-mapped forms, and
  6to4 and Teredo addresses are refused, as is a host that does not resolve. \
  Enforced in: `services/ai-orchestrator/app/services/docs_web.py` (`assert_public_host`, `is_public_address`) \
  Pinned by: `services/ai-orchestrator/tests/test_docs_web.py` (`test_refused_address_classes`, `test_public_addresses_are_allowed`, `test_fetch_refuses_when_any_resolved_address_is_private`, `test_fetch_refuses_when_the_host_does_not_resolve`)
- **AI-DOCS-12.** Redirects are followed by hand, at most 3, each target normalized as in
  AI-DOCS-10, re-matched against the prefixes, and re-resolved. \
  Enforced in: `services/ai-orchestrator/app/services/docs_web.py` (`fetch_web_document`, `MAX_REDIRECTS`) \
  Pinned by: `services/ai-orchestrator/tests/test_docs_web.py` (`test_fetch_follows_an_allowlisted_redirect`, `test_redirect_to_a_private_address_is_refused_mid_chain`, `test_redirect_outside_the_allowlist_is_refused`, `test_redirect_chain_is_bounded`, `test_redirect_without_a_location_is_refused`, `test_redirect_with_dot_segments_out_of_the_prefix_is_refused`)
- **AI-DOCS-13.** The response must be `text/html`, `text/plain`, `text/markdown`, or
  `text/x-markdown` with a status below 400, and is read to at most
  `AI_DOCS_WEB_MAX_BYTES` before being cut. \
  Enforced in: `services/ai-orchestrator/app/services/docs_web.py` (`fetch_web_document`, `_content_type_allowed`, `_read_capped`) \
  Pinned by: `services/ai-orchestrator/tests/test_docs_web.py` (`test_fetch_refuses_a_non_text_content_type`, `test_fetch_reports_an_http_error_status`, `test_fetch_cuts_the_body_at_the_byte_cap`)
- **AI-DOCS-14.** A web request carries only a fixed User-Agent and an Accept header: no
  JWT and no internal token. \
  Enforced in: `services/ai-orchestrator/app/services/docs_web.py` (`fetch_web_document`, `USER_AGENT`) \
  Pinned by: `services/ai-orchestrator/tests/test_docs_web.py` (`test_fetch_returns_converted_text`)

**Out of scope.** Embeddings or a vector store (ADR 0015 chose plain text ranking). Closing
the DNS rebinding window (section 13).

### 8.7 AI-assisted recipe authoring

**What it does.** An administrator describes a hypervisor recipe in plain words; the AI
drafts the driver package, HERD validates it in the execution sandbox and lets the AI fix
what failed, and the administrator reviews the result and, only if it passed, uploads it as
a driver.

**Surfaces.** User interface `frontend/src/components/admin/RecipeDraftPanel.tsx` from
`frontend/src/pages/admin/DriversPage.tsx`; routes `POST /recipes/draft`,
`POST /recipes/draft/{id}/refine`, `GET /recipes/draft/{id}`; the loop in
`services/ai-orchestrator/app/services/recipe_author.py`.

**Rules.**

- **AI-RECIPE-1.** With `AI_RECIPE_AUTHORING_ENABLED` off (the default) all three routes
  answer 403 `AI recipe authoring is disabled`, to an admin as well. \
  Enforced in: `services/ai-orchestrator/app/routes/recipes.py` (`require_recipe_authoring`, `RECIPE_AUTHORING_DISABLED_DETAIL`) \
  Pinned by: `services/ai-orchestrator/tests/test_recipes_routes.py` (`test_flag_off_403_pinned_even_for_configured_admin`); `tests/integration/test_recipe_authoring_gate.py` (`test_recipe_draft_refused_when_flag_off`)
- **AI-RECIPE-2.** With the flag on, only an admin or superadmin may call them; a user gets
  403 `Admin or superadmin role required` and no token gets 401. \
  Enforced in: `services/ai-orchestrator/app/routes/recipes.py` (`create_draft`, `refine_draft`, `get_draft`) \
  Pinned by: `services/ai-orchestrator/tests/test_recipes_routes.py` (`test_non_admin_403`)
- **AI-RECIPE-3.** The flag is checked before authentication, so with it off a caller with
  no token also gets the 403 disabled detail. \
  Enforced in: `services/ai-orchestrator/app/routes/recipes.py` (`create_draft`, `refine_draft`, `get_draft`) \
  Pinned by: `services/ai-orchestrator/tests/test_recipes_routes.py` (`test_flag_is_checked_before_authentication`)
- **AI-RECIPE-4.** Draft and refine check the provider (503) and then the quota (429)
  before any model call; reading a draft checks neither. \
  Enforced in: `services/ai-orchestrator/app/routes/recipes.py` (`_run_authoring`) \
  Pinned by: `services/ai-orchestrator/tests/test_recipes_routes.py` (`test_unconfigured_503_pinned`, `test_usage_recorded_and_quota_enforced`)
- **AI-RECIPE-5.** The model is forced to call `draft_recipe` and supplies only
  `driver_py`, a metadata subset (`name`, `version`, `notes`), and an explanation. \
  Enforced in: `services/ai-orchestrator/app/services/ai_client.py` (`draft_recipe`); `services/ai-orchestrator/app/services/recipe_author.py` (`DRAFT_RECIPE_TOOL`) \
  Pinned by: `services/ai-orchestrator/tests/test_ai_client.py` (`test_draft_recipe_forces_tool_choice`, `test_draft_recipe_returns_parsed_tool_input`, `test_draft_recipe_skips_tool_use_block_with_wrong_name`)
- **AI-RECIPE-6.** The service always sets `connection_type: Hypervisor`,
  `supports_dry_run: true`, `generated_by` (the configured model), `draft_id`, and
  `generated_at` in the metadata, overwriting anything the model sent; a missing name or
  version becomes `generated-recipe` or `0.1.0`. \
  Enforced in: `services/ai-orchestrator/app/services/recipe_author.py` (`build_metadata`) \
  Pinned by: `services/ai-orchestrator/tests/test_recipe_author.py` (`test_build_metadata_injects_owned_fields`, `test_build_metadata_overrides_model_supplied_contract_fields`, `test_build_metadata_defaults_when_model_omits`)
- **AI-RECIPE-7.** The package is a zip of `driver.py` and `driver_metadata.json`, built on
  demand from the stored files and returned base64-encoded; no archive is stored. \
  Enforced in: `services/ai-orchestrator/app/services/recipe_author.py` (`assemble_package_b64`); `services/ai-orchestrator/app/routes/recipes.py` (`_to_response`) \
  Pinned by: `services/ai-orchestrator/tests/test_recipe_author.py` (`test_assemble_package_round_trip`)
- **AI-RECIPE-8.** Each attempt is validated by execution
  `POST /internal/validate-package` with the internal token and a 120 second timeout; a
  transport error or any non-200 answers 503 `Recipe validator is unreachable` and stores
  no draft. The validator's checks are `device-configuration.md`. \
  Enforced in: `services/ai-orchestrator/app/services/recipe_author.py` (`validate_with_execution`, `RECIPE_VALIDATOR_UNREACHABLE_DETAIL`) \
  Pinned by: `services/ai-orchestrator/tests/test_recipe_author.py` (`test_validate_posts_with_internal_token`, `test_validate_unreachable_raises_503_pinned`, `test_validate_non_200_raises_503_pinned`); `services/ai-orchestrator/tests/test_recipes_routes.py` (`test_validator_unreachable_maps_to_503`)
- **AI-RECIPE-9.** A failing report is flattened (structural and policy errors, failed
  dry-run methods) into the next attempt's correction, up to `AI_RECIPE_MAX_ATTEMPTS`
  attempts in all; the last draft is stored with `valid` true or false either way. \
  Enforced in: `services/ai-orchestrator/app/services/recipe_author.py` (`author_recipe`, `_report_feedback`) \
  Pinned by: `services/ai-orchestrator/tests/test_recipe_author.py` (`test_author_valid_first_attempt_persists`, `test_author_repair_loop_feeds_report_back`, `test_author_exhausts_attempts_and_persists_red_draft`, `test_report_feedback_flattens_all_sections`)
- **AI-RECIPE-10.** Refine updates the stored draft in place, seeding the model with the
  stored files and the admin's feedback and adding to the attempt count; an unknown draft
  id answers 404 `Recipe draft not found`. \
  Enforced in: `services/ai-orchestrator/app/services/recipe_author.py` (`author_recipe`); `services/ai-orchestrator/app/routes/recipes.py` (`_get_draft_or_404`) \
  Pinned by: `services/ai-orchestrator/tests/test_recipe_author.py` (`test_author_refine_updates_in_place_and_seeds_previous`); `services/ai-orchestrator/tests/test_recipes_routes.py` (`test_refine_and_get_round_trip`, `test_refine_and_get_404_for_unknown_draft`)
- **AI-RECIPE-11.** A draft can be read and refined by any admin, not only the one who
  created it. \
  Enforced in: `services/ai-orchestrator/app/routes/recipes.py` (`_get_draft_or_404`) \
  Pinned by: `services/ai-orchestrator/tests/test_recipes_routes.py` (`test_any_admin_may_read_and_refine_another_admins_draft`)
- **AI-RECIPE-12.** The answer carries the draft's id, `valid`, `attempts`, model, prompt,
  hypervisor type, explanation, `driver_py`, metadata, validation report, and
  `package_b64`; nothing in the service uploads a driver. \
  Enforced in: `services/ai-orchestrator/app/routes/recipes.py` (`DraftResponse`, `_to_response`) \
  Pinned by: `services/ai-orchestrator/tests/test_recipes_routes.py` (`test_draft_happy_path_shape_and_provenance`)
- **AI-RECIPE-13.** A model failure answers 502 `AI recipe drafting failed` and an
  unreachable provider 503; provider text never reaches the client. \
  Enforced in: `services/ai-orchestrator/app/routes/recipes.py` (`_run_authoring`, `RECIPE_DRAFT_AI_FAILED_DETAIL`) \
  Pinned by: `services/ai-orchestrator/tests/test_recipes_routes.py` (`test_draft_502_on_ai_error_never_leaks_provider_text`)
- **AI-RECIPE-14.** Every attempt's tokens are recorded together after the draft is stored;
  a run that fails records the tokens its attempts reported (AI-QUOTA-5). \
  Enforced in: `services/ai-orchestrator/app/routes/recipes.py` (`_run_authoring`) \
  Pinned by: `services/ai-orchestrator/tests/test_recipes_routes.py` (`test_usage_recorded_and_quota_enforced`, `test_failed_draft_validator_unreachable_meters_the_attempt`)

**Out of scope.** The driver upload itself (`inventory.md`) and the validator's checks
(`device-configuration.md`). Recipes for any connection type other than Hypervisor.

### 8.8 Purpose classification (the orchestrator's side)

**What it does.** HERD can suggest why a reservation exists, choosing from the
reservations service's category list: a live preview while a user fills in the create form,
and a classification after the reservation ends that an admin reviews.

**Surfaces.** Routes `POST /classify-purpose/preview` and `POST /internal/classify-purpose`;
the classifier in `services/ai-orchestrator/app/services/purpose_classifier.py`; signals in
`services/ai-orchestrator/app/services/purpose_signals.py`. Callers: the create modal's
preview (section 8.12), and the reservations sweep and Classify now route
(`reservations.md`, RES-PURPOSE-5 to RES-PURPOSE-8 and RES-PURPOSE-13).

**Rules.**

- **AI-PURPOSE-1.** With `AI_PURPOSE_CLASSIFICATION_ENABLED` off (the default) both routes
  answer 403 `{"error": "purpose_classification_disabled", "message": "Purpose classification is disabled"}`
  before any token or internal-token check. \
  Enforced in: `services/ai-orchestrator/app/routes/purpose_classification.py` (`require_purpose_classification`, `PURPOSE_CLASSIFICATION_DISABLED_DETAIL`) \
  Pinned by: `services/ai-orchestrator/tests/test_purpose_classification_routes.py` (`test_preview_403_when_flag_off`, `test_internal_403_when_flag_off`, `test_preview_403_when_flag_off_even_without_auth`, `test_flag_off_403_detail_carries_structured_marker`)
- **AI-PURPOSE-2.** With the flag on, the preview admits any signed-in user, any role; the
  internal route needs a matching `X-Internal-Token` and otherwise answers 403
  `Invalid internal token`. \
  Enforced in: `services/ai-orchestrator/app/routes/purpose_classification.py` (`classify_purpose_preview`, `_check_internal_token`) \
  Pinned by: `services/ai-orchestrator/tests/test_purpose_classification_routes.py` (`test_internal_403_wrong_token`, `test_preview_happy_path_shape_and_forced_tool_call`)
- **AI-PURPOSE-3.** Both routes check the provider (503) and then the quota (429) before
  any signal fetch; the internal route charges the quota to the body's `user_id`. \
  Enforced in: `services/ai-orchestrator/app/routes/purpose_classification.py` (`_gate_before_gather`, `classify_purpose_internal`) \
  Pinned by: `services/ai-orchestrator/tests/test_purpose_classification_routes.py` (`test_preview_over_quota_429_never_awaits_gatherer`, `test_internal_over_quota_429_never_awaits_gatherer`, `test_preview_unconfigured_503_never_awaits_gatherer`, `test_internal_unconfigured_503_never_awaits_gatherer`)
- **AI-PURPOSE-4.** `categories` holds 1 to 64 entries, `purpose` at most 2000 characters,
  `device_ids` at most 200, and `dynamic_requests` at most 50, each with `count` at least
  1; otherwise 422. \
  Enforced in: `services/ai-orchestrator/app/schemas/purpose.py` (`PreviewClassifyRequest`, `InternalClassifyRequest`, `DynamicRequestItem`) \
  Pinned by: `services/ai-orchestrator/tests/test_purpose_classification_routes.py` (`test_preview_requires_categories`, `test_preview_422_when_field_oversize`, `test_internal_422_when_field_oversize`, `test_preview_at_cap_is_accepted`)
- **AI-PURPOSE-5.** The preview's signals, read with the caller's JWT, are the purpose
  text, the devices of the chosen topology's canvas plus any listed devices (names,
  templates, vendor and model) with the canvas's wiring counts per layer, and the dynamic
  template names with summed counts (one fetch per distinct template); it never includes
  transcripts. \
  Enforced in: `services/ai-orchestrator/app/services/purpose_signals.py` (`gather_preview_signals`, `_gather_topology_block_preview`, `_gather_dynamic_templates_block`) \
  Pinned by: `services/ai-orchestrator/tests/test_purpose_signals.py` (`test_preview_signals_include_purpose_topology_and_dynamic_templates`, `test_preview_uses_explicit_device_ids_without_topology`, `test_dynamic_templates_fetch_count_equals_distinct_templates`, `test_preview_signals_empty_when_nothing_supplied`)
- **AI-PURPOSE-6.** The end pass's signals, read with the internal token, are the purpose
  text, the devices, the dynamic templates, each device's config-apply job count and job
  names (never config contents), the fork's wiring counts per layer and version count,
  the status and duration, and, when `AI_PURPOSE_INCLUDE_TRANSCRIPTS` is set, the
  reservation's assistant transcripts (kept for this read by AI-CONV-13); the body's
  `topology_id` is not used. \
  Enforced in: `services/ai-orchestrator/app/services/purpose_signals.py` (`gather_internal_signals`, `_gather_config_apply_jobs_block`, `_gather_fork_block`) \
  Pinned by: `services/ai-orchestrator/tests/test_purpose_signals.py` (`test_internal_signals_include_all_structured_signals`, `test_internal_config_apply_jobs_never_include_config_contents`, `test_transcripts_included_when_flag_on`, `test_transcripts_omitted_when_flag_off`)
- **AI-PURPOSE-7.** A signal fetch that fails with an HTTP error or a malformed body is
  logged and leaves only that signal out of the prompt and of `signals_used`; any other
  exception fails the request. \
  Enforced in: `services/ai-orchestrator/app/services/purpose_signals.py` (`_dropped`, `_TOLERATED_FETCH_ERRORS`) \
  Pinned by: `services/ai-orchestrator/tests/test_purpose_signals.py` (`test_preview_signal_fetch_failure_is_tolerated`, `test_internal_signal_fetch_failure_is_tolerated`, `test_dynamic_templates_untolerated_exception_still_propagates`)
- **AI-PURPOSE-8.** Per-device and per-template fetches run at most 8 at a time and keep
  input order. \
  Enforced in: `services/ai-orchestrator/app/services/purpose_signals.py` (`_bounded_map`, `FANOUT_CONCURRENCY`) \
  Pinned by: `services/ai-orchestrator/tests/test_purpose_signals.py` (`test_internal_device_fanout_is_bounded_and_order_preserving`)
- **AI-PURPOSE-9.** Transcripts are user and assistant text only (tool messages and tool
  blocks skipped), from the newest 200 rows across the reservation's conversations in
  conversation then position order, trimmed from the front to 12000 characters. \
  Enforced in: `services/ai-orchestrator/app/services/purpose_signals.py` (`_gather_transcripts_block`, `TRANSCRIPT_ROW_CAP`, `TRANSCRIPT_CHAR_BUDGET`) \
  Pinned by: `services/ai-orchestrator/tests/test_purpose_signals.py` (`test_transcripts_skip_tool_role_and_truncate_keeping_most_recent`, `test_bounded_transcript_read_matches_unbounded_reference`, `test_bounded_transcript_read_orders_by_conversation_not_bare_position`)
- **AI-PURPOSE-10.** The model is forced to call `classify_purpose` with the sorted,
  de-duplicated categories as an enum; unknown categories are dropped, negatives clamped
  to 0, the rest normalized to sum to 1, missing categories appended at 0, and the
  rationale cut to 500 characters. An unusable answer is retried once and then answers 502
  `Purpose classifier returned no usable distribution`. \
  Enforced in: `services/ai-orchestrator/app/services/purpose_classifier.py` (`build_classify_purpose_tool`, `normalize_distribution`, `classify_purpose`, `CLASSIFY_PURPOSE_MAX_ATTEMPTS`) \
  Pinned by: `services/ai-orchestrator/tests/test_purpose_classifier.py` (`test_tool_schema_dedupes_and_sorts_categories`, `test_normalize_drops_unknown_categories_and_normalizes`, `test_normalize_clamps_negative_probabilities_to_zero`, `test_normalize_missing_categories_get_zero_and_sort_last`, `test_normalize_caps_rationale_length`, `test_classify_purpose_retries_once_then_succeeds`, `test_classify_purpose_raises_pinned_error_after_exhausting_retries`); `services/ai-orchestrator/tests/test_purpose_classification_routes.py` (`test_preview_502_when_no_usable_distribution_after_retry`)
- **AI-PURPOSE-11.** The answer is `{distribution, top_category, pass, model, rationale, generated_at, signals_used}`
  with `pass` = `creation` for the preview and `end` for the internal route. \
  Enforced in: `services/ai-orchestrator/app/routes/purpose_classification.py` (`_run_classification`); `services/ai-orchestrator/app/schemas/purpose.py` (`PurposeClassification`) \
  Pinned by: `services/ai-orchestrator/tests/test_purpose_classification_routes.py` (`test_preview_happy_path_shape_and_forced_tool_call`, `test_internal_happy_path_pass_is_end`)
- **AI-PURPOSE-12.** A model failure answers 502 `AI classification failed` and an
  unreachable provider 503, with no provider text. \
  Enforced in: `services/ai-orchestrator/app/routes/purpose_classification.py` (`_run_classification`, `AI_CLASSIFICATION_FAILED_DETAIL`) \
  Pinned by: `services/ai-orchestrator/tests/test_purpose_classification_routes.py` (`test_preview_502_on_ai_error_never_leaks_provider_text`)
- **AI-PURPOSE-13.** A classification's tokens are recorded against the user, a failed one's
  as the tokens its attempts reported (AI-QUOTA-5). \
  Enforced in: `services/ai-orchestrator/app/routes/purpose_classification.py` (`_run_classification`); `services/ai-orchestrator/app/services/purpose_classifier.py` (`PurposeClassifierError`) \
  Pinned by: `services/ai-orchestrator/tests/test_purpose_classification_routes.py` (`test_preview_meters_usage`, `test_preview_no_usable_distribution_meters_both_attempts`)

**Out of scope.** Storing, reviewing, accepting, or dismissing a suggestion, the sweep's
schedule and attempt counting, and the Classify now route (`reservations.md`). Reporting by
purpose (`operations-and-observability.md`).

### 8.9 Template identity suggestion

**What it does.** In the template editor an administrator can ask the AI for the likely
vendor, model, and part number of a device template.

**Surfaces.** User interface `frontend/src/pages/TemplateEditorPage.tsx` (Suggest with AI);
route `POST /templates/suggest-identity`.

**Rules.**

- **AI-IDENT-1.** Only an admin or superadmin may call it; a user gets 403
  `Admin or superadmin role required` and no token gets 401. \
  Enforced in: `services/ai-orchestrator/app/routes/template_identity.py` (`suggest_identity`) \
  Pinned by: `services/ai-orchestrator/tests/test_template_identity_route.py` (`test_suggest_identity_requires_admin`); `tests/integration/test_template_identity.py` (`test_template_identity_requires_admin`)
- **AI-IDENT-2.** The body is `name` (1 to 255 characters), optional `description` (at
  most 4000), and optional `sections`; the model is forced to call `suggest_identity`, and
  the answer is `{vendor, model, part_number, confidence, reasoning}` with `part_number`
  null when omitted. \
  Enforced in: `services/ai-orchestrator/app/routes/template_identity.py` (`SuggestIdentityRequest`, `SuggestIdentityResponse`); `services/ai-orchestrator/app/services/ai_client.py` (`suggest_template_identity`) \
  Pinned by: `services/ai-orchestrator/tests/test_template_identity_route.py` (`test_suggest_identity_returns_structured_suggestion`); `services/ai-orchestrator/tests/test_ai_client.py` (`test_suggest_template_identity_forces_tool_choice`, `test_suggest_template_identity_returns_parsed_tool_input`)
- **AI-IDENT-3.** A model failure answers 502 `AI suggestion failed`. \
  Enforced in: `services/ai-orchestrator/app/routes/template_identity.py` (`suggest_identity`, `AI_SUGGESTION_FAILED_DETAIL`) \
  Pinned by: `services/ai-orchestrator/tests/test_template_identity_route.py` (`test_suggest_identity_502_when_ai_fails`)
- **AI-IDENT-4.** A suggestion that does not fit the answer's schema answers 502
  `AI returned a malformed suggestion`. \
  Enforced in: `services/ai-orchestrator/app/routes/template_identity.py` (`suggest_identity`, `AI_SUGGESTION_MALFORMED_DETAIL`) \
  Pinned by: `services/ai-orchestrator/tests/test_template_identity_route.py` (`test_suggest_identity_502_when_ai_returns_malformed`)

**Out of scope.** Saving the suggested identity, which is an ordinary template update
(`inventory.md`).

### 8.10 Daily token quota and usage reporting

**What it does.** An operator can cap how many AI tokens each user spends per UTC day;
users can see their own usage, and admins can read everyone's daily usage.

**Surfaces.** `services/ai-orchestrator/app/services/usage_repo.py`; routes `GET /quota`
and `GET /usage`.

**Rules.**

- **AI-QUOTA-1.** With `AI_DAILY_TOKEN_QUOTA` at 0 (the default) nothing is enforced and
  no usage row is written. \
  Enforced in: `services/ai-orchestrator/app/services/usage_repo.py` (`enforce_quota`, `record_usage`) \
  Pinned by: `services/ai-orchestrator/tests/test_usage_repo.py` (`test_enforce_quota_disabled_is_noop_even_over_usage`, `test_record_usage_disabled_writes_no_row`)
- **AI-QUOTA-2.** With a positive quota, a billable call (generate, both assistant routes,
  suggest-identity, recipe draft and refine, both purpose routes) is refused with 429
  `{limit, used, remaining: 0, reset_at}` before the model is called once the user's input
  plus output tokens for the UTC day reach the limit; a call below the limit is allowed
  even if it then overshoots. \
  Enforced in: `services/ai-orchestrator/app/services/usage_repo.py` (`enforce_quota`) \
  Pinned by: `services/ai-orchestrator/tests/test_usage_repo.py` (`test_enforce_quota_at_limit_rejects_with_structured_body`, `test_enforce_quota_just_under_limit_allows_boundary`); `services/ai-orchestrator/tests/test_generate.py` (`test_generate_over_quota_returns_429_without_calling_provider`); `services/ai-orchestrator/tests/test_reservation_assistant_coverage.py` (`test_buffered_route_over_quota_raises_429`)
- **AI-QUOTA-3.** Recorded usage is the provider's reported input and output tokens; when
  both are zero, a characters-divided-by-4 estimate (at least 1) of the text the route
  passes in (the request, plus the answer on every route but the purpose routes) is booked
  as output. Cache tokens are stored separately and never count
  toward the quota. \
  Enforced in: `services/ai-orchestrator/app/services/usage_repo.py` (`record_usage`, `get_today_total`) \
  Pinned by: `services/ai-orchestrator/tests/test_usage_repo.py` (`test_record_usage_books_provider_tokens`, `test_record_usage_falls_back_to_chars_over_four`, `test_record_usage_fallback_minimum_one`, `test_record_usage_asymmetric_zero_output_does_not_trigger_fallback`, `test_cache_tokens_excluded_from_quota_total`, `test_enforce_quota_ignores_cache_tokens`)
- **AI-QUOTA-4.** Usage is one row per user per UTC date, added to atomically with an
  insert-or-update. \
  Enforced in: `services/ai-orchestrator/app/services/usage_repo.py` (`add_tokens`); `services/ai-orchestrator/app/models/ai_usage.py` (`AIUsage`) \
  Pinned by: `services/ai-orchestrator/tests/test_usage_repo.py` (`test_add_tokens_inserts_then_increments`, `test_add_tokens_is_per_user`, `test_prior_day_usage_is_a_separate_total`)
- **AI-QUOTA-5.** A request that ends in an error after reaching the provider records the
  provider-reported tokens its calls spent: a generation that fails after its attempts, an
  assistant turn that is rolled back (on either route), a recipe run whose validator is
  unreachable or whose model call fails, a classification with no usable answer, and an
  identity or other single call whose answer had no usable tool call. Unlike AI-QUOTA-3
  there is no characters estimate: nothing known spent books nothing (an unreachable
  provider, for example). A metering failure is logged and never replaces the request's
  own error. \
  Enforced in: `services/ai-orchestrator/app/services/usage_repo.py` (`record_failed_usage`, `usage_of`); `services/ai-orchestrator/app/services/llm_provider.py` (`AIError`); `services/ai-orchestrator/app/routes/generate.py` (`generate`); `services/ai-orchestrator/app/routes/reservation_assistant.py` (`reservation_assistant`, `reservation_assistant_stream`); `services/ai-orchestrator/app/routes/recipes.py` (`_run_authoring`); `services/ai-orchestrator/app/routes/purpose_classification.py` (`_run_classification`); `services/ai-orchestrator/app/routes/template_identity.py` (`suggest_identity`) \
  Pinned by: `services/ai-orchestrator/tests/test_usage_repo.py` (`test_record_failed_usage_books_reported_tokens`, `test_record_failed_usage_zero_or_none_books_nothing`, `test_record_failed_usage_disabled_quota_writes_no_row`, `test_record_failed_usage_swallows_a_metering_failure`); `services/ai-orchestrator/tests/test_generate.py` (`test_generate_failed_after_repairs_meters_every_attempt`, `test_generate_ai_error_with_reported_usage_is_metered`, `test_generate_unreachable_provider_meters_nothing`); `services/ai-orchestrator/tests/test_reservation_assistant.py` (`test_buffered_rolled_back_turn_meters_tokens_spent`, `test_stream_rolled_back_turn_meters_tokens_spent`); `services/ai-orchestrator/tests/test_recipes_routes.py` (`test_failed_draft_validator_unreachable_meters_the_attempt`, `test_failed_draft_ai_error_on_a_later_attempt_meters_earlier_attempts`); `services/ai-orchestrator/tests/test_purpose_classification_routes.py` (`test_preview_no_usable_distribution_meters_both_attempts`); `services/ai-orchestrator/tests/test_template_identity_route.py` (`test_suggest_identity_ai_error_with_reported_usage_is_metered`)
- **AI-QUOTA-6.** `GET /quota` answers the caller's own `{enabled, limit, used, remaining, reset_at}`
  for any signed-in user, also when over the limit, and is not gated on the provider. \
  Enforced in: `services/ai-orchestrator/app/routes/quota.py` (`get_quota`); `services/ai-orchestrator/app/services/usage_repo.py` (`get_status`) \
  Pinned by: `services/ai-orchestrator/tests/test_quota_route.py` (`test_quota_requires_auth`, `test_quota_disabled_reports_enabled_false`, `test_quota_reports_used_and_remaining`, `test_quota_works_when_over_limit`)
- **AI-QUOTA-7.** `GET /quota` returns `remaining` 0, never negative, when over. \
  Enforced in: `services/ai-orchestrator/app/services/usage_repo.py` (`get_status`) \
  Pinned by: `services/ai-orchestrator/tests/test_usage_repo.py` (`test_get_status_remaining_floors_at_zero_when_over`)
- **AI-QUOTA-8.** `GET /usage` is admin or superadmin only (a user gets 403, no token 401);
  it takes inclusive `start` and `end` UTC dates (default the last 30 days), answers 400
  when start is after end or the span exceeds 366 days, and lists one row per user per day
  ordered by date then user. \
  Enforced in: `services/ai-orchestrator/app/routes/usage.py` (`get_usage`, `_MAX_RANGE_DAYS`); `services/ai-orchestrator/app/services/usage_repo.py` (`query_usage`) \
  Pinned by: `services/ai-orchestrator/tests/test_usage_route.py` (`test_usage_requires_auth`, `test_usage_rejects_non_admin`, `test_usage_allows_admin_roles`, `test_usage_defaults_to_last_30_days`, `test_usage_rejects_start_after_end`, `test_usage_rejects_oversized_range`, `test_usage_lists_multiple_users`)

**Out of scope.** No frontend page calls `GET /quota` or `GET /usage` at this commit.

### 8.11 Logging on AI paths

**What it does.** Logs record what happened without recording what the user typed or what
the model wrote, because either can carry credentials.

**Surfaces.** The ai-orchestrator logger calls named in the rules. The general redaction of
log extras is `operations-and-observability.md`.

**Rules.**

- **AI-LOG-1.** The assistant's question text and tool result bodies are never logged; the
  per-turn log records counts, lengths, token totals, and tool names. \
  Enforced in: `services/ai-orchestrator/app/routes/reservation_assistant.py` (`reservation_assistant`); `services/ai-orchestrator/app/services/ai_client.py` (`answer_reservation_question_with_tools`) \
  Pinned by: `services/ai-orchestrator/tests/test_reservation_assistant.py` (`test_question_text_not_logged`, `test_tool_call_names_logged_but_result_bodies_not_logged`); `services/ai-orchestrator/tests/test_ai_client.py` (`test_tool_loop_question_text_not_in_log_records`)
- **AI-LOG-2.** A malformed openai_compat tool-call argument is logged only by shape: tool
  name, length, decoder error position, and error class. \
  Enforced in: `services/ai-orchestrator/app/services/providers/openai_provider.py` (`_safe_json_loads`) \
  Pinned by: `services/ai-orchestrator/tests/test_openai_provider.py` (`test_safe_json_loads_malformed_never_logs_raw_content`, `test_safe_json_loads_type_error_has_no_error_position`)
- **AI-LOG-3.** A generation schema violation logs the errors' locations and types without
  the offending input, and the 502 detail is fixed text. \
  Enforced in: `services/ai-orchestrator/app/services/generator.py` (`generate_topology`) \
  Pinned by: `services/ai-orchestrator/tests/test_generate.py` (`test_generate_schema_violation_never_leaks_offending_input`)
- **AI-LOG-4.** Provider exception text is logged server-side only: the generate,
  assistant, identity, recipe, and purpose routes answer fixed details. \
  Enforced in: `services/ai-orchestrator/app/services/generator.py` (`generate_topology`); `services/ai-orchestrator/app/routes/reservation_assistant.py` (`reservation_assistant`); `services/ai-orchestrator/app/routes/template_identity.py` (`suggest_identity`); `services/ai-orchestrator/app/routes/recipes.py` (`_run_authoring`); `services/ai-orchestrator/app/routes/purpose_classification.py` (`_run_classification`) \
  Pinned by: `services/ai-orchestrator/tests/test_generate.py` (`test_generate_bare_exception_never_leaks_text`); `services/ai-orchestrator/tests/test_recipes_routes.py` (`test_draft_502_on_ai_error_never_leaks_provider_text`); `services/ai-orchestrator/tests/test_purpose_classification_routes.py` (`test_preview_502_on_ai_error_never_leaks_provider_text`); `services/ai-orchestrator/tests/test_reservation_assistant_coverage.py` (`test_buffered_route_ai_error_maps_to_502_with_generic_detail`)
- **AI-LOG-5.** The identity route's malformed-suggestion branch logs only the failing
  fields' locations and error types, without the model's values, and its 502 detail is
  fixed text. \
  Enforced in: `services/ai-orchestrator/app/routes/template_identity.py` (`suggest_identity`) \
  Pinned by: `services/ai-orchestrator/tests/test_template_identity_route.py` (`test_suggest_identity_malformed_never_logs_or_returns_model_output`)
- **AI-LOG-6.** No error detail or tool result carries upstream exception text: the
  inventory summary and seed-read 503s are fixed text (AI-GEN-2, AI-CONV-3), the commit's
  port-lookup and validate 503s and its 502 name a status or an exception class, a config
  push records `request failed (<ExceptionClass>)`, an upload's PDF or archive parse error
  names the class, and a failed tool call's result (which reaches the model and
  `tool_calls[].error`) is `upstream service answered HTTP <status>`,
  `upstream service unreachable (<ExceptionClass>)`, or `tool failed (<ExceptionClass>)`,
  logged as `ai_tool_call_failed` with the tool, class, and status only. \
  Enforced in: `services/ai-orchestrator/app/routes/generate.py` (`_inventory_provider`); `services/ai-orchestrator/app/services/committer.py` (`_fetch_device_ports`, `_validate_topology_wireable`, `_apply_configs`, `commit_proposal`); `services/ai-orchestrator/app/services/extractor.py` (`_extract_pdf`, `_extract_tgz`); `services/ai-orchestrator/app/services/tools.py` (`dispatch`, `_http_error_message`, `_log_tool_failure`) \
  Pinned by: `services/ai-orchestrator/tests/test_tools.py` (`test_dispatcher_handles_httpx_failure`, `test_dispatcher_http_status_error_carries_no_url`, `test_dispatcher_unexpected_exception_carries_only_the_class`); `services/ai-orchestrator/tests/test_commit.py` (`test_commit_validate_5xx_fails_closed_with_503`, `test_commit_validate_transport_failure_fails_closed_with_503`, `test_commit_aborts_with_503_when_ports_fetch_hits_5xx`); `services/ai-orchestrator/tests/test_committer_error_paths.py` (`test_apply_configs_records_request_exception_as_failed`, `test_commit_unexpected_error_rolls_back_and_wraps_502`); `services/ai-orchestrator/tests/test_extractor.py` (`test_extract_pdf_raises_for_garbage`, `test_extract_tgz_invalid_archive_raises`); `services/ai-orchestrator/tests/test_generate.py` (`test_generate_503_when_inventory_summary_fails`)

**Out of scope.** Key-name redaction of log extras and the JSON formatter
(`operations-and-observability.md`).

### 8.12 Frontend surfaces

**What it does.** The browser shows AI controls only when the status route says AI is
usable, renders proposals as reviewable ghost nodes, and turns structured refusals into
plain sentences.

**Surfaces.** `frontend/src/api/ai.ts`, `frontend/src/api/recipes.ts`,
`frontend/src/lib/errors.ts`, `frontend/src/config/featureFlags.ts`, and the components
named in each rule.

**Rules.**

- **AI-UI-1.** The topology editor's Use AI button renders only when the status reports
  `enabled`. \
  Enforced in: `frontend/src/pages/TopologyEditorPage.tsx` (`useAIStatus`) \
  Pinned by: `tests/e2e/test_ai_feature_gate.py` (`test_use_ai_button_matches_status`)
- **AI-UI-2.** The generate dialog disables Generate for an empty prompt, names each
  unwireable connection on a `topology_unconnectable` 422, shows the server detail on a
  503 (or a provider-neutral sentence), and prefixes 502s with `AI proposal rejected:`. \
  Enforced in: `frontend/src/components/topology-editor/AIDialog.tsx` (`AIDialog`) \
  Pinned by: `frontend/src/test/components/AIDialog.test.tsx` (`disables Generate when the prompt is empty`, `surfaces the server detail on a 503 and never names ANTHROPIC_API_KEY`, `falls back to a provider-agnostic 503 message when the body has no detail`, `names each unwireable connection on the structured 422`, `keeps the plain-string fallback for a 422 that is not structured`, `toasts the 502 upstream detail`)
- **AI-UI-3.** `topologyUnconnectableDetail` narrows only a 422 whose detail has
  `error: topology_unconnectable` and an array `pairs`; anything else is null and keeps
  the caller's plain fallback. \
  Enforced in: `frontend/src/lib/errors.ts` (`topologyUnconnectableDetail`, `formatUnconnectableDetail`) \
  Pinned by: `frontend/src/test/lib/errors.test.ts` (`narrows the structured 422 body`, `returns null for a different status carrying the same body`, `returns null for a plain-string detail`, `returns null when pairs is not an array`, `renders one 'source to target' line per pair under the message`)
- **AI-UI-4.** A new proposal first removes any earlier ghost nodes, and is discarded with a
  toast when a role has no resolved device or a resolved device is already on the canvas;
  otherwise devices and elements are drawn as ghost nodes and edges marked `isProposal`. \
  Enforced in: `frontend/src/pages/TopologyEditorPage.tsx` (`handleAIProposal`) \
  Pinned by: `frontend/src/test/pages/TopologyEditorPage.AIProposal.test.tsx` (`rejects any stale proposal before rendering a new one`, `discards the proposal with a toast when a role has no resolved device`, `discards the proposal with a toast when a proposed device duplicates one already on the canvas (mixed-node canvas)`, `renders a ghost element node and a device-sourced ghost attachment edge`)
- **AI-UI-5.** In the proposal bar, Accept opens the commit dialog, Modify turns the ghosts
  into ordinary canvas items and closes the bar, and Reject removes them; a successful
  commit removes the ghosts and navigates to the new topology. \
  Enforced in: `frontend/src/pages/TopologyEditorPage.tsx` (`handleProposalAccept`, `handleProposalModify`, `handleProposalReject`); `frontend/src/components/topology-editor/AIProposalBar.tsx` (`AIProposalBar`) \
  Pinned by: `frontend/src/test/pages/TopologyEditorPage.AIProposal.test.tsx` (`accept opens the AI commit dialog with the pending proposal`, `modify accepts the ghost nodes for editing, shows a toast, and clears the proposal bar`, `reject removes the ghost nodes and clears the proposal bar`, `committing the AI proposal clears the proposal, rejects the ghost nodes, and navigates to the new topology`)
- **AI-UI-6.** The commit dialog defaults the name to `AI: <purpose> (<time>)`, refuses an
  end time not after the start time, and sends the proposal's elements and their
  attachment edges. \
  Enforced in: `frontend/src/components/topology-editor/AICommitDialog.tsx` (`CommitForm`) \
  Pinned by: `frontend/src/test/components/AICommitDialog.test.tsx` (`renders the commit form with a default topology name and device summary`, `toasts an error when end time is not after start time`, `forwards elements and their attachment edges in the commit body`)
- **AI-UI-7.** `aiCommitTopologyUnwireableDetail` narrows only a 422 with
  `error: topology_unwireable` and an array `invalid_edges`; the dialog then lists each
  edge as `<role> to <role>: <reason in plain words>`, and otherwise shows
  `Commit failed: <detail>`. \
  Enforced in: `frontend/src/lib/errors.ts` (`aiCommitTopologyUnwireableDetail`); `frontend/src/components/topology-editor/AICommitDialog.tsx` (`invalidEdgeReasonText`, `INVALID_EDGE_REASON_TEXT`) \
  Pinned by: `frontend/src/test/components/AICommitDialog.test.tsx` (`toasts a plain-words per-edge list for a structured topology_unwireable 422`, `falls back to the generic detail toast for a non-topology_unwireable 422`, `toasts the backend detail when commit fails`)
- **AI-UI-8.** The reservation detail modal shows the AI Assistant tab only when the status
  reports `enabled`. \
  Enforced in: `frontend/src/components/reservations/ReservationDetailModal.tsx` (`ReservationDetailModal`) \
  Pinned by: `frontend/src/test/components/ReservationDetailModal.test.tsx` (`hides the AI assistant tab when the AI status is disabled`, `shows the AI assistant tab and renders it when AI is enabled`)
- **AI-UI-9.** `AIAssistantTabLegacy` is the default render path: unless the build sets
  `VITE_AI_CHAT_ENABLED=true`, the tab is a single-shot form on the buffered route. \
  Enforced in: `frontend/src/config/featureFlags.ts` (`AI_CHAT_ENABLED`); `frontend/src/components/reservations/AIAssistantTab.tsx` (`AIAssistantTab`); `frontend/src/components/reservations/AIAssistantTabLegacy.tsx` (`AIAssistantTabLegacy`) \
  Pinned by: `frontend/src/test/components/AIAssistantTab.test.tsx` (`submits the question and renders the answer`, `toasts on 503 and clears the prior answer`, `enforces the 4000-character question cap`)
- **AI-UI-10.** The chat path streams, discards interim tokens on an `interim` status,
  keeps the conversation id in sessionStorage per reservation and resends it, and resets
  the thread on a 404 that mentions the conversation. \
  Enforced in: `frontend/src/components/reservations/AIAssistantTab.tsx` (`ChatAssistant`, `sessionStorageKey`); `frontend/src/api/ai.ts` (`streamReservationAssistant`) \
  Pinned by: `frontend/src/test/components/AIAssistantTab.test.tsx` (`streams tokens then appends the final assistant message on send`, `captures conversation_id and resends it on the second turn`, `Start new conversation resets the thread and the conversation id`, `shows a tool status while tools run and discards interim tokens`, `shows an error bubble and toasts on an error event mid-stream`)
- **AI-UI-11.** A `pending_apply` in an answer, also on an incomplete turn, opens the
  review modal; its Confirm stays disabled until the dry-run job succeeds, and Cancel
  cancels the dry-run job. \
  Enforced in: `frontend/src/components/reservations/ReservationDetailModal.tsx` (`AIApplyConfirmModal`); `frontend/src/components/reservations/AIApplyConfirmModal.tsx` (`AIApplyConfirmModal`) \
  Pinned by: `frontend/src/test/components/AIAssistantTab.test.tsx` (`surfaces pending_apply up to the parent`, `still opens the confirmation modal from pending_apply on an incomplete turn (issue #871)`, `forwards pending_apply to the parent when the assistant scheduled a dry-run`); `frontend/src/test/components/AIApplyConfirmModal.test.tsx` (`disables Confirm until the dry-run succeeds`, `Confirm POSTs to the confirm endpoint and closes`, `Cancel button cancels the dry-run job and closes`, `shows the transcript with simulated badges once the dry-run succeeds`)
- **AI-UI-12.** The drivers page shows "Draft with AI" only when the status reports both
  `enabled` and `recipe_authoring`. \
  Enforced in: `frontend/src/pages/admin/DriversPage.tsx` (`DriversPage`) \
  Pinned by: `frontend/src/test/pages/DriversPage.test.tsx` (`hides Draft with AI when recipe authoring is off`, `hides Draft with AI when AI itself is unconfigured`, `shows Draft with AI and opens the panel when the flag is on`); `tests/e2e/test_recipe_panel_gate.py` (`test_draft_with_ai_button_matches_status`)
- **AI-UI-13.** The recipe panel refuses to upload a draft that failed validation, and
  uploads a passing one through inventory's driver upload with connection type
  Hypervisor under the admin's own token. \
  Enforced in: `frontend/src/components/admin/RecipeDraftPanel.tsx` (`RecipeDraftPanel`); `frontend/src/api/recipes.ts` (`recipePackageFile`) \
  Pinned by: `frontend/src/test/components/RecipeDraftPanel.test.tsx` (`blocks approve for a draft that failed validation`, `approve uploads the package as a Hypervisor driver`, `downloads the generated package as a named zip file`)
- **AI-UI-14.** The template editor shows Suggest with AI only when the status reports
  `enabled`. \
  Enforced in: `frontend/src/pages/TemplateEditorPage.tsx` (`useAIStatus`) \
  Pinned by: `frontend/src/test/pages/TemplateEditorPage.editflow.test.tsx` (`shows the Suggest with AI button only when AI is enabled`, `hides Suggest with AI when the status reports disabled`)
- **AI-UI-15.** The create-reservation modal calls the purpose preview only when the status
  reports `purpose_classification`, after a 700 ms pause once the purpose reaches 12
  characters or a topology is chosen, keeps only the latest answer, and shows a muted
  message on failure without blocking submit. \
  Enforced in: `frontend/src/hooks/usePurposeSuggestion.ts` (`usePurposeSuggestion`, `PURPOSE_SUGGESTION_DEBOUNCE_MS`); `frontend/src/components/reservations/CreateReservationModal.tsx` (`useAIStatus`) \
  Pinned by: `frontend/src/test/hooks/usePurposeSuggestion.test.tsx` (`makes no call when disabled, even with qualifying purpose text`, `calls after the debounce once purpose reaches 12 characters`, `triggers on a selected topology alone, regardless of purpose length`, `cancels a stale in-flight call: only the latest response is kept regardless of resolve order`); `frontend/src/test/components/CreateReservationModalPurposeSuggestion.test.tsx` (`renders nothing and calls nothing when purpose_classification is off`, `shows a muted Suggestion unavailable message on a failed preview call and never blocks submit`)

- **AI-UI-16.** Before sending, the generate dialog drops a file whose extension the server
  does not accept or that is over 5 MB, stops at 5 files, and skips a file already picked
  (same name and size); a 400 is shown as `Upload rejected: <detail>`. \
  Enforced in: `frontend/src/components/topology-editor/AIDialog.tsx` (`handleFilesPicked`, `ACCEPTED_EXTENSIONS`) \
  Pinned by: `frontend/src/test/components/AIDialog.test.tsx` (`checks picked files client-side before sending (#1040)`, `prefixes a 400 with Upload rejected (#1040)`)
- **AI-UI-17.** The commit dialog's default window is one to five hours from now, and it
  offers "Apply device configs" only when a resolved device has a non-empty config, with a
  label saying it needs admin or a manage grant on each device (execution's rule). \
  Enforced in: `frontend/src/components/topology-editor/AICommitDialog.tsx` (`CommitForm`) \
  Pinned by: `frontend/src/test/components/AICommitDialog.test.tsx` (`defaults the window to one to five hours from now`, `offers Apply device configs only when a resolved device has a non-empty config`)
- **AI-UI-18.** The legacy tab never sends a `conversation_id`, so each question starts a
  new conversation on the server. \
  Enforced in: `frontend/src/components/reservations/AIAssistantTabLegacy.tsx` (`AIAssistantTabLegacy`) \
  Pinned by: `frontend/src/test/components/AIAssistantTabLegacy.test.tsx` (`never sends a conversation_id, so each question starts a new conversation`)
- **AI-UI-19.** On a `topology_mixed_types` 422 the generate dialog shows the server's
  message and one `TYPE: template, template` line per type; `topologyMixedTypesDetail`
  narrows only a 422 whose detail has `error: topology_mixed_types` and an array `groups`. \
  Enforced in: `frontend/src/components/topology-editor/AIDialog.tsx` (`AIDialog`); `frontend/src/lib/errors.ts` (`topologyMixedTypesDetail`, `formatMixedTypesDetail`) \
  Pinned by: `frontend/src/test/components/AIDialog.test.tsx` (`names the templates per type on the mixed-types 422 (#1038)`); `frontend/src/test/lib/errors.test.ts` (`narrows the structured 422 body`, `returns null for the unconnectable 422 and a plain string`, `renders the message then one line per type`)

**Out of scope.** The Purpose Review page and the Classify now button are reservations
callers (`reservations.md`, RES-PURPOSE-9 to RES-PURPOSE-14).

### 8.13 AI generation evaluation harness

**What it does.** A maintainer can measure how often generated proposals turn out to be
wireable on a seeded lab, to tell whether a change to generation helps.

**Surfaces.** `tests/ai_eval/` (`make ai-eval`); scoring in `tests/ai_eval/scoring.py`.

**Rules.**

- **AI-EVAL-1.** The live suite skips unless `HERD_AI_EVAL=1`, and no gate or CI job sets
  it; it never asserts a pass rate. \
  Enforced in: `tests/ai_eval/test_generate_eval.py` (`test_generate_topology_wireability`) \
  Pinned by: `tests/unit/test_ai_eval_opt_in.py` (`test_eval_suite_skips_at_module_level_unless_opted_in`, `test_eval_suite_never_asserts_a_pass_rate`, `test_no_workflow_sets_the_opt_in_or_runs_the_target`, `test_only_the_ai_eval_recipe_sets_the_opt_in_and_no_target_depends_on_it`)
- **AI-EVAL-2.** The harness builds its throwaway canvas with the committer's device node
  and device-to-device edge shapes, skipping element edges. \
  Enforced in: `tests/ai_eval/scoring.py` (`build_canvas_data`) \
  Pinned by: `tests/unit/test_ai_eval_scoring.py` (`test_build_canvas_data_top_level_keys_match_committer`, `test_build_canvas_data_node_shape_matches_committer`, `test_build_canvas_data_edge_shape_matches_committer`, `test_build_canvas_data_skips_element_edges_and_dangling_roles`)
- **AI-EVAL-3.** A run passes only when validate reports no invalid edge and the proposal
  meets the prompt's minimum device and edge counts. \
  Enforced in: `tests/ai_eval/scoring.py` (`classify_run`) \
  Pinned by: `tests/unit/test_ai_eval_scoring.py` (`test_classify_run_all_valid_and_minimums_met`, `test_classify_run_no_path_fails`, `test_classify_run_below_minimum_devices_fails_even_with_no_invalid_edges`, `test_classify_run_below_minimum_edges_fails`)

**Out of scope.** Scoring the assistant, recipes, or classification.

### 8.14 Settings wiring

**What it does.** Every ai-orchestrator setting an operator can change in `.env` actually
reaches the container.

**Surfaces.** `docker-compose.yml` (the `ai-orchestrator` service's `environment:` block),
`.env.example`, `services/ai-orchestrator/app/config.py`.

**Rules.**

- **AI-CONF-1.** Each ai-orchestrator Settings field is passed by base compose as
  `${VAR:-default}` with the Settings default, or is exempt (`db_schema` and the four
  service URLs); every active `AI_` key in `.env.example` is wired. \
  Enforced in: `docker-compose.yml` (`AI_PROVIDER`, `AI_DAILY_TOKEN_QUOTA`) \
  Pinned by: `tests/unit/test_compose_settings_wiring.py` (`test_every_settings_field_is_wired_or_exempt`, `test_wired_compose_defaults_match_settings_defaults`, `test_no_stale_exemptions`, `test_every_active_ai_env_example_key_is_wired_to_ai_orchestrator`)
- **AI-CONF-2.** `AI_GENERATE_MAX_REPAIRS` outside 0 to 5,
  `AI_RESOLVER_CANDIDATES_PER_TEMPLATE` outside 1 to 50, and
  `AI_RESOLVER_MAX_SEARCH_STEPS` below 1 are refused at startup. \
  Enforced in: `services/ai-orchestrator/app/config.py` (`Settings`) \
  Pinned by: `services/ai-orchestrator/tests/test_config.py` (`test_generation_and_resolver_knobs_out_of_range_are_refused`, `test_generation_and_resolver_knobs_accept_their_bounds`)

**Out of scope.** The config service's editor for these values (`operations-and-observability.md`).

## 9. Errors

FastAPI validation errors (422) carry `detail` as a list of `{loc, msg, type}`; every
other error carries `detail` as a string or the object shown. Streamed failures after the
stream opens are `error` frames, listed at the end.

| Status | Error key or detail | When | Rule |
|---|---|---|---|
| 400 | `Too many files: limit is <N>, got <M>` | more file parts than `UPLOAD_MAX_FILES` | AI-UPLOAD-1 |
| 400 | `File '<name>' exceeds limit of <N> bytes` or `Upload total exceeds limit of <N> bytes` | an upload past a size cap | AI-UPLOAD-2 |
| 400 | `Unsupported file type '<ext>' for <name>. Accepted: ...`, `Failed to parse PDF (<ExceptionClass>)`, `Failed to read tgz archive (<ExceptionClass>)` | an upload that cannot be extracted | AI-UPLOAD-4, AI-UPLOAD-5 |
| 400 | `start must not be after end` or `date range too large; max 366 days` | bad usage report range | AI-QUOTA-8 |
| 401 | `Not authenticated` or `Could not validate credentials` | no token or a bad token on any route but status, health, and version | AI-GEN-1, AI-COMMIT-1, AI-STREAM-1, AI-QUOTA-6, AI-QUOTA-8 |
| 403 | `Admin or superadmin role required` | identity, usage, or recipe routes as a user | AI-IDENT-1, AI-QUOTA-8, AI-RECIPE-2 |
| 403 | `AI recipe authoring is disabled` | a recipe route with the flag off | AI-RECIPE-1, AI-RECIPE-3 |
| 403 | `{"error": "purpose_classification_disabled", "message": "Purpose classification is disabled"}` | a purpose route with the flag off | AI-PURPOSE-1 |
| 403 | `Invalid internal token` | internal route with a wrong token | AI-PURPOSE-2 |
| 404 | `Reservation not found` | first assistant turn when reservations answers 404 (not owner, or unknown) | AI-CONV-1 |
| 404 | `Conversation not found` | a `conversation_id` that is unknown, another user's, or another reservation's | AI-CONV-4 |
| 404 | `Recipe draft not found` | refine or read of an unknown draft | AI-RECIPE-10 |
| 409 | `No device templates with available devices in inventory. ...` | generate with nothing available | AI-GEN-4 |
| 409 | `Inventory shifted: ...` or `Inventory shifted during generation: ...` | candidates vanished between summary and resolution | AI-RESOLVE-2 |
| relayed 4xx | `Failed to create topology: <detail>`, `Failed to save canvas: <detail>`, `Failed to validate topology: <detail>`, `Failed to create reservation: <detail>` | cabling or reservations refused a commit step | AI-COMMIT-8, AI-COMMIT-9, AI-COMMIT-11 |
| 422 | validation list | a body or form outside its schema (prompt, commit body, question, purpose bounds) | AI-GEN-1, AI-COMMIT-2, AI-CONV-11, AI-PURPOSE-4 |
| 422 | the config validator's message, prefixed with the role | a commit device config that fails validation | AI-COMMIT-3 |
| 422 | `{"error": "topology_unconnectable", "pairs": [...], "message": "..."}` | no wireable device choice after repairs | AI-RESOLVE-14 |
| 422 | `{"error": "topology_mixed_types", "groups": [...], "message": "..."}` | resolved devices of more than one topology type after repairs | AI-GEN-15 |
| 422 | `{"error": "topology_unwireable", "invalid_edges": [...], "message": "..."}` | cabling's validate answered `valid: false` at commit | AI-COMMIT-10 |
| 429 | `{"limit", "used", "remaining": 0, "reset_at"}` | daily quota reached | AI-QUOTA-2, AI-CONV-12, AI-RECIPE-4, AI-PURPOSE-3 |
| 502 | `AI returned no usable response`, `AI call failed`, `AI returned a response that did not match the expected schema` | generation model failure | AI-GEN-7, AI-GEN-8, AI-GEN-9 |
| 502 | `AI referenced unknown templates: ...`, `AI proposed more devices than are available: ...`, `AI returned duplicate role names: ...`, `Edge references unknown role: ...`, `AI proposed a self-loop edge ...`, `AI proposed an element_to_element edge ...`, `AI proposed a duplicate edge ...` | a proposal mistake left after the last repair | AI-GEN-10, AI-GEN-11 |
| 502 | `Unexpected upstream failure (<ExceptionClass>)` | unexpected exception during commit | AI-COMMIT-12 |
| 502 | `Assistant call failed` | buffered assistant model failure with no side effect | AI-TURN-5 |
| 502 | `AI suggestion failed` or `AI returned a malformed suggestion` | identity model failure or bad answer | AI-IDENT-3, AI-IDENT-4 |
| 502 | `AI recipe drafting failed` | recipe model failure | AI-RECIPE-13 |
| 502 | `AI classification failed` or `Purpose classifier returned no usable distribution` | purpose model failure | AI-PURPOSE-10, AI-PURPOSE-12 |
| 503 | `AI orchestrator is not configured` | provider unconfigured or not constructible | AI-PROV-2, AI-PROV-3 |
| 503 | `AI provider is unreachable` | provider transport failure | AI-PROV-10, AI-GEN-9, AI-TURN-5, AI-RECIPE-13, AI-PURPOSE-12 |
| 503 | `Could not verify cabling paths; no topology was generated. Retry the request.` | pathfind batch failed | AI-RESOLVE-6 |
| 503 | `Could not read inventory; no topology was generated. Retry the request.` | inventory failed the summary or candidate read | AI-GEN-2, AI-RESOLVE-3 |
| 503 | `Could not read the reservation or its devices; retry the request.` | reservations or inventory failed the first-turn seed read | AI-CONV-3 |
| 503 | `Failed to fetch ports for device <id>` (transport) or `...: inventory answered HTTP <status>` (5xx) | port lookup 5xx or transport at commit | AI-COMMIT-6 |
| 503 | `Failed to validate topology wireability: ...` | validate unreachable, 5xx, or unreadable | AI-COMMIT-9 |
| 503 | `Recipe validator is unreachable` | execution validate-package failed | AI-RECIPE-8 |
| 504 | `Assistant did not respond within <N>s` | buffered assistant deadline with no side effect | AI-TURN-5 |
| 504 | `Reservation seed gather exceeded its deadline` | seed read past 30 seconds | AI-CONV-1 |
| SSE `error` | `Assistant did not respond within <N>s`, `AI provider is unreachable`, `Assistant call failed`, `Assistant produced no answer` | a streamed turn failed with no side effect | AI-STREAM-4 |

Successes that report a failure inside: an assistant turn whose later step failed after a
write landed answers 200 with `incomplete` set (AI-TURN-6); a commit whose config push
failed answers 200 with `failed` entries in `config_results` (AI-COMMIT-16); a tool refusal
is an `is_error` tool result, not an HTTP error (AI-TOOL-2).

## 10. Interactions with other services

Calls into this area are in section 7. All user-path calls forward the caller's JWT.

| Direction | Peer | Call | Purpose | On failure |
|---|---|---|---|---|
| Out | LLM provider | the provider SDK (`messages.create`, `messages.stream`, or `chat.completions.create`) | every model call | Unreachable: 503 (or `incomplete` / SSE `error`). Other errors: 502. Timeout inside the assistant: `AIError`, then AI-TURN-5 or AI-TURN-6 |
| Out | inventory | `GET /templates`, `GET /devices` (JWT) | generation summary | Fail closed: 503, pinned detail (AI-GEN-2) |
| Out | inventory | `GET /devices?template_id&status=AVAILABLE&dut_only` (JWT) | resolver candidates | Fail closed: 503, pinned detail (AI-RESOLVE-3) |
| Out | cabling | `POST /pathfind/batch` (JWT) | resolver reachability | Fail closed: 503 (AI-RESOLVE-6) |
| Out | inventory | `GET /devices/{id}/ports` (JWT) | element attachment port at commit | 404 and other 4xx: no ports, attachment dropped. 5xx and transport: fail closed, 503 before anything is written (AI-COMMIT-6) |
| Out | cabling | `POST /topologies`, `PUT /topologies/{id}`, `POST /topologies/{id}/validate`, `DELETE /topologies/{id}` (JWT) | commit and rollback | Create and save: relayed status. Validate: fail closed, 503. Delete: logged, never raised (AI-COMMIT-13) |
| Out | reservations | `POST /` (JWT) | commit's reservation | Relayed status; topology rolled back (AI-COMMIT-11) |
| Out | execution | `POST /execute` (JWT) | optional config push | Recorded per device as `failed`; commit stands (AI-COMMIT-16) |
| Out | reservations | `GET /{id}` (JWT) | assistant seed | 404: 404. Deadline: 504. Other: 503 (AI-CONV-3) |
| Out | reservations | `GET /internal/{id}` (internal token, 10 s) | idle-conversation sweeper retention | Fail closed: the conversation is kept; a 404 releases it (AI-CONV-13) |
| Out | reservations | `GET /{id}` (JWT) | assistant tool device scope | Any failure refuses every device-scoped tool call as an `is_error` result; fail closed (AI-TOOL-11) |
| Out | inventory | `GET /devices/{id}` (JWT) | assistant seed devices | 404: device omitted. Other: 503 (AI-CONV-3) |
| Out | inventory | device, port, template, config-version, config-schema, and schedule routes (JWT) | assistant tools | Becomes an `is_error` tool result; the turn continues (AI-TOOL-2). The schema proxy fails open to the registry (AI-TOOL-7) |
| Out | cabling | `POST /pathfind` (JWT) | `find_path` tool | `is_error` tool result |
| Out | execution | `GET /runs?reservation_id` (JWT) | `list_executions_for_reservation` tool | `is_error` tool result |
| Out | allowlisted web host | `GET` (no credentials) | `read_doc` web source | `is_error` tool result (AI-DOCS-10 to AI-DOCS-13) |
| Out | execution | `POST /internal/validate-package` (internal token) | recipe validation | Fail closed: 503, no draft stored (AI-RECIPE-8) |
| Out | cabling, inventory | `GET /topologies/{id}`, `POST /devices/batch`, `GET /templates/{id}` (JWT) | purpose preview signals | Fail open: the signal is left out (AI-PURPOSE-7) |
| Out | inventory, cabling | `GET /devices/{id}/internal`, `GET /templates/{id}/internal`, `GET /devices/{id}/apply-jobs/internal`, `GET /internal/forks/{id}` (internal token) | purpose end-pass signals | Fail open: the signal is left out |

## 11. Configuration

All are ai-orchestrator environment variables unless marked; [ENV_VARS.md](../ENV_VARS.md)
has the full list.

| Setting | Default | Effect |
|---|---|---|
| `AI_PROVIDER` | `anthropic` | `anthropic` or `openai_compat` (AI-PROV-1) |
| `AI_BASE_URL` | empty | Provider endpoint; root for anthropic, with `/v1` for openai_compat |
| `AI_API_KEY` | empty | Provider key; blank sends `EMPTY` |
| `AI_MODEL` | `claude-sonnet-4-6` | Model id; also echoed as `model` in answers and `generated_by` in recipes |
| `AI_MAX_TOKENS` | `4096` | Per-call output cap |
| `AI_TLS_VERIFY` | `true` | Provider TLS verification (AI-PROV-12) |
| `AI_CA_CERT` | empty | CA bundle path; wins over `AI_TLS_VERIFY` |
| `ANTHROPIC_API_KEY` | empty | Never used; triggers a startup warning (AI-PROV-19) |
| `AI_GENERATE_MAX_REPAIRS` | `2` | Generation re-prompts, 0 to 5 |
| `AI_RESOLVER_CANDIDATES_PER_TEMPLATE` | `8` | Resolver candidates per template, 1 to 50 |
| `AI_RESOLVER_MAX_SEARCH_STEPS` | `5000` | Resolver step budget, at least 1 |
| `AI_DAILY_TOKEN_QUOTA` | `0` | Per-user daily tokens; 0 disables metering |
| `UPLOAD_MAX_FILES` | `5` | Files per generate request |
| `UPLOAD_MAX_FILE_BYTES` | `5242880` | Bytes per file |
| `UPLOAD_MAX_EXTRACTED_CHARS` | `80000` | Extracted characters per request |
| `ASSISTANT_MAX_TOOL_ITERATIONS` | `8` | Tool rounds before the forced final answer |
| `ASSISTANT_TOOL_RESULT_CHAR_CAP` | `8000` | Tool result cut; also sizes `read_doc` windows |
| `ASSISTANT_OVERALL_DEADLINE_S` | `90.0` | Loop deadline per turn |
| `ASSISTANT_PER_CALL_TIMEOUT_S` | `20.0` | Per model call inside the loop |
| `ASSISTANT_CONVERSATION_TTL_HOURS` | `24` | Idle conversation lifetime; must be positive |
| `ASSISTANT_MAX_TURNS` | `40` | Messages kept per conversation, seed counted |
| `ASSISTANT_HISTORY_TOKEN_BUDGET` | `60000` | Estimated tokens kept per conversation |
| `ASSISTANT_SWEEPER_INTERVAL_SECONDS` | `3600` | Sweeper period |
| `AI_WRITE_TOOLS_ENABLED` | `false` | Assistant write tools |
| `AI_DOCS_MANUAL_ENABLED` | `true` | Built-in manual source |
| `AI_DOCS_CORPUS_DIRS` | empty | Operator corpora, `name=/abs/path` comma-separated |
| `AI_DOCS_WEB_ENABLED` | `false` | Web documentation source |
| `AI_DOCS_WEB_ALLOWED_PREFIXES` | empty | https prefixes the web source may fetch |
| `AI_DOCS_WEB_MAX_BYTES` | `524288` | Web body cap |
| `AI_DOCS_INDEX_TTL_SECONDS` | `600` | Corpus index lifetime |
| `AI_RECIPE_AUTHORING_ENABLED` | `false` | Recipe routes |
| `AI_RECIPE_MAX_ATTEMPTS` | `3` | Drafting attempts per request |
| `AI_PURPOSE_CLASSIFICATION_ENABLED` | `false` | Purpose routes |
| `AI_PURPOSE_INCLUDE_TRANSCRIPTS` | `true` | Transcripts in the end pass |
| `INTERNAL_API_TOKEN` | empty | Token for validate-package and the internal purpose route |
| `VITE_AI_CHAT_ENABLED` (frontend build) | `false` | Chat UI instead of the legacy single-shot tab |

Fixed in code, not configurable: the status probe cache (30 s); the inventory and seed HTTP
timeouts (15 s); the seed gather deadline (30 s); the pathfind chunk (200) and timeout
(20 s); the commit HTTP timeout (15 s); the validate-package timeout (120 s); the
classifier retry (one) and rationale cap (500); the transcript budget (200 rows, 12000
characters); the search hit cap (10); the corpus file cap (2 MiB); the redirect cap (3);
the upload read chunk (64 KiB).

## 12. Test coverage map

| Level | Where | Notes |
|---|---|---|
| Unit | `services/ai-orchestrator/tests/` (providers, client, resolver, generator, extractor, committer helpers, tools, docs, recipes, purpose, usage, conversation repo, all with stub providers and mocked HTTP); `tests/unit/test_ai_eval_scoring.py`, `tests/unit/test_compose_settings_wiring.py`, `tests/unit/test_no_yield_inside_cancel_scope.py`; frontend `frontend/src/test/components/AIDialog.test.tsx`, `AICommitDialog.test.tsx`, `AIAssistantTab.test.tsx`, `AIApplyConfirmModal.test.tsx`, `RecipeDraftPanel.test.tsx`, `frontend/src/test/lib/errors.test.ts`, `frontend/src/test/hooks/usePurposeSuggestion.test.tsx` | No unit test calls a real provider |
| Functional (through the service API) | the route tests in `services/ai-orchestrator/tests/` (`test_generate.py`, `test_commit.py`, `test_reservation_assistant.py`, `test_reservation_assistant_coverage.py`, `test_reservation_assistant_stream_deadline.py`, `test_recipes_routes.py`, `test_purpose_classification_routes.py`, `test_template_identity_route.py`, `test_quota_route.py`, `test_usage_route.py`, `test_status.py`) via httpx against the app | Upstream services are mocked with respx or dependency overrides |
| Integration (running stack) | `tests/integration/test_ai_status.py`, `test_ai_provider_wiring.py`, `test_ai_commit_route.py`, `test_ai_assistant_multi_turn.py`, `test_ai_assistant_tools.py`, `test_template_identity.py`, `test_recipe_authoring_gate.py`, `test_execution_configure_gate.py`, `test_purpose_review_flow.py` | CI and nightly set no `AI_*` provider, so every test that needs a live model skips there; only the gates, auth, commit, and unconfigured paths run |
| Stress and load | None | No locust task calls an AI route; a load test would need a provider or a stub provider in the stack |
| Browser end-to-end | `tests/e2e/test_ai_feature_gate.py`, `test_ai_generate_dialog.py`, `test_recipe_panel_gate.py`, `test_tier2_playwright.py` (`test_assistant_stream_token_by_token`) | The dialog and stream tests skip when the stack has no provider; the gates run |
| Measurement | `tests/ai_eval/` (`make ai-eval`) | Opt-in, in no gate (AI-EVAL-1) |

Not run for this document: nothing was run against a running stack or a real provider. The
integration, browser, and evaluation suites were read, not run. `test_vllm_live.py` under
`services/ai-orchestrator/tests/integration/` needs a live vLLM and was not run.

## 13. Known limits and gaps

### Open defects

None at this commit: #1039 is resolved by AI-CONV-13.

### Limits by decision

- A duplicate device-to-device edge in a proposal is refused although the canvas supports
  parallel cables (AI-GEN-12): issue #827 and the comment in `_validate_against_inventory`.
- An infeasible proposal fails with 422 rather than being returned flagged or with an edge
  dropped (AI-RESOLVE-14): the `TopologyUnconnectableError` docstring and issue #828.
- The resolver judges reachability only and leaves port choice to cabling's fork-save
  resolver (AI-RESOLVE-11): the module docstring of
  `services/ai-orchestrator/app/services/resolver.py`, ADR 0006, issue #531.
- The web fetch's address check and its connection resolve the host name separately, the
  DNS rebinding window (AI-DOCS-11). Recorded as a known limitation in ADR 0015 (decision
  3) and [AI_ASSISTANT.md](../AI_ASSISTANT.md); the web source ships disabled and behind an
  operator allowlist.
- A terminal reservation whose classification never yields a suggestion (attempt cap
  reached, no Classify now) stays pending, so its idle conversations are kept until it is
  classified or either transcript flag is turned off (AI-CONV-13): Lane's decision on
  #1039 (E(a)), recorded in [AI_PURPOSE_CLASSIFICATION.md](../AI_PURPOSE_CLASSIFICATION.md).
- `GET /status` is unauthenticated and its construction probe is cached for 30 seconds
  (AI-PROV-5, AI-PROV-7): issue #606 and the docstring of `_ProviderConstructionCache`.
- Usage rows are written only when a quota is configured (AI-QUOTA-1): the comment on
  `ai_daily_token_quota` in `services/ai-orchestrator/app/config.py` and
  [ENV_VARS.md](../ENV_VARS.md).
- The end-of-reservation pass resends assistant transcripts to the same provider unless
  `AI_PURPOSE_INCLUDE_TRANSCRIPTS` is off (AI-PURPOSE-6): ADR 0013 point 11.
- Write tools, recipe authoring, purpose classification, and web documentation are off by
  default and refused at the route or dispatch boundary when off (AI-WRITE-1, AI-RECIPE-1,
  AI-PURPOSE-1, AI-DOCS-9): issue #113's discipline, recorded in the comments in
  `services/ai-orchestrator/app/config.py`.
- The legacy single-shot assistant tab is the default render path (AI-UI-9): the
  `VITE_AI_CHAT_ENABLED` row in [ENV_VARS.md](../ENV_VARS.md) and the comment in
  `frontend/src/config/featureFlags.ts`.

Where the AI guides, docstrings, or UI text disagree with the code, the rules above
describe the code; issue #1041 corrected every disagreement found when this document was
written.

### Rules with no test

None at this commit: issue #1040 added the tests for the rules this section listed.
