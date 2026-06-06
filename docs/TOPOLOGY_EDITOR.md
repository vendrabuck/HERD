# Topology Editor Guide

The topology editor is the canvas where you build lab diagrams by dragging devices and wiring them together. This is a user-level guide; see [USER_GUIDE.md](USER_GUIDE.md) for broader app context.

![HERD topology editor (design-system mockup)](img/topology.png)

*Devices on the canvas with L1/L2/L3 edges colored by layer, port labels, and pathfinding hop counts. Design-system mockup.*

## Opening the editor

Navigation: **Topology** in the nav bar, then either pick an existing topology to edit or **New topology**. The canvas is a React Flow grid with a floating equipment palette and a top toolbar.

## The equipment palette

The palette is a floating, collapsible panel in the upper-left of the canvas. You can drag it around by its title bar and collapse it with the `-` button.

The palette lists every DUT (Management-connection device) you have visibility on:

- Already-reserved exclusive devices are hidden by default. Toggle **Show reserved resources** to include them.
- Filters: search, template dropdown, topology type dropdown.
- Search matches device name and any string/dropdown field value. Password-type fields are excluded; fields with the key `login` are also excluded from search.
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

Physical and cloud devices cannot be mixed in a single topology. This is enforced at the UI (invalid drops are blocked with a toast), in the reservation service (create-reservation validates uniformity), and in the database (topology_type enum).

## Edge visual indicators

Every edge you draw is checked against the physical cabling graph. The check applies to L1, L2, and L3 edges; L2 and L3 are logical overlays, but they still need a real path through the underlying cabling for the connection to mean anything.

Once an edge is added, it renders with one of several badges:

- **Green stroke + "N hops"**: a route exists through the switch infrastructure. N is the hop count.
- **Red stroke + "no path"**: no route exists between the two devices. Either they sit in physically isolated fabrics (a common case in multi-lab deployments where DUTs at different sites share no cabling) or the relevant switches are missing from inventory.
- **Red stroke + "uncabled port"**: one or both selected ports are not physically cabled. This takes precedence over the path badge.
- **Default stroke (no badge)**: an edge whose pathfinder request is still resolving, or which sits in a state with no result yet.

A red edge is a hard signal: the **Reserve Topology** button is disabled whenever any edge is red. The reservations service applies the same check server-side, so reserving via the API does not bypass the gate.

### Connectivity check

The check uses the cabling service's pathfind endpoint, which rebuilds the `Connection` adjacency graph per request. Every canvas load resolves the state of every edge in one batched call. When you draw a new edge, change endpoints, or an admin updates physical cabling, the affected edges revalidate automatically and immediately reflect the latest cabling state.

To express overlay links between physically isolated sites (for example, an MPLS tunnel between two labs), add a virtual device representing the tunnel and cable it into both fabrics in the cabling service. The validator then sees a real path and the topology edge turns green.

You can also see pathfinding output per reservation on the **Routes** tab of the reservation detail modal; that tab batches pathfinding for every DUT-to-DUT pair.

## Saving

Click **Save** in the toolbar. The first save prompts for a topology name. Subsequent saves update the existing topology.

The canvas is persisted by the cabling service under `/cabling/topologies/{id}`; the payload includes node positions, edge layers, and the currently selected layer filter. When you reopen the topology, the canvas loads exactly as you left it.

## Version history

Every save writes a new version unless the canvas is byte-identical to the previous save, so you can roll back without cluttering the list with no-op entries.

- Open the **History** sidebar from the toolbar to see the full version list (newest first), with the author name and saved timestamp.
- **Preview** opens a version in a read-only view so you can check the state before restoring.
- **Restore** rolls the current canvas back to that version. Restore is blocked when an active or pending reservation still references the topology; cancel or wait for the reservation to release and try again. The confirmation dialog lets you add a restore note and optionally restore the topology name as well; the restored version is saved as a new entry at the top of the list and marked "restored from v<N>".
- **Compare**: tick exactly two versions and click Compare to see three sections: **Added**, **Removed**, **Modified** (with before/after JSON for each modified node or edge).

Permissions: restoring requires admin or the topology's original creator; any authenticated user with read access to the topology can view the version list and run a diff.

## AI generation (optional)

If the AI orchestrator is configured, the toolbar includes an **AI Generate** button. See [AI_GENERATE.md](AI_GENERATE.md) for the full flow (prompt entry, ghost-node preview, Accept / Modify / Reject, commit dialog that creates both the topology and a reservation).

## Keyboard and mouse reference

- Pan: drag empty canvas
- Select node: click
- Multi-select: shift-click or drag-box
- Delete selected: `Delete` / `Backspace`
- Connect: drag from node handle to another node
- Zoom: mouse wheel or the `+`/`-` buttons in the bottom-right mini-map

## Common issues

- **"Can't drop the device here"**: you're trying to mix a physical device with a cloud device in the same topology. Create a separate topology for each topology type.
- **Palette is empty**: you have no device visibility. See [TROUBLESHOOTING.md](TROUBLESHOOTING.md#empty-device-list) and ask an admin to assign your user group to the relevant device group.
- **Edge stays red with "no path" on L1**: either no physical cabling connects the two DUTs, or the switches between them are missing from inventory. Ask an admin to verify cabling entries.
- **Edge has "uncabled port" badge**: the selected port has no physical connection recorded. Choose a different port or ask an admin to record the cabling.
