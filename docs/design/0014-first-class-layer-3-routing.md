# Decision: First-Class Layer 3 Routing Intent, Issue #34

Status: Accepted 2026-09-08 (all six decisions made by Lane on 2026-09-08:
1 and 2 outright, 3, 4, and 6 confirmed as proposed, 5 resolved to the
strict fail-closed variant; see Decision). No code in this doc. Context verified against the
live HERD-public tree on 2026-09-08 (main at 4ea94541). Symbols are the
stable reference; line numbers are as of that commit.

## Context

Issue #34 asks for Layer 3 routing as a first-class construct: a dedicated
connection type with a configuration model decoupled from the physical
cabling 4-tuple, a distinct editor treatment, and a validate gate that judges
L3 under routing rules rather than the L1/L2 physical BFS. The issue's own
2026-07-31 comment sharpened the ask: the canvas L3 layer is decorative today
and every L3 provisioning decision is inferred from device roles. This ADR
records what exists, then decides what "first-class" means.

Relevant existing fabric, verified:

- Route content is device-scoped. An L3 switch's routes are the `routes`
  array of its latest inventory config version under the `Layer 3 Switch`
  schema (`services/common/herd_common/device_config.py:53-102`: `interfaces`
  with `name`, `ip`, `zone`; `virtual_routers` with `name`, `interfaces`;
  `routes` with `destination`, optional `next_hop`, `interface`). Execution
  reads it through `_FetchContext.get_latest_config`
  (`services/execution/app/services/nats_consumer.py:262-285` and `:406-410`)
  and applies `detail["config"]["routes"]` (`:3120-3123`). Every reservation
  that touches the switch gets the same routes.
- Adjacency is inferred from device roles. `_derive_l3_adjacency`
  (`nats_consumer.py:2798-2831`) marks a reservation adjacent to a switch when
  a resolved L1 hop has a `Layer 3 Switch` on exactly one end. The intended
  wires it walks are L1-filtered (`:334-371`, `:370`).
- Pins are per reservation. `route_assignments`
  (`services/execution/app/models/route_assignment.py:31-70`) stores the
  applied route list per `(reservation_id, device_id)` with a partial unique
  index on ACTIVE rows; `route_service.py` owns the lifecycle (`:65`
  `record_route_active`, `:202` `record_route_failed`, `:278`
  `release_route_membership`, `:385` `get_effective_pinned_routes`). Issue
  #20 semantics: pin what was applied, remove exactly that. ADR 0009 kept
  this unchanged and named this issue as the later, out-of-scope step
  (`docs/design/0009-l2-l3-connection-driven-reconcile.md:171-176`).
- The canvas L3 layer is read by nothing that provisions. Every resolved hop
  is a hardcoded `layer="L1"` `WireSpec`
  (`services/cabling/app/services/fork_save_service.py:23-28` and `:408`);
  `fork_connections.layer` is therefore uniformly `L1`. The per-line layer
  in the wiring dialog is a canvas annotation by decision (ADR 0009 option
  C, `frontend/src/components/topology-editor/WiringDialog.tsx:82-92`,
  `docs/TOPOLOGY_EDITOR.md:89-95`). The AI committer emits it onto the
  canvas and nothing else reads it
  (`services/ai-orchestrator/app/services/committer.py:216-224`).
- Validation is physical for every edge. `_run_topology_validation`
  (`services/cabling/app/routes/topologies.py:205-340`) reads `layer` only to
  echo it back (`:265`), classifies element edges (`:277-310`), and runs the
  shortest-path batch over everything else (`:325-334`). The reason
  vocabulary is `missing_device`, `no_path`, `element_to_element`,
  `element_edge_no_port` (`services/cabling/app/schemas/topology.py:92-110`).
  Reservations consumes only `valid`, `device_ids`, and the `invalid_edges`
  summaries (`services/reservations/app/services/reservation_service.py:213-273`).
- Cabling holds no L3 data. `connections` has no layer or route column
  (`services/cabling/app/models/connection.py:14-29`); the fork tables carry
  the L1 hops and the canvas JSON (`services/cabling/app/models/fork.py`).
  The issue's acceptance criterion "existing minimal L3 data migrates
  without loss" therefore has no referent and is retired by this ADR.
- Network elements (ADR 0012) are canvas nodes with a closed four-type
  vocabulary and free-form `attrs` that nothing reads
  (`frontend/src/lib/networkElements.ts:17-22`,
  `frontend/src/types/topology.types.ts:29-37`); an attachment is valid with
  no BFS and is skipped at fork save (`fork_save_service.py:341-347`).

What no current model can express: this topology wants these routes on that
switch. That is the whole gap. Adjacency, pinning, and the driver contract
already work; they simply have nothing reservation-specific to carry.

## Decision

First-class L3 means reservation-scoped routing intent: a per-switch route
table that lives in the topology, is edited as its own construct, is
validated under routing rules, rides the fork like every other intent, and
is what execution applies. Six decisions follow.

### 1. Intent lives on the L3 switch's canvas node (decided by Lane)

A device node whose device is a `Layer 3 Switch` may carry
`data.l3 = {"routes": [...]}` in `canvas_data`, each route
`{"destination": str, "next_hop": str | null, "interface": str,
"virtual_router": str | null}`. The key set mirrors the config schema's
`routes` items plus the optional virtual-router grouping the issue asks for;
`additionalProperties` is refused at the cabling boundary the way element
edges are classified there, not in the frontend alone.

Why the node and not a new topology table: `canvas_data` is already the
source of truth for everything a topology expresses (devices, edges, element
nodes and their attrs), and the fork copies, versions, diffs, and restores it
without any per-construct code. Intent stored on the node gets fork version
history, preview, diff, and restore for free, the same way ADR 0012's
elements did. A separate relational store for pre-reservation intent would
have to be mirrored into the fork and versioned by hand.

Resolved intent gets its own table. Fork save and fork activation resolve
the canvas intent into `fork_l3_routes` (cabling migration 0011): `id`,
`fork_id`, `device_id`, `destination`, `next_hop` (nullable), `interface`,
`virtual_router` (nullable), `created_by`, `created_at`, unique on
`(fork_id, device_id, destination, interface, next_hop)` with the same
NULL-safe treatment the fork tables use. This is the L3 analogue of
`fork_connections`: the canvas is what the user edits, the table is what
execution reads, and the two never disagree because the same reconcile
writes both under the fork row lock (issue #626 discipline). Its rows are
identity-keyed like `fork_connections`, so a save is a set reconcile
(release, build, unchanged) and the fork version appends exactly once.

### 2. Topology intent wins; config-version routes are the fallback (decided by Lane)

When a fork carries `fork_l3_routes` rows for a switch, those rows are the
route content execution applies for that reservation. When it carries none
for a switch, execution keeps reading the switch's latest config version
exactly as today. Precedence, not union and not replacement: a union would
make teardown ambiguous (which routes belong to whom), and replacement
would break every existing lab that never set intent.

Consequence for the pin lifecycle: unchanged in shape. `route_assignments`
still records exactly what was applied, and teardown still removes exactly
that. Only the source of the applied list changes, and only when intent is
present.

### 3. A fork save reconciles the route set on switches with intent (decided by Lane)

On a switch that stays adjacent across a fork save, execution diffs the new
intended route list against the pinned list: routes that left get
`remove_route`, routes that arrived get `configure_route`, unchanged routes
are not touched, and the pin is advanced to the applied set. This is the
same shape as the L2 membership full-reconcile, keyed on the route identity
`(destination, interface, next_hop)` that `_route_run_identity` already
packs (`nats_consumer.py:413-424`).

ADR 0009's rule "latest config version at provision time, pinned, never
re-derived" stays true for switches running on config-version routes: their
content is still read once at first adjacency and never refreshed by a save.
Only intent-driven switches re-derive, because for them the fork IS the
intent and a save that changes it must land, or editing intent in the fork
would do nothing until the reservation ends, which is the decorative failure
this issue exists to fix.

Direction scoping and freeze rules are unchanged: release-direction route
removals run while frozen, build-direction additions do not (ADR 0009
decision 5), and the #412 invariant (an ACTIVE row is immutable to failure
writers) extends to route-set deltas.

### 4. The editor construct is a per-switch Routing panel; the L3 edge layer stays an annotation (decided by Lane)

Selecting a device node whose device is a `Layer 3 Switch` exposes a
Routing panel: the route table (destination, next hop, interface, virtual
router), add and remove rows, and an "Import from device config" action
that copies the switch's current config-version `routes` as a starting
point. The panel writes `data.l3` on the node and marks the canvas dirty
like any edit. A switch node carrying intent shows a small route-count badge
so the construct is visible on the canvas without opening the panel.

The per-line L3 layer on edges keeps ADR 0009 option C: an annotation,
provisioning never reads it. The issue's phrase "dedicated connection type
rather than a layer flag on a physical link" is honored by giving L3 a
model of its own rather than by making the layer flag load-bearing; a route
has a destination, a next hop, and an egress interface, and forcing that
onto a point-to-point edge is the mismatch the issue names. The wiring
dialog, the quick-connect popover, and the layer palette are untouched.

### 5. Validation fails closed, on facts and on the undecidable alike (decided by Lane)

`_run_topology_validation` gains an L3 pass over every device node carrying
`data.l3`, reported in a new additive `invalid_routes` list beside
`invalid_edges`; `valid` is false when either list is non-empty, so the
reservations gate (`_validate_topology_connectivity`) agrees with no shape
change on its side beyond folding `invalid_routes` into its error summary.
Each entry is `{node_id, device_id, index, reason, detail}`; a switch-level
refusal uses index `null`.

Lane chose the strict variant, matching the repo's rule for boundaries that
guard provisioning: anything HERD cannot verify is refused, never assumed.
Reason vocabulary:

- `l3_not_a_router`: the node's device is not a `Layer 3 Switch`.
- `l3_switch_unconfigured`: the switch has no latest config version, or its
  config lists no `interfaces`. A route needs a real egress interface and
  HERD cannot know one exists without the config, so intent on an
  unconfigured switch is refused rather than passed unverified. The Routing
  panel's import action makes this cheap to satisfy: configure the switch
  first, then express intent.
- `l3_bad_destination`: `destination` is not a parseable IP prefix
  (`ipaddress.ip_network(strict=False)`).
- `l3_bad_next_hop`: `next_hop` is present and not a parseable IP address.
- `l3_unknown_interface`: `interface` is not among the config's interface
  names.
- `l3_next_hop_unverifiable`: `next_hop` is present and the named interface
  carries no `ip`, or an `ip` without a prefix length, so subnet membership
  cannot be checked. Interface routes (no `next_hop`) are exempt.
- `l3_next_hop_outside_interface`: the interface's prefixed `ip` is present
  and `next_hop` is not inside that network.
- `l3_switch_unattached`: the switch node carrying intent has no valid
  device edge in the topology (nothing can reach it, so its routes serve
  nothing). Element attachments do not count as wiring.

Duplicate route identities within one switch are collapsed, not refused
(the set reconcile makes them harmless). Virtual-router names are not
validated against the config's `virtual_routers` in phase 1: the config's
grouping is a description of the device, and intent may legitimately name a
router the device does not describe yet; recorded as a follow-up check.

Cabling fetches the switch's config version through inventory's existing
internal latest-config route with the internal token, batched per
validation call and memoized. An inventory transport error or non-2xx (other
than the 404 that means "no config version", which is `l3_switch_unconfigured`)
fails the validate call closed with 503 and a pinned detail
(`l3_config_unavailable`), on both the user route and the internal route;
reservations already maps a non-2xx from the internal gate to a refused
create, so a routed topology cannot commit while inventory is down. This is
the same posture as cabling's bulk-create device-group check and the secrets
reverse guard, and deliberately not the single-read device-group guard's
fail-open: a topology without any `data.l3` never triggers the fetch, so the
outage only ever blocks topologies that actually need the check.

### 6. Execution consumes fork intent; the driver contract is untouched (decided by Lane)

Execution's intended-wires fetch (`_fetch_fork_intended_wires`) reads the
additive `l3_routes` list cabling adds to `GET /internal/forks/{rid}` (and
to the listing payload where the heal needs it). `_reconcile_l3_adjacency`
resolves route content per adjacent switch in this order: an effective
pinned set on a switch without intent (today's rule), else the fork's intent
for that switch, else the latest config version. Switches with intent go
through the decision 3 delta. `configure_route` and `remove_route`
signatures, the mock L3 driver, and `docs/DRIVERS.md`'s contract text do not
change; the "where routes come from" paragraph there is amended.

Why in scope: every acceptance criterion in the issue is on the topology and
UX side, and all of them are satisfiable by data that provisions nothing.
That is precisely the current state the 2026-07-31 comment calls decorative.
The issue's out-of-scope clause excludes changes to the driver contract and
its execution-side plumbing, not the choice of which route list to hand that
plumbing; this ADR changes only the latter.

## Contract summary

Canvas (topology and fork `canvas_data`), on a device node:

```
"data": {
  "deviceId": "...",
  "l3": {
    "routes": [
      {"destination": "10.20.0.0/24", "next_hop": "10.0.0.2",
       "interface": "eth1", "virtual_router": "default"}
    ]
  }
}
```

Cabling table `fork_l3_routes` (migration 0011), written by `save_fork` and
`create_fork` under the fork row lock, released by prune and archive the way
`fork_connections` rows are.

Cabling API, additive: `GET /internal/forks/{rid}` and the fork listing gain
`l3_routes: [{device_id, destination, next_hop, interface, virtual_router}]`;
`POST /topologies/{id}/validate` and `/validate/internal` gain
`invalid_routes` and a 503 `l3_config_unavailable` refusal; `ForkSaveResponse` gains `l3_routes_built` and
`l3_routes_released` counts. Contract snapshots regenerate additively.

Reservations: `_validate_topology_connectivity` folds `invalid_routes` into
its `ValueError` summary with the same first-five truncation; the user-facing
message names routes as `<device short id>[<index>] (<reason>)`.

Execution: `reservation.wiring_changed` handling reads `l3_routes` from the
fork; route content precedence as in decision 6; the route-set delta as in
decision 3; wiring-status L3 rows keep their shape (`route_count` now
reflects the applied set, which may differ per reservation).

Frontend: Routing panel, route-count badge, `invalid_routes` rendered in the
validation result next to invalid edges, fork diff extended so a route
change shows as a change to the switch node (the edge diff keys are
untouched), `data.l3` preserved by `persistableCanvas` and by the AI
committer's canvas builder (which never emits it in phase 1).

## Delivery phases (each independently mergeable)

1. Cabling model and validate: migration 0011, the `fork_l3_routes` model,
   the canvas-side parser that refuses unknown keys, the resolver in
   `save_fork` and `create_fork` (set reconcile under the fork lock, counts
   on the response), `l3_routes` on the internal fork routes, the L3
   validation pass with the eight reasons and the 503, `invalid_routes` on both validate
   routes, prune and archive handling, reservations' gate folding
   `invalid_routes` into its error, contract snapshots. Unit tests for every
   reason, the resolver's set arithmetic, and the retry-loop reapply path;
   an integration test that validates a routed topology, commits a
   reservation, and reads the fork's `l3_routes` back.
2. Editor: the Routing panel, import from device config, the badge,
   validation display, fork diff of `data.l3`, persistence through save,
   autosave, fork commit, restore preview. Vitest for the panel and the
   diff; a live Playwright pass through add route, save, validate, commit,
   read back, since the canvas is a jsdom blind spot by standing rule.
3. Execution: `l3_routes` in the intended-wires fetch, precedence, the
   route-set delta on adjacent switches with intent, pin advancement,
   direction scoping, wiring-status counts. Unit tests for precedence and
   the delta (add-only, remove-only, mixed, unchanged, frozen release
   direction); integration tests against `mock_l3` for first adjacency with
   intent, a save that changes intent on a live reservation, a save that
   removes all intent (falls back to config routes? No: a switch whose
   intent is removed keeps its applied set until it loses adjacency or the
   reservation ends; decided here to avoid a surprise teardown mid
   reservation), and cancel teardown removing exactly the applied set.
4. Docs and manual: `docs/TOPOLOGY_EDITOR.md`, `docs/USER_GUIDE.md`,
   `docs/DRIVERS.md` (route source paragraph), `docs/ARCHITECTURE.md`,
   `docs/ROLES.md` if any route changes visibility (none planned),
   `FEATURES.md` and `PLANNED_FEATURES.md` status flip, CHANGELOG, and the
   published manual's topology and live-editing pages.

## Testing

The five QA levels apply to every phase: unit (both services and the
frontend), functional (route handlers direct), integration (the stack, with
`mock_l3`), stress (the load profile is unchanged; one locust task validates
a routed topology), and e2e (the phase 2 Playwright pass, gated on the
seeded stack). Every validation reason has a unit test that pins the exact
reason string and a positive control. The set reconcile has the same
identity tests `fork_connections` has (move, release, build, unchanged,
duplicate collapse). Execution's delta tests assert driver call order:
removals before additions on one switch inside one login and logout, the
batching rule `docs/DRIVERS.md` already documents.

## Out of scope

- The driver contract (`configure_route`, `remove_route`) and dynamic
  routing protocols (BGP, OSPF): unchanged, as the issue states.
- AI-proposed routing intent: the generator's tool schema cannot propose an
  L3 config shape today (`services/ai-orchestrator/app/services/ai_client.py:200-212`);
  a follow-up issue extends the proposal with `l3` per switch once phase 2
  has an editor to land it in.
- Validating `virtual_router` against the switch config's `virtual_routers`
  and modeling DUT addressing (which would let a route's usefulness be
  judged, not only its shape): follow-ups.
- Element-anchored routing (a `subnet` element as a route destination):
  composes with this model later, the way anchored VLANs compose with ADR
  0012's elements, and is not needed for phase 1.
- Bulk import and export of routing intent: the topology importer and
  exporter carry `canvas_data` whole, so intent rides along; a CSV column
  is not added.

## Amendment: phase 1 delivery (2026-09-08)

Phase 1 (cabling model and validate) shipped as designed, with three points
worth recording against the Decision and Contract summary sections above,
none of which change the shape decided there.

**A ninth validation reason, `l3_malformed`.** Decision 5 lists eight reasons
and calls the canvas shape itself out of scope for enumeration ("`data.l3`
must be an object whose only key is `routes`..." lives in the canvas parser,
not the validation reason list). Implementation surfaced that a malformed
`data.l3` shape needs its own reason so it can be reported alongside every
other switch, rather than raised as an exception that would abort validating
the rest of the topology: `l3_malformed` (index null, `detail` carrying the
parser's message) is evaluated first, before `l3_not_a_router`, and stops
further evaluation for that switch, exactly like the other switch-level
reasons. It is also the 422 the two fork write paths (save, create_fork) map
a malformed shape to, distinct from the 409 every other `invalid_routes`
entry produces (see the write-path refusal shape below).

**The route identity is a stored column, `route_key`.** The Contract summary
names the `fork_l3_routes` unique constraint as
`(fork_id, device_id, destination, interface, next_hop)`. The shipped schema
instead stores the packed identity `f"{destination}|{interface}|{next_hop or
''}"` as its own `route_key` column (`String(200)`) and uniques on
`(fork_id, device_id, route_key)`. This is the same three-field identity
Decision 3 already points at (`_route_run_identity`'s
`(destination, interface, next_hop)`), packed into one string instead of
three columns so the set-reconcile's identity tuple and the database
constraint are the same value, never three columns that could drift out of
sync with the reconcile's own notion of identity. `RouteSpec.route_key` (the
canvas parser's dataclass) and the stored column are the same string by
construction.

**The inventory internal batch route was added.** Decision 5 says device type
comes from inventory "batched per validation call"; inventory had a
single-device internal read (`GET /devices/{id}/internal`) but no batch form,
so `POST /internal/devices/batch` was added (`X-Internal-Token`, body
`{"device_ids": [...]}` capped at 500, returning
`[{id, name, connection_type, status}]`), with its own unit tests and an
additive inventory contract snapshot entry. The L3 validation pass batches
every L3-carrying switch's device-type lookup into one call per validation
request and memoizes both device type and each switch's latest config version
per device id for the rest of that call.

Two more implementation notes, neither a deviation: the fork write paths'
refusal shape (as shipped in the initial phase 1 delivery: save and create_fork
both ran the validation pass first and refused before writing any
`fork_l3_routes` row, 422 `{"error": "l3_intent_malformed", "node_id",
"message"}` for a malformed switch, otherwise 409
`{"error": "l3_intent_invalid", "invalid_routes": [...]}` for any other
refusal) was implementable directly from Decision 5's reason vocabulary plus
the Contract summary's write-path sketch, so it needed no separate decision.
**This shape changed for `create_fork` under the round-2 review fixes (S4);
see the later amendment for the current, authoritative behavior: only `save`
still gates this way, while `create_fork` parses tolerantly and never
refuses.** And device-removal pruning (`prune_fork_devices`) releases
a removed device's `fork_l3_routes` rows outright rather than reasoning about
edge incidence the way wiring hops do: a route belongs to the switch itself,
not to any particular canvas edge, so there is no "through-hop" case to
preserve.

## Amendment: phase 1 review fixes (2026-09-09)

Adversarial review of the phase 1 delivery (PR #754) found five points worth
recording against the Decision, Contract summary, and prior amendment
sections above; none change the shape those sections already committed to.

**Module layout untangled the real cycle (R1).** The phase 1 amendment above
already named the shipped module split; the review found that the actual
implementation still leaned on function-local imports and one `noqa: F821` to
paper over a real circular import between `routes/topologies.py` (which
needed `classify_element_edge`/`node_to_element_map` from
`fork_save_service.py`) and `fork_save_service.py` (whose `gate_l3_intent`
needed the topology validator back). The fix is a leaf module,
`services/cabling/app/services/canvas_nodes.py`, holding
`node_to_device_map`, `node_to_element_map`, and `classify_element_edge` with
no dependency on anything else in `app.services` or `app.routes`;
`fork_save_service.py` re-exports all three for existing importers.
`routes/topologies.py`'s `_run_topology_validation` moved out entirely, split
into `services/topology_validation.py`'s `validate_canvas_edges` (the edge
pass, returning an `EdgeValidation` of `invalid_edges` and `device_ids`) and
`services/l3_validation.py`'s `validate_canvas_l3` (the L3 pass), composed by
`topology_validation.py`'s `run_full_topology_validation` for the two
`/validate` routes. `l3_intent.py` imports from `canvas_nodes.py` only. The
loose canvas PUT and fork-version restore endpoints (`routes/forks.py`) now
call `validate_canvas_edges` alone: their contract is "the draft stores
regardless," so they must never make an inventory call or 503 on an unsaved
draft, which running the full pass (as shipped) would have risked the moment
a draft carried `data.l3`. No function-local imports or `noqa: F821` remain
anywhere this PR touched.

**Attachment (`l3_switch_unattached`) is defined against resolved wiring, not
the edge-validation BFS (R2).** Decision 5's `l3_switch_unattached` says "no
valid device-to-device edge in this validation touches the switch node," which
the shipped code read as the edge pass's own BFS touching the switch as an
edge *endpoint*. That undercounts a transit device: a switch that sits
mid-path on someone else's multi-hop resolved wire is never itself an edge
endpoint, yet the wiring genuinely reaches it. It also overcounts a
port-constrained edge whose named port has no physical cable: the edge
existed in the canvas and would have contributed to the old BFS-touch
notion built from device-pair reachability, even though
`resolve_canvas_wiring`'s own port-constrained search (issue #531) resolves
to no hop at all for it. The fix: `touched_devices` for the L3 pass is now
the set of both endpoints of every `WireSpec` in
`resolve_canvas_wiring(canvas).specs` (`fork_save_service.touched_devices_from_specs`),
in both the validate routes and the save gate. The save gate reuses the exact
`CanvasWiringResolution` `save_fork`'s caller already computed rather than
resolving the canvas a second time; the validate routes call
`resolve_canvas_wiring` once, and only when `l3_intent.canvas_has_l3` says the
canvas carries any `data.l3` at all, so a topology with none still makes
neither a second graph build nor an inventory call.

To be precise about what `l3_switch_unattached` asserts (S7 review fix, round
2): the switch is an endpoint of at least one resolved hop (a `WireSpec`) in
this canvas, transit devices on a multi-hop path included. It does NOT assert
that the specific `interface` a route names is the one actually wired; a
switch with two interfaces, one wired and one not, is "attached" for every
route regardless of which interface a route claims. Interface-to-port matching
is a named follow-up (recorded as a filed issue), not phase 1 scope.

**Lock ordering: the gate now runs before ANY lock, not just before the ones
`gate_l3_intent` takes itself (R3).** The phase 1 amendment's second commit
moved the gate's own inventory calls outside `save_fork`'s reconcile closure,
but the SAVE ROUTE (`routes/forks.py`'s `save_fork_internal`) still loaded the
fork row `FOR UPDATE` before calling `save_fork` at all, so the gate's
inventory HTTP calls ran while that row lock was already held the entire
time, exactly the hazard Decision 5 and the fork-lock discipline (issue #626)
both exist to avoid. The route now: loads the fork WITHOUT `FOR UPDATE` to
check ARCHIVED; resolves the canvas (`resolve_canvas_wiring`) and gates intent
(`gate_l3_intent`), neither of which touches the fork row; re-loads the fork
WITH `FOR UPDATE` (re-checking ARCHIVED, since status could have changed in
the gap); and only then calls `save_fork`, which now takes the already
-resolved `CanvasWiringResolution` and already-parsed, already-gated intent as
parameters instead of computing either itself. The version-allocation retry
loop's reapply still covers only the set arithmetic, unchanged.

**Activation does not gate on L3 intent (R4).** Decision 6's "the driver
contract is untouched" did not settle whether *activation itself* should be
revocable by an L3 refusal the way a save is; phase 1 shipped `create_fork`
running the identical `gate_l3_intent` a save runs, so a topology edit or an
inventory answer that changed between reservation-create and activation could
422 or 409 the activation, stranding the reservation with no fork and no
wiring at any layer, physical or routed alike. This amendment decides
activation must not gate, matching physical wiring's own posture (a booking
already judged connectable at create time is never re-refused at activation
for a physical-path reason either). `create_fork` now calls
`l3_intent.parse_l3_intent_tolerant`: a malformed node's `data.l3` is dropped
with one WARNING naming the node, and every well-formed route on every other
node is still written as booked. `fork_service.py` no longer imports or calls
`gate_l3_intent` at all. Reservations' `_create_reservation_fork` widens its
409-is-definitive branch to any `4xx` (cabling's fork-create route can now
only still refuse with the endpoint-membership 409, the cross-reservation
port-claim 409, or a request-shape 422, none of which resolve on a retry), and
`_relay_cabling_fork_response` relays a structured JSON `5xx` body (e.g.
cabling's L3-gate 503 `{"error": "l3_config_unavailable"}` from the SAVE path,
which still gates) instead of the generic "Cabling service is unavailable"
message, keeping the generic message only when the body is not valid JSON at
all.

**Empty intent is no intent (R10).** A device node whose `data.l3` is exactly
`{"routes": []}` carries the key but expresses nothing: it now produces no
entry from the parser (absent from `parse_l3_intent`'s map, not present with
an empty list) and triggers no switch-level check and no inventory call,
identically to a node that never carried `data.l3` at all, on both the
validate routes and the save gate. `l3_validation.py`'s node walk
(`_split_l3_nodes`) applies the same rule directly, so a switch whose only
contribution is an empty routes list is treated as carrying no intent rather
than being reported `l3_switch_unconfigured` or evaluated for attachment.

**The device-set PATCH path never judges routing (R11).**
`POST /topologies/{id}/validate/internal` gained a query parameter `l3`
(default true); `l3=0` skips the L3 pass entirely (no `resolve_canvas_wiring`
call, no inventory call, `invalid_routes` always empty).
`_validate_topology_connectivity` gained `check_routes: bool = True`;
reservations' ACTIVE device-set PATCH path (`update_reservation`, the issue
#701 membership check) passes `False`, while the create path leaves the
default. Rationale: a device-set edit judges only physical connectivity for
the revised membership, and the topology's L3 routing intent was already
judged at create time and is unrelated to which devices the booking currently
includes; without this, editing a reservation's device set could refuse (or
503) on routing intent nobody asked it to re-check.

Two smaller fixes rode along with the above, neither its own decision:
`l3_intent.py`'s parser now merges two nodes that resolve to the same device
(deduplicating on `route_key`, first occurrence wins) rather than the second
node's routes silently overwriting the first's; `next_hop` and
`virtual_router` submitted as `""` normalize to null; `destination` is
canonicalized via `ipaddress.ip_network(value, strict=False)` when parseable
and kept verbatim otherwise (so `l3_bad_destination` still fires for a
genuinely bad string); and a literal `|` in any field is refused as malformed,
since `route_key` packs its three fields with `|` as the separator.
Reservations' error text (`_validate_topology_connectivity`) now builds its
`ValueError` from whichever of the two non-empty sections apply, joined with
`"; "` when both are present, and names the offending canvas node id rather
than a truncated device id, so a routes-only failure no longer reads as if it
were an edge failure.

## Amendment: phase 1 round-2 review fixes (2026-09-09)

A second adversarial review of PR #754 (head aad7fedc, the round-1 fixes above)
found four more points worth recording against the Decision and Contract summary
sections; three (S4, S5, S6) change the shipped shape, and the fourth (S7) is the
contradiction fix folded into the section above.

**S4: route identity includes the virtual router.** Decision 1's `RouteSpec`
shape and the phase 1 amendment's `route_key` both described the identity as
three fields, `(destination, interface, next_hop)`, packed with `|` as a
separator. That was wrong on its own terms: two routes differing only by
`virtual_router` (e.g. the same destination and interface routed via two
different VRFs) collided on `route_key` and could never coexist as distinct
`fork_l3_routes` rows, even though ADR 0014 Decision 1 lists `virtual_router` as
part of a route's identity-bearing shape. `route_key` now JSON-packs all FOUR
fields (`json.dumps([destination, interface, next_hop or "", virtual_router or
""])`); the `|`-separator and its associated "no field may contain `|`"
malformed rule are gone, since JSON's own escaping makes every field safe to
pack regardless of content. `fork_l3_routes.route_key` widens from
`String(200)` to `String(400)` to fit the packed JSON of four 64-character
fields plus quoting; migration 0011 was still unreleased on this branch, so the
column width and the `validated_config_version_id` addition from S6 (below) are
folded into that same migration rather than added as a follow-up revision. The
driver contract carries no VRF concept (see the "Out of scope" section and the
filed follow-up issue on this), so phase 3 collapses `route_key`'s four-field
identity down to the three-field one execution's `_route_run_identity`
already uses when it actually calls the driver; a VRF-aware driver contract is
a separate, explicitly out-of-scope follow-up.

**S5: a fork save gates the L3 validation pass on an actual intent delta, not
on "any L3 intent present."** Decision 3 already established that a save
reconciles the route set only on switches with intent; this amendment adds the
missing precondition on the validation pass itself: `save_fork_internal`
computes the parsed intent and reads the fork's EXISTING `fork_l3_routes` rows
before deciding whether to call the L3 pass at all
(`fork_save_service.l3_intent_changed`, comparing route-key sets per device).
When they match exactly, the pass (and its inventory calls) never runs. This
matters because a save can be triggered by a purely physical wiring edit on a
canvas that also happens to carry `data.l3` nobody touched; without this check,
every such save would re-validate routes that were already judged valid when
last written, paying for inventory round trips (and risking a spurious 503) for
no reason. Consequence, stated explicitly: a switch that loses its wiring
attachment through a wiring-only save (no L3 edit) is NOT caught by that save;
it is caught by the next save that actually edits L3 intent on that switch, or
by execution's own re-validation in phase 3. This is a deliberate trade
-off, the same shape as ADR 0009's "pinned, never re-derived" rule for
config-version routes: intent-driven switches only re-derive when the fork
save reconciles their intent, not on every save that happens to touch the
fork.

**S6: the save path records which inventory config version it validated
against.** `fork_l3_routes` gains a nullable `validated_config_version_id`
(bare UUID, no FK, matching this repo's no-cross-schema-FK convention),
written by `L3InventoryContext`'s config fetch (`L3InventoryContext.config_version_id`,
sourced from the `id` field on inventory's `GET
/devices/{id}/config-versions/latest/internal` response) and threaded through
`validate_canvas_l3`'s result, `fork_save_service.gate_l3_intent`, and
`save_fork`'s `l3_row_from_spec` calls onto every BUILT row from a save that
actually ran the gate (S5's delta check). Left `NULL` by
`fork_service.create_fork`'s tolerant activation path, which never validates,
and by any save whose L3 intent was unchanged (S5) and so never re-ran the
gate; those rows simply keep whatever value (possibly still `NULL`) a prior
save recorded, since they are UNCHANGED rows in the reconcile and are never
rewritten. Additive on the internal fork GET payload as
`validated_config_version_id`. Phase 3 will compare this to the switch's
CURRENT config version at drive time and re-run the per-route checks when they
differ, closing the gap S5 deliberately leaves open (a config or wiring change
after the last L3-touching save is invisible to cabling until the next L3 edit,
but execution can still catch it before actually driving a stale route).

**S7: the phase 1 delivery section's write-path description was stale.** The
original phase 1 amendment (above) said "save and create_fork both run the
validation pass first and refuse before writing any row"; that stopped being
true the moment R4 (round 1) made `create_fork` tolerant. The sentence there
now points forward to this section rather than asserting the old, now-false
behavior. Also recorded here for clarity: `l3_switch_unattached` asserts that
the switch is an endpoint of at least one resolved hop in the canvas (transit
devices on a multi-hop path count; element attachments do not); it does NOT
assert that the specific interface a route names is the one actually wired,
which is a named follow-up (filed as an issue, not phase 1 scope).
