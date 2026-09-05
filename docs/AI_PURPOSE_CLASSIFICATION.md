# AI Purpose Classification

Phase 2 of issue #646 (lab purpose classification). Phase 1 (the manual
`purpose_category` field, the taxonomy, and reporting breakdowns) lives in
the reservations service; see `docs/design/0013-lab-purpose-classification.md`
(ADR 0013) for the full decision record, including the taxonomy and the
confirmed-versus-suggested split. This document covers phase 2's two
AI-orchestrator endpoints only: what they take, what they return, which
signals feed the model, and the privacy tradeoff of including reservation
assistant transcripts.

Both endpoints are dark by default behind `AI_PURPOSE_CLASSIFICATION_ENABLED`
(see `docs/ENV_VARS.md`) and additionally require the AI provider to be
configured (`ai_is_configured()`); `GET /api/ai/status` reports the combined
state as `purpose_classification: bool`.

## Two endpoints, two passes

### `POST /api/ai/classify-purpose/preview` (creation pass)

Called by the create-reservation modal while the user is still filling out
the form, so it runs on the caller's own JWT and has no reservation yet:
there is only a candidate topology and/or a device/dynamic-template
selection. Body:

```json
{
  "categories": ["qa_regression", "feature_development", "other"],
  "purpose": "regression pass ahead of the 4.2 release",
  "topology_id": "…uuid or null…",
  "device_ids": ["…uuid, only if selecting devices directly…"],
  "dynamic_requests": [{"template_id": "…uuid…", "count": 2}]
}
```

`categories` is required and non-empty; the orchestrator is deliberately
taxonomy-agnostic (it never hardcodes the reservations service's category
list), so the caller always supplies it. The modal prefills its category
select with the top result and shows the full distribution as percentages;
the user may accept it or pick something else, and whatever they submit is
an owner pick (ADR 0013 point 10), not an AI suggestion.

### `POST /api/ai/internal/classify-purpose` (end-of-reservation pass)

Called by the reservations service's purpose-classify reconciler once a
reservation reaches a terminal state, authenticated with `X-Internal-Token`
(no user JWT: this is a background job). Since issue #702 this reconciler
runs on its own loop (`PURPOSE_CLASSIFY_INTERVAL_SECONDS`, see
`docs/ENV_VARS.md`), separate from the reservations expiration sweep: it is
the only reconciler bound by an LLM call rather than a DB or fast HTTP round
trip, so it no longer shares a loop with the reconcilers that must not be
delayed behind a slow or hung orchestrator. Body:

```json
{
  "reservation_id": "…uuid…",
  "categories": ["qa_regression", "feature_development", "other"],
  "purpose": "regression pass ahead of the 4.2 release",
  "user_id": "…uuid, the reservation owner…",
  "device_ids": ["…uuid…"],
  "topology_id": "…uuid or null…",
  "dynamic_requests": [{"template_id": "…uuid…", "count": 2}],
  "start_time": "2026-09-01T09:00:00Z",
  "end_time": "2026-09-01T17:30:00Z",
  "status": "COMPLETED"
}
```

The result is stored as a suggestion, not written directly to
`purpose_category`; it waits for an admin to accept, override, or dismiss it
on the `/admin/purpose-review` page. A second call for the same reservation
(a later terminal transition, or an admin-triggered backfill) may revise an
earlier suggestion.

## Response shape (`PurposeClassification`, shared by both endpoints)

```json
{
  "distribution": [
    {"category": "qa_regression", "probability": 0.71},
    {"category": "feature_development", "probability": 0.24},
    {"category": "other", "probability": 0.05}
  ],
  "top_category": "qa_regression",
  "pass": "creation",
  "model": "claude-sonnet-4-6",
  "rationale": "Purpose text and device mix (regression-lab switches) both point at a QA pass.",
  "generated_at": "2026-09-04T04:50:00Z",
  "signals_used": ["purpose_text", "topology", "dynamic_templates"]
}
```

`distribution` is sorted by probability descending and always sums to 1.0
across exactly the categories the caller supplied (a category the model
never mentioned appears with probability 0.0, sorted last). `pass` is
`"creation"` from the preview endpoint and `"end"` from the internal one.
`signals_used` lists which of the signals below actually made it into the
prompt for this call; a signal that failed to fetch (see below) is simply
absent, never a reason to fail the request.

A `502` with detail `Purpose classifier returned no usable distribution` means
the model's answer had no distribution recognizable against the supplied
categories, even after one retry.

## The internal route's error taxonomy, and how the reconciler reads it (issue #706)

`POST /internal/classify-purpose` can answer 403 for two unrelated reasons:
`AI_PURPOSE_CLASSIFICATION_ENABLED` is off (`require_purpose_classification`),
or the caller's `X-Internal-Token` does not match this service's
`INTERNAL_API_TOKEN` (`_check_internal_token`). Both are the same status
code, but only the first means "not available yet"; the second is a
configuration problem (an out-of-sync token after a rotation) that will not
resolve itself on a later tick or a later row. The two are distinguished by
body shape, not status code: the flag-off refusal carries the structured
detail `{"error": "purpose_classification_disabled", "message": "Purpose
classification is disabled"}` (`PURPOSE_CLASSIFICATION_DISABLED_DETAIL` in
`app/routes/purpose_classification.py`); the bad-token refusal carries the
plain string `"Invalid internal token"`. The reservations-service reconciler
checks for the structured marker (with a fallback to the exact legacy
plain-string detail, for a pre-#706 orchestrator image) before treating a 403
as feature-off; any other 403 is logged at WARNING as a likely internal-token
mismatch, not silently folded into "the feature is off there".

The reconciler's outcome taxonomy, all documented in
`services/reservations/app/tasks/expiration.py`'s `_classify_purpose_one`:

- `ok`: a suggestion was stored, or the row was already resolved.
- `feature_off`: 403 with the disabled marker (or the legacy string), or 404
  (a mixed-version deployment where reservations was upgraded first and the
  running orchestrator does not expose this route yet). Ends the tick; no
  attempt counted.
- `transient`: 429 (a rate limit, including this route's own daily-quota
  429), 502/503/504 (misconfiguration or an outage, including the
  `AI_NOT_CONFIGURED_DETAIL` 503 from `ai_is_configured()`), or any transport
  error OTHER than a timeout. Ends the tick; no attempt counted. Without this
  class, a quota 429 that lasts until UTC midnight would burn a row's whole
  `purpose_classify_max_attempts` cap in three ticks despite never having had
  a real classification attempt.
- `timeout` (2026-09-05 amendment, issue #706 follow-up): the call raised
  `httpx.TimeoutException`. Unlike the other transient outcomes, a timeout is
  per-row evidence, not provider-wide evidence: it costs the provider up to
  `purpose_classify_timeout_seconds` trying to answer THIS row, so it
  increments `purpose_classify_attempts` and the reconciler continues to the
  next row in the same tick instead of ending it. Without this distinction,
  one reservation whose call happened to exceed the timeout could end every
  tick before reaching any row behind it, oldest-requested-first, a
  permanent head-of-line stall (observed live: the same row timed out on
  seven consecutive ticks with attempts still 0 while 45 eligible rows
  waited behind it). A provider uniformly slower than the timeout still
  burns every row's attempts, which `POST /admin/purpose/backfill` recovers
  from by resetting them.
- `forbidden`: 403 without the disabled marker. Ends the tick; no attempt
  counted, but logged at WARNING (not `feature_off`'s INFO) since this is a
  problem an operator needs to see and fix, not an expected state.
- `failed`: any other non-200 status, or a 200 with an unparseable body.
  Increments `purpose_classify_attempts`; this and `timeout` are the only
  outcomes that affect the row's attempt cap.

## Signals

Both endpoints assemble a compact, XML-tagged prompt block from whatever
signals are available, then make exactly one forced `classify_purpose` tool
call through the `LLMProvider` abstraction (see
`app/services/purpose_classifier.py`), the same forced-tool-call pattern the
recipe-drafting and topology-generation endpoints already use.

| Signal | `signals_used` name | Preview (creation) | Internal (end) | Source |
|---|---|---|---|---|
| The `purpose` field, verbatim | `purpose_text` | yes, if given | yes, if given | request body |
| Device names and templates | `topology` | yes, from the topology's canvas (cabling) + inventory | yes, per device (inventory) | cabling `GET /topologies/{id}` + inventory `POST /devices/batch` (preview); inventory `GET /devices/{id}/internal` (internal, no batch endpoint exists for the internal-token path) |
| Wiring shape (layer counts) | `topology` | from the topology canvas's edges | from the fork's resolved connections | cabling canvas `edges` (preview) / cabling `GET /internal/forks/{reservation_id}` `connections` (internal) |
| Dynamic template names | `dynamic_templates` | yes, if `dynamic_requests` given | yes, if `dynamic_requests` given | inventory `GET /templates/{id}` (preview) / `GET /templates/{id}/internal` (internal) |
| Config-apply job names and counts | `config_apply_jobs` | not applicable (no reservation yet) | yes, per device | inventory `GET /devices/{id}/apply-jobs/internal` (new, internal-token only; see below) |
| Fork version count | `fork` | not applicable | yes | cabling `GET /internal/forks/{reservation_id}` `versions` |
| Duration and terminal status | `duration_status` | not applicable | always | request body (`start_time`, `end_time`, `status`) |
| Reservation assistant transcripts | `transcripts` | never (no reservation yet) | yes, when `AI_PURPOSE_INCLUDE_TRANSCRIPTS=true` | this service's own `assistant_conversations`/`assistant_messages` tables |

**Not a signal, by design:** the natural-language generation prompt for an
AI-built topology. Nothing in this service or in cabling's `Topology` model
persists that prompt once a topology is committed
(`app/services/committer.py` builds `canvas_data` on the fly from the
accepted proposal and never writes the original prompt anywhere durable), so
there is nothing to fetch. If a future change starts persisting it, this is
the natural place to wire it in as an additional signal.

**Not a signal, out of scope (ADR 0013):** uploaded bulk-import file
contents, and anything from device configuration contents or secrets.

### The config-apply job summary is a new inventory endpoint

The fixed contract for this feature calls for "config-apply job names and
counts... never job contents or configs" as an end-of-reservation signal, but
inventory's existing apply-job listing
(`GET /devices/{device_id}/apply-jobs`) requires a user JWT, which the
internal, no-acting-user reconciler call never has. Inventory gained one
small additive endpoint for this: `GET /devices/{device_id}/apply-jobs/internal`
(`X-Internal-Token`), returning `{"count": int, "names": [str, ...]}` where
`names` is the deduplicated, non-null set of the associated config versions'
free-text `description` field (a human label the scheduling user wrote), not
the version's `config` JSON. It cannot leak configuration contents or
credentials by construction. See
`services/inventory/app/routers/apply_jobs.py`.

### Signal-fetch failures never fail the request

Every external fetch (inventory, cabling, the transcript read) is wrapped
independently: a non-2xx response, a transport error, or a malformed body is
logged as a warning and that signal is simply dropped from both the prompt
and `signals_used`. Classification proceeds with whatever signals succeeded;
in the worst case (every fetch fails), the preview endpoint still has
`purpose_text` if the caller supplied one, and the internal endpoint always
has `duration_status` (derived from the request body, never fetched) plus
`purpose_text` if given.

## Privacy note on transcripts (ADR 0013 point 11)

Reservation-assistant transcripts are user-authored chat that was already
sent to the configured AI provider once, when the user actually asked the
assistant a question. The end-of-reservation classifier pass sends that same
text again, to the same provider, alongside the reservation's structured
metadata (device names, purpose text, duration, and so on). No new provider
or data path is introduced by this feature; it is a second call to the
provider you already configured.

A deployment that must not resend user chat text sets
`AI_PURPOSE_INCLUDE_TRANSCRIPTS=false`. The creation-pass preview endpoint
never includes transcripts regardless of this flag, since no reservation (and
therefore no transcript) exists yet at that point.

The prompt assembled for either pass never includes credentials, secret
values, or device configuration contents: `field_data` is never forwarded,
config versions contribute only their free-text `description` label (never
their `config` JSON), and the transcript extraction keeps only `TextBlock`
content from `USER`/`ASSISTANT` turns, skipping `TOOL`-role echoes and
`ToolUseBlock`/`ToolResultBlock` content entirely.

## Metering

Every classify call (including the retry, if one happens) is metered through
the same `ai_usage` table and `usage_repo.enforce_quota`/`record_usage` hooks
every other AI feature uses, so it counts against `AI_DAILY_TOKEN_QUOTA` like
topology generation, the assistant, and recipe drafting. The preview
endpoint meters against the calling user; the internal endpoint meters
against the reservation's owner (`user_id` in the request body), since there
is no other acting user for a background call.

## Operating it

Neither endpoint here is where an admin actually works with suggestions day
to day; that surface, the review queue, accept/override/dismiss, and the
`Classify history` backfill action, lives in the reservations service and is
described in `docs/ADMIN_HANDBOOK.md` (the Utilization report section,
"Purpose review" and "Classify history (backfill)" entries). This document
covers the two orchestrator endpoints those features call.

Privacy, restated from above since it is the one operating decision most
likely to matter to a deployment: the end-of-reservation pass resends the
reservation's own assistant transcripts to the same AI provider already
configured for everything else HERD does with it. Nothing new is exposed
that a chat with the assistant did not already expose once. Set
`AI_PURPOSE_INCLUDE_TRANSCRIPTS=false` if a deployment must not resend that
text; both classification endpoints keep working on the remaining
structured signals with the flag off.
