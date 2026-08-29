# Decision: Network Element Objects, Issue #22

Status: Accepted 2026-08-29 (three decision points resolved by Lane the same
day; see Decision). No code in this doc. Context verified against the live
HERD-public tree on 2026-08-29 (main at 16fe4b28). Line-number citations in
this document are as of main 16fe4b28 unless a later amendment says
otherwise; symbols are the stable reference.

## Context

HERD's cabling model is strictly point-to-point. A `Connection` row is a
device-to-device 4-tuple, and the React Flow canvas persisted in
`topologies.canvas_data` carries only `deviceNode` nodes joined by `layerEdge`
edges (`frontend/src/pages/TopologyEditorPage.tsx:94`, where `nodeTypes` is
`{ deviceNode, dynamicPlaceholderNode }`). Real labs contain infrastructure
that is not a device with ports: a shared VLAN segment, a management subnet, an
external cloud or upstream provider, a patch-panel trunk. Modeling these as
devices is wrong (no driver, no port inventory, no reservation semantics), and
modeling a shared segment as a full mesh of point-to-point links is both
inaccurate and combinatorially noisy. Issue #22 asks for a non-device canvas
element that many device ports attach to with many-to-one connections.

Relevant existing fabric, verified:

- Canvas node kinds: `DeviceNodeData` and `DynamicPlaceholderNodeData` in
  `frontend/src/types/topology.types.ts:6-22`, unioned as `CanvasNodeData`
  at `:24` and typed as React Flow nodes at `:44-45`. The placeholder type
  carries `templateId`/`templateName`/`templateIcon`/`count` and NO `device`
  field, which is the precedent for a canvas node whose data is not an
  inventory device.
- The placeholder discriminator is isolated behind one predicate,
  `isDynamicPlaceholder` (`frontend/src/lib/canvasNodes.ts`, `node.type ===
  "dynamicPlaceholderNode"`, alongside `isNetworkElement` and `isDeviceNode`
  since phase 2 moved all three predicates out of `TopologyEditorPage.tsx`),
  used at six call sites in `TopologyEditorPage.tsx`: the persist filter
  `persistableCanvas` (which STRIPS placeholders and their edges), the
  canvas device-id set built via `collectCanvasDeviceIds` (also since phase
  2), the reserve-modal prefill, the connection guard `isValidConnection`,
  the second guard in `handleConnect`, and the minimap color callback, which
  keys off the raw type string. A seventh call site was missed by this list
  at design time and only surfaced during phase 2 review:
  `handleAIProposal`'s device-id set (the resolver's AI-proposal duplicate
  check), which does not consult `isDynamicPlaceholder` at all. It instead
  reads `.device.id` off every node it iterates, so a negated
  `isDynamicPlaceholder`/`isNetworkElement` pair would have stayed
  exhaustive only until a fourth node type existed, and in the meantime
  would have crashed on a `networkElementNode`'s absent `.device` field
  the moment an AI proposal ran over a canvas that already had an element
  on it. Phase 2 fixed this with a positive predicate instead of a negated
  pair: `isDeviceNode` (`frontend/src/lib/canvasNodes.ts`, `node.type ===
  "deviceNode"`) guards every node before its `.device` is read, and the
  extracted, unit-testable `collectCanvasDeviceIds(nodes)` helper filters on
  `isDeviceNode` (excluding proposal ghosts) to build the set
  `handleAIProposal` diffs against. The general lesson this leaves for any
  future canvas node kind: prefer a positive "is this the node type I can
  safely read `.device` from" check over negating the list of node kinds
  known not to be a device, since the latter silently stops being exhaustive
  the moment a new node kind is added and nobody remembers to touch every
  negation.
- Drop handling: `TopologyEditorPage.tsx:541` reads the
  `application/herd-dynamic-template` MIME from the drag payload and builds a
  `dynamicPlaceholderNode` at `:560`; the device path reads
  `application/herd-device` and builds a `deviceNode` at `:588`. The drag
  source is `DynamicTemplateCard` in
  `frontend/src/components/equipment-browser/EquipmentBrowser.tsx:67-93`
  (`setData` at `:70`), rendered inside a collapsible section gated on
  `showDynamic` (`:120`, `:235-253`) with the shared `ChevronIcon` (`:97`).
- Edge creation: every enriched edge goes through `buildEnrichedEdge`
  (`frontend/src/stores/topologyStore.ts:20-30`), which mints its own id and
  sets `type: "layerEdge"`; `addEnrichedEdge`/`addEnrichedEdges`
  (`:40-41`) are the only append paths and deliberately never call React
  Flow's `addEdge`, whose dedupe guard would refuse a second edge on identical
  source/target/handles. `removeEdgesIncidentToNodes` (`:47`) is the explicit
  node-delete safety net that drops every store edge incident to a removed
  node.
- Render bundling: `groupEdgesForRender`
  (`frontend/src/components/topology-editor/edges/groupEdgesForRender.ts:89`)
  keys groups on `[edge.source, edge.target].sort().join("::")`, so many edges
  between the same node PAIR already collapse into one `BundledEdge` with a
  count badge. The key is node ids, not device ids, so it works unchanged for a
  device-to-element pair.
- Wiring surfaces: `WiringDialogProps`
  (`frontend/src/components/topology-editor/WiringDialog.tsx:32-49`) requires
  `sourceDeviceId`/`targetDeviceId` plus both topology types and drives two
  port columns from `usePortAvailability(sourceDeviceId, targetDeviceId)`
  (`wiring/usePortAvailability.ts:52-61`, four queries: ports and connections
  per side). The reusable primitives are `PortColumn`, `filterPorts`,
  `usePortAvailability`, and `portAvailability` under
  `components/topology-editor/wiring/`.
- Validation: `_run_topology_validation`
  (`services/cabling/app/routes/topologies.py:204`) builds `node_to_device`
  from `node.data.device.id` (`:220-231`), classifies an edge whose either
  endpoint is unresolvable as `missing_device` (`:263-271`), and BFS-checks the
  rest into `no_path` (`:283-291`). `InvalidEdge.reason` is a bare `str`
  (`services/cabling/app/schemas/topology.py:92-97`), so a new reason value
  needs no schema enum change. Both `/validate/internal`
  (`topologies.py:297-315`) and `/validate` (`:318-338`) delegate to the same
  helper.
- Reservations' pre-commit gate: `_validate_topology_connectivity`
  (`services/reservations/app/services/reservation_service.py:174`) POSTs
  `/topologies/{id}/validate/internal` (`:190`) and is called from the create
  path (`:1139`) and the device-add path (`:1749`).
- Fork save: `resolve_canvas_wiring`
  (`services/cabling/app/services/fork_save_service.py:113`) maps nodes via
  `node_to_device_map` (`:97-111`, the same `data.device.id` extraction) and,
  for each committed edge, does `if source_device is None or target_device is
  None: continue` (`:179-182`). Any edge with a non-device endpoint is dropped
  SILENTLY today, with no counter and no log line.
- Execution: the consumer never sees a canvas node. It consumes recorded
  `fork_connections` hops and chain-walks them (`_chain_walk_group`,
  `services/execution/app/services/nats_consumer.py:1163-1241`). Only an
  INTERIOR node gets a cross-connect pair: the walk appends to
  `pairs_by_switch` only when `in_port is not None and
  device_is_switch.get(current)` (`:1234-1237`), so a chain ENDPOINT never
  yields a driver call. `_apply_wiring_pairs` (`:1377`) then drives exactly
  those interior switches.
- Bulk import/export: `_node_device`
  (`services/cabling/app/services/bulk_service.py:93-94`) reads
  `node.data.device`, returning `{}` for any node without one.
  `canvas_ids_to_names` (`:97-113`) deep-copies the canvas and only pops
  `device.id` when present; `_collect_device_names` (`:233-240`) and
  `rewrite_canvas_names_to_ids` (`:242`) both skip a node with no device name.
  CSV export (`topology_to_csv_rows`, `:127-151`) is a flat edge list keyed by
  resolved device NAMES (`node_to_name`, `:132-137`), so an endpoint with no
  device name emits an empty string.
- AI orchestrator: `_build_canvas_data`
  (`services/ai-orchestrator/app/services/committer.py:54`) emits only
  `"type": "deviceNode"` nodes (`:73`).

Two comments on issue #22 add forward context. The 2026-07-29 comment names
ADR 0009's layer-agnostic L2 membership derivation as the mechanism an element
hub would have to interact with. The 2026-07-31 comment goes further: it
observes that a many-to-one hub is not expressible as a hop, that
`resolve_canvas_wiring` would resolve it to nothing, and that the execution
consumer's L2 and L3 derivations would each need an explicit element
classification rule. That second comment's conclusion is superseded by
decision 2 below: with no provisioning in v1, an element never becomes a hop
and execution never has to classify one.

## Decision

### 1. Storage: canvas-native and topology-local (decided)

A network element is a new canvas node kind living inside
`topologies.canvas_data`. No new tables, no Alembic revision, no
`element_attachments` relation, no cabling registry. Because forks and version
snapshots copy `canvas_data` wholesale, elements ride into fork drafts and
version snapshots for free with zero additional code.

Alternative considered and rejected: the registry shape issue #22's body
proposes, a `network_elements` table in the cabling schema plus an
`element_attachments` relation binding `(device_id, port)` to an `element_id`.
Rejected on four grounds:

1. Attachments edited during a reservation would mutate GLOBAL rows. Every
   other reservation-scoped edit in HERD goes to the fork, never the parent
   (ADR 0006); a registry-backed attachment would be the first thing that
   breaks that invariant.
2. Forks would need their own element story: either a `fork_elements` table
   mirroring `fork_connections`, or a rule for how a fork's element edits
   relate to the parent's rows. Neither is small.
3. Element delete would have to sweep every topology's `canvas_data` to remove
   orphaned nodes, an unindexed JSONB scan with no existing precedent in the
   service.
4. The registry's only real advantage, cross-topology reuse, is recoverable
   later WITHOUT changing this node shape: a registry becomes a palette that
   stamps a pre-filled element node onto the canvas, and the canvas node stays
   the source of truth for what is actually on this topology. Deferring costs
   nothing; adopting the registry now costs all four items above.

The cost accepted: an element is not reusable across topologies in v1, and two
topologies referencing "the same" VLAN segment carry two independent nodes with
different ids. That is the same trade the canvas already makes for node
positions and labels.

### 2. Provisioning: none in v1 (decided)

An element is a reachability and documentation hub only. Attaching a port to an
element produces no driver call, no VLAN, no route, and no ledger row. Issue
#22's own out-of-scope list already excludes driver and provisioning behavior;
this decision holds the line there rather than partially crossing it.

Alternative considered and rejected for v1: anchored VLAN provisioning. An
element would carry an anchor L2 switch id and a VLAN id; every attached device
port would get an L1 path resolved to that anchor switch, and the resulting
hops would join one VLAN through ADR 0009's membership derivation. Rejected
because it pulls execution's L1 and L2 derivations, all three wiring ledgers,
and both retry channels into scope for what is otherwise a canvas feature.

Recorded as the natural phase 2, with its hook named now so a later
implementer does not have to rediscover it: since a chain ENDPOINT never
receives a driver call (`_chain_walk_group` appends a pair only for an interior
node, `nats_consumer.py:1234-1237`), the anchored variant expresses as
SYNTHETIC device-to-anchor-switch hops emitted at fork save, not as a new hop
kind. The element node itself would never appear in `fork_connections`; the
anchor switch would appear as an ordinary interior L1 switch, and everything
downstream (chain walk, L1 reconcile, L2 membership derivation, both retry
channels, supersession) would work unmodified. That is the property this v1
shape deliberately preserves.

### 3. AI generation: deferred to a follow-up issue (decided)

`_build_canvas_data` (`committer.py:54`) keeps emitting only `deviceNode`. The
AI topology generator proposing elements is issue #632. Nothing in this design
blocks it: an element node is plain JSON the committer could emit once the tool
schema learns the vocabulary.

### Canvas shape

A new node type `"networkElementNode"` with:

```
data: {
  element: {
    id: string,            // UUID, minted client-side at drop time
    element_type: string,  // one of the four below
    label: string,         // user-editable
    attrs: object          // free-form, per type
  },
  isProposal?: boolean
}
```

`element_type` is a closed enum in v1 with four values:

- `vlan_segment`, `subnet`, `external_cloud`: the three examples issue #22's
  motivation paragraph names verbatim.
- `patch_trunk`: the fourth thing the same paragraph names ("a patch-panel
  trunk"), and the only one of the four with a plausible physical realization,
  which is why it is worth having a distinct type for when phase 2 arrives.

The set is deliberately closed rather than a free string so the frontend can
ship four fixed palette entries and four icons; widening it later is additive.

`attrs` is a free JSON object whose per-type conventions are DESCRIPTIVE in v1,
not enforced: a `vlan_segment` may carry `vlan_id`, a `subnet` may carry
`cidr`. The backend does not validate node data shape in v1: the canvas PUT
already accepts `deviceNode` data without schema checks, and the validator
reads only `node.type` to build its element map, so `attrs` is carried
verbatim. Nothing reads `attrs` in v1; enforcing a per-type schema before any
consumer exists would pin a shape that phase 2's real requirements would then
have to migrate.

The element `id` is a UUID minted CLIENT-SIDE at drop time, because there is no
server registry to mint it (decision 1). It is distinct from the React Flow
node id: the node id addresses the node on the canvas, the element id is the
element's own identity, stable across a copy-paste or a re-layout. This follows
the shape `DynamicPlaceholderNodeData` already sets
(`topology.types.ts:17-22`): a canvas node whose `data` carries no `device`
field at all, rather than a device-shaped object with a fake id.

The discriminator is `node.type`, exactly as for placeholders, and it is
isolated behind ONE predicate `isNetworkElement(node)` defined beside
`isDynamicPlaceholder` (`TopologyEditorPage.tsx:99-100`). Every filter site
that currently consults `isDynamicPlaceholder` needs an element decision:

- `:192` (`persistableCanvas`): elements are the OPPOSITE of placeholders here.
  Placeholder nodes and their edges are STRIPPED from the persisted canvas;
  element nodes and their edges MUST persist. The site therefore keeps
  filtering on `isDynamicPlaceholder` only, and the element predicate is
  deliberately absent. This is the one site where copying the placeholder
  treatment would be wrong, so it is called out explicitly.
- `:489` (`allDeviceIds`): must exclude elements, which carry no
  `data.device.id`; reading one would throw.
- `:500` (`dynamicPrefill`): unchanged, placeholder-only by construction.
- `:514` (`isValidConnection`) and `:615` (`handleConnect`): must branch on
  elements rather than refuse them, since a device-to-element line is the whole
  feature. Element-to-element is refused here (see Attachments).
- `:1159` (minimap color): gains a neutral color for the element type.

### Attachments

An attachment is an ORDINARY `layerEdge` from a device node handle to the
element node:

- `data.source_port_name` is set (the device-side port). There is no target
  port: the element side has no ports.
- Direction is normalized so the DEVICE is always `source` and the ELEMENT is
  always `target`. React Flow will happily hand back a connection drawn
  element-first, so normalization is enforced in two places: the store's add
  path (`addEnrichedEdges`, `topologyStore.ts:41`, which already owns every
  enriched-edge construction through `buildEnrichedEdge` at `:20-30`) swaps the
  endpoints before building, and the server-side validator ACCEPTS either
  direction when classifying, so an edge from an older client or a hand-edited
  import is still judged correctly rather than reported invalid on a
  cosmetic ordering.
- N ports on one element are N separate edges, one per port. This satisfies
  issue #22's acceptance criterion that N attachments produce N records rather
  than N-squared device-to-device connections, without any new relation.
- Rendering needs no work: `groupEdgesForRender`'s pairKey
  (`groupEdgesForRender.ts:89`) is `[edge.source, edge.target].sort()`, keyed on
  node ids, so N edges between one device and one element already collapse to
  one `BundledEdge` with a count badge, with member selection, per-member
  delete, and the invalid-if-any-member-invalid projection all inherited.
- Element-to-element edges are REFUSED in v1 with a toast, in
  `isValidConnection` and again in `handleConnect` (the same double-guard
  `isDynamicPlaceholder` gets at `:514` and `:615`). Two elements have no
  device and no port between them, so such an edge would be meaningless under
  every rule below.
- The per-edge LAYER annotation on an attachment edge is IGNORED. Elements are
  layer-agnostic hubs in v1 precisely because nothing provisions them
  (decision 2): there is no L2 membership and no L3 adjacency for the layer to
  select. The edge still carries whatever layer the canvas was on, since it is
  an ordinary `layerEdge` and stripping the field would complicate the shared
  edge shape for no gain; it simply has no consumer. This is a narrower version
  of the same canvas-annotation-only ruling issue #531 made for device-to-device
  lines.

### Editing surface

Equipment Browser gains a collapsible "Network elements" section holding the
four types as drag sources, mirroring the dynamic-templates section
(`EquipmentBrowser.tsx:235-253`) with its own `show*` state and the shared
`ChevronIcon` (`:97`). The drag payload uses a new MIME
`application/herd-network-element`, mirroring
`application/herd-dynamic-template` (`:70`), carrying the `element_type` and a
default label. Unlike the dynamic section, this one renders unconditionally:
the four types are static, not fetched, so there is no "absent when none
exist" case.

The drop handler in `TopologyEditorPage.tsx` gains a third branch beside the
existing dynamic-template branch (`:541-570`) and device branch (`:572-600`),
reading the new MIME and building a `networkElementNode` with a fresh element
UUID and an editable label. Unlike the placeholder branch (`:557-558`, one
placeholder per template), multiple elements of the same type are allowed:
a topology can legitimately carry two distinct VLAN segments.

Drawing a line from a device to an element opens a NEW single-column dialog,
`ElementAttachDialog`, NOT `WiringDialog`. `WiringDialogProps`
(`WiringDialog.tsx:32-49`) requires `sourceDeviceId`, `targetDeviceId`, and
both topology types, and drives `usePortAvailability(sourceDeviceId,
targetDeviceId)` (`usePortAvailability.ts:52-61`) with four queries assuming a
device on each side; an element satisfies none of that. Bending it into a
one-sided mode would add a second shape to a component that took several review
rounds to stabilize, exactly the concern issue #539 already records about the
`WiringDialog`/`MultiConnectDialog` duplication.

`ElementAttachDialog` reuses the shared primitives directly: `PortColumn` for
the single device-side column, `filterPorts` for its search box, and
`usePortAvailability`/`portAvailability` for cabling state (called with the
device id on both sides, or with a one-sided variant if that reads cleaner;
either way the element side renders as a static target card, not a port list).
Multi-select in the column creates N attachments in ONE
`addEnrichedEdges` call, so the batch lands as one store commit exactly as the
multi-port wiring dialog's confirm does.

Port availability is identical to `WiringDialog`'s rule: any registered cable
makes a port selectable (`computeCabledNames`, `portAvailability.ts:32-48`,
sets `portsCabled` true), and a port already wired ON THE CANVAS to any node
(device or element) is unavailable. The second half is the one substantive
extension: the existing rule already blocks a port used by another line, and
element attachments join that same set, so a port cannot be both patched to a
peer device and attached to an element.

### Validation

`_run_topology_validation` (`services/cabling/app/routes/topologies.py:204`)
builds a SECOND map beside `node_to_device` (`:220-231`):
`node_to_element: dict[str, str]`, populated from nodes whose `type` is
`"networkElementNode"`, keyed by node id, valued by the element id (or simply
present-as-a-set; the id is carried for the error payload's benefit).

Classification, applied in the existing first pass (`:250-273`) before the
BFS pending list is built:

- Exactly one endpoint in `node_to_element` and the other in `node_to_device`,
  with a non-empty device-side port name: VALID, with NO BFS. The edge is not
  appended to `pending`, so it never reaches
  `find_all_shortest_paths_batch_async`. The reason it needs no path check:
  attachments are DECLARATIVE. An element is not a physical thing the cabling
  graph could contain a path to, so a BFS against it would be a guaranteed
  `no_path` for a topology the user modeled correctly. Direction is accepted
  either way (see Attachments). Port EXISTENCE on the device is not checked in
  v1: the attach dialog offers only real ports, and device-to-device edges
  only get that check as a side effect of the BFS port filters, so an
  attachment with a misspelled port name in a hand-edited canvas validates;
  phase 2 will need a real check when a port must be driven.
- Both endpoints in `node_to_element`: INVALID, new reason
  `element_to_element`. The frontend refuses these, so reaching the server
  means an import or a hand-edited canvas.
- One endpoint in `node_to_element`, the other a known device, but the
  device-side port name absent or empty: INVALID, new reason
  `element_edge_no_port`. An attachment with no port names nothing on the
  device and cannot be rendered, exported, or later provisioned.
- An element-side endpoint whose node id is in NEITHER map: falls through to
  the existing `missing_device` (`:263-271`), unchanged. A dangling node
  reference is a dangling node reference regardless of what the other end is.

`InvalidEdge.reason` is a plain `str`
(`services/cabling/app/schemas/topology.py:97`), so the two new values need no
schema change; only the docstring enumerating reasons does. Both endpoints gain
the behavior automatically since `/validate/internal` (`topologies.py:297-315`)
and `/validate` (`:318-338`) both delegate to the shared helper. Reservations
therefore needs NO change: `_validate_topology_connectivity`
(`services/reservations/app/services/reservation_service.py:174`, POSTing at
`:190`, called at `:1139` and `:1749`) sees `valid: true` for a well-formed
element topology and commits, satisfying issue #22's acceptance criterion that
the internal gate agrees.

Hub reachability is defined as a PROPERTY THE VALIDATOR REPORTS, not a BFS
rewrite: two device ports attached to the same element are reachable BY
DEFINITION, because that is what attaching them to a shared segment asserts.
The validator does not add element hops to the adjacency graph, and
`services/cabling/app/services/pathfind_service.py` is UNTOUCHED. The reason
this is sound and not a shortcut: an element edge is TERMINAL. No
device-to-device canvas edge ever routes THROUGH an element, because the only
edges an element can carry are device-to-element attachments (element-to-element
is refused above). So there is no device pair whose BFS result would change if
elements were in the graph. If phase 2's anchored variant ever makes an element
imply real transit, that is the point at which `pathfind_service.py` gets a
rule, and not before.

### Fork save and execution

`resolve_canvas_wiring`
(`services/cabling/app/services/fork_save_service.py:113`) currently drops any
edge with an unresolvable endpoint SILENTLY at `:179-182`
(`if source_device is None or target_device is None: continue`). An element
edge would land there today with no trace, indistinguishable from a genuinely
broken canvas.

Decision: make the skip EXPLICIT. `resolve_canvas_wiring` builds the same
`node_to_element` map (a shared helper beside `node_to_device_map`,
`fork_save_service.py:97-111`, so the validator and the resolver derive it
identically), recognizes an element edge before the existing `None` check,
increments a counter, and logs one debug line. The fork save response gains an
ADDITIVE field `element_attachments_skipped: int` on `ForkSaveResponse`
(`services/cabling/app/schemas/fork.py:160-167`), defaulted to 0. A genuinely
unresolvable NON-element endpoint keeps falling through the existing silent
`continue`, unchanged.

Because `ForkSaveResponse` is in the published OpenAPI, the contract snapshot
`tests/contract/snapshots/cabling.json` must be regenerated after this change
with `HERD_UPDATE_OPENAPI_SNAPSHOTS=1 make test-contract` against a running
stack, and the diff committed.

Nothing else changes. The invariant, stated plainly:

> An element edge never becomes a hop.

Consequences that fall out of it: `fork_connections` never carries an element
endpoint; execution never sees one; and the ADR 0009 derivations need NO
element rule in v1. This directly contradicts issue #22's second comment
(2026-07-31), which correctly concluded that element hops reaching execution
would need explicit classification rules in `_derive_l2_memberships` and
`_derive_l3_adjacency`. That conclusion assumed elements would provision.
Under decision 2 they do not, so the hops never exist and the classification
question never arises. It returns, exactly as that comment framed it, if phase
2's anchored variant is built, except that the synthetic-hop formulation
(decision 2) makes even that case need no new hop kind: the anchor switch is an
ordinary L1 switch to every derivation.

### Bulk import and export

JSON round trip passes element nodes through UNCHANGED with no code change.
`_node_device` (`services/cabling/app/services/bulk_service.py:93-94`) returns
`{}` for a node with no `data.device`, so `canvas_ids_to_names` (`:97-113`)
deep-copies the element node and pops nothing from it, `_collect_device_names`
(`:233-240`) contributes no name for it, and `rewrite_canvas_names_to_ids`
(`:242`) skips it via the `if not name: continue` guard. The node therefore
survives export and import byte-for-byte, and its edges are kept verbatim
because edges reference React Flow node ids and are never rewritten.

Nothing more is needed, and the reason is decision 1: elements have NO
cross-instance identity to resolve. A device node's `id` must be rewritten on
import because the same device has a different UUID in a different HERD
instance; an element's id is topology-local and means nothing outside the
canvas it lives on, so carrying it verbatim is correct rather than merely
tolerable.

CSV export does NOT carry element attachments. `topology_to_csv_rows`
(`bulk_service.py:127-151`) is a flat edge list keyed by resolved device names
(`node_to_name`, `:132-137`); an element endpoint resolves to `""` and the row
is meaningless. Documented as a known limitation of the CSV format rather than
patched: CSV is explicitly the lossy interchange format, and JSON is the
round-trip-faithful one (`docs/BULK_IMPORT_EXPORT.md`).

### AI orchestrator

Untouched. `_build_canvas_data`
(`services/ai-orchestrator/app/services/committer.py:54`) keeps emitting only
`deviceNode` (`:73`), per decision 3.

### Documentation

- `docs/TOPOLOGY_EDITOR.md`: a "Network elements" section beside the existing
  "Dynamic placeholders" section (`:26`), covering the palette, the attach
  dialog, and the persist-versus-strip contrast with placeholders.
- `docs/manual/user-topology.html` and `docs/manual/user-reservations.html`
  (the latter is the manual page that documents the Equipment Browser),
  plus `docs/USER_GUIDE.md`, which also covers the browser.
- `docs/BULK_IMPORT_EXPORT.md`: the CSV limitation.
- `PLANNED_FEATURES.md`: the "Network element objects" bullet links this ADR
  at design time and flips to `Shipped` with the delivered scope at delivery.
- `FEATURES.md`: gains the capability at delivery, not before.

## Delivery phases

Each phase is independently mergeable and independently useful.

1. **Cabling backend.** `_run_topology_validation`
   (`services/cabling/app/routes/topologies.py:204`) gains the
   `node_to_element` map and the three classification rules; the shared
   node-map helper lands beside `node_to_device_map`
   (`services/cabling/app/services/fork_save_service.py:97`);
   `resolve_canvas_wiring` (`:113`) gains the explicit element skip and
   counter; `ForkSaveResponse`
   (`services/cabling/app/schemas/fork.py:160`) gains
   `element_attachments_skipped`; `InvalidEdge`
   (`services/cabling/app/schemas/topology.py:92`) gains the two reasons in its
   docstring; `tests/contract/snapshots/cabling.json` is regenerated; unit
   tests under `services/cabling/tests/`. No frontend, no migration. After this
   phase a hand-crafted element canvas validates and saves correctly, which is
   what makes the phase independently testable.
2. **Frontend.** `frontend/src/types/topology.types.ts`
   (`NetworkElementNodeData`, added to the `CanvasNodeData` union at `:24` and
   the node-type alias set at `:44-45`); the `isNetworkElement` predicate and
   the six call-site decisions in
   `frontend/src/pages/TopologyEditorPage.tsx` (`:99`, `:192`, `:489`, `:514`,
   `:615`, `:1159`) plus the drop-handler branch (`:541`) and the `nodeTypes`
   registration (`:94`); a new
   `frontend/src/components/topology-editor/nodes/NetworkElementNode.tsx`
   styled dashed-neutral (gray), deliberately distinct from the dashed-purple
   `DynamicPlaceholderNode.tsx` so the two ephemeral-looking node kinds are not
   confusable, since one persists and one does not; the Equipment Browser
   section in
   `frontend/src/components/equipment-browser/EquipmentBrowser.tsx`; a new
   `frontend/src/components/topology-editor/ElementAttachDialog.tsx`; edge
   direction normalization in `frontend/src/stores/topologyStore.ts` (`:41`);
   the element-to-element refusal; and vitest coverage including an explicit
   bundling check that N attachments to one element render as one
   `BundledEdge`.
3. **Docs plus e2e.** The documentation list above, plus a Playwright test
   under `tests/e2e/` that drops an element, attaches two ports through the
   dialog, saves, reloads, and validates, asserting via API READ-BACK (per the
   e2e effect-assertion rule) that `canvas_data` holds the element node and
   both edges and that `POST /topologies/{id}/validate` reports `valid: true`;
   plus a reservation commit against that topology, proving the internal gate
   accepts it. Seeding trap (issue #629): these tests need an AVAILABLE
   inventory device, and both `make everything` and `nightly.yml` run e2e
   BEFORE `make seed`, so a device-gated test skips silently in every automated
   run. Until #629 lands, this phase's tests must be run explicitly against a
   seeded stack and that run recorded as the phase's verification story; a
   green gate is not evidence they executed.

## Testing

All five QA levels, per the repo's conventions.

- **Unit (`services/cabling/tests/`)**: the validator classifies a
  device-to-element edge with a port as VALID with no BFS call (assert the
  batch pathfind was invoked with a pair list excluding it); element-first and
  device-first orderings both classify identically; an element-to-element edge
  reports `element_to_element`; an element edge with a missing or empty
  `source_port_name` reports `element_edge_no_port`; an element edge whose
  device node id is absent from both maps still reports `missing_device`;
  `resolve_canvas_wiring` returns zero `WireSpec`s for an element edge and
  reports the skip count, while a genuinely broken non-element edge still
  skips silently. Frontend vitest: `isNetworkElement` narrows correctly;
  `persistableCanvas` KEEPS element nodes and their edges while still stripping
  placeholders (the one asymmetry most likely to be miscopied);
  `groupEdgesForRender` bundles N device-to-element edges into one member
  group; the store normalizes an element-first connection to device-source;
  `isValidConnection` refuses element-to-element with a toast.
- **Functional**: `POST /topologies/{id}/validate` end to end over a canvas
  containing one element, three attachments, and one deliberately malformed
  attachment, asserting the exact reason strings; a fork save over the same
  canvas returning `element_attachments_skipped: 3` and creating zero
  `fork_connections` rows carrying an element endpoint. Contract: the
  regenerated `cabling.json` snapshot pins the additive response field.
- **Integration (stack up, `tests/integration/`)**: a topology carrying an
  element passes `/topologies/{id}/validate/internal`, and a reservation
  created against it commits (the reservations gate at
  `reservation_service.py:1139` accepting it is the criterion issue #22 states);
  after activation the fork's snapshot contains the element node in its canvas
  and zero element-endpoint wiring rows; deleting the element node from the
  canvas and re-saving removes its edges and leaves the topology's other edges
  intact (issue #22's fifth acceptance criterion), verified by API read-back of
  both `canvas_data` and the fork's connections; the bulk JSON round trip
  (export then import into a fresh topology) preserves the element node
  byte-for-byte, asserted as a dict equality on the node, not a field spot
  check.
- **Stress/load**: a canvas with one element carrying 200 attachments (the same
  order as the `POST /connections/bulk` cap) validates within the existing
  validate latency budget and saves without a BFS fan-out, since element edges
  bypass pathfinding entirely; the assertion worth pinning is that the batched
  pathfind pair count is independent of the attachment count.
- **E2E (Playwright, seeded stack)**: as phase 3 above, drop, attach two ports,
  save, reload, validate, and commit a reservation, each asserted by API
  read-back rather than UI acknowledgment, with any canvas mutation restored to
  baseline. Run explicitly on a seeded stack (issue #629).

## Out of scope

- A cross-topology element registry and reuse. Explicitly deferred by decision
  1, which preserves the option: a registry layers on as a palette without
  changing the canvas node shape.
- Provisioning of any kind. Decision 2 sketches the anchored-VLAN phase 2 and
  names its synthetic-hop hook, but v1 ships no driver call, no VLAN, no route,
  and no ledger row for an element. Per issue #22's own out-of-scope list.
- AI generation of elements. Decision 3; a follow-up issue.
- Layer 3 element semantics beyond reachability, per issue #22's out-of-scope
  list; first-class L3 routing is tracked separately in `PLANNED_FEATURES.md`.
- Element-to-element links. Refused in the frontend and reported invalid by the
  validator; a segment-to-segment relationship has no meaning without the
  provisioning model phase 2 would bring.
- CSV export of element attachments. A documented limitation of the lossy
  interchange format; JSON is the faithful one.
- Dynamic creation of the backing infrastructure an element represents, per
  issue #22 (covered by dynamic resources, ADR 0004).
