# Topology Editor Guide

The topology editor is the canvas where you build lab diagrams by dragging devices and wiring them together. This is a user-level guide; see [USER_GUIDE.md](USER_GUIDE.md) for broader app context.

![HERD topology editor (design-system mockup)](img/topology.png)

*Devices on the canvas with L1/L2/L3 edges colored by layer, port labels, and pathfinding hop counts. Design-system mockup.*

## Opening the editor

Navigation: **Topology** in the nav bar, then either pick an existing topology to edit or **New topology**. The canvas is a React Flow grid with a floating equipment palette and a top toolbar.

## The equipment palette

The palette is a floating, collapsible panel in the upper-left of the canvas. You can drag it around by its title bar and collapse it with the `-` button.

The palette lists every DUT (Management-connection device) you have visibility on:

- Already-reserved exclusive devices are shown by default. Toggle **Show reserved resources** off to hide them.
- Filters: search, template dropdown, topology type dropdown.
- Search matches the device name (a case-insensitive substring match); it does not search field values.
- Devices already on the canvas are hidden from the palette. Removing a node from the canvas restores it to the palette.

Regardless of your role, the palette only shows Management-type devices; infrastructure (L1/L2/L3 switches) is used automatically by the routing system and is not dragged in by hand.

## Adding devices

1. Drag a device card from the palette onto the canvas.
2. The device appears as a node labeled with its name and a small device-type icon.
3. Drop duplicates are prevented: dropping the same device twice is a no-op.

Nodes can be repositioned by dragging. Positions are saved with the topology when you save.

## Drawing connections

Click-and-drag from one device's handle to another's. A **Connection Modal** opens:

1. Pick the layer: `L1`, `L2`, or `L3`.
   - `L1`: physical cabling.
   - `L2`: Ethernet / VLAN link.
   - `L3`: IP / routing.
2. Pick a port on each device. The port dropdowns show all ports on the device.
3. Each port is labeled with whether it has an upstream physical cable. Ports with no cable show a `(no cable)` suffix and a warning.
4. Click **Connect** to add the edge.

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

A red edge is a hard signal: the **Reserve Topology** button is disabled whenever any edge is red. The reservations service applies the same check server-side, so reserving via the API does not bypass the gate.

### Connectivity check

The check uses the cabling service's batch pathfind endpoint (`POST /cabling/pathfind/batch`), which builds the `Connection` adjacency graph once per request and resolves every edge's device pair against it in memory. Every canvas load resolves the state of every edge in one batched call. When you draw a new edge, change endpoints, or an admin updates physical cabling, the affected edges revalidate automatically and immediately reflect the latest cabling state.

To express overlay links between physically isolated sites (for example, an MPLS tunnel between two labs), add a virtual device representing the tunnel and cable it into both fabrics in the cabling service. The validator then sees a real path and the topology edge turns green.

You can also see pathfinding output per reservation on the **Routes** tab of the reservation detail modal; that tab batches pathfinding for every DUT-to-DUT pair.

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

The History sidebar above is for standalone topologies. A reservation's topology fork (see the next section) has its own version list with the same visual pattern, but it is view-only: forks have no preview, diff, or restore.

## Live-edit mode (editing a reservation's topology)

When you open the editor bound to a reservation (from the reservation detail modal's **Edit topology** button, which navigates to `/topology/<id>?reservationId=<id>`), you are not editing the shared master topology. You are editing the reservation's **fork**: a private working copy created when the reservation activated. Your edits never touch the parent topology or its version history.

- **Editing live reservation banner.** A blue banner marks the mode and shows the current device count and autosave status. Edits autosave as loose drafts ("Draft saved" / "Saving draft..."), so an in-progress change survives leaving and returning.
- **Commit to reservation.** Committing reconciles the fork: release-before-build set arithmetic in one transaction, appending a new fork version. A result toast reports "Fork saved as vN" with released, built, and unchanged counts, expandable to the exact connections. The commit is blocked while any edge has no physical path, matching the server-side check. Committing also updates the reservation's device set so provisioning re-runs for the affected devices.
- **Port-conflict dialog.** If a commit would claim a port already held by another active reservation, a "Ports already claimed" dialog lists each blocking reservation, device, and port. Your drawing stays on the canvas; re-wire the conflicting ports and commit again.
- **As-built (read-only).** After the reservation ends the fork is archived. The button becomes **View as-built** and the editor opens the fork read-only, showing the immutable as-built record of the last wiring the reservation was reconciled to.

Only the reservation owner or an admin can open the fork, and the fork is editable only while the reservation is `ACTIVE`. A `PENDING` reservation has no fork yet, so it offers no topology editing.

## AI generation (optional)

If the AI orchestrator is configured, the toolbar includes a **Use AI** button. See [AI_GENERATE.md](AI_GENERATE.md) for the full flow (prompt entry, ghost-node preview, Accept / Modify / Reject, commit dialog that creates both the topology and a reservation).

## Keyboard and mouse reference

- Pan: drag empty canvas
- Select node: click
- Multi-select: shift-click or drag-box
- Delete selected: `Delete` (the editor binds only `Delete`; `Backspace` does not delete)
- Connect: drag from node handle to another node
- Zoom: mouse wheel, or the `+`/`-` controls in the bottom-left of the canvas. The mini-map sits in the bottom-right and is view-only (no zoom buttons of its own).

## Common issues

- **"Cannot connect PHYSICAL and CLOUD devices: topology types must match"**: you tried to draw an edge between a physical device and a cloud device. Dropping both device types onto the same canvas is allowed; only connecting them is blocked. Create a separate topology for each topology type if you need to wire them independently.
- **Palette is empty**: you have no device visibility. See [TROUBLESHOOTING.md](TROUBLESHOOTING.md#empty-device-list) and ask an admin to assign your user group to the relevant device group.
- **Edge stays red with "no path" on L1**: either no physical cabling connects the two DUTs, or the switches between them are missing from inventory. Ask an admin to verify cabling entries.
- **Edge has "uncabled port" badge**: the selected port has no physical connection recorded. Choose a different port or ask an admin to record the cabling.
