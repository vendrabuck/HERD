# HERD Roadmap and Planned Features

This document is the forward-looking companion to [FEATURES.md](FEATURES.md). FEATURES.md describes what HERD does today; this file lays out where it is going: planned work, design directions that are agreed but not yet built, and longer-range considerations.

Each item is tagged with a status:

- `Shipped`: implemented and on the main branch.
- `Partial`: a first iteration is shipped; later iterations are planned.
- `Planned`: scoped and intended, not yet built.
- `Exploring`: a direction under consideration, not yet committed.

HERD is a lab reservation and topology-management platform built as independent FastAPI services behind a single gateway, with a React topology editor, PostgreSQL per service, and NATS JetStream for events. The roadmap below is organized by theme rather than release.

## Extensibility and plugins

- `Shipped` Device driver system. A plugin model for device behavior: user-authored Python driver packages, classified by connection type (Management, Layer 1/2/3), that the execution service loads and invokes at reservation lifecycle events. Packages may declare their own dependencies and capability metadata (for example, dry-run support). Driver storage is the local filesystem by default, with an S3-compatible backend when configured.
- `Shipped` Pluggable LLM provider. An `LLMProvider` interface with two backends selected by configuration: a hosted-API backend and an OpenAI-compatible backend that works against self-hosted inference servers and gateways. This makes fully on-premise, air-gapped AI deployments possible.
- `Shipped` Layer 3 routing driver contract. All four driver contracts (Management, Layer 1/2/3) are live and invoked by the execution service at reservation lifecycle events. An L3 switch's routes come from its latest inventory config version; the provisioned set is pinned per reservation so teardown removes exactly what was applied. See the Layer 3 section of [docs/DRIVERS.md](docs/DRIVERS.md).
- `Shipped` Driver-published configuration schemas. A driver may declare an optional `config_schema()` classmethod; the execution service extracts and caches it per-SHA256 at load time, the configure boundary validates against the published schema first and falls back to the central registry only when none is published, and inventory exposes it via `GET /api/inventory/drivers/{id}/config-schema`. The reservation assistant's `get_device_config_schema` tool reads the published schema too, so it reasons about the real accepted shape. The checked-in `frr_mgmt` driver is the worked example, accepting raw `{commands, command}` vtysh lines the registry vocabulary does not describe. A broken or missing schema falls back to the registry, so a new device type needs no central change.
- `Exploring` General extension system. A broader extension surface for third-party or internal add-ons: custom validators, workflow automations, and integrations beyond device drivers.

## Identity, security, and compliance

- `Shipped` LDAP / Active Directory authentication. A pluggable authentication method with just-in-time user provisioning on first bind and strict separation between local and directory-sourced identities.
- `Shipped` Resource-level access control. Group-based view and manage grants on topologies and reservations, providing the foundation for multi-tenant isolation.
- `Planned` SAML / OIDC single sign-on. A third authentication method alongside local and directory auth, for federated enterprise identity.
- `Planned` Directory group mapping and sync. Mirror directory groups into HERD groups and periodically reconcile membership, including deactivating users removed upstream.
- `Shipped` Encrypted-at-rest credential store. A dedicated secrets service holding named secrets whose payloads are AES-GCM envelope-encrypted, with group-based grants via the ACL service, an internal-token retrieval surface, and online key rotation. This unblocks dynamic resource provisioning.
- `Exploring` Audit logging service. A comprehensive, tamper-evident trail of user actions and system events, aimed at troubleshooting and compliance reporting.
- `Exploring` Compliance posture (including FedRAMP alignment). Longer-range hardening toward recognized control frameworks: the audit trail, SSO, encrypted secrets, and least-privilege access above are the building blocks. FedRAMP-style alignment is an aspirational target that would shape configuration defaults, logging, and access controls rather than a near-term deliverable.

## Scale, operations, and reliability

- `Shipped` Durable event delivery (transactional outbox). Reservation lifecycle and device-health events are written to a per-service outbox table in the same database transaction as the state change, then relayed to NATS by a background publisher that claims rows with `FOR UPDATE SKIP LOCKED`, retries across a messaging outage, and prunes published rows. Consumers are idempotent on a producer-stamped event id (with a stream-sequence fallback for pre-outbox events), so a relay republish is collapsed rather than reprocessed. This closes the dual-write gap where a post-commit publish could silently drop a provisioning or notification event.
- `Shipped` Health monitoring at fleet scale. Building on shipped per-device health polling and alerting: a configurable per-tick batch size and bounded polling concurrency (each poll runs a driver subprocess, so a semaphore caps how many run at once, with a startup warning if the setting exceeds the asyncio thread-pool cap), a persisted per-device polling tier (idle or in-use) flipped by the reservation lifecycle events the service already consumes, with optional per-tier interval overrides, and an `EXECUTION_POLLER_ONLY` run mode that splits the API and the background poller into independently scalable replica fleets.
- `Shipped` Bulk import and export. CSV and JSON import and export for devices, templates, and topologies, with a dry-run preview, per-row error reporting, and cross-instance reference resolution by name, to support bulk onboarding and migration between HERD instances. Reservations, ACL grants, and users are out of scope.
- `Shipped` External integration API and webhooks. A stable, versioned `/api/v1` surface, owned by the `integration` service and decoupled from the internal UI endpoints, for CI/CD and test-automation systems to reserve and release resources programmatically. Automation authenticates with admin-minted API tokens (role-capped at the principal's own role) exchanged for short-lived access JWTs, and the facade forwards the caller's identity so RBAC, device-group visibility, and ACL grants apply unchanged. Admins register outbound webhooks on reservation lifecycle events, each HMAC-SHA256 signed, delivered at-least-once with retry and backoff, idempotent on a stable event id, dead-lettered on exhaustion, and recorded in an inspectable delivery ledger. See [docs/EXTERNAL_API.md](docs/EXTERNAL_API.md).
- `Planned` Multi-tenancy and team workspaces. Organizational isolation layered on the access-control service, so independent teams can share an instance without seeing each other's resources.
- `Shipped` Notification dispatch channels. In-app notifications plus opt-in email, chat (Slack-style), and HMAC-signed outbound-webhook channels as peer dispatchers, with outbound sends deduped on NATS redelivery and per-channel failure isolation, and an upcoming-expiry reminder that fires once within a configurable lead window of a reservation's end time. Bidirectional chat and per-user channel credentials remain out of scope.
- `Shipped` Reporting and analytics. Administrative utilization dashboards by user, device, topology type, day, and group, with CSV export, plus a fleet utilization section (per-device utilization rate, idle-device view, fleet-wide summary).
- `Shipped` Structured logging baseline. Per-service structured JSON logs with request-scoped context; richer observability builds on this and the audit-trail work above.

## AI capabilities

- `Shipped` LLM-driven topology generation. Describe a lab in natural language; the model proposes a topology resolved against real, available inventory (never invented devices), rendered as a reviewable proposal before a transactional commit that creates the topology and reservation together, with optional per-device configuration.
- `Shipped` AI reservation assistant. A multi-turn assistant scoped to a single reservation, with read-only inspection tools (device, ports, current and historical config, reachability, recent executions) that run under the caller's own permissions. Optional write tools, off by default, can propose and schedule configuration changes through the existing apply pipeline with a dry-run-then-confirm flow.
- `Shipped` AI-assisted service-recipe authoring. An admin describes a recipe in natural language; the AI drafts the Hypervisor driver package, validated in the execution sandbox (structural, policy, simulated dry-run) with bounded auto-repair, and a review panel on the drivers page gates the explicit admin approval that uploads it. Dark by default behind AI_RECIPE_AUTHORING_ENABLED.

## Topology and resource modeling

- `Planned` Network element objects. Place non-device elements on the canvas, such as a shared VLAN segment or an external cloud, with many-to-one connections, so a topology can model shared infrastructure rather than only point-to-point device links.
- `Partial` Editable reservation topologies. Give each reservation an editable fork of its parent topology: edit loosely during the reservation, reconcile on save, and capture an immutable as-built record at teardown, so the master template stays clean while reservations evolve. ADR 0006 P3a is shipped end to end (phases 1 to 5): the per-reservation fork, the cabling reconcile-on-save with cross-reservation port-claim 409s and the as-built archive, the reservations user-facing fork endpoints with teardown archive and a standing reconciler, and the frontend live-edit switch (edits go to the fork, the parent's history stays clean). Two items remain planned: P3b (issue #345), where the execution service consumes fork wiring deltas so a save reconciles hardware rather than only recording the intended wiring; and fork version preview, diff, and restore in the fork history panel (P3a ships the version list read-only, with no per-version preview/diff/restore endpoints).
- `Shipped` Dynamic resources. A `dynamic` template type backed by registered hypervisors, where a service recipe is an ordinary driver package with a new `Hypervisor` connection type (`login`, `logout`, `create_instance`, `destroy_instance`, `status`) run as a sandboxed job to bring a resource into existence when a reservation books it and tear it down at the end. A dynamic-carrying reservation books through `PENDING_PROVISION` and activates only once the execution service's provision-result callback (or a timeout backstop, guarding against a lost callback) resolves it, so `ACTIVE` still means "usable"; the materialized instance becomes a real inventory device like any other. See [docs/design/0004-dynamic-resources.md](docs/design/0004-dynamic-resources.md).
- `Planned` First-class Layer 3 routing. Promote Layer 3 routing to a dedicated connection type with a decoupled configuration model, replacing the current minimal representation. An initial version is shipped.

## Future considerations

These are directions of interest that are not yet scoped:

- Federated labs: connect multiple HERD instances into a unified, searchable view.
- Hardware-in-the-loop: model virtual or simulated devices alongside physical hardware in the same topology.
- Mobile-friendly interface: dedicated mobile views beyond the already-responsive list and table pages.

## How this maps to today

For the full list of shipped capabilities and how to use them, see [FEATURES.md](FEATURES.md), the [docs](docs/) directory (architecture, drivers, AI providers, roles, operations), and the hosted user manual linked from the README.
