# HERD Improvement Directions

This document is a standing, forward-looking assessment of how HERD gets faster, more
secure, and better structured. It is deliberately separate from
[PLANNED_FEATURES.md](../PLANNED_FEATURES.md) (new capabilities) and
[FEATURES.md](../FEATURES.md) (what ships today): this file is about the quality of what
already exists, the non-functional work, and the refactors worth doing before the
codebase grows further.

It doubles as the scoping document for a larger refactor. Nothing here is a new feature;
it is all about making the current thirteen services faster, safer, and easier to change.
Each item is grounded in a specific file and, where one exists, a tracking issue. Items
are ordered within each section by impact.

Last substantive review: 2026-07-02 (four read-only audit passes: security, integration
correctness, performance, source-vs-symptom).

## Security

The four issues previously tracked here (config session-secret forgeability #246,
cleartext `field_data` password read #247, api-token role clamp #248, and the
non-constant-time internal-token comparison #311) have all closed and are pruned per this
doc's own rule. Remaining security-adjacent work is tracked in the issue queue rather than
here (at the time of this refresh: the assistant message-retention decision in
[#338](https://github.com/vendrabuck/HERD/issues/338)).

One lower-severity observation was examined and deliberately not filed as a bug, but is
worth noting for a hardening pass: outbound webhook targets are admin-registered with no
SSRF allowlist (within admin authority today, but worth an allowlist when multi-tenancy
lands).

## Performance and scalability

The backend hot paths are in good shape: conflict detection is an indexed join, the list
endpoints paginate and eager-load, the calendar endpoint bounds its window span (issue
#315), the reporting rollup batches its lookups, the
outbox relay claims with `FOR UPDATE SKIP LOCKED`, and per-event connection pooling
(issue #137, landed) removed the provisioning fan-out. The remaining quantifiable costs are
all in the frontend data layer fanning out per item because the backend exposes no batch
endpoints.

- **Per-pair pathfind fan-out ([#249](https://github.com/vendrabuck/HERD/issues/249), landed).**
  The reservation Routes tab used to issue one `POST /cabling/pathfind` per device pair
  (n(n-1)/2 requests for n reserved devices, 435 at n=30), and the topology editor one
  per edge on every canvas load, with each server call rebuilding the entire fabric
  component (`services/cabling/app/services/pathfind_service.py` `build_adjacency_graph`).
  `POST /cabling/pathfind/batch` now builds the graph once and answers all pairs
  (capped at 2000 per request) in memory, and `usePathfindPairs` sends one request, so
  both UI paths collapsed O(pairs) graph builds to one.
- **Per-device hydration fan-out ([#250](https://github.com/vendrabuck/HERD/issues/250), landed).**
  The topology editor used to fetch each canvas device with an individual
  `GET /inventory/devices/{id}` on every open. `POST /inventory/devices/batch` (one
  `id.in_(...)` query, capped at 500 ids, with the same visibility filtering and
  password redaction as the existing device reads) now replaces n round trips with
  one; the editor's hydration calls it.

Deferred-but-known: the reporting rollup loads an unbounded in-window result set into
memory and is fine at current volumes but should gain a guard before long-window
heavy-usage reports become common
(`services/reservations/app/services/reporting_service.py:65-73`).

## Reliability and correctness

- **AI provider misconfiguration returns 500 instead of 503 ([#245](https://github.com/vendrabuck/HERD/issues/245)).**
  An unrecognized `AI_PROVIDER` makes `get_ai_client` raise a bare `RuntimeError` that
  escapes as a 500 before the route's 503 gate runs, because the client is a FastAPI
  dependency resolved first. `ai_is_configured()` and `get_ai_client()` disagree about the
  same state; reconciling them at the source restores the documented degradation contract.

## Structural and refactor directions

These are not bugs; they are shape improvements that reduce the cost of the next change.
They are the core of the "larger refactor another time" scope.

- **Extract the per-switch driver-execution loop.** `_execute_switch_operations` (L1),
  `_execute_l2_switch_operations`, and `_execute_l3_switch_operations` in
  `services/execution/app/services/nats_consumer.py` now share a large, near-identical
  skeleton: resolve adjacent switches, per switch load the driver, one login, a guarded
  per-item action loop with ExecutionRun bookkeeping, one logout. The three copies drift
  (the L2/L3 deprovision ordering difference is real and load-bearing, but the login /
  logout / ExecutionRun / dedupe scaffolding is boilerplate). A shared helper that takes a
  per-connection-type "plan" (the ordered list of guarded actions plus their kwargs and
  identity) would collapse three ~250-line functions into one skeleton plus three small
  planners, and would make a future Layer 4 or vendor-specific contract a planner rather
  than a fourth copy. This is the single highest-leverage refactor in the backend.
- **A shared internal-service HTTP client in `herd_common`.** Every service hand-rolls
  `httpx.AsyncClient(...)` calls to peers with an inline timeout and an `X-Internal-Token`
  header, and each re-implements the 5xx-is-transient / 404-is-absent classification
  (`nats_consumer._get_internal` is the most developed version). Promoting that helper
  into `herd_common` (with the retry-with-backoff helper already there, and the shared
  `internal_auth.internal_token_matches` verifier now covering the inbound side) would give
  every service one consistent, correctly-classifying client and remove a class of
  copy-paste drift.
- **Frontend batch-fetch hooks.** The two perf items above are symptoms of the same
  structural gap: `frontend/src/api/` has per-item fetch hooks but no batch primitives.
  Adding `useDevicesByIds` and `usePathfindBatch` (backed by the new endpoints) and making
  them the default in the editor and Routes tab removes the fan-out at the source rather
  than capping concurrency downstream.
- **Consolidate the config-schema resolution path.** Driver-published schema vs registry
  fallback is resolved in inventory (`_validate_config_for_device`), consumed in execution
  (config extraction), and read again by the AI assistant. The resolution rule is
  documented but lives in three places; a single `herd_common` resolver would keep them
  from diverging.

## How to use this document

When starting non-feature work, check here first: if it is listed, the grounding and the
fix direction are already scoped. When a review or audit surfaces something new, add it
here with a file reference and (if filed) an issue link, ordered by impact within its
section. Prune items when their issue closes.
