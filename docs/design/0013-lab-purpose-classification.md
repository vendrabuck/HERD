# Decision: Lab Purpose Classification, Issue #646

Status: Accepted 2026-09-04 (eleven decision points resolved by Lane the same
day, recorded on issue #646; see Decision). No code in this doc. Context
verified against the live HERD-public tree on 2026-09-04 (main at af83bdb3).
Symbols are the stable reference; line numbers are as of that commit.

## Context

HERD's reporting answers who used what and for how long. The utilization
report (`services/reservations/app/routers/reservations.py`,
`GET /reports/utilization` at line 162 and its CSV twin at line 196) breaks
device-hours down by user, device, topology type, day, and group. It cannot
answer why the lab was used: a director looking at a rack at 80 percent
utilization cannot tell support-case replication from regression runs from
feature work. Issue #646 asks for a purpose per reservation, drawn from a
closed taxonomy so it can be charted, suggested by the AI HERD already runs,
and confirmed by a human before it counts.

Relevant existing fabric, verified:

- `Reservation.purpose` is a free-text column
  (`services/reservations/app/models/reservation.py:61`, `Text`, nullable),
  accepted on create and update (`app/schemas/reservation.py:52` and `:116`,
  max length 2000) and returned in the response (`:147`). It is the first
  classification signal and stays as it is.
- Reporting lives in the reservations service, not a separate service: the
  report handlers sit in the same router as the CRUD endpoints and compute
  device-hours from the reservation's own device list. Transit gear the
  pathfinder pulled in is recorded as hops in cabling's `fork_connections`
  (ADR 0006), in another schema, reachable only over HTTP.
- Lifecycle events reach NATS through the transactional outbox
  (`herd_common/outbox.py`, issue #21); consumers are execution,
  notifications, and integration. The `/api/v1` facade in the integration
  service re-exposes reservations, with its contract checked in by hand at
  `docs/api/v1-openapi.json` and snapshotted at
  `tests/contract/snapshots/v1.json`.
- The AI orchestrator already holds the richest intent signals: the
  natural-language generation prompt for AI-built topologies, and the
  reservation assistant's conversations (`assistant_conversations` and
  `assistant_messages` in its own schema). Provider access goes through the
  `LLMProvider` Protocol (`services/ai-orchestrator/app/services/llm_provider.py`),
  and feature gating follows the write-tools discipline: a flag enforced at
  the dispatch or route boundary, not only hidden from a list
  (`AI_WRITE_TOOLS_ENABLED`, `AI_RECIPE_AUTHORING_ENABLED`).
- Settings are `HerdBaseSettings` subclasses with env override; list-valued
  settings already exist (`cors_origins`), so a configurable list needs no new
  mechanism.
- Admin route gating is one pathless group under `AdminGuard` in
  `frontend/src/routes.tsx`, pinned structurally by `test/routes.test.tsx`,
  which lists the guarded paths literally. A new admin page means adding it
  to both.
- The reservations expiration sweep already hosts standing reconcilers
  (archive, wiring-heal, pending prune) that pick bounded batches of rows
  each tick. A new reconciler is the established shape for best-effort
  background work that must converge without a request in flight.

## Decision

Eleven decision points, all resolved on 2026-09-04. Phase 1 points are 1 to
7, phase 2 points are 8 to 11.

### 1. Taxonomy storage: configurable list, plain string column (decided)

The taxonomy is a list in the reservations service settings,
`purpose_categories`, with the default
`qa_regression, support_case_replication, feature_development,
customer_demo_poc, training, performance_benchmark, other`, overridable by
the env variable `PURPOSE_CATEGORIES` (comma-separated, replaces the list).
The column `purpose_category` is a nullable `Text`, validated at write time
against the configured list; it is NOT a Postgres enum type and there is no
categories table. Extending the taxonomy is a config change with no
migration. A row keeps its value if the category is later removed from the
list; reporting shows historical values regardless. Rejected: a Postgres
enum (every change is a migration) and an admin-editable categories table
(a feature of its own; revisit only if the env override proves inadequate).

### 2. Optional at creation, explicit unclassified bucket (decided)

`purpose_category` is optional. Requiring it would break every existing API
client, the load suite, and the v1 facade on day one. Reporting carries an
explicit `unclassified` bucket so the gap is visible rather than hidden. An
admin toggle to require it is a later ask, not phase 1.

### 3. One confirmed column now; suggestion storage in phase 2 (decided)

Phase 1 adds `purpose_category`, `purpose_category_set_by`, and
`purpose_category_set_at`. The confirmed value IS `purpose_category`: reporting
counts a row as classified when the column is not null. Phase 2 adds its own
columns for the suggestion (point 9) in its own migration, so phase 1 does
not pre-shape storage for a design that had no ADR yet.

### 4. Mutability: owner or admin, any status (decided)

The owner or an admin can set or clear the category in any reservation
status, including terminal ones. The category is reporting data, not
provisioning state, and retroactive classification is how leadership gets a
clean first quarter. `set_by` and `set_at` record who and when. Rejected:
locking the field at terminal (owners could never classify history) and
admin-only after terminal (splits authority for no invariant).

### 5. No new event (decided)

The category rides every existing `reservation.*` lifecycle payload and the
v1 facade as an additive field. A category edit on its own publishes nothing.
If an external system needs category edits pushed, that is a later ask.

### 6. Device-level reporting covers reserved devices only in phase 1 (decided)

Phase 1 breakdowns are by category at reservation, user, and reserved-device
level, computed inside the reservations service from its own device list.
Transit-gear inheritance (the issue's "every device in the reservation,
including transit gear") needs cabling's fork hops per reservation and moves
to phase 3 with the device rollups. Port-level attribution stays out of
scope by the issue's decision.

### 7. This ADR precedes phase 1 (decided)

The taxonomy and the confirmed-versus-suggested split are invariants every
phase depends on, so the ADR is written before phase 1 ships rather than
before phase 2 as the issue first proposed.

### 8. Two classifier passes: creation and end of reservation (decided)

- Creation pass: interactive. The create-reservation modal calls a
  user-JWT endpoint on the AI orchestrator, `POST /classify-purpose/preview`,
  with the purpose text, the selected topology id, and the dynamic requests;
  the orchestrator adds the generation prompt if the topology was AI-built
  plus the topology's device names, templates, and wiring shape, and returns
  a distribution. The modal prefills the select with the top category and
  shows the percentages; the user may accept or change it. A value the user
  submits is an owner pick (point 10). Gated by `ai_is_configured()` and the
  feature flag (point 11): when either is off the modal is the plain phase 1
  dropdown.
- End-of-reservation pass: background. A new reservations sweep reconciler
  picks a bounded batch per tick of terminal reservations that have no
  suggestion yet (and, for backfill, rows an admin marked eligible), calls
  the orchestrator's internal `POST /internal/classify-purpose` with the
  reservation id and the signals reservations owns (purpose text, devices,
  dynamic requests, duration, terminal status), and stores the returned
  distribution as a suggestion. The orchestrator enriches from its own
  transcripts and generation prompt and from inventory (device names,
  templates, config-apply job names and counts) and cabling (fork version
  count) before calling the model through the `LLMProvider` with a forced
  structured tool call `classify_purpose`. A failed call leaves the row
  without a suggestion and the reconciler retries on a later tick with a
  per-row attempt cap; a rejection or a provider outage never blocks the
  terminal transition. The second pass may revise the first: a creation-pass
  suggestion is replaced by the end-of-reservation one.

### 9. Suggestion storage and states (decided)

Phase 2 adds to reservations: `purpose_suggestion` (JSON: `distribution` as
an ordered list of `{category, probability}`, `pass` as `creation` or `end`,
`model`, `generated_at`, `rationale`), `purpose_suggested_at`, and
`purpose_classify_attempts`. The three states the UI and reporting use are
derived, never stored as a separate column:

- unclassified: `purpose_category` null and no suggestion;
- ai_suggested: `purpose_category` null and a suggestion present;
- confirmed: `purpose_category` not null (set by owner or admin).

Reporting counts confirmed rows in the category totals and reports
ai_suggested rows in their own bucket, labeled with the top suggested
category, never mixed into confirmed totals. A confirmed row that also has a
suggestion keeps the suggestion for the admin surface (agreement rate is a
useful metric) but reports under its confirmed value only.

### 10. Authority: owner sets, admin reviews the AI (decided)

A category an owner picks by hand is confirmed as is. AI suggestions wait in
an admin review surface, `/admin/purpose-review`, grouped by top suggested
category with percentages per reservation; an admin accepts (copies the top
category, or a chosen one, into `purpose_category` with `set_by` the admin)
or dismisses (keeps the row ai_suggested with a dismissed marker so it does
not resurface), and may overrule an owner's pick from the same page. The
page joins the `AdminGuard` route group and the literal list in
`test/routes.test.tsx`. Rejected: admin accepts everything (the queue grows
with every reservation) and owner confirms the AI (weakest gate for the
number leadership will quote).

### 11. Signals: structured data plus assistant transcripts, flagged (decided)

The end-of-reservation classifier reads structured HERD data (purpose text,
generation prompt, device names and templates, wiring shape, config-apply
job names and counts, fork version count, duration, dynamic instances) AND
the reservation assistant transcripts for that reservation. Uploaded
bulk-import file contents stay out. Two flags on the orchestrator, both
enforced at the route boundary like `AI_RECIPE_AUTHORING_ENABLED`:

- `AI_PURPOSE_CLASSIFICATION_ENABLED` (default false): both classify
  endpoints 403 when off; the reservations reconciler treats a 403 as
  "feature off" and skips the tick without counting an attempt.
- `AI_PURPOSE_INCLUDE_TRANSCRIPTS` (default true, per the decision to use
  them): when false the transcript signal is omitted. Privacy note: assistant
  transcripts are user-authored chat already sent to the configured provider
  once; the classifier sends them again, to the same provider, with the
  reservation's own metadata. Deployments that must not resend user text set
  the flag false. The prompt never includes credentials, secrets-service
  values, or device configuration contents; config-apply jobs contribute
  their names and counts only.

## Amendment 2026-09-04 (phase 2 delivery)

Phase 2 shipped on 2026-09-04 across three PRs against a fixed contract:
`services/ai-orchestrator` (PR #688), `services/reservations` (PR #690), and a
same-day reconciler fix (PR #691). It carries six refinements over the
decision points above, posted to issue #646 as the build started and recorded
here without rewriting the original text.

1. **Eligibility is one marker, not two mechanisms.** The decision above
   describes the sweep picking up "terminal reservations that have no
   suggestion yet (and, for backfill, rows an admin marked eligible)" as if
   those were separate paths. As delivered, both are the same column:
   `purpose_classify_requested_at`. Every terminal transition (the five call
   sites: cancel, release, the provision-result failure branch, the
   expiration sweep's auto-complete loop, and its dynamic-timeout backstop)
   stamps it once, idempotently, in the same transaction as the status
   change. The reconciler's eligibility query is simply "requested, not yet
   suggested, under the attempt cap," ordered oldest-requested-first.
   `POST /admin/purpose/backfill` stamps the same column on historical rows
   that predate this feature, so end-of-reservation classification and
   backfill are one mechanism with two ways to become eligible, not two
   mechanisms. Backfill additionally resets `purpose_classify_attempts` to 0
   on any row that is already at `purpose_classify_max_attempts` and still
   has no suggestion (PR #691), so a batch capped out by a transient
   condition (see point 2) gets another run without a manual database edit;
   a row's `purpose_classify_requested_at` is left untouched by that reset,
   so it keeps its place in the oldest-first queue rather than jumping to
   the back.
2. **A 404 from the orchestrator is treated exactly like a 403.** The
   original decision only names 403 (`AI_PURPOSE_CLASSIFICATION_ENABLED` off)
   as the "feature absent, end the tick, do not count an attempt" case. A 404
   means the same thing in practice: the running orchestrator image predates
   the classify endpoint entirely, the situation on a mixed-version
   deployment mid-upgrade or a stack where only reservations was updated. PR
   #691 folded 404 into the same handling as 403 after the original 403-only
   version was found to burn a backfilled row's attempts to the cap within
   three sweep ticks on such a stack. The two cases still log distinguishable
   wording (403: the flag is off there; 404: the endpoint does not exist yet)
   so an operator can tell a flag flip from a stale image, but neither counts
   against `purpose_classify_attempts` and neither is the retryable-failure
   path described elsewhere in this ADR (an unreachable orchestrator, a
   timeout, or any other status code still increments attempts as decided).
3. **Reporting keeps `by_purpose`'s shape and adds a separate list.** Point 9
   above says ai_suggested rows report "in their own bucket... never mixed
   into confirmed totals"; delivered as `by_purpose_suggested`, a list with
   the same three-field shape as `by_purpose` (`purpose_category`,
   `reservations`, `device_hours`) but keyed by the suggestion's
   `top_category`, never appended to `by_purpose` itself. One consequence
   worth stating explicitly since it narrows existing phase 1 behavior:
   `unclassified` in `by_purpose` now excludes any row that carries a
   suggestion, confirmed or not. A row with no confirmed category and no
   suggestion is `unclassified`; a row with no confirmed category and a
   suggestion moves to `by_purpose_suggested` instead. `unclassified` after
   phase 2 means genuinely untouched, not merely unconfirmed. The CSV export
   gained a matching standalone `purpose_suggested` section.
4. **`GET /api/ai/status` gains `purpose_classification`.** Not spelled out
   as its own point in the original decision, but implied by the "gated by
   `ai_is_configured()` and the feature flag" language in point 8: the status
   payload now reports `purpose_classification: bool`, true only when
   `AI_PURPOSE_CLASSIFICATION_ENABLED` is set AND the provider is configured
   and not degraded, so the frontend can gate the create-modal preview call
   and its UI on one field the way it already gates recipe authoring.
5. **The orchestrator stays taxonomy-agnostic; the caller supplies
   categories.** Point 8's example payload for the internal call did not
   originally list `categories`, but the taxonomy lives in the reservations
   service (point 1), and the orchestrator holding its own copy would be a
   second source of truth. Both endpoints take a required, non-empty
   `categories` list from the caller instead, and both responses add
   `signals_used`, the subset of the signal table in point 11 that actually
   made it into the prompt for that call (a signal that failed to fetch is
   simply absent, never a reason to fail the request). Neither field changes
   the decision; both make the contract between the two services explicit.
6. **Suggestion state is not on the v1 facade in this phase.** Point 5 says
   the v1 facade gets the confirmed category additively; it says nothing
   about suggestions. As delivered, `purpose_suggestion`,
   `purpose_suggested_at`, and `purpose_suggestion_dismissed_at` are additive
   fields on the reservations service's own `ReservationResponse` only. They
   are review state for an admin surface, not a fact external integrations
   need, so `docs/api/v1-openapi.json` and its contract snapshot are
   untouched by phase 2. Revisit if an external consumer ever needs
   suggestion visibility.

Storage, migration 0016 (`services/reservations`): `purpose_suggestion`
(JSON, `none_as_null=True` so an absent suggestion reads as SQL `NULL` rather
than the JSON literal `"null"`; the reconciler's and review queue's
`is_(None)`/`is_not(None)` filters depend on this), `purpose_suggested_at`,
`purpose_classify_requested_at` (indexed), `purpose_classify_attempts`
(default 0), and `purpose_suggestion_dismissed_at`.

## Delivery phases

1. Phase 1, delivered (schema, API, manual dropdown, reporting): the three columns and
   migration; settings and write-time validation with the pinned 422 wording
   `Unknown purpose_category '<value>'; allowed: <list>`; `GET
   /purpose-categories`; `purpose_category` on create; `PATCH
   /{id}/purpose-category` (owner or admin, any status); the additive field on
   every lifecycle payload and on the v1 facade (hand-updated
   `docs/api/v1-openapi.json` plus contract snapshots); `by_purpose`,
   `by_user_purpose`, and `by_device_purpose` on the utilization report and
   its CSV, with the `unclassified` bucket; the select in the create modal
   and the inline edit in the detail modal; the Purpose section on the
   reporting page. Delivered by two PRs, backend and frontend, against a
   fixed contract, then a live check on the gate stack.
2. Phase 2, delivered 2026-09-04 (PRs #688, #690, #691; see the amendment
   above for the refinements over this outline): the suggestion columns;
   the orchestrator's `classify_purpose` tool and two endpoints behind the
   flags; the create-modal preview prefill; the reservations sweep
   reconciler; the admin review page with accept, override, and dismiss;
   the `Classify history` admin action, `POST /admin/purpose/backfill`, that
   marks terminal rows without a suggestion as eligible for the same
   reconciler (bounded, idempotent, pausable by turning the flag off); the
   ai_suggested bucket in reporting.
3. Phase 3, remaining (device rollups): transit-gear inheritance through
   cabling's fork hops so device-level breakdowns include the switches and
   routers a reservation consumed on its paths; a per-device category mix
   that answers which devices are the support-replication workhorses.

## Testing

Phase 1 pins, all required before merge:

- Unit (SQLite, reservations): create with a valid and an unknown category
  (exact 422 wording), PATCH by owner, by admin, by a third user (403), PATCH
  on a COMPLETED reservation, PATCH null clears `set_by` and `set_at`, the
  categories endpoint reflects an env override, report breakdowns with a
  mix of classified and unclassified rows including the user and device
  mixes, CSV content. Integration service unit tests for the facade field.
- Integration (stack): create with a category through the gateway, read
  back, PATCH, and the report's `by_purpose` includes it; the lifecycle
  payload carries the field where an existing test already inspects
  payloads.
- Contract: reservations, integration, and v1 snapshots regenerated.
- Frontend (vitest): categories hook labels and humanizes; create modal sends
  null by default and the chosen value when picked, and stays submittable
  while categories load; detail modal shows the select for owner and admin
  and hides it for a third user, reverting with a toast on failure; the
  reporting section renders from a mocked report including `unclassified`
  and hides cleanly when `by_purpose` is absent.
- Live: the frontend live-gate recipe against the gate stack once the
  backend PR is deployed there, and the e2e tests that open the create and
  detail modals re-read for impact (e2e does not run in per-PR CI).

Phase 2 pins, as delivered:

- Unit (SQLite, ai-orchestrator): `test_purpose_classifier.py` (tool schema,
  normalization edge cases, retry-then-pinned-502), `test_purpose_signals.py`
  (prompt assembly per pass, the transcript flag on and off, tool-role
  exclusion, signal-fetch-failure tolerance, the no-config-leak assertion),
  `test_purpose_classification_routes.py` (gating order with the flag checked
  before auth/config, the happy path for both passes, the forced-tool-call
  assertion, the 502 wording, usage metering), plus the additive
  `purpose_classification` status field. Unit (SQLite, inventory):
  `test_apply_jobs_internal_summary.py` for the new
  `GET /devices/{id}/apply-jobs/internal` signal endpoint.
- Unit (SQLite, reservations): `test_purpose_classify_marker.py` (all five
  terminal-transition sites stamp the marker, idempotently);
  `test_purpose_classify_reconcile.py` (a 200 stores a suggestion, a 403 or a
  404 ends the tick without touching any row including a later row in the
  same batch, a 5xx or a transport error increments attempts, a row at the
  cap or with no marker is skipped without a call, batch size and
  oldest-first ordering are respected, backfill resets capped unsuggested
  rows and counts them alongside newly marked rows in one total);
  `test_purpose_review.py` (the review-list filter, accept with null and a
  chosen override, dismiss, 404 and 409 on both, admin-only 403 on all four
  endpoints); `test_reporting_service.py`'s
  `test_build_report_by_purpose_suggested_split` and a CSV section test for
  the three-way confirmed/ai_suggested/unclassified split.
- Contract: `tests/contract/snapshots/ai.json`, `inventory.json`, and
  `reservations.json` regenerated (additive paths and schemas only);
  `docs/api/v1-openapi.json` and its snapshot are untouched, since suggestion
  state is not on the v1 facade in this phase (refinement 6 above).
- Integration (stack): `tests/integration/test_purpose_review_flow.py`, an
  AI-gated test (`seeded_skip_ok`, since nightly carries no `AI_*` env) that
  creates and cancels a reservation and polls the review queue for a
  suggestion, plus ungated backfill-idempotency and 403 tests.
- Frontend (vitest): the preview call gated on `useAIStatus().purpose_classification`,
  debounced, prefilling only an untouched select, failing silently; the
  detail modal's suggestion line and dismissed marker; the admin page's
  grouping, category filter, accept (top and as-a-chosen-category), dismiss
  with optimistic removal and revert-on-error, the backfill toast, the empty
  state, and admin-gated route membership in the routes test; the reporting
  page's suggested bars and legend.
- E2E: `tests/e2e/test_admin_guard_redirect_playwright.py` carries
  `/admin/purpose-review` in its guarded-path list.

## Out of scope

- Port-level attribution (a core switch has hundreds of ports and no
  question needs per-port task tracking).
- Free-text categories, and any user-editable taxonomy.
- Any automatic write of an AI suggestion into `purpose_category`.
- Uploaded file contents as a classifier signal.
- A dedicated event for category edits.
