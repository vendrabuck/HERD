# HERD Features

This document tracks what HERD currently supports and what is on the roadmap.
For the big-picture story of why HERD exists, see [README.md](README.md). For
architectural detail, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

**Status legend:**

- **Shipped**: implemented and on the main branch.
- **Partial**: a working subset is shipped; further iterations are planned.
- **Planned**: not yet started.

---

## Identity and access

- **Local authentication** (Shipped): username and password auth with bcrypt-hashed
  passwords and JWT issuance with refresh-token rotation.
- **LDAP / Active Directory authentication** (Shipped): pluggable via `AUTH_METHOD=ldap`.
  Users JIT-provision on first successful bind; superadmin accounts remain local
  regardless of auth source.
- **Directory group sync** (Shipped): admin-managed mappings from directory
  groups to HERD groups, an on-demand or interval-scheduled fail-closed
  reconcile of membership (with pre-provisioning of new users), and an opt-in
  deactivation/reactivation sweep for users removed or disabled upstream,
  guarded by a circuit breaker (ADR 0011, `docs/design/0011-ldap-group-sync.md`,
  issue #38). Managed from the admin UI (`/admin/ldap-sync`): mapping CRUD, a
  sync-now button, and run history.
- **Three-role RBAC** (Shipped): user, admin, superadmin. See
  [docs/ROLES.md](docs/ROLES.md).
- **User groups** (Shipped): organize users into teams with bulk member management.
- **Resource-level ACL grants** (Shipped): group-based view and manage grants on
  devices, topologies, reservations, and secrets, via the acl service's API and
  an admin-only Grants page (`/admin/grants`) to list, filter, create, and
  delete grants (issue #397).
- **Encrypted-at-rest credential store** (Shipped): a dedicated secrets service
  holding named secrets whose payloads are AES-GCM envelope-encrypted (an
  environment-supplied key-encryption key wraps per-version data-encryption
  keys), with ACL-gated reveal, an internal-token retrieval surface for
  automated provisioning, and online key rotation. Deleting a secret is
  refused with 409 while any hypervisor still references it (fail-closed when
  inventory is unreachable, no force flag). See
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
- **Device group visibility** (Shipped): non-admin users only see devices in their
  assigned groups.

## Inventory

- **Device and port templates** (Shipped): user-defined templates with typed custom
  fields (string, number, boolean, dropdown, password) and per-field defaults.
- **Driver packages** (Shipped): standalone driver entities classified by connection
  type (Management, Layer 1/2/3 Switch). Stored on local filesystem by default;
  MinIO/S3-compatible storage supported when configured.
- **Exclusive vs non-exclusive flag** (Shipped): exclusive devices get conflict
  detection; shared infrastructure (such as switches) can take concurrent
  reservations.
- **Bulk import and export** (Shipped): CSV and JSON import-export for devices,
  templates, and topologies, with a dry-run preview, per-row error reporting, and
  cross-instance reference resolution by name. Targets migration between HERD
  instances and bulk onboarding of existing inventory. Reservations, ACL grants,
  and users are out of scope.

## Topology

- **Drag-and-drop topology editor** (Shipped): floating equipment palette with search,
  template, and topology-type filters, plus a dynamic-templates section whose entries
  drop onto the canvas as count-carrying placeholder nodes that prefill a
  reservation's dynamic requests (issue #472). Drawing a line between two device nodes
  opens a multi-port wiring dialog (issue #517): source ports in a left column, target
  ports in a right column, drag or click-to-connect, per-line L1/L2/L3 tagging, and a
  "Connect 1:1 in order" fast path for wiring two same-size switches at once; a
  toolbar "Quick connect" toggle swaps in a compact single-pair popover for the common
  one-connection case, with an escalation link back to the full dialog. Multiple
  connections between the same device pair collapse into one bundled edge on the
  canvas with a count badge and a per-connection delete. Provisioning now honors each
  line's own ports, so N connections between the same device pair provision as N
  distinct wires; the per-line layer is still recorded on the canvas only and every
  provisioned hop stays L1 until the layer half lands (issue #531).
- **Physical and cloud topology separation** (Shipped): physical and cloud devices
  cannot be mixed in a single topology, enforced at database, service, and UI
  layers.
- **Topology versioning and history** (Shipped): every save creates a version with
  preview, diff, and restore. Restore is blocked while an active reservation
  references the topology.
- **Topology cloning and reusable templates** (Shipped): clone an existing topology
  as a starting point; promote a topology to a reusable template.
- **Editable reservation topologies (fork, reconcile-on-save, as-built)** (Shipped):
  each reservation gets an editable fork of its parent topology, created on
  activation. While the reservation is ACTIVE the owner (or an admin) edits the fork
  in the topology editor's live-edit mode: edits autosave as loose drafts, and
  committing runs a set-arithmetic reconcile (release-before-build) that appends a
  fork version and leaves the parent topology's history untouched. A commit whose
  wiring would claim a port already held by another active reservation is refused
  with a conflict that names the blocking reservation. When the reservation ends the
  fork is frozen to an immutable as-built record, viewable read-only. Two behavior
  changes from the previous design: live edits no longer mutate the shared parent
  topology or append parent versions, and PENDING reservations no longer offer
  topology editing (the fork exists only from activation). The fork is the wiring
  source of truth for every layer: a commit reconciles the hardware
  connection-by-connection across L1 switch cross-connects, L2 VLAN memberships, and
  L3 route pins, recording each outcome in a per-layer wiring ledger
  (`l1_connection_assignments`, `l2_port_assignments`, `route_assignments`), and the
  reservation detail's Wiring tab groups the rows by layer with each row's status
  (ACTIVE / RELEASED / FAILED, attempts, last error) and a manual retry for the
  hardware-retryable failures on top of the background auto-retry, both
  direction-aware (a failed release retries as a release). See ADR 0006, ADR 0007,
  and ADR 0009 (issues #345 and #416, delivered).
- **Shortest-path cable routing** (Shipped): on-demand BFS (minimum-hop) pathfinding through
  Layer 1 switch infrastructure, with visual feedback on the canvas (green stroke
  and hop-count badge when a path exists, red stroke when not).
- **Port cable validation** (Shipped): the editor warns about uncabled ports before
  connections are created.

## Reservations

- **Conflict detection** (Shipped): time-window conflict checks for exclusive devices.
- **Automatic expiration** (Shipped): pending reservations activate and active
  reservations complete on schedule.
- **Automatic infrastructure provisioning** (Shipped): the reservation's topology
  fork drives the connected infrastructure through drivers, initial provisioning
  included (activation stages the fork's wiring for the same connection-driven
  reconcile that later commits use): Layer 1 port cross-connects, Layer 2 VLAN
  definition and membership (fabric-aware, conflict-free VLAN ids, defined on
  the switches on first use and deleted when the last membership releases), and Layer 3
  static routes taken from the switch's latest config version, pinned at provision
  time so teardown removes exactly what was applied. Deprovisioning on cancel or
  completion releases from the per-layer wiring ledgers (ADR 0009).
- **Live editing** (Shipped): modify device lists, extend end times, and update
  purpose on an active reservation. A device added to the device list wires
  nothing by itself: its connections are built when a topology commit draws them
  (ADR 0009 Decision 6); removing a device releases its wiring via the fork prune.
- **Calendar view** (Shipped): Gantt-style timeline with day, week, and month views,
  status filters, and click-to-view details.

## Dynamic resources

- **Hypervisor-backed dynamic templates** (Shipped): a `dynamic` template type
  materializes an instance from a registered hypervisor when a reservation needs it,
  rather than referencing a pre-existing device row. Admins register hypervisors
  from an admin Hypervisors page (list, register, edit, delete) with an endpoint, a
  free-string hypervisor type, and a secrets-service credential reference validated
  at registration, and pair a dynamic template with both a hypervisor and a recipe
  driver package from the template editor, which offers a Dynamic type option
  alongside Device and Port and enforces the Hypervisor-driver requirement
  client-side (issue #398). Booking is both reservation-first (the Create
  Reservation modal's dynamic instances block) and canvas-first (Equipment Browser
  dynamic templates drop as placeholder nodes whose counts prefill Reserve
  Topology, issue #472), and a booked reservation's dynamic requests render in the
  detail modal's Details tab (issue #473). The recipe driver is an ordinary driver
  package with a new `Hypervisor` connection type
  (`login`, `logout`, `create_instance`, `destroy_instance`, `status`) that reuses the
  existing sandbox, cache, and metadata machinery. Booking a dynamic template lists it
  under `dynamic_requests` on the reservation; a reservation carrying any dynamic
  request always books through `PENDING_PROVISION` and activates only once the
  execution service reports every requested instance materialized, so `ACTIVE` keeps
  meaning "usable". Execution runs the recipe's `login`/`create_instance`/`logout` in
  the sandbox, materializes a successful create as a real inventory device (visible to
  topology, health polling, ACLs, and the AI tools like any other device), and records
  the outcome in an instance ledger; a recipe must derive its hypervisor-side resource
  name deterministically from the per-instance request id so a NATS redelivery
  converges on the same instance instead of duplicating it. Completion, cancellation,
  or failure tears the instance down from that ledger, mirroring the applied-state
  discipline used for L2/L3 teardown. A configurable timeout (default 900s) fails a
  reservation stuck in `PENDING_PROVISION` if the completion callback is ever lost, so
  provisioning can never strand a booking. See
  [docs/design/0004-dynamic-resources.md](docs/design/0004-dynamic-resources.md) and
  [docs/DRIVERS.md](docs/DRIVERS.md). (Issue #32.)

## AI features

- **LLM-driven topology generation** (Shipped): natural-language prompts plus
  optional PDFs, tarballs, or text files generate topology proposals as ghost
  nodes for human review. Accept transactionally creates the topology and books
  the reservation, with optional per-device config push. Feature-gated by the
  presence of an AI provider configuration.
- **Reservation assistant** (Shipped): a multi-turn tool-use loop lets the
  reservation owner ask read-only questions about a running reservation
  (device state, config history, paths, recent executions) and, when
  `AI_WRITE_TOOLS_ENABLED=true`, propose and schedule config changes through
  the existing apply pipeline. Every AI-initiated apply defaults to a
  dry-run that captures the commands the driver would emit; the frontend
  shows the transcript in a confirmation modal so the user reviews and
  confirms before any real apply runs. ACL widening lets reservation owners
  manage their own reserved devices for the duration of the window. Drivers
  must opt into dry-run via a `driver_metadata.json` declaring
  `supports_dry_run: true`; the inventory schedule endpoint, AI tool, and
  execution sandbox all refuse dry-runs against drivers that did not opt in.
- **AI-assisted recipe authoring** (Shipped): an admin describes a dynamic-resource
  recipe in natural language and the AI drafts the driver package, which is
  validated in the execution sandbox (AST structural checks, a stricter
  generated-recipe policy, and a simulated dry-run of the full lifecycle) with
  a bounded auto-repair loop before the admin ever sees it. A review panel on
  the drivers page shows the code, the validation report, and the dry-run
  transcripts; nothing runs against real infrastructure and nothing is
  uploaded until the admin explicitly approves. Dark by default behind
  `AI_RECIPE_AUTHORING_ENABLED`. See [docs/AI_RECIPES.md](docs/AI_RECIPES.md).
- **Pluggable LLM provider** (Shipped): decouple the AI features from a single
  proprietary provider. Set `AI_PROVIDER=openai_compat` and `AI_BASE_URL` to
  point HERD at any OpenAI-compatible chat-completions endpoint (vLLM, Ollama,
  LM Studio, OpenAI, Azure OpenAI, or an internal gateway); set
  `AI_PROVIDER=anthropic` to keep the Anthropic SDK path. Unlocks fully local
  deployments on self-hosted inference. See
  [docs/ENV_VARS.md](docs/ENV_VARS.md) for the full env-var reference and
  vLLM tool-call parser flags.

## Device configuration

- **Per-device config versioning** (Shipped): list, detail, unified diff, and
  restore of device configuration snapshots.
- **Scheduled config apply** (Shipped): schedule a config push to fire when a linked
  reservation goes active. Owner or admin can cancel while pending. Optional
  dry-run mode runs the driver in simulation, captures the commands it would
  have emitted, and returns them via `GET /api/execution/runs/{id}/commands`
  for review before promotion to a real apply.
- **Per-command execution transcripts** (Shipped): drivers can opt into a
  per-command transcript via the in-process `record_command` helper. Rows
  persist to `execution_command_log` with sequence, command bytes, response,
  duration, and exit status (real or simulated). The transcript backs the AI
  apply-confirmation modal and provides a queryable audit trail for any
  driver execution.
- **Health monitoring** (Shipped): on-demand device health checks via driver-defined
  `login`, `status`, `logout` are shipped, as is periodic scheduled polling.
  Admins set `poll_interval_seconds` per device or per template; the execution
  service polls each opted-in device on its cadence and stores the snapshot
  (HEALTHY, DEGRADED, UNREACHABLE, UNKNOWN). The device detail page shows a
  colored health badge with the last-poll timestamp. Failures past a threshold
  back off exponentially so unreachable devices do not flood the audit log.
  When a device crosses the failure threshold or recovers, an in-app
  notification fans out to all admins and to any users with an active
  reservation on that device. The emit-on-Nth-failure rule provides natural
  flap dedupe: a device that never accumulates threshold consecutive failures
  generates no notifications. Fleet-scale bounds cap how many due rows one
  scheduler tick claims and how many polls run concurrently, and each device
  carries a persisted poll tier (idle or in-use) flipped by the reservation
  lifecycle events the service already consumes, with optional per-tier
  interval overrides. A poller-only run mode lets the background polling work
  scale as its own replica fleet, separate from the API replicas.

## Operations and observability

- **Reporting and analytics** (Shipped): admin-only utilization dashboards by user,
  device, topology type, day, and group, with CSV export. Includes a fleet
  utilization section: a per-device utilization rate against the full window, an
  idle-device view (devices with zero bookings in the window), and fleet-wide
  summary numbers, counting active plus completed reservations by default.
- **Notifications and dispatch channels** (Shipped): durable NATS consumers turn
  reservation lifecycle and device-health events into per-user notifications. The
  in-app bell ships alongside opt-in email, chat (Slack-style), and outbound-webhook
  channels as peer dispatchers, with the webhook HMAC-signed. Outbound sends are
  deduped on NATS redelivery and a failure on one channel does not block the others
  or in-app. An upcoming-expiry reminder fires once within a configurable lead window
  of a reservation's end time. Per-channel and per-event opt-outs live in user
  preferences; outbound channels default off. Bidirectional chat (slash commands,
  replies) and per-user channel credentials remain out of scope.
- **Durable event delivery** (Shipped): reservation lifecycle and device-health
  events are staged in a per-service transactional outbox in the same database
  transaction as the state change, then drained to NATS by a background relay that
  retries across a messaging outage and prunes old rows, so an outage delays delivery
  instead of silently dropping a provisioning or notification event. Consumers dedupe
  on a stable per-event id, so a relay republish is recognized as a duplicate rather
  than reprocessed. (Issue #21.)
- **Structured JSON logging** (Shipped): every service emits JSON logs with request
  middleware and business-event logging; per-service log level configurable.
- **Config service** (Shipped): zero-database web UI for configuring HERD on first
  start. Values saved through the UI take precedence over `.env`; an auto-bootstrapped
  config file stays subordinate, so pure-`.env` setups behave unchanged.

## Integration

- **External integration API and webhooks** (Shipped): a stable, versioned
  `/api/v1` surface owned by the `integration` service for CI/CD pipelines and
  test automation to reserve and release devices, decoupled from the internal UI
  endpoints. Automation authenticates with admin-minted API tokens (a token's
  role can never exceed its principal's) exchanged for short-lived access JWTs,
  and the facade forwards the caller's identity so RBAC, device-group visibility,
  and ACL grants apply exactly as for interactive users. Admins register outbound
  webhooks for reservation lifecycle events (`reservation.created`, `.updated`,
  `.cancelled`, `.completed`, `.failed`, `.expiring_soon`); each delivery is
  HMAC-SHA256 signed via `X-HERD-Signature`, at-least-once with retry and backoff,
  idempotent on the payload `event_id`, dead-lettered on exhaustion, and recorded
  in an inspectable delivery ledger. See
  [docs/EXTERNAL_API.md](docs/EXTERNAL_API.md) and
  [docs/api/v1-openapi.json](docs/api/v1-openapi.json). (Issue #33.)

## Multi-tenancy

- **Team workspaces** (Planned): organizational isolation where teams have their own
  device pools, topologies, and reservations. Builds on resource-level ACL to
  provide workspace-level boundaries. Cross-workspace resource sharing is a
  later consideration, deliberately out of the first slice (issue #35).

## Future considerations

Longer-term ideas under exploration; not yet committed to a near-term slot.

- **Federated lab support**: connect multiple HERD instances across geographically
  distributed labs into a unified view.
- **Hardware-in-the-loop simulation**: integrate virtual or simulated devices
  alongside physical hardware in the same topology.
- **Mobile-friendly interface**: list and table pages degrade gracefully on
  narrow viewports (tables scroll horizontally rather than reflowing), and the
  hover-based navigation is not touch-friendly; dedicated mobile views for
  on-the-go reservation management are a future consideration (issue #44).
- **Audit logging service**: comprehensive, tamper-evident audit trail of user
  actions and system events for compliance and troubleshooting.
- **Plugin and extension system**: allow third-party or internal extensions for
  custom device drivers, validators, or workflow automations.

---

To suggest a new feature, open an issue.
