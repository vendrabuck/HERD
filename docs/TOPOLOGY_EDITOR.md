# Topology Editor Guide

The topology editor is the canvas where you build lab diagrams by dragging devices and wiring them together. This is a user-level guide; see [USER_GUIDE.md](USER_GUIDE.md) for broader app context.

![HERD topology editor (design-system mockup)](img/topology.png)

*Devices on the canvas with L1/L2/L3 edges colored by layer, port labels, and pathfinding hop counts. Design-system mockup.*

## Opening the editor

Navigation: **Topology** in the nav bar, then either pick an existing topology to edit or **New topology**. The canvas is a React Flow grid with a floating equipment palette and a top toolbar.

## The equipment palette

The palette is a floating, collapsible panel in the upper-left of the canvas. You can drag it around by its title bar and collapse it with the `-` button.

The palette lists every DUT (Management-connection device) you have visibility on, plus a collapsible **Dynamic templates** section when any hypervisor-backed templates are published, and a **Network elements** section that is always present.

- Already-reserved exclusive devices are shown by default. Toggle **Show reserved resources** off to hide them.
- Filters: search, template dropdown, topology type dropdown.
- Search matches the device name (a case-insensitive substring match); it does not search field values.
- Devices already on the canvas are hidden from the palette. Removing a node from the canvas restores it to the palette.

Regardless of your role, the palette shows Management-type devices and published dynamic templates; infrastructure (L1/L2/L3 switches) is used automatically by the routing system and is not dragged in by hand.

## Dynamic placeholders

Dragging a dynamic template onto the canvas creates one placeholder node per template (dashed purple, tagged DYNAMIC) carrying an editable instance count. Placeholders are planning artifacts, not devices: they cannot be cabled (a connection attempt is refused with a toast, since instances have no ports until the reservation activates), they are never saved into the parent topology (saving with placeholders present says so in the toast; reserve first to keep them), and **Reserve Topology** prefills the reservation's dynamic instances from them, one request per count. A canvas holding only placeholders can still be reserved (dynamic-only bookings are valid).

## Network elements

A network element models infrastructure that is not a device with ports and not driven by HERD: a shared VLAN segment, a management subnet, an external cloud or upstream provider, or a patch-panel trunk. Real labs have this kind of thing everywhere, and modeling it as a fake device (no driver, no port inventory) or as a full mesh of point-to-point device links (accurate but combinatorially noisy for anything more than two devices sharing a segment) is both worse than leaving it out.

The palette's **Network elements** section holds four fixed types, each its own drag card: **VLAN segment**, **Subnet**, **External cloud**, **Patch trunk**. Unlike dynamic templates, this section is not fetched from anything, so it is never absent and never needs an admin to publish something first.

1. Drag a type card onto the canvas. It drops in as a dashed, neutral-gray node with an editable label (double-click to rename) and exactly one connection point. Dropping the same type twice is fine; a topology can carry two distinct VLAN segments, each its own node.
2. Draw a line from a device's handle to the element to open the **Attach** dialog, a single port column for that one device (there is no second device-shaped side, since an element has no ports of its own). Select one or more ports, then **Attach**; every selected port becomes its own attachment line to the element, added in one step.
3. Ports already used elsewhere on the canvas, by a device-to-device line or by another attachment, are unavailable here too, same rule as the wiring dialog.
4. Multiple attachments to the same element bundle into one edge with a count badge, exactly like multiple device-to-device connections do.
5. An element-to-element line is refused with a toast; two segments have no device or port between them, so a line drawn between them would not mean anything.

The persistence rule is the opposite of a dynamic placeholder, and it is worth stating plainly because the two dashed node kinds look similar at a glance: **a network element and its attachment lines are saved with the topology**; a dynamic placeholder is never saved (see above). The gray, non-purple dash is the visual cue for which kind of ephemeral-looking node you are looking at.

A network element is a reachability and documentation hub only, nothing more, in this release: attaching a port to one records no driver call, no VLAN, no route, and no ledger row. Two device ports attached to the same element validate as reachable to each other by definition (that is what attaching them to a shared segment asserts), but the element itself is never routed through, and it never becomes part of what a reservation actually provisions. An element is topology-local: it is not a reusable catalog entry, so two topologies that both need "the same" VLAN segment each carry their own independent element node.

CSV export does not carry element attachments (see [BULK_IMPORT_EXPORT.md](BULK_IMPORT_EXPORT.md)); use JSON export/import if you need a byte-for-byte round trip of a topology that includes elements.

## Adding devices

1. Drag a device card from the palette onto the canvas.
2. The device appears as a node labeled with its name and a small device-type icon.
3. Drop duplicates are prevented: dropping the same device twice is a no-op.

Nodes can be repositioned by dragging. Positions are saved with the topology when you save.

## Drawing connections

Click-and-drag from one device's handle to another's to open the **wiring dialog**, titled
"Wire {source} to {target}". It shows the source device's ports as a column on the left,
the target device's ports as a column on the right, and an open wiring area between them,
so you can wire any number of port pairs in one session instead of one round trip per
connection.

1. Draw a line between a port on each side: either drag from one port to the other, or
   click a free port then click its counterpart (the click-to-connect path works
   identically, useful without a mouse). Each drawn line gets a small `L1`/`L2`/`L3` pill
   at its midpoint; click the pill to change that line's layer or delete it. A "New
   lines" control in the header sets the default layer for lines you draw next. The
   layer is recorded on the canvas as the line's intended layer; provisioning derives
   L2 and L3 from the resolved path instead of reading it (see below).
2. Ports already used, either by an earlier line in this session or by an existing canvas
   edge, are grayed out and tagged `WIRED`; clicking one explains why instead of letting
   you reuse it. A port with a registered physical cable is tagged `CABLED`
   (informational only, still selectable); an uncabled port is tagged `no cable`
   (informational too, mirroring the old `(no cable)` warning).
3. **Connect 1:1 in order** pairs every remaining free port on both sides top to bottom in
   one click, for the common case of wiring two same-size switches straight across.
4. **Review (N)** expands a summary strip listing every line before you commit.
5. Click **Add N connections** to add every drawn line to the canvas at once. **Cancel**
   or the dialog's close button discards the whole session.

When two or more connections end up between the same device pair, the canvas renders them
as one bundled edge with a count badge (for example "3 connections"); click the badge to
expand the per-connection list, which also shows each connection's own path/cabling
status and its own delete control.

**Issue #531 resolution:** provisioning honors each line's own ports: cabling's
fork-save resolver resolves every canvas edge against that edge's
`source_port_name`/`target_port_name` when present, so N connections between the same
device pair provision as N distinct wires, not one. The per-line **layer** is a canvas
annotation only, by decision (ADR 0009 option C): every provisioned hop is recorded as
L1, and the execution service derives L2 VLAN membership and L3 route adjacency from
the resolved path hops rather than from a layer read off the fork row. Picking `L2` or
`L3` for a line changes how it is drawn and grouped on the canvas; it does not change
what provisioning configures.

### Quick connect

For a single connection, toggle **Quick connect** in the toolbar before drawing a line. It
opens a compact popover instead of the full dialog: pick one port on each side and a
layer, then **Connect**. The popover's **Open wiring dialog** link escalates to the full
dialog for the same device pair without losing your place, if you decide you want to wire
more than one pair after all.

### Topology-type enforcement

Physical and cloud devices cannot be mixed in a single topology. Dropping a device onto
the canvas is never blocked by topology type; the check runs when you connect an edge
between two devices. Drawing an edge between a physical and a cloud device is rejected
with a toast (`Cannot connect PHYSICAL and CLOUD devices: topology types must match`)
and the edge is not created. The reservation service also validates uniformity at
create-reservation time, and the database enforces the `topology_type` enum, so the rule
holds even if a client bypassed the editor.

## Edge visual indicators

Every edge you draw is checked against the physical cabling graph. The check applies to L1, L2, and L3 edges; L2 and L3 are logical overlays, but they still need a real path through the underlying cabling for the connection to mean anything.

Once an edge is added, it renders with one of several badges:

- **Green stroke + "N hops"**: a route exists through the switch infrastructure. N is the hop count.
- **Red stroke + "no path"**: no route exists between the two devices. Either they sit in physically isolated fabrics (a common case in multi-lab deployments where DUTs at different sites share no cabling) or the relevant switches are missing from inventory.
- **Red stroke + "uncabled port"**: one or both selected ports are not physically cabled. This takes precedence over the path badge.
- **Default stroke (no badge)**: an edge whose pathfinder request is still resolving, or which sits in a state with no result yet.

A red edge is a hard signal: the **Reserve Topology** button is disabled whenever any edge is red. The reservations service applies the same check server-side, so reserving via the API does not bypass the gate. It also refuses the booking outright (422 `topology_device_not_member`) if the topology's canvas names a device you have not included in the reservation's device list; add every canvas device to the booking first (issue #701).

### Connectivity check

The check uses the cabling service's batch pathfind endpoint (`POST /cabling/pathfind/batch`), which builds the `Connection` adjacency graph once per request and resolves every edge's device pair against it in memory. Every canvas load resolves the state of every edge in one batched call. When you draw a new edge, change endpoints, or an admin updates physical cabling, the affected edges revalidate automatically and immediately reflect the latest cabling state.

To express overlay links between physically isolated sites (for example, an MPLS tunnel between two labs), add a virtual device representing the tunnel and cable it into both fabrics in the cabling service. The validator then sees a real path and the topology edge turns green.

You can also see pathfinding output per reservation on the **Routes** tab of the reservation detail modal; that tab batches pathfinding for every DUT-to-DUT pair.

## Layer 3 routing intent (ADR 0014, issue #34)

Selecting exactly one device node whose device is a **Layer 3 Switch** opens a **Routing**
panel: the device name, a table of its routes (destination, next hop, interface, virtual
router), an **Add route** button, and a per-row **Remove**. The panel does not appear for a
proposal (ghost) node, in fork version preview or diff mode, or when more than one node is
selected. Every edit writes to the node's `data.l3.routes` in the canvas the same way any
other canvas edit does: it is ordinary node data, so it rides fork commit, autosave, and
restore preview along with the rest of the node, and is saved with the topology or the fork
draft exactly like positions and edges are. A next hop or virtual router left blank is
stored as absent, not as an empty string; removing a switch's last route clears its routing
intent entirely rather than leaving an empty table behind. Destination and interface are
required to add a row (the **Add route** button stays disabled until both are filled), but
neither field is validated as a real address in the browser: the server is the sole
authority on whether a route is well-formed and usable (see Validation below). Editing an
existing row commits on blur or Enter rather than on every keystroke, and every field
(destination, next hop, interface, virtual router, on both existing rows and the add-row
draft) is capped at 64 characters, matching the server's own field-length limit.

**Import from device config** loads the switch's newest config version and copies its
`routes` (destination, next hop, interface) into the table, leaving `virtual_router` blank
on every imported row, since the device config schema has no such grouping. If the table
already has rows, importing asks for confirmation first and then replaces the table
outright; it does not merge. The **Import from device config** button is disabled (labeled
"Importing..." while a fetch is in flight) until the switch's config version has loaded, and
stays disabled with no config version to import from.

A switch with at least one route shows a small route-count badge on its canvas node (for
example "2 routes") so routing intent is visible without opening the panel. The badge turns
red when the most recent validation found a problem on that switch.

**Validation.** The editor does not validate routing intent as you type. It is checked at
three points, each surfacing the same kind of problem list:

- **Saving a standalone topology** (not a reservation's live-edit fork) that carries any
  routing intent runs a validation pass right after the save completes. A clean result shows
  nothing new; a problem shows a toast naming the first three problems as
  `<device>[<index>] <reason>` (a switch-level problem shows `-` for the index), turns the
  affected switch's badge red, and adds a reason line under the offending row in the Routing
  panel (or at the top of the panel for a switch-level problem).
- **Saving a reservation's live-edit fork** runs the same check as part of the save itself:
  a routing problem refuses the save outright rather than saving broken intent, with a
  "Routing intent refused: N problems" toast and the same badge/reason-line treatment. A
  canvas shape the panel could not have produced (rare, and worth reporting rather than
  hiding) toasts "Routing intent malformed: ..." instead. If the switch's config could not be
  read at all (inventory unreachable), the toast reads "Could not verify routing intent:
  inventory unavailable" and nothing is saved.
- **Creating a reservation** from a topology with routing intent runs the same check as part
  of booking; a problem refuses the reservation with a "Reservation refused: routing intent
  has N problems" toast and the same badge/reason-line treatment, so you can fix the routes
  (or the switch's config) and try again. This gate validates the topology's PERSISTED
  canvas, not the editor's live draft: a routing edit made but never saved is invisible to
  it. Opening the Reserve dialog (the "Reserve from this topology" flow, not a reservation's
  live-edit fork) with unsaved edits that touched routing intent shows a one-time
  "Unsaved routing changes are not checked until you save" toast rather than blocking Reserve
  outright, since the gate itself is the real authority and you may prefer to fix the switch's
  config instead of saving first.

One reason, `l3_duplicate_route`, is informational rather than a problem: it never counts
toward the "N problems" total, never turns a switch's badge red, and never refuses a save
or a reservation. It renders as an amber note under the affected row instead of a red one,
naming the row as a duplicate of another route on the same switch, harmless because a
duplicate simply collapses in the set the switch actually applies.

The reasons you may see, in plain words:

| Reason | Meaning |
|---|---|
| `l3_malformed` | The routing data on this node is not shaped the way the editor writes it; this should not happen through the Routing panel itself. |
| `l3_not_a_router` | The device is not a Layer 3 Switch, so it cannot carry routes. |
| `l3_switch_unconfigured` | The switch has no config version, or its config lists no interfaces; import a config first, or configure the switch, then add routes. |
| `l3_switch_unattached` | Nothing on the canvas actually wires to this switch, so its routes would serve nothing. |
| `l3_bad_destination` | The destination is not a valid IP network (e.g. not a parseable address or prefix). |
| `l3_bad_next_hop` | The next hop is set but is not a valid IP address. |
| `l3_unknown_interface` | The interface name does not match any interface in the switch's config. |
| `l3_next_hop_unverifiable` | The next hop is set, but the named interface has no IP address (or no prefix length) to check it against. |
| `l3_next_hop_outside_interface` | The next hop is set, but it does not fall inside the named interface's own subnet. |
| `l3_duplicate_route` | Informational, not a problem: another row on this switch has the same destination, next hop, interface, and virtual router. |

A route's `destination` is canonicalized on the server the way a routing table normally
is: `10.0.0.5/24` is stored as `10.0.0.0/24`, and a bare address gets an implicit host
prefix (`/32` for IPv4, `/128` for IPv6). The Routing panel never rewrites what you typed,
so the table can show your original text even after a save; this is cosmetic for display,
but the Compare view diffs the raw text you typed, not the canonicalized form, so two
versions written differently for the same network (`10.0.0.5/24` in one, `10.0.0.0/24` in
the other) show up there as a routing change even though the switch treats them alike.

## Saving

A topology's name is set once, at creation, on the Topology list page (**New topology**);
the editor itself never prompts for a name. Click **Save** in the editor toolbar and an
optional change-description field appears; leave it blank or describe what changed, then
confirm. Every save updates the existing topology.

The canvas is persisted by the cabling service under `/cabling/topologies/{id}`; the payload includes node positions, edge layers, and the currently selected layer filter. When you reopen the topology, the canvas loads exactly as you left it.

## Version history

Every save writes a new version unless the canvas is byte-identical to the previous save, so you can roll back without cluttering the list with no-op entries.

- Open the **History** sidebar from the toolbar to see the full version list (newest first), with the author name and saved timestamp.
- **View** opens a version in a read-only view so you can check the state before restoring; the button reads **Previewing** while that version is open.
- **Restore** rolls the current canvas back to that version. Restore is blocked when an active or pending reservation still references the topology; cancel or wait for the reservation to release and try again. The confirmation dialog's optional description field is pre-filled with the placeholder "Restored from v<N>" and lets you optionally restore the topology name as well; the restored version is saved as a new entry at the top of the list.
- **Compare**: tick exactly two versions and click Compare to see six sections: **Nodes added**, **Nodes removed**, **Nodes modified**, **Edges added**, **Edges removed**, **Edges modified** (with before/after JSON for each modified node or edge).

Permissions: restoring requires admin or the topology's original creator; any authenticated user with read access to the topology can view the version list and run a diff.

The History sidebar above is for standalone topologies. A reservation's topology fork (see the next section) has its own version list with the same visual pattern, plus its own Preview/Diff/Restore actions described there.

## Live-edit mode (editing a reservation's topology)

When you open the editor bound to a reservation (from the reservation detail modal's **Edit topology** button, which navigates to `/topology/<id>?reservationId=<id>`), you are not editing the shared master topology. You are editing the reservation's **fork**: a private working copy created when the reservation activated. Your edits never touch the parent topology or its version history.

- **Editing live reservation banner.** A blue banner marks the mode and shows the current device count and autosave status. Edits autosave as loose drafts ("Draft saved" / "Saving draft..."), so an in-progress change survives leaving and returning.
- **Commit to reservation.** Committing reconciles the fork: release-before-build set arithmetic in one transaction, appending a new fork version. A result toast reports "Fork saved as vN" with released, built, and unchanged counts, expandable to the exact connections. The commit is blocked while any edge has no physical path, matching the server-side check. Since the 2026-09-04 fork endpoint-membership fix, every canvas node's device must be part of the reservation's device set before the fork save runs, so committing PATCHes any newly drawn device into the reservation FIRST and only then saves the fork; a device the PATCH could not add (or one lost to a race) still gets refused with 409, surfaced as a toast naming the offending device ids, rather than silently growing the reservation. A device you removed from the canvas is the opposite order: it is PATCHed out of the reservation's device set only AFTER the save succeeds, since that PATCH prunes wiring off the newly-saved intended set, not the draft.
- **Port-conflict dialog.** If a commit would claim a port already held by another active reservation, a "Ports already claimed" dialog lists each blocking reservation, device, and port. Your drawing stays on the canvas; re-wire the conflicting ports and commit again.
- **As-built (read-only).** After the reservation ends the fork is archived. The button becomes **View as-built** and the editor opens the fork read-only, showing the immutable as-built record of the last wiring the reservation was reconciled to.

Only the reservation owner or an admin can open the fork, and the fork is editable only while the reservation is `ACTIVE`. A `PENDING` reservation has no fork yet, so it offers no topology editing.

### Fork version preview, diff, and restore (issue #622)

The fork's own History sidebar lists its versions the same way the standalone one does, but each row also offers:

- **Preview.** Renders that version's canvas read-only on top of the live draft, with a purple "Previewing version N" banner and an **Exit preview** control. While it is up, editing, drawing wires, and Commit are all locked; exiting restores your draft exactly as you left it.
- **Diff.** Pick a compare target for the row's version, either another version or "current draft", and click Compare. Added and removed devices and connections are listed in the panel and, for connections, color-highlighted on the canvas (green for added, red dashed for removed). Diff shares the same read-only lock as Preview while it is up.
- **Restore**, shown only while the reservation is `ACTIVE` (the same rule as the Wiring tab's Retry button). Restore is a canvas operation, never a reconcile: it copies that version's canvas onto the fork's draft and nothing is wired until you run Commit. Because restore does not itself append a version, the panel shows an amber "Draft restored from version N (unsaved)" chip until the next Commit; that Commit is what appends the new version carrying the "restored" marker.

## AI generation (optional)

If the AI orchestrator is configured, the toolbar includes a **Use AI** button. See [AI_GENERATE.md](AI_GENERATE.md) for the full flow (prompt entry, ghost-node preview, Accept / Modify / Reject, commit dialog that creates both the topology and a reservation).

## Keyboard and mouse reference

- Pan: drag empty canvas
- Select node: click
- Multi-select: shift-click or drag-box
- Delete selected: `Delete` (the editor binds only `Delete`; `Backspace` does not delete)
- Connect: drag from node handle to another node, opening the wiring dialog; inside the
  dialog, wire a port pair by dragging between the two columns or clicking a port on each
  side in turn
- Zoom: mouse wheel, or the `+`/`-` controls in the bottom-left of the canvas. The mini-map sits in the bottom-right and is view-only (no zoom buttons of its own).

## Common issues

- **"Cannot connect PHYSICAL and CLOUD devices: topology types must match"**: you tried to draw an edge between a physical device and a cloud device. Dropping both device types onto the same canvas is allowed; only connecting them is blocked. Create a separate topology for each topology type if you need to wire them independently.
- **Palette is empty**: you have no device visibility. See [TROUBLESHOOTING.md](TROUBLESHOOTING.md#empty-device-list) and ask an admin to assign your user group to the relevant device group.
- **Edge stays red with "no path" on L1**: either no physical cabling connects the two DUTs, or the switches between them are missing from inventory. Ask an admin to verify cabling entries.
- **Edge has "uncabled port" badge**: the selected port has no physical connection recorded. Choose a different port or ask an admin to record the cabling.
