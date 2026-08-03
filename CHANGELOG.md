# Changelog

## [0.2.0] - 2026-08-03

- Completed the connection-driven reconcile epic (ADR 0009): initial provisioning, fork saves, and terminal teardown all flow through one full-reconcile pipeline over the three wiring ledgers, retiring the legacy device-set resolvers; device-set changes wire and release only through fork saves.
- Layered wiring-status surface: every wiring row is tagged l1/l2/l3 (L2 rows carry the resolved fabric VLAN, L3 rows a route count), the reservation Wiring tab renders all three layers with six-outcome retry counting, and the external `/api/v1` facade gains a read-only wiring-status passthrough.
- HERD-owned VLAN definition lifecycle: `create_vlan` on a fabric allocation's first built membership over a transit-inclusive switch scope, `delete_vlan` on last-free with a reuse-race supersession guard; driver `create_vlan` is required to be idempotent.
- Reliability hardening: unreadable fork intent defers convergence instead of tearing down live wiring; terminal teardown freezes wiring first, with commit-time frozen re-checks in all three ledgers; device removal releases wiring from the saved intended set, never the draft canvas, with a durable retry marker; NATS consumers wait for schema readiness during upgrades; startup never runs create_all on a migration-managed schema.
- Security: bulk topology import updates enforce the creator-or-admin gate per row, and visible-device lookups are self-or-admin.
- Frontend: ACL grants management UI, and dynamic-template authoring with hypervisor registration.
- Developer platform: Playwright effect-assertion e2e suite, the validation-gate stack isolated in its own compose project, and a shared HerdBaseSettings config base class.

## [0.1.0] - 2026-07-27

- AI topology generation with human-in-the-loop ghost-node review, feature-gated behind an Anthropic or OpenAI-compatible LLM provider.
- Reservation lifecycle with editable per-reservation topology forks, release-before-build reconcile, port-claim conflict detection, and immutable as-built records at teardown.
- Automatic L1/L2/L3 infrastructure provisioning on reservation events, with a connection-driven fork-save reconcile at all three layers backed by per-item ledgers, direction-aware auto and manual retry, and per-connection L1 Wiring-tab status.
- Driver sandbox for admin-uploaded packages under POSIX rlimit caps, with driver-published JSON Schema config vocabularies and per-command execution transcripts.
- Encrypted-at-rest secrets service with AES-GCM envelope encryption, online key rotation, and ACL-gated reveal.
- Dynamic hypervisor-backed resources that materialize instances through recipe drivers with an idempotent instance ledger and a provisioning timeout backstop.
- Local and LDAP/Active Directory authentication with three-role RBAC, device-group visibility, and resource-level ACL grants.
- External versioned `/api/v1` integration facade with admin-minted API tokens and HMAC-signed, at-least-once outbound webhooks.
- Zero-database first-startup config UI, utilization reporting with CSV export, multi-channel notifications, bulk import/export, topology versioning, BFS pathfinding, and fleet-scale device health polling.
