# HERD User Guide

This guide walks a lab engineer through day-to-day use of HERD. If you are an admin setting the platform up, start with [ADMIN_HANDBOOK.md](ADMIN_HANDBOOK.md) and [ROLES.md](ROLES.md).

![HERD reservations list (design-system mockup)](img/reservations.png)

*Your reservations, with status, topology type, device count, and period. Design-system mockup.*

## Getting started

### Signing in

![HERD sign-in (design-system mockup)](img/login.png)

*The sign-in screen. Design-system mockup.*

1. Open `https://<your-host>` (e.g. `https://localhost` for a local install).
2. The first time the site loads, you may see a banner about the system not being configured; that is for administrators. If you see it and the login form is disabled, ask your admin to finish the [config-service first-run](OPERATIONS.md#config-service-first-run).
3. Click **Register** if you do not have an account, or **Log in** if you do. Registration gives you the default `user` role; promoting users to `admin` is handled by a superadmin (see [ROLES.md](ROLES.md)). If your admin has wired HERD up to your company's directory (LDAP / Active Directory), the **Register** button still renders, but the backend rejects it with 409; your account is instead created automatically the first time you log in with your directory credentials, so use **Log in** and type your directory username (not necessarily your email) into the form.

### What you can see

By default a new account is added to the **Not Grouped** user group. Admins decide which device groups that user group can see. If the Inventory or Topology pages look empty:

- Ask an admin to add your user group to the relevant device groups.
- Once groups are assigned, reload the page; the equipment palette and the Inventory table will populate.

Admins see every device; regular users see only devices in groups their user group has access to.

## Reservations

A reservation holds one or more devices over a time window. Creating a reservation is how you claim lab equipment.

### Create a reservation

1. Go to **Reservations** in the nav bar.
2. Click **New reservation**.
3. Fill in purpose, start time, end time, and pick the devices you want. The UI hides devices you cannot see and marks already-reserved exclusive devices.
4. Submit. You will see the reservation in your list.

You can also create a reservation from the topology editor's AI flow (see [AI_GENERATE.md](AI_GENERATE.md)).

### Reservation lifecycle

Reservation status shows as a colored badge. The full set of states:

| Status | Color | Meaning |
|---|---|---|
| `PENDING` | yellow | Scheduled for the future; not yet active. The auto-expiration task flips this to `ACTIVE` when the start time passes. |
| `PENDING_PROVISION` | amber | Reservation row exists but the inventory service has not yet confirmed device status. This window is short (seconds) on a healthy system. |
| `ACTIVE` | green | Your reservation is live. Exclusive devices have been flipped to `RESERVED` in the inventory, and downstream provisioning (L1 switch ports, L2 VLANs) has been triggered. |
| `COMPLETED` | gray | The reservation's end time has passed and the auto-expiration task has released the devices. |
| `CANCELLED` | red | You or an admin cancelled the reservation before its end time. |
| `FAILED` | dark red | The reservation could not complete provisioning after retries. No devices were actually reserved. The row is kept for audit. Create a new reservation to try again, or check [TROUBLESHOOTING.md](TROUBLESHOOTING.md#reservation-is-failed). |

Transitions you can trigger:

- **Cancel** (`CANCELLED`): stops an active reservation and releases its devices. (The Cancel and Release actions appear in the UI for `ACTIVE` reservations.)
- **Release** (`COMPLETED`): ends an `ACTIVE` reservation early. Frees the devices immediately.

Transitions the system runs automatically:

- **Activate** (`PENDING -> ACTIVE`): at your reservation's start time.
- **Provision** (`PENDING_PROVISION -> ACTIVE` or `FAILED`): during create, as soon as the inventory service confirms.
- **Expire** (`ACTIVE -> COMPLETED`): at your reservation's end time.

### Exclusive vs shared devices

- **Exclusive** devices (DUTs, individual test equipment): only one reservation at a time. Creating a second reservation for the same exclusive device during an overlapping window fails with a conflict.
- **Shared / non-exclusive** devices (network infrastructure switches, shared resources): multiple reservations can run concurrently. Status does not change on reserve/release.

The device's template decides which category it falls in. If a reservation is rejected for conflicts, the response tells you which specific exclusive devices are already booked.

### Reservation detail

Click any reservation in the list to open the detail modal. Five tabs (plus an AI Assistant tab when the assistant is enabled):

- **Details** (default): basic info (id, purpose, dates, status).
- **Inventory**: the devices in the reservation. Expand a row to see its ports and what each port is physically connected to.
- **Routes**: for DUT-to-DUT pairs, shows the hop-by-hop path through L1 switches computed by the pathfinder. Green badges for reachable pairs, red for unreachable.
- **Wiring**: the applied wiring state across all three layers (L1 cross-connects, L2 VLAN memberships, L3 route pins), with per-row status and a Retry button for failed rows on active reservations. See the topology-editing section for details.
- **Schedule**: inline edit of end time and purpose.

The reservation owner can also click **Edit Resources** to add or remove devices while the reservation is active or pending. Added exclusive devices are checked for conflicts and flipped to `RESERVED`; removed devices are released back to `AVAILABLE`. Adding a device does not wire it to anything by itself: draw its connections in the topology editor and commit, and the commit is what builds them (removing a device releases its wiring the same way, through the topology reconcile).

### Editing the topology during a reservation

When a reservation goes `ACTIVE`, HERD gives it an editable **fork** of its parent topology: a private working copy you can re-wire during the reservation without touching the shared master template. From the reservation detail modal, the owner (or an admin) clicks **Edit topology** to open the fork in the topology editor's live-edit mode. The editor loads the fork, not the parent topology, so the parent's version history stays clean.

- **Load and edit.** The canvas opens on the fork with an "Editing live reservation" banner. Re-wire it exactly like any topology: drag devices, draw or remove L1/L2/L3 links. Each edge is still checked against the physical cabling graph, and a red (unroutable) edge blocks committing.
- **Autosave drafts.** Your changes autosave as loose drafts as you work (the banner shows "Draft saved" / "Saving draft..."). A draft is stored without reconciling, so you can leave and come back to an in-progress edit.
- **Commit.** Click **Commit to reservation** to reconcile the fork. The save runs release-before-build set arithmetic: connections you removed are released and connections you added are built, in one transaction, and a new fork version is appended. A toast reports the result ("Fork saved as vN", released X, built Y, unchanged Z) and expands to list the exact released and built connections. The fork is the source of truth for wiring: committing is exactly what reconciles your L1/L2/L3 wiring to the hardware, not a side effect of the reservation's device set.
- **Port conflicts.** If your wiring would claim a port already held by another active reservation, the save is refused with a "Ports already claimed" dialog that names each blocking reservation, device, and port. Your drawing is kept on the canvas so you can re-wire the conflicting ports and save again.
- **Wiring status.** After a commit, HERD applies your wiring to the hardware one connection at a time, layer by layer, and the reservation detail modal's **Wiring** tab shows the per-connection result grouped into three sections: **L1 cross-connects** (each switch port pair), **L2 VLAN memberships** (each switch port and its allocated VLAN), and **L3 route pins** (each L3 switch's pinned route count). Every row carries an `ACTIVE` / `RELEASED` / `FAILED` status, the attempt count, and (on a failure) the driver's error; a failed row whose intent was a release is additionally marked "release pending". A row that fails to apply does not roll back your saved wiring; the intent stays durable. Most failures are transient (a driver timeout or a switch login blip): HERD auto-retries them in the background with backoff, and while the reservation is `ACTIVE` you can also press **Retry failed** to reattempt every retryable failed row across all layers now (the toast summarizes how many reconnected, released, are still failing, or are not retryable). A connection marked not retryable cannot be reapplied as recorded (its cabling path changed under the reservation); its recovery is to re-save the fork wiring, which re-resolves the path against current inventory, then retry. The Wiring tab stays readable after the reservation ends as part of the as-built record, but the retry action is offered only while the reservation is `ACTIVE`.
- **Fork version history.** The editor's fork history panel lists every commit (newest first) with author and timestamp. In this release the fork history is view-only: there is no per-version preview, diff, or restore for fork versions (those exist for standalone topologies, not forks).
- **After the reservation ends.** Once the reservation is `COMPLETED`, `CANCELLED`, or `FAILED`, the fork is frozen as the immutable **as-built record**: the last wiring the reservation was reconciled to. The detail modal's button becomes **View as-built**, and the editor opens the fork read-only ("As-built record (read-only)"). Nothing can edit it after this point.

Two changes from HERD's earlier behavior are worth calling out:

- **Live edits no longer touch the shared parent topology.** Editing during a reservation now edits the fork and appends fork versions only; it does not modify the parent topology or add entries to the parent's version history. The master template stays exactly as saved.
- **`PENDING` reservations no longer offer topology editing.** The fork exists only from activation onward, so there is nothing to edit before a reservation is `ACTIVE`. (Previously, editing a not-yet-active reservation mutated the shared parent topology; that path is gone.)

### Reservation calendar

**Reservations > Calendar** shows a Gantt-style timeline:

- Switch between Day / Week / Month views.
- Cross-user: you see everyone's reservations, filtered by your device visibility.
- Status filter chips at the top let you hide `COMPLETED` / `CANCELLED` / `FAILED` to declutter.
- Click any block to open the reservation detail modal.

Useful for finding a free window before creating a reservation.

### Reporting (admins only)

**Reporting** appears in the nav for every signed-in user, but the page itself denies
non-admins (you'll be redirected if you land on it without the role). It shows a
utilization report for a 7-day, 30-day, or custom window: total reservation-hours, reservations counted, and execution runs at the top; a daily trend chart; a fleet utilization card with per-device utilization rates and an idle-device filter; tables broken out by user, device, group (cost center), topology type, and template; and CSV download per table. See [ADMIN_HANDBOOK.md](ADMIN_HANDBOOK.md#utilization-report).

## Inventory

The Inventory page lists devices you have visibility on. The table has five columns:

- Name
- Template
- Topology (`PHYSICAL` or `CLOUD`)
- Status (`AVAILABLE`, `RESERVED`, etc.)
- ID

Expand a row to see device audit info (who created/modified, when), device-group membership, and the device's ports. Each port row includes a **Connected To** link if the port is cabled to another device.

Click a row to open the device detail page. A left-hand sidebar on that page shows the same audit and group information (created / modified dates, created-by and modified-by names, device groups, and the user groups that have access through each device group), so you do not need to navigate back to the table to see it.

The Inventory page's only filter is a name search box (a case-insensitive substring
match). The template dropdown, topology-type dropdown, and "Show reserved" toggle live
in the topology editor's equipment palette (Equipment Browser), not on this page; see
[TOPOLOGY_EDITOR.md](TOPOLOGY_EDITOR.md#the-equipment-palette).

## Topology editor

See [TOPOLOGY_EDITOR.md](TOPOLOGY_EDITOR.md) for the full walkthrough. In short:

- Drag devices from the floating palette onto the canvas.
- Click between devices to draw connections at Layer 1 (physical cabling), Layer 2 (Ethernet/VLAN), or Layer 3 (IP).
- Each connection is checked against the physical cabling graph. Edges that have a real route between the two devices turn green; edges that do not (for example, devices in separate isolated labs) turn red and block reservation. The check applies regardless of layer; see [TOPOLOGY_EDITOR.md](TOPOLOGY_EDITOR.md#edge-visual-indicators) for details.
- Save a canvas as a named topology; it can later feed into a reservation.
- Every save is versioned. Open the **History** sidebar to preview, compare, or restore an older version; restore is blocked while a reservation still references the topology.

## AI topology generation (optional feature)

If your admin has configured an AI provider, the topology editor gets a **Use AI** button. It takes a prompt (plus optional reference files) and proposes a topology you can accept, modify, or reject. See [AI_GENERATE.md](AI_GENERATE.md) for details.

If the button isn't there, the feature is off on this deployment. The check is `ai_is_configured()`: for the default `anthropic` provider, either `AI_API_KEY` (hosted API) or `AI_BASE_URL` (a local Anthropic-compatible endpoint) being set is enough; for `openai_compat`, `AI_BASE_URL` must be set. Nothing you can do as a user; ask your admin if it should be enabled.

## Notifications

A bell icon in the top-right of the header tracks in-app notifications driven by your reservations. It ticks up automatically when any reservation you own is created, updated, cancelled, or completed, and it now also warns you before a reservation expires. The red badge counts unread items; clicking the bell opens a panel with the latest 20 where you can mark individual items read, delete them, or hit **Mark all read**. The unread count refreshes every 30 seconds while you're signed in.

Beyond the in-app bell, you can also receive notifications on outbound channels: **Email**, **Chat**, and **Webhook**. These are off by default and you opt in per channel from Settings. Channel transport (the SMTP server, the chat channel, the webhook destination) is set up once by your administrator; until they wire it up, opting in is harmless and simply does nothing.

If the feed feels noisy or too quiet:

- Click the bell and choose **Settings** (or head to **Settings** from the top-right link) to open the Notifications section.
- Toggle the **Show in-app notifications** channel off to silence the bell entirely without losing existing history.
- Under **Outbound channels**, toggle **Email**, **Chat**, or **Webhook** on to also receive notifications outside the app. They start off, so you only get what you opt into.
- Use the per-event checkboxes (Reservation confirmed / updated / cancelled / completed / expiring soon) to opt out of individual event types. Missing entries default to on, so you only need to uncheck the ones you don't want. The expiring-soon reminder fires once, ahead of a reservation's end time.
- Hit **Save**. The change applies to new events immediately; no restart needed.

LDAP-authenticated and local accounts share the same notification prefs; they live under your profile in HERD, not in the directory.

## Settings

The **Settings** page (reachable from the user menu or the bell's Settings link) currently holds the Notifications section above. Additional sections (theme, default landing page) are planned; your saved filters and page-size choices on the Inventory page are already persisted automatically and don't need a setting here.

## Profile and password

Your profile has basic account info (email, username, role, group memberships). To change your password, use the auth service's change-password endpoint or the profile page if your UI version exposes it. LDAP-authenticated users change their password in the directory; HERD doesn't store one for them.

Forgotten passwords: contact an admin. There is no self-serve reset flow today.

## Glossary

- **DUT**: Device Under Test. A device you reserve and run tests against. Maps to the `Management` connection type.
- **Template**: A user-defined schema that describes a class of device or port (fields, defaults, which driver to use). Devices reference a template.
- **Driver**: A Python package that teaches HERD how to log into and configure a real piece of hardware. Admins upload drivers; templates point at them.
- **Connection type**: A label on a driver (`Management`, `Layer 1 Switch`, `Layer 2 Switch`, `Layer 3 Switch`) that classifies the device and picks which driver methods are called.
- **Topology**: A saved diagram of devices and their connections. Lives in the cabling service.
- **Reservation**: A time-window claim on one or more devices, optionally tied to a topology.
- **Exclusive device**: A device only one reservation can hold at a time. Status toggles on reserve/release.
- **Non-exclusive / shared device**: Infrastructure that many reservations can share simultaneously. Status does not change.
- **Fabric**: A connected component of L2 switches and the ports they interconnect. Used for VLAN-conflict avoidance: VLAN ids are unique within a fabric, reusable across isolated fabrics.
- **Device group**: A named collection of devices. Device groups control visibility for non-admin users.
- **User group**: A named collection of users. User groups are granted access to device groups.
- **No Pool / Not Grouped**: Seeded default groups a new device or user lands in automatically, so nothing is unassigned on creation.

## Where to look when something goes wrong

- Badge you don't understand: see [Reservation lifecycle](#reservation-lifecycle) above.
- Empty device list or error toast: see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
- Question about the API: every backend service has live OpenAPI docs at `https://<your-host>/api/<service>/docs` (you must be logged in as an admin to hit most endpoints directly).
- Feature reference: the [README.md](../README.md) has the full feature list and architecture overview.

For admins, the next stop is [ADMIN_HANDBOOK.md](ADMIN_HANDBOOK.md).
