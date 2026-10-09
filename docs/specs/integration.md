# Integration specification

| | |
|---|---|
| Area prefix | `INTEG` (used in rule identifiers, for example `INTEG-HOOK-1`) |
| Verified at | commit `cd3eaeed` (`v0.6.0-215-gcd3eaeed`), 2026-10-08 |
| Owning services | integration (`services/integration/`: the `/api/v1` facade, webhook subscriptions, the webhook delivery consumer); notifications (`services/notifications/`: the notification consumer, in-app notifications, the preferences proxy, the outbound channels); the bell and the Settings page in `frontend/` |
| Other services involved | reservations (every facade call, the lifecycle events, the active-holder lookup), execution (the device health events), auth (the admin list and user contact lookups, API token exchange), user-profile (stored notification preferences), NATS JetStream, external webhook receivers, an SMTP server, a chat incoming webhook |
| Design records | none; the facade and webhooks shipped under issue #33, the health webhooks under issue #831, the notification channels under issue #40 (see `FEATURES.md`) |
| Related guides | [EXTERNAL_API.md](../EXTERNAL_API.md), [api/v1-openapi.json](../api/v1-openapi.json), [ARCHITECTURE.md](../ARCHITECTURE.md) (NATS JetStream, Versioned `/api/v1` reservation facade, Outbound webhook consumer), [USER_GUIDE.md](../USER_GUIDE.md), [ENV_VARS.md](../ENV_VARS.md), [OPERATIONS.md](../OPERATIONS.md) (inspecting the NATS DLQ), [ROLES.md](../ROLES.md) |

This document depends on four others. Every reservation rule the facade reaches (who may
create, list, view, cancel, release, and read wiring status, and every error those routes
answer) is specified in `reservations.md` (section 5, rules RES-CREATE-1 to RES-CREATE-18,
RES-LIST-1 to RES-LIST-9, RES-VIEW-1, RES-VIEW-2, RES-CANCEL-1 to RES-CANCEL-5,
RES-RELEASE-1 to RES-RELEASE-4, RES-FORK-4, RES-FORK-18); the facade adds only what it
does to the request and the answer. The lifecycle events this area consumes, their
payload keys, and when they are staged are owned by `reservations.md` (section 6, rule
RES-EVENT-1). Token verification and the admin gate are the shared rules IAM-CLAIM-1 to
IAM-CLAIM-3 and the machine-token exchange is IAM-APITOK-1 to IAM-APITOK-11, all in
`identity-and-access.md`. The shared JetStream transport (the outbox relay, the
`ensure_consumer` and `ensure_stream_exists` helpers, the heartbeat helper, the NAK delay
schedule, the `HERD_DLQ` stream, and the health events execution publishes) belongs to
`operations-and-observability.md`; this document states only how the two consumers here
are configured and what they do with each message.

## 1. Purpose

Two audiences outside the web interface need HERD's reservations. Automation (CI
pipelines, test harnesses) reserves and releases devices through a small, frozen HTTP
API under `/api/v1`, and external systems learn about reservation and device health
changes from signed webhooks instead of polling. People learn about the same changes
from a notification bell in the app and, if they opt in, by email, a shared chat
channel, or an instance-level webhook. This area does not let automation reach any
reservation feature beyond create, list, view, cancel, release, and wiring status, does
not offer bidirectional chat, and keeps no per-user channel credentials.

## 2. Actors and permissions

The endpoint matrix is in [ROLES.md](../ROLES.md); it does not yet list this area's
routes (#1080). Rules beyond role are numbered in section 8.

| Actor | May | May not |
|---|---|---|
| User | Call every `/api/v1/reservations` route with their own access token or an exchanged API token (INTEG-FACADE-1); read, mark read, and delete their own notifications (INTEG-INAPP-2); read and change their own notification preferences (INTEG-PREFS-1) | Register, read, or delete a webhook (INTEG-HOOK-1); see another user's notifications; reach any reservation route the facade does not expose (INTEG-FACADE-11) |
| Admin | Everything a user may; register, list, read, pause, resume, and delete webhook subscriptions and read their delivery ledgers (INTEG-HOOK-1, INTEG-HOOK-9); receive every device health notification (INTEG-HEALTH-1) | Change a subscription's URL, event names, or secret (INTEG-HOOK-9) |
| Superadmin | Same as admin | Same as admin |
| Another service (internal token) | Nothing: neither service serves an internal route | |
| External receiver | Receive signed POSTs for the event names a subscription lists (INTEG-HOOK-11, INTEG-HOOK-12) | Call anything back; the delivery is one-way |

## 3. Concepts and data

| Concept | Meaning | Owner | Stored in |
|---|---|---|---|
| v1 facade | The routes under `/api/v1/reservations`, a thin translation in front of reservations with its own frozen request and response models | integration | no storage; `V1ReservationRequest`, `V1ReservationResponse`, `V1ReservationList` in `services/integration/app/schemas/reservation.py` |
| Contract version | The fixed `1.0.0` in the facade's OpenAPI `info.version`; not the product version | integration | `services/integration/app/main.py` |
| Published contract | The hand-maintained OpenAPI document external clients read | integration | `docs/api/v1-openapi.json` |
| Contract snapshot | The method, path, and schema-signature record the contract test compares against | tests | `tests/contract/snapshots/v1.json` |
| Webhook subscription | An admin-registered target URL, the event names it wants, a shared HMAC secret, an `is_active` flag, and the creator's user id (bare, no foreign key) | integration | `webhook_subscriptions` (`WebhookSubscription` in `services/integration/app/models/webhook.py`) |
| Delivery ledger row | One row per (subscription, source event): status, attempts, last response status, last error text, timestamps. It is the redelivery dedupe record and the dead-letter record | integration | `webhook_deliveries` (`WebhookDelivery`), unique on `(subscription_id, event_id)`, foreign key to the subscription with `ON DELETE CASCADE` |
| Event id | The producer-stamped `event_id` in an event payload, else the JetStream `<stream>:<sequence>`; the idempotency key both services use | the producing service; read here | the event payload |
| Notification | One in-app message for one user: event name, title, body, a copy of the event payload, read time | notifications | `notifications` (`Notification` in `services/notifications/app/models/notification.py`), partial unique on `(user_id, dedupe_key)` where the key is not null |
| Outbound claim | A record that an email, chat, or webhook-channel send for (channel, user, event id) was claimed | notifications | `outbound_deliveries` (`OutboundDelivery` in `services/notifications/app/models/outbound_delivery.py`) |
| Notification preferences | Per-user channel toggles (`in_app`, `email`, `chat`, `webhook`) and per-event toggles | user-profile (stored), notifications (defaults and merge) | user-profile `extras.notifications` |

## 4. State model

Two small state machines live in this area. The rules the tables cite are in section 8.

**Statuses of a delivery ledger row.**

- `delivered`: a POST for this (subscription, event) answered 2xx. Never written again.
- `dead`: every attempt of the last delivery failed.
- `failed`: the destination was not allowed when the delivery was due (INTEG-HOOK-23);
  nothing was POSTed.

| From | To | Performed by | Guard | Stages | Rule |
|---|---|---|---|---|---|
| (none) | `delivered` | webhook consumer (`deliver_one`) | an attempt answered 2xx | nothing | INTEG-HOOK-13 |
| (none) | `dead` | webhook consumer (`deliver_one`) | every attempt failed | nothing | INTEG-HOOK-14 |
| (none) | `failed` | webhook consumer (`deliver_one`) | the destination is not allowed | nothing | INTEG-HOOK-23 |
| `dead` or `failed` | `delivered`, `dead`, or `failed` | a redelivered or republished event (`deliver_one`) | none | nothing | INTEG-HOOK-15, INTEG-HOOK-23 |
| `delivered` | anything | nothing | none | nothing | INTEG-HOOK-17 |

**Statuses of a notification.** Unread (`read_at` null) and read (`read_at` set).

| From | To | Performed by | Guard | Stages | Rule |
|---|---|---|---|---|---|
| (none) | unread | in-app dispatch | no row for (user, event id) | nothing | INTEG-INAPP-1 |
| unread | read | `PATCH /notifications/{id}/read`, `POST /notifications/read-all` | the caller owns the row | nothing | INTEG-INAPP-6, INTEG-INAPP-8 |
| read | read, `read_at` unchanged | `PATCH /notifications/{id}/read` | the caller owns the row | nothing | INTEG-INAPP-7 |
| unread or read | deleted | `DELETE /notifications/{id}` | the caller owns the row | nothing | INTEG-INAPP-9 |

**Concurrency.** The unique indexes are the arbiters. A second ledger insert for the same
(subscription, event) loses on `uq_webhook_delivery_subscription_event` (INTEG-HOOK-16);
a second notification for the same (user, event id) loses on
`uq_notifications_user_dedupe` (INTEG-INAPP-1); a second outbound claim loses on
`uq_outbound_channel_user_dedupe` (INTEG-OUT-3). No row lock is taken.

## 5. API surface

Integration's routes are served under `/api/v1` (Traefik strips the prefix);
notifications' routes under `/api/notifications`. Paths below are as the gateway sees
them.

| Method | Path | Who may call | Success | Rules |
|---|---|---|---|---|
| POST | `/api/v1/reservations` | any valid access token; reservations decides the rest | 201 | INTEG-FACADE-1 to INTEG-FACADE-5, INTEG-FACADE-12 to INTEG-FACADE-14 |
| GET | `/api/v1/reservations` | any valid access token | 200 | INTEG-FACADE-1, INTEG-FACADE-2, INTEG-FACADE-5 to INTEG-FACADE-7, INTEG-FACADE-12 to INTEG-FACADE-14 |
| GET | `/api/v1/reservations/{reservation_id}` | any valid access token | 200 | INTEG-FACADE-1, INTEG-FACADE-2, INTEG-FACADE-5, INTEG-FACADE-12 to INTEG-FACADE-15 |
| DELETE | `/api/v1/reservations/{reservation_id}` | any valid access token | 204 | INTEG-FACADE-1, INTEG-FACADE-2, INTEG-FACADE-8, INTEG-FACADE-12 to INTEG-FACADE-15 |
| PUT | `/api/v1/reservations/{reservation_id}/release` | any valid access token | 200 | INTEG-FACADE-1, INTEG-FACADE-2, INTEG-FACADE-5, INTEG-FACADE-9, INTEG-FACADE-12 to INTEG-FACADE-15 |
| GET | `/api/v1/reservations/{reservation_id}/wiring-status` | any valid access token | 200 | INTEG-FACADE-1, INTEG-FACADE-2, INTEG-FACADE-10, INTEG-FACADE-12 to INTEG-FACADE-15 |
| POST | `/api/v1/webhooks` | admin or superadmin | 201 | INTEG-HOOK-1 to INTEG-HOOK-5 |
| GET | `/api/v1/webhooks` | admin or superadmin | 200 | INTEG-HOOK-1, INTEG-HOOK-6 |
| GET | `/api/v1/webhooks/{webhook_id}` | admin or superadmin | 200 | INTEG-HOOK-1, INTEG-HOOK-6 |
| PATCH | `/api/v1/webhooks/{webhook_id}` | admin or superadmin | 200 | INTEG-HOOK-1, INTEG-HOOK-9 |
| DELETE | `/api/v1/webhooks/{webhook_id}` | admin or superadmin | 204 | INTEG-HOOK-1, INTEG-HOOK-7, INTEG-HOOK-8 |
| GET | `/api/v1/webhooks/{webhook_id}/deliveries` | admin or superadmin | 200 | INTEG-HOOK-1, INTEG-HOOK-10 |
| GET | `/api/v1/version` | anyone | 200 | INTEG-VERSION-2 |
| GET | `/api/v1/health` | anyone | 200 | INTEG-VERSION-3 |
| POST | `/api/v1/webhooks/echo` | anyone, only when the test sink is enabled | 200 | INTEG-SINK-1, INTEG-SINK-2 |
| GET | `/api/v1/webhooks/echo/hits?event_id` | anyone, only when the test sink is enabled | 200 | INTEG-SINK-1, INTEG-SINK-3 |
| GET | `/api/notifications/notifications?limit&offset&unread_only` | any valid access token, own rows | 200 | INTEG-INAPP-2 to INTEG-INAPP-4 |
| GET | `/api/notifications/notifications/unread-count` | any valid access token, own rows | 200 | INTEG-INAPP-2, INTEG-INAPP-5 |
| PATCH | `/api/notifications/notifications/{notification_id}/read` | the row's owner | 200 | INTEG-INAPP-2, INTEG-INAPP-6, INTEG-INAPP-7 |
| POST | `/api/notifications/notifications/read-all` | any valid access token, own rows | 200 | INTEG-INAPP-2, INTEG-INAPP-8 |
| DELETE | `/api/notifications/notifications/{notification_id}` | the row's owner | 204 | INTEG-INAPP-2, INTEG-INAPP-9 |
| GET | `/api/notifications/notifications/preferences` | any valid access token, own preferences | 200 | INTEG-PREFS-1 to INTEG-PREFS-3 |
| PUT | `/api/notifications/notifications/preferences` | any valid access token, own preferences | 200 | INTEG-PREFS-3 to INTEG-PREFS-5 |

## 6. Events

This area publishes no NATS event. A webhook delivery is an outbound HTTP POST, not an
event (section 8.3). Dead-letter copies go to the four subjects below, retained by the
`HERD_DLQ` stream that execution creates (`provisioning-and-wiring.md`, WIRE-CONSUME-13).

| Subject | Producer | Staged when | Consumers | Payload keys | Rules |
|---|---|---|---|---|---|
| `herd.reservations.dlq.integration` | integration | a reservations-stream message is poison or exhausted its deliveries | none (operator inspection) | the original message bytes | INTEG-CONSUME-7, INTEG-CONSUME-8 |
| `herd.health.dlq.integration` | integration | the same, on the health stream | none | the original message bytes | INTEG-CONSUME-7, INTEG-CONSUME-8 |
| `herd.reservations.dlq.notifications` | notifications | the same, reservations stream | none | the original message bytes | INTEG-NCONSUME-7, INTEG-NCONSUME-8 |
| `herd.health.dlq.notifications` | notifications | the same, health stream | none | the original message bytes | INTEG-NCONSUME-7, INTEG-NCONSUME-8 |

### Events consumed

| Subject | Published by | Consumer | What this area does | Rules |
|---|---|---|---|---|
| `herd.reservations.*` (every reservation event) | reservations | integration (`integration-webhooks-consumer`) | POSTs the message to every active subscription listing its `event` name | INTEG-CONSUME-1, INTEG-HOOK-11 to INTEG-HOOK-22 |
| `herd.health.status_changed` (`device.health_transition`) | execution | integration (`integration-webhooks-health-consumer`) | the same | INTEG-CONSUME-1, INTEG-HOOK-11 |
| `herd.reservations.created`, `updated`, `cancelled`, `completed`, `failed`, `expiring_soon` | reservations | notifications (`notifications-consumer`) | one notification to the reservation's owner, on each channel they enabled | INTEG-ROUTE-1, INTEG-ROUTE-3 to INTEG-ROUTE-6, INTEG-PREFS-9 |
| `herd.reservations.provision_requested`, `wiring_changed` | reservations | notifications | nothing; acked | INTEG-ROUTE-2 |
| `herd.health.status_changed` (`device.health_transition`) | execution | notifications (`notifications-health-consumer`) | one notification to every admin and every active holder of the device | INTEG-HEALTH-1 to INTEG-HEALTH-7 |

Only the seven event names in `KNOWN_EVENT_TYPES` can be subscribed (INTEG-HOOK-3), so a
`provision_requested` or `wiring_changed` message reaches the webhook consumer and is
delivered to no one.

## 7. Internal API

None. Neither service serves a route to another service. The routes they call are in
section 10.

## 8. Features

### 8.1 Versioned reservation facade

**What it does.** A pipeline holding an access token (a user's, or one exchanged from an
API token) can reserve devices, list and read its reservations, cancel or release one,
and read what wiring HERD applied, through a small API whose shape does not change when
the web interface's API does.

**Surfaces.** The six `/api/v1/reservations` routes (section 5),
`services/integration/app/routers/reservations.py`; the frozen models in
`services/integration/app/schemas/reservation.py`; the published contract
`docs/api/v1-openapi.json`.

**Rules.**

- **INTEG-FACADE-1.** Every facade route needs a bearer access token verified locally
  (IAM-CLAIM-1 in `identity-and-access.md`); no header answers 401 `Not authenticated`
  with `WWW-Authenticate: Bearer`, an invalid token 401. The facade checks no role. \
  Enforced in: `services/integration/app/routers/reservations.py` (`get_current_user_payload`, `bearer_scheme`) \
  Pinned by: `services/integration/tests/test_facade.py` (`test_missing_jwt_is_rejected`, `test_invalid_jwt_is_401`, `test_wiring_status_requires_jwt`); `tests/integration/test_v1_facade.py` (`test_v1_requires_authentication`)
- **INTEG-FACADE-2.** The facade forwards the caller's own bearer token to reservations,
  never an internal token, so ownership, visibility, and every reservation rule are
  decided there as for an interactive user; an exchanged API token works the same way. \
  Enforced in: `services/integration/app/routers/reservations.py` (`_forward`) \
  Pinned by: `services/integration/tests/test_facade.py` (`test_create_forwards_jwt_and_translated_body`); `tests/integration/test_v1_facade.py` (`test_v1_reserve_status_list_release`, `test_machine_principal_reserves_via_token_exchange`)
- **INTEG-FACADE-3.** A create sends reservations exactly `device_ids`, `start_time`,
  `end_time`, `purpose`, `topology_id`, and `purpose_category` (null when omitted), and
  answers 201 with the v1 view of the created reservation. \
  Enforced in: `services/integration/app/routers/reservations.py` (`create_reservation`) \
  Pinned by: `services/integration/tests/test_facade.py` (`test_create_forwards_jwt_and_translated_body`, `test_create_forwards_and_returns_purpose_category`, `test_create_purpose_category_defaults_to_none`)
- **INTEG-FACADE-4.** The facade validates its own request body before any upstream call:
  `device_ids` holds 1 to 200 UUIDs, `start_time` and `end_time` are required datetimes,
  `purpose` is at most 2000 characters; a failure is 422 in FastAPI's
  `{"detail": [{type, loc, msg, input}]}` envelope. \
  Enforced in: `services/integration/app/schemas/reservation.py` (`V1ReservationRequest`) \
  Pinned by: `services/integration/tests/test_facade.py` (`test_validation_error_envelope_shape`)
- **INTEG-FACADE-5.** Every reservation in a facade answer carries exactly `id`, `status`
  (a plain string, not an enumeration), `device_ids`, `topology_id`, `start_time`,
  `end_time`, `created_at`, and `purpose_category`; every other field reservations
  returns is dropped. \
  Enforced in: `services/integration/app/routers/reservations.py` (`_to_v1`); `services/integration/app/schemas/reservation.py` (`V1ReservationResponse`) \
  Pinned by: `services/integration/tests/test_facade.py` (`test_list_maps_to_v1_paginated`, `test_get_status_maps`)
- **INTEG-FACADE-6.** The list forwards only `skip` (at least 0, default 0) and `limit`
  (1 to 500, default 50); any other query parameter, including every list filter
  reservations accepts, is ignored. By decision; see issue #959 and the docstring of
  `test_list_does_not_pass_through_internal_filters`. \
  Enforced in: `services/integration/app/routers/reservations.py` (`list_reservations`) \
  Pinned by: `services/integration/tests/test_facade.py` (`test_list_does_not_pass_through_internal_filters`)
- **INTEG-FACADE-7.** The list answers `{items, total, skip, limit}` with `total`,
  `skip`, and `limit` taken from reservations' answer, falling back to the caller's own
  `skip` and `limit` when reservations omits them. \
  Enforced in: `services/integration/app/routers/reservations.py` (`list_reservations`); `services/integration/app/schemas/reservation.py` (`V1ReservationList`) \
  Pinned by: `services/integration/tests/test_facade.py` (`test_list_maps_to_v1_paginated`, `test_list_does_not_pass_through_internal_filters`)
- **INTEG-FACADE-8.** A cancel forwards to reservations' `DELETE /{id}` and answers 204
  with no body. \
  Enforced in: `services/integration/app/routers/reservations.py` (`cancel_reservation`) \
  Pinned by: `services/integration/tests/test_facade.py` (`test_cancel_returns_204`)
- **INTEG-FACADE-9.** A release forwards to reservations' `PUT /{id}/release` and answers
  the v1 view of the released reservation. \
  Enforced in: `services/integration/app/routers/reservations.py` (`release_reservation`) \
  Pinned by: `services/integration/tests/test_facade.py` (`test_release_maps`); `tests/integration/test_v1_facade.py` (`test_v1_reserve_status_list_release`)
- **INTEG-FACADE-10.** The wiring status answer is relayed verbatim, not frozen into a
  v1 model, because its shape is owned by reservations and execution and grows
  additively. By decision; see the docstring of `get_reservation_wiring_status`. \
  Enforced in: `services/integration/app/routers/reservations.py` (`get_reservation_wiring_status`) \
  Pinned by: `services/integration/tests/test_facade.py` (`test_wiring_status_relays_layered_payload_verbatim`)
- **INTEG-FACADE-11.** The facade exposes no other reservation route: no edit, no
  purpose-category change, no fork, no dynamic requests, and no manual wiring retry. By
  decision for the retry; see the docstring of `get_reservation_wiring_status`. \
  Enforced in: `services/integration/app/main.py` (`reservations_router`) \
  Pinned by: `tests/contract/test_openapi_schema.py` (`test_openapi_signature_matches_snapshot`)
- **INTEG-FACADE-12.** An upstream answer that is not 2xx is answered with the same
  status, and its `detail` is the upstream JSON `detail` when the body is an object that
  has one. \
  Enforced in: `services/integration/app/routers/reservations.py` (`_propagate_if_error`) \
  Pinned by: `services/integration/tests/test_facade.py` (`test_upstream_error_is_propagated`); `tests/integration/test_v1_facade.py` (`test_v1_propagates_not_found`)
- **INTEG-FACADE-13.** When the upstream error body is JSON without a `detail` key, the
  whole body becomes the `detail`; when it is not JSON, the raw text does, or `Upstream
  error` when the text is empty. \
  Enforced in: `services/integration/app/routers/reservations.py` (`_propagate_if_error`) \
  Pinned by: none
- **INTEG-FACADE-14.** A transport error or a 10 second timeout reaching reservations
  answers 503 `Reservations service unavailable`, fail closed. \
  Enforced in: `services/integration/app/routers/reservations.py` (`_forward`, `UPSTREAM_TIMEOUT`) \
  Pinned by: `services/integration/tests/test_facade.py` (`test_upstream_unreachable_is_503`)
- **INTEG-FACADE-15.** The `{reservation_id}` path segment must be a UUID: anything else
  is FastAPI's 422 (`uuid_parsing` at `["path", "reservation_id"]`) and no upstream call
  is made, so a percent-encoded `?` or a dot segment can never change the upstream route.
  A valid id is forwarded in its canonical lower-case hyphenated form (#1105). \
  Enforced in: `services/integration/app/routers/reservations.py` (`get_reservation`, `cancel_reservation`, `release_reservation`, `get_reservation_wiring_status`) \
  Pinned by: `services/integration/tests/test_facade.py` (`test_reservation_id_must_be_a_uuid`, `test_reservation_id_is_forwarded_in_canonical_form`)

**Out of scope.** Token minting and exchange (`identity-and-access.md`); every rule about
which reservation a caller may act on (`reservations.md`).

### 8.2 Contract version and published contract

**What it does.** External clients read a fixed contract version and a published OpenAPI
document; the running product version is available separately without entering that
contract.

**Surfaces.** `services/integration/app/main.py`; `docs/api/v1-openapi.json`;
`tests/contract/snapshots/v1.json`.

**Rules.**

- **INTEG-VERSION-1.** The facade's OpenAPI `info.version` is the fixed contract version
  `1.0.0`, never the product version. \
  Enforced in: `services/integration/app/main.py` (`app`) \
  Pinned by: `services/integration/tests/test_version.py` (`test_facade_contract_version_untouched`)
- **INTEG-VERSION-2.** `GET /version` answers 200 with `service`, `version`, `build`, and
  `build_date` (`build` is `dev` when unset), and the route is left out of the OpenAPI
  document. By decision (issue #846). \
  Enforced in: `services/integration/app/main.py` (`add_version_route`) \
  Pinned by: `services/integration/tests/test_version.py` (`test_version_answers_200`, `test_version_build_unset_is_dev`, `test_version_absent_from_openapi_paths`)
- **INTEG-VERSION-3.** The contract test compares the running service's method and path
  pairs and its schema signatures (type, required fields, property types) with the
  snapshot, so a removed route or field fails it; query parameters are not recorded, so
  a parameter change does not. `GET /health` is part of the contract. \
  Enforced in: `tests/contract/test_openapi_schema.py` (`_signature`, `_schema_signature`) \
  Pinned by: `tests/contract/test_openapi_schema.py` (`test_openapi_signature_matches_snapshot`)
- **INTEG-VERSION-4.** The published contract is maintained by hand and nothing compares
  it with the running service. At this commit its paths and schemas equal the service's
  generated document exactly; only `info.title` and `info.description` differ. \
  Enforced in: `docs/api/v1-openapi.json` (`HERD External API`) \
  Pinned by: none

**Out of scope.** The deprecation policy for a future `/api/v2` is prose in
[EXTERNAL_API.md](../EXTERNAL_API.md) (Versioning and deprecation policy); no code
enforces it.

### 8.3 Webhook subscriptions

**What it does.** An admin registers an HTTP endpoint and the event names it wants, gets
back a signing secret once, and can list, read, pause, resume, and delete subscriptions
and read each one's delivery history.

**Surfaces.** The six `/api/v1/webhooks` routes (section 5),
`services/integration/app/routers/webhooks.py`; validation in
`services/integration/app/schemas/webhook.py`.

**Rules.**

- **INTEG-HOOK-1.** Every subscription route requires a `role` claim of `admin` or
  `superadmin` (IAM-CLAIM-2 in `identity-and-access.md`); any other role is 403. \
  Enforced in: `services/integration/app/routers/webhooks.py` (`require_admin`) \
  Pinned by: `services/integration/tests/test_webhooks.py` (`test_create_requires_admin`); `tests/integration/test_webhooks_flow.py` (`test_webhooks_require_admin`)
- **INTEG-HOOK-2.** `target_url` is 1 to 2048 characters and must start with `http://`
  or `https://`, else 422 (validation envelope). Its host must then resolve to public
  addresses only: every A and AAAA answer must pass `is_public_address` (loopback,
  link-local, RFC 1918, shared address space 100.64.0.0/10, IPv6 unique-local,
  multicast, unspecified, reserved, and IPv4-mapped, 6to4, and Teredo forms are
  refused), an IP literal host is judged as written, and a host that does not resolve
  or answers nothing is refused (fail closed). `WEBHOOK_ALLOWED_HOSTS` admits named
  internal destinations: a hostname entry matching the URL host exactly
  (case-insensitive, trailing dot ignored) is admitted without resolving, and an address
  inside a CIDR entry counts as allowed. A refusal is 422 `target_url must resolve to a
  public address` and nothing is stored. The check and the later connection resolve DNS
  separately (the limit AI-DOCS-11 shares). \
  Enforced in: `services/integration/app/schemas/webhook.py` (`WebhookCreate`, `_validate_url`); `services/integration/app/routers/webhooks.py` (`create_webhook`); `services/integration/app/services/destination.py` (`destination_allowed`, `destination_allowed_sync`, `parse_allowed_hosts`, `TARGET_NOT_PUBLIC_DETAIL`); `services/common/herd_common/public_address.py` (`is_public_address`, `default_resolver`) \
  Pinned by: `services/integration/tests/test_webhooks.py` (`test_create_rejects_non_http_url`); `services/integration/tests/test_webhook_destinations.py` (`test_webhook_target_must_be_public`, `test_webhook_target_public_address_is_accepted`, `test_webhook_target_refused_when_any_answer_is_not_public`, `test_webhook_target_refused_when_host_does_not_resolve`, `test_webhook_target_ip_literal_is_judged_without_resolving`, `test_allowed_hosts_admits_a_named_host_without_resolving`, `test_allowed_hosts_name_match_is_exact`, `test_allowed_hosts_admits_addresses_inside_a_cidr`, `test_allowed_hosts_cidr_does_not_admit_other_private_addresses`, `test_malformed_cidr_refuses_to_boot`); `services/common/tests/test_public_address.py` (`test_refused_address_classes`); `tests/integration/test_webhooks_flow.py` (`test_webhook_target_must_be_public`)
- **INTEG-HOOK-3.** `event_types` is a non-empty list whose every entry is one of the
  seven names in `KNOWN_EVENT_TYPES` (the six reservation names `created`, `updated`,
  `cancelled`, `completed`, `failed`, `expiring_soon` under `reservation.`, and
  `device.health_transition`); an unknown name is 422 naming it and the allowed set. \
  Enforced in: `services/integration/app/schemas/webhook.py` (`KNOWN_EVENT_TYPES`, `_validate_events`) \
  Pinned by: `services/integration/tests/test_webhooks.py` (`test_create_rejects_unknown_event_type`, `test_known_event_types_are_the_documented_seven`); `tests/integration/test_webhooks_flow.py` (`test_unknown_event_type_rejected`, `test_device_health_transition_event_type_accepted`)
- **INTEG-HOOK-4.** Repeated names in `event_types` are stored once, in first-seen order. \
  Enforced in: `services/integration/app/schemas/webhook.py` (`_validate_events`) \
  Pinned by: none
- **INTEG-HOOK-5.** `secret` is optional and at most 512 characters; when omitted a
  random one is generated (`secrets.token_urlsafe(32)`). The 201 answer is the only one
  that carries the secret, and `created_by` is the caller's `sub` when it is a UUID,
  else null. \
  Enforced in: `services/integration/app/routers/webhooks.py` (`create_webhook`, `_principal_id`); `services/integration/app/schemas/webhook.py` (`WebhookCreated`) \
  Pinned by: `services/integration/tests/test_webhooks.py` (`test_create_generates_secret_when_omitted`, `test_create_accepts_explicit_secret`); `services/integration/tests/test_webhooks_router_direct.py` (`test_create_webhook_direct_sets_created_by_from_sub`, `test_principal_id_none_on_malformed_sub`)
- **INTEG-HOOK-6.** The list (newest first) and the single read never carry the secret;
  an unknown id is 404 `Webhook not found`. \
  Enforced in: `services/integration/app/routers/webhooks.py` (`list_webhooks`, `get_webhook`); `services/integration/app/schemas/webhook.py` (`WebhookResponse`) \
  Pinned by: `services/integration/tests/test_webhooks.py` (`test_list_omits_secret_and_get_404`); `services/integration/tests/test_webhooks_router_direct.py` (`test_list_webhooks_direct_orders_newest_first`)
- **INTEG-HOOK-7.** A delete answers 204; an unknown id is 404 `Webhook not found`. \
  Enforced in: `services/integration/app/routers/webhooks.py` (`delete_webhook`) \
  Pinned by: `services/integration/tests/test_webhooks.py` (`test_delete_returns_204`); `services/integration/tests/test_webhooks_router_direct.py` (`test_delete_webhook_direct_404_raises_http_exception`)
- **INTEG-HOOK-8.** Deleting a subscription deletes its delivery ledger rows through the
  `ON DELETE CASCADE` foreign key. \
  Enforced in: `services/integration/migrations/versions/0002_webhooks.py` (`CASCADE`); `services/integration/app/models/webhook.py` (`WebhookDelivery`) \
  Pinned by: none (issue #1081)
- **INTEG-HOOK-9.** A subscription is created with `is_active` true. `PATCH
  /webhooks/{webhook_id}` with the body `{"is_active": <boolean>}` pauses (false) or
  resumes (true) it and answers the subscription without its secret (#1078). Any other
  key, a missing or null `is_active`, or a value that is not a JSON boolean is 422 (the
  validation envelope) and changes nothing; an unknown id is 404 `Webhook not found`. The
  delivery ledger is kept. A paused subscription is left out of every event whose
  targets are loaded after the commit (INTEG-HOOK-11) and gains no ledger row for it;
  nothing is queued, so resuming does not replay the events it missed, and a later
  redelivery of an event that has no row for the subscription delivers it normally. A
  delivery whose targets were loaded before the pause is not interrupted: it makes its
  remaining attempts and records its row. The change is logged with action
  `webhook_active_changed`. \
  Enforced in: `services/integration/app/routers/webhooks.py` (`update_webhook`); `services/integration/app/schemas/webhook.py` (`WebhookUpdate`); `services/integration/app/services/delivery.py` (`load_matching_targets`) \
  Pinned by: `services/integration/tests/test_webhooks.py` (`test_patch_pauses_and_resumes_a_subscription`, `test_patch_requires_admin`, `test_patch_unknown_webhook_is_404`, `test_patch_refuses_anything_but_a_boolean_is_active`, `test_patch_keeps_the_delivery_ledger`, `test_paused_subscription_gets_no_new_event_and_nothing_queued`, `test_delivery_loaded_before_a_pause_finishes_its_attempts`); `services/integration/tests/test_webhooks_router_direct.py` (`test_create_webhook_direct_sets_created_by_from_sub`, `test_update_webhook_direct_toggles_is_active`, `test_update_webhook_direct_404_raises_http_exception`); `tests/integration/test_webhooks_flow.py` (`test_paused_webhook_receives_nothing_until_resumed`)
- **INTEG-HOOK-10.** The delivery history of a subscription is returned newest first;
  `limit` defaults to 100 and is clamped into 1 to 500 rather than refused; an unknown
  subscription is 404 `Webhook not found`. \
  Enforced in: `services/integration/app/routers/webhooks.py` (`list_deliveries`) \
  Pinned by: `services/integration/tests/test_webhooks.py` (`test_deliveries_endpoint_lists_ledger`, `test_deliveries_endpoint_404_for_missing_webhook`, `test_deliveries_endpoint_clamps_limit`); `services/integration/tests/test_webhooks_router_direct.py` (`test_list_deliveries_direct_orders_newest_first_and_returns_rows`)

**Out of scope.** Changing a subscription's URL, event names, or description (the only
update is `is_active`); rotating a secret other than by deleting and re-registering.

### 8.4 Webhook delivery

**What it does.** When a subscribed event happens, every matching endpoint receives the
event as JSON with a signature it can verify; a failing endpoint is retried a few times
and then recorded as dead without holding up anyone else.

**Surfaces.** `handle_event` in `services/integration/app/services/nats_consumer.py`;
`load_matching_targets` and `deliver_one` in
`services/integration/app/services/delivery.py`; the ledger route
`GET /api/v1/webhooks/{webhook_id}/deliveries`.

**Rules.**

- **INTEG-HOOK-11.** An event is delivered to every subscription that is active and lists
  the payload's `event` value; a subscription for other names receives nothing. \
  Enforced in: `services/integration/app/services/delivery.py` (`load_matching_targets`) \
  Pinned by: `services/integration/tests/test_webhooks.py` (`test_load_matching_targets_filters_by_event_and_active`); `services/integration/tests/test_nats_consumer.py` (`test_handle_event_routes_health_transition_to_matching_subscription`, `test_handle_event_health_transition_not_delivered_to_reservation_only_subscription`); `tests/integration/test_webhooks_flow.py` (`test_webhook_delivered_for_health_transition`)
- **INTEG-HOOK-12.** The POST body is the consumed message's exact bytes, with
  `Content-Type: application/json` and `X-HERD-Signature: sha256=<hex>`, the lowercase
  hex HMAC-SHA256 of those bytes keyed by the subscription secret. \
  Enforced in: `services/integration/app/services/delivery.py` (`deliver_one`); `services/common/herd_common/webhooks.py` (`sign_body`, `WEBHOOK_SIGNATURE_HEADER`) \
  Pinned by: `services/integration/tests/test_webhooks.py` (`test_delivery_2xx_writes_delivered_row`, `test_sign_body_is_stable_and_verifiable`)
- **INTEG-HOOK-13.** Each attempt is bounded by `WEBHOOK_DELIVERY_TIMEOUT_SECONDS`; a
  timeout, transport error, or non-2xx answer is retried up to
  `WEBHOOK_DELIVERY_ATTEMPTS` attempts in all, 0.5 seconds apart at first and doubling to
  a 5 second cap. Redirects are not followed: a 3xx answer is a failed attempt. A 2xx
  writes a `delivered` row with the attempt count, the status code, and `delivered_at`. \
  Enforced in: `services/integration/app/services/delivery.py` (`deliver_one`, `RETRY_INITIAL_DELAY`, `RETRY_MAX_DELAY`) \
  Pinned by: `services/integration/tests/test_webhooks.py` (`test_delivery_2xx_writes_delivered_row`, `test_delivery_persistent_failure_retries_then_dead`, `test_delivery_connection_error_is_retried_then_dead`); `services/integration/tests/test_webhook_destinations.py` (`test_delivery_does_not_follow_redirects`); `tests/integration/test_webhooks_flow.py` (`test_webhook_delivered_exactly_once`)
- **INTEG-HOOK-14.** When every attempt fails the row is written `dead`, with the attempt
  count, the status of the last answer received (null when none came), and `last_error`,
  which is only `upstream answered HTTP <status>` when the last attempt got an answer or
  `delivery failed (<ClassName>)` otherwise; the exception's own text (which can carry the
  URL and transport detail) goes to the log message (`webhook_delivery_dead`), never to
  the row or the deliveries read. \
  Enforced in: `services/integration/app/services/delivery.py` (`deliver_one`, `delivery_error_text`, `_record`) \
  Pinned by: `services/integration/tests/test_webhooks.py` (`test_delivery_persistent_failure_retries_then_dead`, `test_delivery_connection_error_is_retried_then_dead`); `services/integration/tests/test_webhook_destinations.py` (`test_delivery_ledger_records_the_answer_status_only`, `test_delivery_ledger_records_the_exception_class_only`); `tests/integration/test_webhooks_flow.py` (`test_webhook_failure_dead_letters`)
- **INTEG-HOOK-15.** A redelivered or republished event whose row is `dead` is POSTed
  again, and the same row is overwritten with the new outcome and a fresh attempt
  count. \
  Enforced in: `services/integration/app/services/delivery.py` (`deliver_one`, `_record`) \
  Pinned by: none (issue #1081)
- **INTEG-HOOK-16.** When a concurrent delivery of the same (subscription, event) wrote
  its row first, the losing insert is rolled back and logged, and the delivery returns
  its own outcome without raising. \
  Enforced in: `services/integration/app/services/delivery.py` (`_record`) \
  Pinned by: none (issue #1081)
- **INTEG-HOOK-17.** A redelivered or republished event whose row for a subscription is
  `delivered` is not POSTed again and gains no second row. \
  Enforced in: `services/integration/app/services/delivery.py` (`deliver_one`); `services/integration/app/models/webhook.py` (`uq_webhook_delivery_subscription_event`) \
  Pinned by: `services/integration/tests/test_webhooks.py` (`test_redelivered_event_with_delivered_row_is_skipped`); `tests/integration/test_webhooks_flow.py` (`test_webhook_delivered_exactly_once`)
- **INTEG-HOOK-18.** The ledger key is the payload's `event_id`, else the message's
  `<stream>:<sequence>`. \
  Enforced in: `services/integration/app/services/nats_consumer.py` (`process_message`); `services/common/herd_common/outbox.py` (`event_dedupe_key`) \
  Pinned by: `services/integration/tests/test_nats_consumer.py` (`test_handle_event_calls_deliver_one_with_expected_args`); `services/common/tests/test_outbox.py` (`test_dedupe_key_prefers_payload_event_id`, `test_dedupe_key_falls_back_to_stream_sequence`)
- **INTEG-HOOK-19.** A failure to POST never fails the message: it ends in a `dead` row.
  An unexpected error while delivering to one subscription (a ledger write that raises)
  is logged, the other subscriptions are still delivered, and the message is acked, so
  that subscription's event is not retried. By decision; see the comment in
  `handle_event`. \
  Enforced in: `services/integration/app/services/nats_consumer.py` (`handle_event`) \
  Pinned by: `services/integration/tests/test_nats_consumer.py` (`test_handle_event_swallows_unexpected_delivery_exception`)
- **INTEG-HOOK-20.** An event without an `event` field, or with no matching subscription,
  is delivered nowhere and acked. \
  Enforced in: `services/integration/app/services/nats_consumer.py` (`handle_event`) \
  Pinned by: `services/integration/tests/test_nats_consumer.py` (`test_handle_event_missing_event_field_is_noop`, `test_handle_event_no_targets_is_noop`)
- **INTEG-HOOK-21.** The deliveries for one event run concurrently, each in its own
  database session, with the timeout and attempt count read from settings. \
  Enforced in: `services/integration/app/services/nats_consumer.py` (`handle_event`) \
  Pinned by: `services/integration/tests/test_nats_consumer.py` (`test_handle_event_calls_deliver_one_with_expected_args`)
- **INTEG-HOOK-22.** When a message has neither an `event_id` nor JetStream metadata, the
  ledger key is the text `None`, shared by every such message. \
  Enforced in: `services/integration/app/services/nats_consumer.py` (`handle_event`) \
  Pinned by: none
- **INTEG-HOOK-23.** Before any POST the destination rule of INTEG-HOOK-2 is checked
  again against the stored `target_url`, so a subscription stored before the rule or a
  host whose answers changed is caught. A destination that is not allowed, or whose host
  does not resolve, gets a `failed` row with `attempts` 0, `response_status` null, and
  `last_error` `destination not allowed`; nothing is sent, no retry runs, the message is
  acked, and the log action is `webhook_destination_refused`. A `failed` row is not
  `delivered`, so a redelivered event checks again. \
  Enforced in: `services/integration/app/services/delivery.py` (`deliver_one`); `services/integration/app/services/destination.py` (`destination_allowed`, `DESTINATION_NOT_ALLOWED`) \
  Pinned by: `services/integration/tests/test_webhook_destinations.py` (`test_delivery_to_a_destination_not_allowed_is_failed_and_not_sent`, `test_delivery_to_an_internal_literal_is_failed`, `test_delivery_to_an_allowed_host_is_sent`, `test_failed_destination_row_is_retried_on_redelivery`)

**Out of scope.** The JetStream transport and its redelivery timing
(`operations-and-observability.md`); a receiver's own deduplication, which
[EXTERNAL_API.md](../EXTERNAL_API.md) asks of it.

### 8.5 Webhook event consumer

**What it does.** Two durable consumers read the reservation and health streams and hand
each message to the delivery fan-out of section 8.4, so a slow receiver neither stalls
the stream nor causes a duplicate.

**Surfaces.** `start_nats_consumer`, `process_batch`, and `process_message` in
`services/integration/app/services/nats_consumer.py`; startup in
`services/integration/app/main.py`.

**Rules.**

- **INTEG-CONSUME-1.** Two durable pull consumers run: `integration-webhooks-consumer` on
  `HERD_RESERVATIONS` filtered to `herd.reservations.*`, and
  `integration-webhooks-health-consumer` on `HERD_HEALTH` filtered to `herd.health.*`,
  each with `max_deliver` 5, `ack_wait` `NATS_ACK_WAIT_SECONDS`, and no `backoff`, each
  created or updated on the server before it binds, and both dispatching to the same
  handler. \
  Enforced in: `services/integration/app/services/nats_consumer.py` (`start_nats_consumer`, `NATS_DURABLE`, `HEALTH_DURABLE`, `NATS_MAX_DELIVER`, `NATS_ACK_WAIT_SECONDS`) \
  Pinned by: `services/integration/tests/test_nats_consumer_lifecycle.py` (`test_start_nats_consumer_wires_both_subscriptions`); `services/integration/tests/test_nats_consumer.py` (`test_health_consumer_config_pinned`); `tests/integration/test_nats_consumer_configs_live.py` (`test_consumers_have_real_ack_wait_and_no_backoff`)
- **INTEG-CONSUME-2.** Each loop fetches one message per pull with a 5 second wait; an
  empty wait fetches again, and any other fetch error is logged, waits 5 seconds, and
  fetches again. \
  Enforced in: `services/integration/app/services/nats_consumer.py` (`start_nats_consumer`, `NATS_FETCH_TIMEOUT_SECONDS`) \
  Pinned by: `services/integration/tests/test_nats_consumer_lifecycle.py` (`test_consumer_loop_fetches_one_message_at_a_time_on_both_subscriptions`, `test_consumer_loop_continues_after_idle_timeout`, `test_consumer_loop_survives_fetch_exception_and_retries`)
- **INTEG-CONSUME-3.** When NATS cannot be reached at startup the failure is logged and
  the service serves its API with no webhook delivery until it is restarted (the first
  connect is bounded, `operations-and-observability.md` OPS-NATS-1); a failure to ensure
  a stream is logged and both consumers still start. \
  Enforced in: `services/integration/app/services/nats_consumer.py` (`start_nats_consumer`); `services/common/herd_common/jetstream.py` (`connect_nats`) \
  Pinned by: `services/integration/tests/test_nats_connect_real_client.py` (`test_start_nats_consumer_returns_when_broker_is_down`); `services/integration/tests/test_nats_consumer_lifecycle.py` (`test_start_nats_consumer_connection_failure_is_swallowed`, `test_start_nats_consumer_stream_ensure_failure_still_starts_both_consumers`)
- **INTEG-CONSUME-4.** On a migration-managed schema missing a model table, the consumers
  start only once the tables exist, so events wait on the stream. \
  Enforced in: `services/integration/app/main.py` (`lifespan`); `services/common/herd_common/consumer_schema_gate.py` (`start_consumer_when_schema_ready`) \
  Pinned by: `services/common/tests/test_consumer_schema_gate.py` (`test_gated_defers_then_starts_when_table_appears`, `test_managed_schema_with_no_drift_starts_immediately`)
- **INTEG-CONSUME-5.** While a fetched message is handled, it gets an in-progress signal
  every half of `ack_wait`, so a fan-out longer than `ack_wait` is not redelivered to a
  peer consumer while it runs. \
  Enforced in: `services/integration/app/services/nats_consumer.py` (`process_batch`, `NATS_HEARTBEAT_SECONDS`); `services/common/herd_common/jetstream.py` (`process_batch_with_heartbeat`) \
  Pinned by: `services/integration/tests/test_nats_consumer_heartbeat.py` (`test_running_handler_is_heartbeated_then_stops_after_ack`, `test_heartbeat_interval_is_below_ack_wait`); `services/integration/tests/test_nats_consumer_lifecycle.py` (`test_started_consumer_loop_heartbeats_a_slow_handler`); `tests/integration/test_webhook_slow_receiver_live.py` (`test_slow_receiver_gets_the_event_exactly_once`)
- **INTEG-CONSUME-6.** A body that is valid JSON but not an object (`null`, a number, a
  string, a boolean, a list) is poison, handled as INTEG-CONSUME-7 handles a body that is
  not JSON: published to the subscription's dead-letter subject and acked, logged
  `nats_poison_message`, before any handler runs. \
  Enforced in: `services/integration/app/services/nats_consumer.py` (`process_message`); `services/common/herd_common/jetstream.py` (`decode_event_object`) \
  Pinned by: `services/integration/tests/test_nats_consumer.py` (`test_process_message_non_object_json_goes_to_dlq`); `services/common/tests/test_jetstream.py` (`test_decode_event_object_accepts_only_json_objects`); `tests/integration/test_dlq_and_idempotency.py` (`test_non_object_json_event_is_dead_lettered_by_each_consumer`)
- **INTEG-CONSUME-7.** A body that is not JSON is published to the subscription's
  dead-letter subject (`herd.reservations.dlq.integration` or
  `herd.health.dlq.integration`) and acked, logged `nats_poison_message`. \
  Enforced in: `services/integration/app/services/nats_consumer.py` (`process_message`, `NATS_DLQ_SUBJECT`, `HEALTH_DLQ_SUBJECT`) \
  Pinned by: `services/integration/tests/test_nats_consumer.py` (`test_process_message_poison_goes_to_dlq`, `test_process_message_poison_health_message_routes_to_health_dlq`); `services/integration/tests/test_nats_consumer_lifecycle.py` (`test_consumer_loop_passes_health_dlq_subject_for_health_subscription`)
- **INTEG-CONSUME-8.** A handler error before the fifth delivery naks with a delay from
  `NATS_NAK_BACKOFF_SECONDS`, logged `nats_message_nak`; at the fifth delivery the
  message is dead-lettered and acked, logged `nats_dlq_exhausted`. \
  Enforced in: `services/integration/app/services/nats_consumer.py` (`process_message`, `NATS_NAK_BACKOFF_SECONDS`) \
  Pinned by: `services/integration/tests/test_nats_consumer.py` (`test_process_message_nak_on_transient_error`, `test_process_message_nak_on_transient_error_first_delivery`, `test_process_message_dlq_on_max_deliver_exhausted`, `test_process_message_max_deliver_exhausted_health_routes_to_health_dlq`)
- **INTEG-CONSUME-9.** A failed dead-letter publish is logged and the message is still
  acked. \
  Enforced in: `services/integration/app/services/nats_consumer.py` (`_publish_to_dlq`) \
  Pinned by: `services/integration/tests/test_nats_consumer.py` (`test_process_message_dlq_publish_failure_does_not_propagate`, `test_process_message_dlq_publish_failure_on_exhaustion_still_acks`)
- **INTEG-CONSUME-10.** A handler that returns acks the message. \
  Enforced in: `services/integration/app/services/nats_consumer.py` (`process_message`) \
  Pinned by: `services/integration/tests/test_nats_consumer.py` (`test_process_message_acks_on_success`)
- **INTEG-CONSUME-11.** Shutdown cancels both loops and closes the connection; a close
  failure is logged. \
  Enforced in: `services/integration/app/services/nats_consumer.py` (`stop_nats_consumer`) \
  Pinned by: `services/integration/tests/test_nats_consumer_lifecycle.py` (`test_stop_nats_consumer_cancels_both_tasks_and_closes_connection`, `test_stop_nats_consumer_close_failure_is_swallowed`)

**Out of scope.** The shared helpers' own behavior (`operations-and-observability.md`).

### 8.6 Test delivery sink

**What it does.** On the development and test stack only, the integration service hosts
a receiver that always accepts a delivery, can answer slowly, and counts arrivals, so the
live tests can prove a delivery arrives exactly once.

**Surfaces.** `test_sink_router` in `services/integration/app/routers/webhooks.py`,
registered by `services/integration/app/main.py`; `docker-compose.override.yml`.

**Rules.**

- **INTEG-SINK-1.** The two sink routes exist only when `WEBHOOK_TEST_SINK_ENABLED` is
  true, which only `docker-compose.override.yml` sets, and neither appears in the
  OpenAPI document. \
  Enforced in: `services/integration/app/main.py` (`webhook_test_sink_enabled`); `docker-compose.override.yml` (`WEBHOOK_TEST_SINK_ENABLED`) \
  Pinned by: `services/integration/tests/test_webhooks_router_direct.py` (`test_sink_routes_stay_out_of_the_published_schema`)
- **INTEG-SINK-2.** `POST /webhooks/echo` needs no authentication and answers 200
  `{"ok": true, "received_bytes": N}`, after sleeping `delay_ms` clamped into 0 to
  10000. \
  Enforced in: `services/integration/app/routers/webhooks.py` (`echo_receiver`, `SINK_MAX_DELAY_MS`) \
  Pinned by: `services/integration/tests/test_webhooks_router_direct.py` (`test_echo_receiver_reports_received_byte_count`, `test_echo_receiver_delay_ms_sleeps_and_is_clamped`)
- **INTEG-SINK-3.** The sink counts each arrival by the body's string `event_id` before
  any delay, keeps at most 1000 ids (oldest dropped), ignores bodies without one, and
  `GET /webhooks/echo/hits?event_id` answers `{event_id, count}`. \
  Enforced in: `services/integration/app/routers/webhooks.py` (`_record_sink_hit`, `echo_hits`, `_SINK_HITS_MAX_KEYS`) \
  Pinned by: `services/integration/tests/test_webhooks_router_direct.py` (`test_echo_receiver_counts_arrivals_per_event_id`, `test_echo_receiver_ignores_bodies_without_a_string_event_id`, `test_echo_receiver_hit_table_is_bounded`)
- **INTEG-SINK-4.** The development and test stack pins integration's
  `NATS_ACK_WAIT_SECONDS` to 4, and only integration's, so a live test's slow receiver
  outlasts `ack_wait` inside the test time limit. \
  Enforced in: `docker-compose.override.yml` (`NATS_ACK_WAIT_SECONDS`) \
  Pinned by: `tests/integration/test_nats_consumer_configs_live.py` (`test_consumers_have_real_ack_wait_and_no_backoff`)

**Out of scope.** Any production receiver.

### 8.7 Notification event consumer

**What it does.** Two durable consumers read the reservation and health streams and turn
each event into notifications for the right people, exactly once per person even when an
event is delivered twice.

**Surfaces.** `start_nats_consumer`, `process_batch`, `process_message`, and
`handle_event` in `services/notifications/app/services/nats_consumer.py`; startup in
`services/notifications/app/main.py`.

**Rules.**

- **INTEG-NCONSUME-1.** Two durable pull consumers run: `notifications-consumer` on
  `HERD_RESERVATIONS` filtered to `herd.reservations.*`, and
  `notifications-health-consumer` on `HERD_HEALTH` filtered to `herd.health.*`, each with
  `max_deliver` 5, `ack_wait` `NATS_ACK_WAIT_SECONDS`, and no `backoff`, each created or
  updated before it binds, both dispatching to the same handler. \
  Enforced in: `services/notifications/app/services/nats_consumer.py` (`start_nats_consumer`, `NATS_DURABLE`, `HEALTH_DURABLE`, `NATS_MAX_DELIVER`) \
  Pinned by: `services/notifications/tests/test_nats_consumer.py` (`test_start_nats_consumer_wires_both_subscriptions`); `tests/integration/test_nats_consumer_configs_live.py` (`test_consumers_have_real_ack_wait_and_no_backoff`)
- **INTEG-NCONSUME-2.** Each loop fetches one message per pull with a 5 second wait, and
  an unexpected error in a loop is logged without stopping it. \
  Enforced in: `services/notifications/app/services/nats_consumer.py` (`start_nats_consumer`, `NATS_FETCH_TIMEOUT_SECONDS`) \
  Pinned by: `services/notifications/tests/test_nats_consumer.py` (`test_start_nats_consumer_loop_fetches_one_message_at_a_time`, `test_start_nats_consumer_loop_swallows_unexpected_errors`)
- **INTEG-NCONSUME-3.** When NATS cannot be reached at startup the failure is logged and
  the service serves its API with no event-driven notifications until it is restarted
  (the first connect is bounded, `operations-and-observability.md` OPS-NATS-1); a stream
  failure is tolerated. \
  Enforced in: `services/notifications/app/services/nats_consumer.py` (`start_nats_consumer`); `services/common/herd_common/jetstream.py` (`connect_nats`) \
  Pinned by: `services/notifications/tests/test_nats_connect_real_client.py` (`test_start_nats_consumer_returns_when_broker_is_down`); `services/notifications/tests/test_nats_consumer.py` (`test_start_nats_consumer_swallows_connect_failure`, `test_start_nats_consumer_tolerates_add_stream_failure`)
- **INTEG-NCONSUME-4.** On a migration-managed schema missing a model table, the
  consumers start only once the tables exist. \
  Enforced in: `services/notifications/app/main.py` (`lifespan`); `services/common/herd_common/consumer_schema_gate.py` (`start_consumer_when_schema_ready`) \
  Pinned by: `services/common/tests/test_consumer_schema_gate.py` (`test_gated_defers_then_starts_when_table_appears`, `test_managed_schema_with_no_drift_starts_immediately`)
- **INTEG-NCONSUME-5.** While a fetched message is handled, it gets an in-progress signal
  every half of `ack_wait`, so a slow outbound channel does not cause a redelivery. \
  Enforced in: `services/notifications/app/services/nats_consumer.py` (`process_batch`, `NATS_HEARTBEAT_SECONDS`); `services/common/herd_common/jetstream.py` (`process_batch_with_heartbeat`) \
  Pinned by: `services/notifications/tests/test_nats_consumer_heartbeat.py` (`test_running_handler_is_heartbeated_then_stops_after_ack`, `test_heartbeat_interval_is_below_ack_wait`); `services/notifications/tests/test_nats_consumer.py` (`test_started_consumer_loop_heartbeats_a_slow_handler`)
- **INTEG-NCONSUME-6.** A body that is valid JSON but not an object is poison, handled as
  INTEG-NCONSUME-7 handles a body that is not JSON: dead-lettered and acked, logged
  `nats_poison_message`, before any handler runs. \
  Enforced in: `services/notifications/app/services/nats_consumer.py` (`process_message`); `services/common/herd_common/jetstream.py` (`decode_event_object`) \
  Pinned by: `services/notifications/tests/test_nats_consumer.py` (`test_process_message_non_object_json_goes_to_dlq`); `tests/integration/test_dlq_and_idempotency.py` (`test_non_object_json_event_is_dead_lettered_by_each_consumer`)
- **INTEG-NCONSUME-7.** A body that is not JSON is published to
  `herd.reservations.dlq.notifications` or `herd.health.dlq.notifications` and acked,
  logged `nats_poison_message`. \
  Enforced in: `services/notifications/app/services/nats_consumer.py` (`process_message`, `NATS_DLQ_SUBJECT`, `HEALTH_DLQ_SUBJECT`) \
  Pinned by: `services/notifications/tests/test_nats_consumer.py` (`test_process_message_poison_goes_to_dlq`, `test_process_message_poison_publishes_to_notifications_dlq_subject`)
- **INTEG-NCONSUME-8.** A handler error before the fifth delivery naks with a delay from
  `NATS_NAK_BACKOFF_SECONDS`; at the fifth the message is dead-lettered and acked. \
  Enforced in: `services/notifications/app/services/nats_consumer.py` (`process_message`, `NATS_NAK_BACKOFF_SECONDS`) \
  Pinned by: `services/notifications/tests/test_nats_consumer.py` (`test_process_message_nak_on_transient_error`, `test_process_message_nak_on_transient_error_third_delivery`, `test_process_message_dlq_on_max_deliver_exhausted`, `test_process_message_max_deliver_publishes_to_notifications_dlq_subject`)
- **INTEG-NCONSUME-9.** A failed dead-letter publish is logged and the message is still
  acked; a handler that returns acks. \
  Enforced in: `services/notifications/app/services/nats_consumer.py` (`_publish_to_dlq`, `process_message`) \
  Pinned by: `services/notifications/tests/test_nats_consumer.py` (`test_process_message_dlq_publish_failure_does_not_propagate`, `test_process_message_ack_on_success`)
- **INTEG-NCONSUME-10.** The dedupe key is the payload's `event_id`, else
  `<stream>:<sequence>`, else none. \
  Enforced in: `services/notifications/app/services/nats_consumer.py` (`process_message`); `services/common/herd_common/outbox.py` (`event_dedupe_key`) \
  Pinned by: `services/notifications/tests/test_nats_consumer.py` (`test_dedupe_key_prefers_payload_event_id`, `test_dedupe_key_falls_back_to_stream_and_sequence`, `test_dedupe_key_none_when_no_event_id_and_no_sequence`)
- **INTEG-NCONSUME-11.** The key is stamped on every recipient's message, so a
  redelivery, or a republish under a new sequence with the same `event_id`, yields one
  notification per recipient. \
  Enforced in: `services/notifications/app/services/nats_consumer.py` (`handle_event`) \
  Pinned by: `services/notifications/tests/test_nats_consumer.py` (`test_redelivered_message_creates_one_notification`, `test_republished_event_same_id_new_sequence_dedupes_to_one`); `services/notifications/tests/test_functional_dispatch_path.py` (`test_full_fanout_then_redelivery_is_idempotent`)
- **INTEG-NCONSUME-12.** An error raised while reading a recipient's preferences
  propagates, so the message naks and is retried. \
  Enforced in: `services/notifications/app/services/nats_consumer.py` (`_dispatch`) \
  Pinned by: `services/notifications/tests/test_nats_consumer.py` (`test_handle_event_propagates_prefs_client_failure`, `test_process_message_naks_when_prefs_client_fails`)

**Out of scope.** The shared helpers' own behavior (`operations-and-observability.md`).

### 8.8 Event routing

**What it does.** A reservation's owner is told when it goes live, changes in a way that
matters, is cancelled, completes, fails, or is about to end.

**Surfaces.** `build_messages` in `services/notifications/app/services/event_router.py`.

**Rules.**

- **INTEG-ROUTE-1.** `reservation.created`, `cancelled`, `completed`, and
  `expiring_soon` each produce one message for the payload's `user_id`, titled
  `Reservation confirmed`, `Reservation cancelled`, `Reservation completed`, and
  `Reservation expiring soon`, with a body naming the device count (`no devices`,
  `1 device`, `N devices`) and, where the event has one, the end time. \
  Enforced in: `services/notifications/app/services/event_router.py` (`build_messages`, `_RENDERERS`, `_device_summary`) \
  Pinned by: `services/notifications/tests/test_event_router.py` (`test_created_event_produces_single_message`, `test_expiring_soon_event_produces_single_message`, `test_cancelled_and_completed_events_emit`, `test_created_event_zero_devices`); `tests/integration/test_notifications_flow.py` (`test_reservation_created_produces_in_app_notification`); `tests/integration/test_notification_channels_flow.py` (`test_expiring_soon_reminder_produces_single_notification`)
- **INTEG-ROUTE-2.** Every other event name produces nothing and the message is acked:
  `reservation.provision_requested` and `reservation.wiring_changed` included. \
  Enforced in: `services/notifications/app/services/event_router.py` (`build_messages`, `_RENDERERS`) \
  Pinned by: `services/notifications/tests/test_event_router.py` (`test_unknown_event_is_skipped`, `test_other_unrendered_reservation_events_are_still_skipped`)
- **INTEG-ROUTE-3.** `reservation.updated` produces a message only when devices were
  added or removed or the end time changed (the `end_time_changed` flag, or for an older
  payload without it, a non-empty `end_time`). \
  Enforced in: `services/notifications/app/services/event_router.py` (`_should_emit_update`, `_end_time_changed`) \
  Pinned by: `services/notifications/tests/test_event_router.py` (`test_updated_event_with_no_material_change_skips`, `test_updated_metadata_only_with_unchanged_end_time_skips`, `test_updated_event_with_changed_end_time_emits`, `test_updated_event_with_added_devices_emits`)
- **INTEG-ROUTE-4.** A reservation event with no `user_id`, or one that is not a UUID,
  produces nothing; the second is logged `event_invalid_user_id`. \
  Enforced in: `services/notifications/app/services/event_router.py` (`build_messages`) \
  Pinned by: `services/notifications/tests/test_event_router.py` (`test_missing_user_id_is_skipped`, `test_invalid_user_id_is_skipped`)
- **INTEG-ROUTE-5.** A message's `data` is a copy of the whole event payload, and it is
  stored with the notification and returned by the list. \
  Enforced in: `services/notifications/app/services/event_router.py` (`build_messages`) \
  Pinned by: `services/notifications/tests/test_event_router.py` (`test_created_event_produces_single_message`)

- **INTEG-ROUTE-6.** `reservation.failed` produces one message for the payload's
  `user_id`, titled `Reservation failed`, with the body `Reservation <first eight
  characters of the id> for <device count> failed.` (`Reservation for <device count>
  failed.` when the payload has no `reservation_id`). The text is built from the id and
  the device count only, so no upstream error text reaches any channel (#1077). The event
  key is in `DEFAULT_EVENT_TYPES`, so it is on unless the user stored it off, including
  for a user whose stored preferences predate the key (INTEG-PREFS-1). \
  Enforced in: `services/notifications/app/services/event_router.py` (`_render_failed`, `_reservation_label`, `_RENDERERS`); `services/notifications/app/schemas/preferences.py` (`DEFAULT_EVENT_TYPES`, `with_defaults`) \
  Pinned by: `services/notifications/tests/test_event_router.py` (`test_failed_event_notifies_the_owner`, `test_failed_event_text_carries_no_upstream_error_text`, `test_failed_event_without_reservation_id_still_notifies`, `test_failed_event_without_user_id_is_skipped`); `services/notifications/tests/test_preferences.py` (`test_stored_preferences_predating_the_key_receive_it_on`, `test_explicit_opt_out_of_failed_is_kept`); `services/notifications/tests/test_preferences_client.py` (`test_preferences_predating_reservation_failed_read_it_as_on`); `services/notifications/tests/test_nats_consumer.py` (`test_handle_event_delivers_failed_under_preferences_predating_the_key`, `test_handle_event_failed_respects_opt_out`); `services/notifications/tests/test_router.py` (`test_put_preferences_predating_failed_writes_it_on`, `test_get_preferences_predating_failed_reads_it_on`); `tests/integration/test_reservation_failed_notification.py` (`test_failed_reservation_notifies_owner_in_app`)

**Out of scope.** When reservations stages each event (`reservations.md`, section 6).

### 8.9 Device health fan-out

**What it does.** When a polled device becomes unhealthy or recovers, every admin and
everyone with a live reservation on it is told.

**Surfaces.** `_build_health_messages` in
`services/notifications/app/services/event_router.py`; `resolve_health_recipients` and
`AdminListClient` in `services/notifications/app/services/health_recipients.py`.

**Rules.**

- **INTEG-HEALTH-1.** A `device.health_transition` produces one message for each user in
  the union of the admins (auth's admin list) and the users holding an `ACTIVE`
  reservation on the device (reservations' active-users lookup, RES-INTERNAL-4 in
  `reservations.md`), each user once, admins first. \
  Enforced in: `services/notifications/app/services/health_recipients.py` (`resolve_health_recipients`); `services/notifications/app/services/event_router.py` (`_build_health_messages`) \
  Pinned by: `services/notifications/tests/test_health_recipients.py` (`test_resolver_returns_deduped_union`); `services/notifications/tests/test_event_router_health.py` (`test_bad_news_event_fans_out_to_all_recipients`); `tests/integration/test_health_alerting_flow.py` (`test_bad_news_event_reaches_admin_in_app`, `test_active_reservation_holder_also_receives`)
- **INTEG-HEALTH-2.** A `recovery` transition is titled `Device <name> recovered`; any
  other is titled `Device <name> unhealthy` and names the consecutive failure count; a
  missing `device_name` reads `device`. \
  Enforced in: `services/notifications/app/services/event_router.py` (`_render_health_transition`) \
  Pinned by: `services/notifications/tests/test_event_router_health.py` (`test_recovery_event_uses_different_renderer`, `test_event_missing_device_name_falls_back`); `tests/integration/test_health_alerting_flow.py` (`test_recovery_event_reaches_admin_in_app`)
- **INTEG-HEALTH-3.** An event with no `device_id`, or one that is not a UUID, produces
  nothing, logged `health_event_no_device_id` or `health_event_invalid_device_id`. \
  Enforced in: `services/notifications/app/services/event_router.py` (`_build_health_messages`) \
  Pinned by: `services/notifications/tests/test_event_router_health.py` (`test_event_missing_device_id_is_skipped`, `test_event_invalid_device_id_is_skipped`)
- **INTEG-HEALTH-4.** Only an answer from auth is cached, for
  `HEALTH_NOTIFY_ADMIN_CACHE_TTL_SECONDS`: a 200 with a list, an empty one included.
  The empty list a failed fetch answers (INTEG-HEALTH-5) covers only the event that hit
  the failure; it is returned as `Uncached`, nothing is stored, and the next event asks
  auth again. \
  Enforced in: `services/notifications/app/services/health_recipients.py` (`AdminListClient`); `services/common/herd_common/ttl_cache.py` (`SingletonTTLCache`, `Uncached`) \
  Pinned by: `services/notifications/tests/test_health_recipients.py` (`test_admin_list_failure_is_not_cached`, `test_admin_list_missing_token_is_not_cached`, `test_admin_list_empty_answer_is_cached`); `services/common/tests/test_ttl_cache.py` (`test_singleton_uncached_fallback_is_returned_but_not_stored`)
- **INTEG-HEALTH-5.** The admin-list fetch answers an empty list, fail open, on a missing
  internal token, a transport error, a non-200, a body that is not JSON, or a JSON body
  that is not a list; entries that
  are not UUIDs are skipped. \
  Enforced in: `services/notifications/app/services/health_recipients.py` (`AdminListClient`) \
  Pinned by: `services/notifications/tests/test_health_recipients.py` (`test_admin_list_returns_empty_when_token_missing`, `test_admin_list_returns_empty_on_http_error`, `test_admin_list_returns_empty_on_non_200`, `test_admin_list_returns_empty_on_malformed_json`, `test_admin_list_skips_unparseable_ids`)
- **INTEG-HEALTH-6.** The active-holder lookup is made per event, uncached, and answers
  an empty list on the same failures. \
  Enforced in: `services/notifications/app/services/health_recipients.py` (`_fetch_active_reservation_holders`) \
  Pinned by: `services/notifications/tests/test_health_recipients.py` (`test_holders_returns_empty_when_token_missing`, `test_holders_returns_empty_on_http_error`, `test_holders_returns_empty_on_non_200`, `test_holders_returns_empty_on_malformed_json`, `test_holders_skips_unparseable_ids`)
- **INTEG-HEALTH-7.** With no recipient the event produces nothing and is acked: when
  both lookups fail, the notification is lost, fail open. By decision; see the
  docstring of `services/notifications/app/services/health_recipients.py`. \
  Enforced in: `services/notifications/app/services/event_router.py` (`_build_health_messages`) \
  Pinned by: `services/notifications/tests/test_event_router_health.py` (`test_empty_recipient_list_produces_no_messages`); `services/notifications/tests/test_health_recipients.py` (`test_resolver_returns_empty_when_both_sides_fail`); `tests/integration/test_health_alerting_flow.py` (`test_event_with_no_recipients_drops_silently`)
- **INTEG-HEALTH-8.** The admin list is cached for the TTL, and concurrent misses make
  one fetch when that fetch answers; after a failed fetch nothing is stored, so each
  waiting caller asks again (INTEG-HEALTH-4). \
  Enforced in: `services/notifications/app/services/health_recipients.py` (`AdminListClient`); `services/common/herd_common/ttl_cache.py` (`SingletonTTLCache`) \
  Pinned by: `services/notifications/tests/test_health_recipients.py` (`test_admin_list_caches_within_ttl`, `test_admin_list_concurrent_callers_fetch_once`, `test_admin_list_refetches_after_invalidate`); `services/common/tests/test_ttl_cache.py` (`test_concurrent_callers_during_a_failure_each_ask_again`)

**Out of scope.** When execution publishes a transition (`operations-and-observability.md`).

### 8.10 In-app notifications

**What it does.** Each user has a list of their own notifications, a count of unread
ones, and can mark one or all read and delete one.

**Surfaces.** The six `/api/notifications/notifications` list and item routes (section
5), `services/notifications/app/routers/notifications.py`;
`services/notifications/app/services/notification_service.py`; the in-app dispatcher
`services/notifications/app/services/dispatchers/in_app.py`.

**Rules.**

- **INTEG-INAPP-1.** The in-app channel stores at most one notification per (user, event
  key): a duplicate insert is rolled back and logged `notification_deduped`; distinct
  users each get a row for the same event; a message with no key is never deduplicated. \
  Enforced in: `services/notifications/app/services/notification_service.py` (`create`); `services/notifications/app/models/notification.py` (`uq_notifications_user_dedupe`); `services/notifications/app/services/dispatchers/in_app.py` (`InAppDispatcher`) \
  Pinned by: `services/notifications/tests/test_in_app_dispatcher.py` (`test_redelivery_is_idempotent`, `test_same_key_different_users_each_get_a_row`, `test_null_dedupe_keys_are_unconstrained`, `test_distinct_dedupe_keys_create_distinct_rows`)
- **INTEG-INAPP-2.** Every notification route needs a valid access token and acts only on
  rows whose `user_id` is the caller's `sub` (IAM-CLAIM-3 in `identity-and-access.md`);
  another user's row answers as if absent. \
  Enforced in: `services/notifications/app/routers/notifications.py` (`list_notifications`, `mark_read`, `delete_notification`); `services/notifications/app/services/notification_service.py` (`list_for_user`, `mark_read`, `delete_one`) \
  Pinned by: `services/notifications/tests/test_router.py` (`test_list_unauthenticated`, `test_list_returns_only_caller_notifications`, `test_mark_read_foreign_notification_returns_404`, `test_delete_foreign_returns_404`, `test_caller_id_rejects_non_uuid_subject`)
- **INTEG-INAPP-3.** The list is newest first and pages with `limit` (1 to 200, default
  50) and `offset` (at least 0), not the `skip` other services use. By decision; see
  the CHANGELOG entry for issue #597. \
  Enforced in: `services/notifications/app/routers/notifications.py` (`list_notifications`); `services/notifications/app/services/notification_service.py` (`list_for_user`) \
  Pinned by: `services/notifications/tests/test_router.py` (`test_list_handler_returns_items`)
- **INTEG-INAPP-4.** `unread_only` narrows `items` only: `total` is always every
  notification the caller has, and `unread` the caller's unread count. By decision; see
  the docstring of `list_for_user`. \
  Enforced in: `services/notifications/app/services/notification_service.py` (`list_for_user`) \
  Pinned by: `services/notifications/tests/test_router.py` (`test_unread_only_filter`)
- **INTEG-INAPP-5.** The unread count is the caller's notifications with no `read_at`. \
  Enforced in: `services/notifications/app/services/notification_service.py` (`unread_count`) \
  Pinned by: `services/notifications/tests/test_router.py` (`test_unread_count`)
- **INTEG-INAPP-6.** Marking a notification read sets `read_at` and returns the
  notification; an unknown or foreign id is 404 `Notification not found`. \
  Enforced in: `services/notifications/app/services/notification_service.py` (`mark_read`); `services/notifications/app/routers/notifications.py` (`mark_read`) \
  Pinned by: `services/notifications/tests/test_router.py` (`test_mark_read`, `test_mark_read_foreign_notification_returns_404`)
- **INTEG-INAPP-7.** Marking an already read notification read leaves its `read_at`
  unchanged. \
  Enforced in: `services/notifications/app/services/notification_service.py` (`mark_read`) \
  Pinned by: none
- **INTEG-INAPP-8.** Mark all read sets `read_at` on every unread notification of the
  caller and answers `{updated}`, the number changed. \
  Enforced in: `services/notifications/app/services/notification_service.py` (`mark_all_read`) \
  Pinned by: `services/notifications/tests/test_router.py` (`test_mark_all_read`)
- **INTEG-INAPP-9.** Deleting a notification answers 204; an unknown or foreign id is
  404 `Notification not found`. \
  Enforced in: `services/notifications/app/services/notification_service.py` (`delete_one`); `services/notifications/app/routers/notifications.py` (`delete_notification`) \
  Pinned by: `services/notifications/tests/test_router.py` (`test_delete_own`, `test_delete_foreign_returns_404`)

**Out of scope.** Expiry of old notifications: none is deleted except by its owner.

### 8.11 Notification preferences

**What it does.** Each user chooses which channels they hear on and which kinds of event
they hear about; everything is on in the app by default and off everywhere else.

**Surfaces.** `GET` and `PUT /api/notifications/notifications/preferences` (section 5);
`NotificationPreferences` in `services/notifications/app/schemas/preferences.py`;
`PreferencesClient` in `services/notifications/app/services/preferences_client.py`;
`_dispatch` in `services/notifications/app/services/nats_consumer.py`.

**Rules.**

- **INTEG-PREFS-1.** Preferences are stored in user-profile under
  `extras.notifications` and read with the caller's own token. Missing values default:
  `in_app` on, `email`, `chat`, and `webhook` off, and every event in
  `DEFAULT_EVENT_TYPES` on unless stored false; an event name not stored counts as on. \
  Enforced in: `services/notifications/app/routers/notifications.py` (`get_preferences`, `_fetch_prefs_via_jwt`); `services/notifications/app/schemas/preferences.py` (`NotificationPreferences`, `NotificationChannels`, `DEFAULT_EVENT_TYPES`) \
  Pinned by: `services/notifications/tests/test_router.py` (`test_get_preferences_proxies_user_profile`); `services/notifications/tests/test_preferences.py` (`test_none_yields_defaults`, `test_valid_dict_validated_and_filled`); `tests/integration/test_notifications_flow.py` (`test_preferences_round_trip_via_proxy`)
- **INTEG-PREFS-2.** A stored value that is not an object, or fails validation, reads as
  the defaults and is logged. \
  Enforced in: `services/notifications/app/schemas/preferences.py` (`with_defaults`) \
  Pinned by: `services/notifications/tests/test_preferences.py` (`test_non_dict_string_falls_back`, `test_non_dict_list_falls_back`, `test_malformed_channels_falls_back`, `test_malformed_events_falls_back`)
- **INTEG-PREFS-3.** The read and write proxy fail closed: a transport error answers 503
  `user-profile unreachable`, and any user-profile status of 400 or above answers 503
  `user-profile error: <status>`. \
  Enforced in: `services/notifications/app/routers/notifications.py` (`_fetch_prefs_via_jwt`, `put_preferences`) \
  Pinned by: `services/notifications/tests/test_router.py` (`test_get_preferences_returns_503_on_transport_error`, `test_get_preferences_returns_503_on_upstream_4xx`, `test_put_preferences_returns_503_on_transport_error`, `test_put_preferences_returns_503_on_upstream_4xx`)
- **INTEG-PREFS-4.** A write reads the current preferences, merges the body's `events`
  key by key, sends the result to user-profile as a `PATCH` of
  `extras.notifications`, drops the caller's cached entry, and answers the merged
  preferences. \
  Enforced in: `services/notifications/app/routers/notifications.py` (`put_preferences`) \
  Pinned by: `services/notifications/tests/test_router.py` (`test_put_preferences_writes_merged_payload`); `tests/integration/test_notification_channels_flow.py` (`test_channel_preferences_round_trip`)
- **INTEG-PREFS-5.** A write's `channels`, when present, replaces the stored channels as
  a whole object, so a channel the body omits is reset to its default rather than kept. \
  Enforced in: `services/notifications/app/routers/notifications.py` (`put_preferences`); `services/notifications/app/schemas/preferences.py` (`NotificationPreferencesUpdate`) \
  Pinned by: none (issue #1081)
- **INTEG-PREFS-6.** Only a 200 from user-profile is cached, for
  `PREFERENCES_CACHE_TTL_SECONDS`. The defaults a failed consumer-side fetch answers
  (INTEG-PREFS-7) cover only the event that hit the failure; they are returned as
  `Uncached`, nothing is stored, and the next event reads the stored preferences again,
  so an opt-out applies from the next event on. \
  Enforced in: `services/notifications/app/services/preferences_client.py` (`PreferencesClient`); `services/common/herd_common/ttl_cache.py` (`TTLCache`, `Uncached`) \
  Pinned by: `services/notifications/tests/test_preferences_client.py` (`test_failed_fetch_is_not_cached_so_an_opt_out_applies_next_call`); `services/common/tests/test_ttl_cache.py` (`test_uncached_fallback_is_returned_but_not_stored`)
- **INTEG-PREFS-7.** The consumer reads preferences through user-profile's internal route
  with the internal token and answers the defaults, fail open, on a transport error or a
  non-200. \
  Enforced in: `services/notifications/app/services/preferences_client.py` (`PreferencesClient`) \
  Pinned by: `services/notifications/tests/test_preferences_client.py` (`test_fetch_falls_back_on_http_error`, `test_fetch_falls_back_on_non_200`, `test_get_returns_stored_prefs`)
- **INTEG-PREFS-8.** Consumer-side preferences are cached per user for
  `PREFERENCES_CACHE_TTL_SECONDS`, and concurrent misses make one fetch. \
  Enforced in: `services/notifications/app/services/preferences_client.py` (`PreferencesClient`); `services/common/herd_common/ttl_cache.py` (`TTLCache`) \
  Pinned by: `services/notifications/tests/test_preferences_client.py` (`test_cache_hit_avoids_second_http_call`, `test_cache_expires_after_ttl_forces_refetch`, `test_concurrent_callers_fetch_once`)
- **INTEG-PREFS-9.** An event the user turned off is sent on no channel, and a channel
  the user turned off is skipped for every event. \
  Enforced in: `services/notifications/app/services/nats_consumer.py` (`_dispatch`) \
  Pinned by: `services/notifications/tests/test_nats_consumer.py` (`test_handle_event_respects_event_opt_out`, `test_handle_event_respects_channel_opt_out`); `services/notifications/tests/test_multichannel_dispatch.py` (`test_only_enabled_channels_receive`); `tests/integration/test_notifications_flow.py` (`test_disabled_event_suppresses_notification`)

**Out of scope.** User-profile's own storage and merge of `extras`.

### 8.12 Outbound notification channels

**What it does.** A user who opts in can also receive notifications by email, in a
shared chat channel, or at an instance-level webhook; a broken channel never blocks the
others or the bell.

**Surfaces.** `default_dispatchers` and the dispatchers in
`services/notifications/app/services/dispatchers/`; `run_outbound` in
`services/notifications/app/services/dispatchers/outbound.py`; `ContactClient` in
`services/notifications/app/services/contact_client.py`.

**Rules.**

- **INTEG-OUT-1.** Channels are tried in the order in-app, email, chat, webhook. \
  Enforced in: `services/notifications/app/services/dispatchers/__init__.py` (`default_dispatchers`) \
  Pinned by: `services/notifications/tests/test_multichannel_dispatch.py` (`test_default_dispatchers_lists_all_channels_in_app_first`, `test_event_fans_out_to_all_enabled_channels`)
- **INTEG-OUT-2.** A channel whose transport is not configured is skipped with a log and
  no claim: email needs `SMTP_HOST` and `EMAIL_FROM`, chat needs `CHAT_WEBHOOK_URL`, and
  the webhook channel needs both `OUTBOUND_WEBHOOK_URL` and `WEBHOOK_SIGNING_SECRET`, so
  an unsigned webhook is never sent. \
  Enforced in: `services/notifications/app/services/dispatchers/email.py` (`_is_configured`); `services/notifications/app/services/dispatchers/chat.py` (`_is_configured`); `services/notifications/app/services/dispatchers/webhook.py` (`_is_configured`) \
  Pinned by: `services/notifications/tests/test_outbound_dispatchers.py` (`test_email_noop_when_not_configured`, `test_chat_noop_when_not_configured`, `test_webhook_noop_when_secret_missing`)
- **INTEG-OUT-3.** An outbound send first commits a claim unique on (channel, user, event
  key); a claim that already exists skips the send, and a send that fails releases the
  claim so a redelivery retries. A crash between the claim and the send's end suppresses
  that notification for good, so outbound delivery is at most once. By decision; see
  issue #83 and the docstring of `OutboundDelivery`. \
  Enforced in: `services/notifications/app/services/dispatchers/outbound.py` (`run_outbound`, `already_delivered`, `_release`); `services/notifications/app/models/outbound_delivery.py` (`OutboundDelivery`) \
  Pinned by: `services/notifications/tests/test_outbound_dispatchers.py` (`test_email_redelivery_does_not_resend`, `test_webhook_redelivery_does_not_repost`, `test_email_send_failure_releases_ledger_for_retry`, `test_run_outbound_releases_reserved_slot_on_send_failure`)
- **INTEG-OUT-4.** A failed outbound send is logged `outbound_dispatch_failed` and
  swallowed: the other channels still run and the message is acked, so that channel is
  retried only by a redelivery. \
  Enforced in: `services/notifications/app/services/dispatchers/outbound.py` (`run_outbound`) \
  Pinned by: `services/notifications/tests/test_multichannel_dispatch.py` (`test_one_channel_failure_does_not_block_others`)
- **INTEG-OUT-5.** Only an answer from auth is cached, for
  `PREFERENCES_CACHE_TTL_SECONDS`: a contact, or `None` for a 404 (unknown or
  deactivated user). The `None` a missing token, a transport error, another non-200, or
  a malformed body answers (INTEG-OUT-6) is returned as `Uncached` and not stored, so the
  next event asks auth again. \
  Enforced in: `services/notifications/app/services/contact_client.py` (`ContactClient`); `services/common/herd_common/ttl_cache.py` (`TTLCache`, `Uncached`) \
  Pinned by: `services/notifications/tests/test_contact_client.py` (`test_failed_lookup_is_not_cached`, `test_not_found_is_an_answer_and_is_cached`, `test_missing_token_is_not_cached`)
- **INTEG-OUT-6.** The contact lookup (auth, internal token) answers `None` on a missing
  token, a transport error, a non-200, or a malformed body; email is then skipped,
  logged `email_no_recipient`, and chat names the user by id. \
  Enforced in: `services/notifications/app/services/contact_client.py` (`ContactClient`); `services/notifications/app/services/dispatchers/email.py` (`EmailDispatcher`); `services/notifications/app/services/dispatchers/chat.py` (`ChatDispatcher`) \
  Pinned by: `services/notifications/tests/test_contact_client.py` (`test_returns_none_on_transport_error`, `test_returns_none_on_404`, `test_no_token_returns_none`, `test_returns_none_on_malformed_json`); `services/notifications/tests/test_email_dispatcher.py` (`test_send_skips_when_contact_is_none`, `test_send_skips_when_contact_has_no_email`)
- **INTEG-OUT-7.** Email is plain text to the user's address, with the title as subject,
  sent on a worker thread; STARTTLS when `SMTP_USE_TLS`, and a login only when
  `SMTP_USERNAME` is set. \
  Enforced in: `services/notifications/app/services/dispatchers/email.py` (`_send_smtp`, `EmailDispatcher`) \
  Pinned by: `services/notifications/tests/test_email_dispatcher.py` (`test_send_smtp_builds_message_and_sends_plain`, `test_send_smtp_starttls_and_login_when_configured`, `test_send_smtp_tls_without_credentials_skips_login`, `test_send_runs_send_smtp_in_thread_when_configured`)
- **INTEG-OUT-8.** The webhook channel POSTs compact JSON `{user_id, event_type, title,
  body, data, dedupe_key}` signed `X-HERD-Signature: sha256=<hex>` over the exact bytes
  with `WEBHOOK_SIGNING_SECRET`, through the shared `sign_body` and
  `WEBHOOK_SIGNATURE_HEADER` that integration's registered webhooks also use (#1079), so
  the two outbound paths sign identically. \
  Enforced in: `services/notifications/app/services/dispatchers/webhook.py` (`WebhookDispatcher`); `services/common/herd_common/webhooks.py` (`sign_body`, `WEBHOOK_SIGNATURE_HEADER`) \
  Pinned by: `services/notifications/tests/test_outbound_dispatchers.py` (`test_webhook_posts_signed_when_configured`, `test_sign_body_is_hmac_sha256_hex`, `test_webhook_channel_signs_through_the_shared_helper`, `test_webhook_channel_bytes_and_headers_unchanged_by_shared_signer`)
- **INTEG-OUT-9.** The chat channel POSTs `{"text": "[<username>] <title>: <body>"}` to
  the one configured chat URL. \
  Enforced in: `services/notifications/app/services/dispatchers/chat.py` (`ChatDispatcher`) \
  Pinned by: `services/notifications/tests/test_outbound_dispatchers.py` (`test_chat_posts_when_configured`)
- **INTEG-OUT-10.** A message with no event key is sent without a claim and never
  deduplicated. \
  Enforced in: `services/notifications/app/services/dispatchers/outbound.py` (`already_delivered`, `_release`) \
  Pinned by: `services/notifications/tests/test_outbound_dispatchers.py` (`test_null_dedupe_key_is_not_deduplicated`, `test_release_is_noop_for_null_dedupe_key`)

**Out of scope.** Per-user chat or webhook credentials, and replies from chat. By
decision; see `FEATURES.md` (Notifications and dispatch channels).

### 8.13 Notification bell and settings page

**What it does.** A signed-in user sees a bell with an unread badge in the header, opens
it to read, mark read, or delete recent notifications, and chooses channels and event
kinds on the Settings page.

**Surfaces.** `frontend/src/components/NotificationBell.tsx`,
`frontend/src/pages/SettingsPage.tsx`, `frontend/src/api/notifications.ts`.

**Rules.**

- **INTEG-UI-1.** The bell renders nothing for a signed-out visitor; for a signed-in user
  it polls the unread count every 30 seconds and shows it as a badge when above zero. \
  Enforced in: `frontend/src/components/NotificationBell.tsx` (`NotificationBell`); `frontend/src/api/notifications.ts` (`useUnreadCount`) \
  Pinned by: `frontend/src/test/components/NotificationBell.test.tsx` (`returns null when not authenticated`, `shows unread badge when count > 0`); `frontend/src/test/api/notifications.test.tsx` (`useUnreadCount is disabled when not authenticated`); `tests/e2e/test_notifications_bell.py` (`test_notifications_bell_renders_for_admin`)
- **INTEG-UI-2.** Opening the panel refetches the 20 newest notifications and lists them,
  or shows `No notifications yet.`; its Settings button goes to `/settings`. \
  Enforced in: `frontend/src/components/NotificationBell.tsx` (`NotificationBell`) \
  Pinned by: `frontend/src/test/components/NotificationBell.test.tsx` (`opens the panel and lists notifications`, `shows empty state when there are no notifications`); `tests/e2e/test_notifications_bell.py` (`test_notifications_bell_opens_panel`, `test_notifications_panel_empty_state`, `test_notifications_settings_link_navigates`)
- **INTEG-UI-3.** Each listed notification is a list item holding two sibling buttons,
  never one inside the other: the notification itself and its `Delete notification`
  control, each a separate keyboard stop with its own accessible name; the list query
  runs only while signed in, the same gate as the unread count. \
  Enforced in: `frontend/src/components/NotificationBell.tsx` (`NotificationBell`, `handleDelete`); `frontend/src/api/notifications.ts` (`useNotifications`) \
  Pinned by: `frontend/src/test/components/NotificationBell.test.tsx` (`makes no list request while signed out`, `Delete removes that notification through the API and does not mark it read`, `the item and its Delete are separate keyboard stops with their own names`, `never nests a button inside a button`)
- **INTEG-UI-4.** Clicking an unread notification marks it read; Mark all read is
  disabled while nothing is unread. \
  Enforced in: `frontend/src/components/NotificationBell.tsx` (`handleItemClick`) \
  Pinned by: `frontend/src/test/components/NotificationBell.test.tsx` (`clicking an unread notification marks it read through the API`, `clicking a read notification sends nothing`, `Mark all read is disabled while nothing is unread`, `Mark all read is enabled while something is unread`); `tests/e2e/test_flows_effects_playwright.py` (`test_notification_round_trip`)
- **INTEG-UI-5.** The Settings page shows the in-app toggle, the three outbound toggles
  (off unless stored on), and one toggle per event kind for the seven kinds notifications
  renders (`Reservation failed` since #1077), each on unless stored off; Save sends all channels and events and toasts `Preferences saved` or
  `Failed to save preferences`. \
  Enforced in: `frontend/src/pages/SettingsPage.tsx` (`SettingsPage`, `EVENT_LABELS`, `OUTBOUND_CHANNELS`, `handleSave`) \
  Pinned by: `frontend/src/test/pages/SettingsPage.test.tsx` (`shows a loading state then renders all event toggles`, `shows the failed event on by default and sends an opt-out explicitly`, `renders outbound channel toggles defaulting off`, `toggling and saving sends the new payload to the API`, `toasts an error when the save call fails`); `tests/e2e/test_settings_page.py` (`test_settings_event_toggle_round_trip`, `test_settings_email_channel_round_trip`, `test_settings_shows_failed_event_toggle_checked_by_default`)

**Out of scope.** The header layout around the bell (`frontend/src/components/layout/AppLayout.tsx`).

## 9. Errors

| Status | Error key or detail | When | Rule |
|---|---|---|---|
| 401 | `Not authenticated` | no bearer header on any route of either service | INTEG-FACADE-1 |
| 401 | `Could not validate credentials` | bad, expired, or subject-less token | INTEG-FACADE-1 |
| 401 | `Invalid subject in token` | a notification route with a `sub` that is not a UUID | INTEG-INAPP-2 |
| 403 | `Admin or superadmin role required` | a webhook route without an admin role | INTEG-HOOK-1 |
| 404 | `Webhook not found` | unknown subscription on read, pause or resume, delete, or deliveries | INTEG-HOOK-6, INTEG-HOOK-7, INTEG-HOOK-9, INTEG-HOOK-10 |
| 422 | `target_url must resolve to a public address` | a webhook destination whose host is not public, not allowlisted, or does not resolve | INTEG-HOOK-2 |
| 404 | `Notification not found` | unknown or foreign notification on mark read or delete | INTEG-INAPP-6, INTEG-INAPP-9 |
| 422 | FastAPI validation envelope | a facade body that fails `V1ReservationRequest`; a facade reservation id that is not a UUID; a webhook body with a bad URL, unknown or empty event types, or an over-long field; a webhook PATCH body that is not exactly a boolean `is_active`; a facade list or notification list parameter out of range | INTEG-FACADE-4, INTEG-FACADE-6, INTEG-FACADE-15, INTEG-HOOK-2, INTEG-HOOK-3, INTEG-HOOK-9, INTEG-INAPP-3 |
| upstream status | upstream `detail`, body, or text | reservations refused a facade call | INTEG-FACADE-12, INTEG-FACADE-13 |
| 503 | `Reservations service unavailable` | reservations unreachable or slower than 10 seconds | INTEG-FACADE-14 |
| 503 | `user-profile unreachable` | user-profile unreachable on a preferences read or write | INTEG-PREFS-3 |
| 503 | `user-profile error: <status>` | user-profile answered 400 or above | INTEG-PREFS-3 |

Event outcomes. Neither consumer answers a caller; the outcome is the acknowledgement and
the log action.

| Outcome | Log action | When | Rule |
|---|---|---|---|
| acked, dead-lettered | `nats_poison_message` | the body is not JSON, or is JSON but not an object | INTEG-CONSUME-6, INTEG-CONSUME-7, INTEG-NCONSUME-6, INTEG-NCONSUME-7 |
| nacked with delay | `nats_message_nak` | a handler error before the fifth delivery | INTEG-CONSUME-8, INTEG-NCONSUME-8 |
| acked, dead-lettered | `nats_dlq_exhausted` | a handler error at the fifth delivery | INTEG-CONSUME-8, INTEG-NCONSUME-8 |
| acked, ledger row `dead` | `webhook_delivery_dead` | every POST attempt to a receiver failed | INTEG-HOOK-14 |
| acked, ledger row `failed` | `webhook_destination_refused` | the destination was not allowed when the delivery was due | INTEG-HOOK-23 |
| acked, nothing sent | `notification_deduped` | the user or channel already has this event | INTEG-INAPP-1, INTEG-OUT-3 |
| acked, channel skipped | `outbound_dispatch_failed` | an email, chat, or webhook-channel send failed | INTEG-OUT-4 |

## 10. Interactions with other services

| Direction | Peer | Call | Purpose | On failure |
|---|---|---|---|---|
| Out (integration) | reservations | `POST /`, `GET /`, `GET /{id}`, `DELETE /{id}`, `PUT /{id}/release`, `GET /{id}/wiring-status` (caller's JWT, 10 s) | every facade route | Fail closed: 503 (INTEG-FACADE-14); a refusal is relayed (INTEG-FACADE-12) |
| Out (integration) | external receiver | `POST <target_url>` (`WEBHOOK_DELIVERY_TIMEOUT_SECONDS` per attempt, redirects not followed) | deliver an event | Retried, then a `dead` row; never fails the message (INTEG-HOOK-14, INTEG-HOOK-19); a destination not allowed is a `failed` row with no POST (INTEG-HOOK-23) |
| Out (integration) | DNS | resolve the `target_url` host at registration and before each delivery | the destination rule | Fail closed: 422 at registration, a `failed` row at delivery (INTEG-HOOK-2, INTEG-HOOK-23) |
| Out (notifications) | auth | `GET /internal/admins` (internal token, 5 s), cached | health recipients | Fail open: no admin recipients for that event, not cached (INTEG-HEALTH-4, INTEG-HEALTH-5) |
| Out (notifications) | reservations | `GET /internal/active-users?device_id` (internal token, 5 s) | health recipients | Fail open: no holder recipients (INTEG-HEALTH-6) |
| Out (notifications) | user-profile | `GET /preferences/internal?user_id` (internal token, 5 s), cached | a recipient's preferences | Fail open: defaults for that event, not cached (INTEG-PREFS-6, INTEG-PREFS-7) |
| Out (notifications) | user-profile | `GET /preferences`, `PATCH /preferences` (caller's JWT, 10 s) | the preferences proxy | Fail closed: 503 (INTEG-PREFS-3) |
| Out (notifications) | auth | `GET /internal/users/{id}/contact` (internal token, 5 s), cached | email address, chat username | Fail open: email skipped, chat by id, not cached (INTEG-OUT-5, INTEG-OUT-6) |
| Out (notifications) | SMTP server, chat URL, outbound webhook URL | SMTP send, `POST` (the channel's timeout) | outbound channels | Logged and swallowed; claim released (INTEG-OUT-3, INTEG-OUT-4) |
| Out (both) | NATS | DLQ publish | dead letters | Logged; the message is still acked (INTEG-CONSUME-9, INTEG-NCONSUME-9) |

## 11. Configuration

[ENV_VARS.md](../ENV_VARS.md) has the full list.

| Setting | Default | Effect |
|---|---|---|
| `WEBHOOK_DELIVERY_TIMEOUT_SECONDS` (integration) | `10.0` | Limit for each POST attempt to a receiver |
| `WEBHOOK_DELIVERY_ATTEMPTS` (integration) | `4` | Attempts in all before a `dead` row |
| `WEBHOOK_TEST_SINK_ENABLED` (integration) | `false` | Registers the echo sink; only the development and test override sets it |
| `WEBHOOK_ALLOWED_HOSTS` (integration) | empty | Hostnames or CIDRs admitted as webhook destinations although not public (INTEG-HOOK-2); the development and test override sets `integration,reservations`; a malformed CIDR refuses to start |
| `NATS_ACK_WAIT_SECONDS` (both) | `30` | In-flight window of every durable here; the override pins integration's to `4` (INTEG-SINK-4); below 2 refuses to start |
| `NATS_NAK_BACKOFF_SECONDS` (both) | `1,5,15,60,120` | Delay before each redelivery of a nacked message |
| `RESERVATIONS_SERVICE_URL` (both) | `http://reservations:8000` | The facade's upstream; notifications' holder lookup |
| `PREFERENCES_CACHE_TTL_SECONDS` (notifications) | `30` | Cache for preferences and contacts |
| `HEALTH_NOTIFY_ADMIN_CACHE_TTL_SECONDS` (notifications) | `60` | Cache for the admin list |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_TLS`, `SMTP_TIMEOUT_SECONDS`, `EMAIL_FROM` (notifications) | empty, `587`, empty, empty, `true`, `10`, empty | Email transport (INTEG-OUT-2, INTEG-OUT-7) |
| `CHAT_WEBHOOK_URL`, `CHAT_TIMEOUT_SECONDS` (notifications) | empty, `10` | Chat transport |
| `OUTBOUND_WEBHOOK_URL`, `WEBHOOK_SIGNING_SECRET`, `WEBHOOK_TIMEOUT_SECONDS` (notifications) | empty, empty, `10` | Instance-level webhook channel |
| `INTERNAL_API_TOKEN` (notifications) | empty | Token for every internal call notifications makes |

Fixed in code, not configurable: five deliveries before a dead letter; one message per
fetch with a 5 second wait; the facade's 10 second upstream timeout; webhook retry delays
of 0.5 seconds doubling to 5; 5 second internal lookups and 10 second preference proxy
calls in notifications; the 20-item bell list and its 30 second unread poll.

## 12. Test coverage map

| Level | Where | Notes |
|---|---|---|
| Unit | `services/integration/tests/` (`test_facade.py`, `test_webhooks.py`, `test_webhook_destinations.py`, `test_webhooks_router_direct.py`, `test_nats_consumer.py`, `test_nats_consumer_lifecycle.py`, `test_nats_consumer_heartbeat.py`, `test_version.py`, `test_config_ack_wait.py`); `services/notifications/tests/` (every file); `tests/unit/test_consumer_heartbeat_wiring.py`; the frontend tests named in section 8.13 | In-memory SQLite, upstreams and NATS stubbed; SQLite does not enforce the cascade of INTEG-HOOK-8 |
| Functional (through the service API) | the httpx-against-the-app tests in `test_facade.py`, `test_webhooks.py`, and `services/notifications/tests/test_router.py`; `services/notifications/tests/test_functional_dispatch_path.py` | The facade's upstream is a stubbed transport |
| Integration (running stack) | `tests/integration/test_v1_facade.py`, `test_webhooks_flow.py`, `test_webhook_slow_receiver_live.py`, `test_notifications_flow.py`, `test_notification_channels_flow.py`, `test_reservation_failed_notification.py`, `test_health_alerting_flow.py`, `test_nats_consumer_configs_live.py`; `tests/contract/test_openapi_schema.py` | The health tests publish the event straight to `HERD_HEALTH` with a fresh `event_id` rather than driving the poller. The slow-receiver test binds a peer consumer and flakes on a stack other sessions use |
| Stress and load | `tests/load/locustfile.py` (`NotificationUser`: unread count, list, preference reads and writes) | Nothing loads the facade, webhook fan-out, or the consumers |
| Browser end-to-end | `tests/e2e/test_notifications_bell.py`, `tests/e2e/test_settings_page.py` | `tests/e2e/test_flows_effects_playwright.py` (`test_notification_round_trip`) clicks mark read; nothing clicks delete in a browser; the facade and webhooks have no interface |

Not run for this document: nothing here was checked against a running stack. The unit,
functional, integration, contract, load, and browser suites were read, not run; only
`tests/unit/` was run. The facade's generated OpenAPI document was compared with
`docs/api/v1-openapi.json` by building the app in-process.

## 13. Known limits and gaps

Two documents are incomplete against the code this specification describes, tracked in
#1080: [ROLES.md](../ROLES.md) lists none of this area's routes, and
[ARCHITECTURE.md](../ARCHITECTURE.md) omits the facade's wiring status route.

### Open defects


### Limits by decision

- The facade list forwards only `skip` and `limit` (INTEG-FACADE-6). Recorded in issue
  #959 and the docstring of `test_list_does_not_pass_through_internal_filters`.
- The facade relays wiring status verbatim and does not expose the manual wiring retry
  (INTEG-FACADE-10, INTEG-FACADE-11). Recorded in the docstring of
  `get_reservation_wiring_status`.
- The contract version stays `1.0.0` and the product version is served only by
  `GET /version`, outside the OpenAPI document (INTEG-VERSION-1, INTEG-VERSION-2).
  Recorded in issue #846 and the comments in `services/integration/app/main.py`.
- Webhook delivery is at least once: a receiver can see an event twice after a crash
  mid-POST, and must dedupe on `event_id`. Recorded in
  [EXTERNAL_API.md](../EXTERNAL_API.md) (Delivery semantics).
- A delivery error never fails the message, so one receiver's failure is not retried by
  redelivery unless the event is republished (INTEG-HOOK-19). Recorded in the comment in
  `handle_event`.
- Outbound notification channels are at most once (INTEG-OUT-3). Recorded in issue #83
  and the docstring of `OutboundDelivery`.
- Notification lists page by `offset` with `limit` up to 200, and `total` ignores
  `unread_only` (INTEG-INAPP-3, INTEG-INAPP-4). Recorded in the CHANGELOG entry for issue
  #597.
- A health event whose recipients cannot be resolved is lost (INTEG-HEALTH-7). Recorded
  in the docstring of `services/notifications/app/services/health_recipients.py`.
- The webhook subscription secret is stored in the integration schema as given, because
  every signature needs it. Recorded in the comment on `WebhookSubscription.secret`.
- Chat and the outbound webhook are instance-level destinations with no per-user
  credentials. Recorded in `FEATURES.md` (Notifications and dispatch channels).

### Rules with no test

- INTEG-FACADE-13: the error detail fallbacks for a body without `detail` or not JSON.
- INTEG-VERSION-4: the published contract compared with the running service.
- INTEG-HOOK-4: repeated event names stored once.
- INTEG-HOOK-8: subscription delete cascades to its ledger rows.
- INTEG-HOOK-15: a `dead` row retried and overwritten on redelivery.
- INTEG-HOOK-16: the concurrent ledger insert race.
- INTEG-HOOK-22: the ledger key with neither an event id nor metadata.
- INTEG-INAPP-7: marking a read notification read again.
- INTEG-PREFS-5: `channels` replaced whole on a write.
