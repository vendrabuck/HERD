# Changelog

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
