# Decision: First-Class Layer 3 Routing Intent, Issue #34

Status: Accepted 2026-09-08 (decisions 1 and 2 made by Lane on 2026-09-08,
3 to 6 proposed the same day and accepted by default pending the phase 1
PR review; see Decision). No code in this doc. Context verified against the
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

### 3. A fork save reconciles the route set on switches with intent (decided)

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

### 4. The editor construct is a per-switch Routing panel; the L3 edge layer stays an annotation (decided)

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

### 5. Validation is structural and fail-closed only on facts the data can decide (decided)

`_run_topology_validation` gains an L3 pass over every device node carrying
`data.l3`, reported in a new additive `invalid_routes` list beside
`invalid_edges`; `valid` is false when either list is non-empty, so the
reservations gate (`_validate_topology_connectivity`) agrees with no shape
change on its side beyond folding `invalid_routes` into its error summary.
Each entry is `{node_id, device_id, index, reason, detail}`.

Reason vocabulary, all fail-closed:

- `l3_not_a_router`: the node's device is not a `Layer 3 Switch`.
- `l3_bad_destination`: `destination` is not a parseable IP prefix
  (`ipaddress.ip_network(strict=False)`).
- `l3_bad_next_hop`: `next_hop` is present and not a parseable IP address.
- `l3_unknown_interface`: the switch's latest config version lists
  `interfaces` and `interface` is not one of their names. When the switch
  has no config version, or its config lists no interfaces, this check does
  not run: the data cannot decide it.
- `l3_next_hop_outside_interface`: the named interface carries an `ip` that
  parses as a prefixed address and `next_hop` is not inside that network.
  An `ip` without a prefix, or absent, skips the check.
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
validation call and memoized, and treats an inventory outage as "cannot
decide" (the interface and next-hop checks skip, the structural checks still
run). The validate response gains no new failure mode for that outage: a
fail-closed 503 here would make every topology validate fail whenever
inventory hiccups, which the existing device-group guard deliberately avoids
for single reads.

### 6. Execution consumes fork intent; the driver contract is untouched (decided)

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
`invalid_routes`; `ForkSaveResponse` gains `l3_routes_built` and
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
   validation pass with the six reasons, `invalid_routes` on both validate
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
