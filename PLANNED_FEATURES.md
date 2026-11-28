# HERD Roadmap

This document is the forward-looking companion to [FEATURES.md](FEATURES.md). FEATURES.md describes what HERD does today and is the only place a shipped capability is described; this file lists only work that has not shipped. When an item ships, it moves to FEATURES.md and leaves this file.

Items are grouped by when they are expected, not by theme:

- **Now**: being built, or next to be built.
- **Next**: agreed, and waiting on a design or on the Now items.
- **Later**: directions of interest with no committed slot. Some have no open issue; a new issue opens when someone picks one up.

Each entry is a few lines and links its issue when one is open.

## Now

- **OIDC single sign-on, phase 6 (#37).** ADR 0016 phases 1 to 5b are merged and auth accepts `AUTH_METHOD=oidc`, but the config page does not offer it and the handbook does not describe it, so operators cannot use it yet. Phase 6 adds the config option, the handbook walk-through, and the docs. It waits on the response size bounds in #1396.

## Next

- **Audit logging.** A tamper-evident record of who did what in the lab, which answers the README's second founding question (what was done, by whom, on what, for how long). The transactional outbox already emits the lifecycle events a trail would record. It needs an ADR before any build work.
- **Published images.** Every deployer builds the service images from a clone today. Planned: images published for each release tag, so an install or an upgrade pulls instead of builds. It has no design yet.
- **Release workflow on the tag.** No workflow runs on a tag today; the release gate (`make everything`) runs by hand on the release commit. Planned: a workflow triggered by a version tag that runs the gate on the tagged commit and then publishes the images and the release.

## Later

Security:

- **Per-service Postgres roles.** The services connect with one shared database role, and schema isolation holds by convention (no cross-schema foreign keys or joins), as [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) says. Per-service roles would have the database enforce it. It has no design yet.
- **Compliance posture, including FedRAMP alignment.** A control-mapping document that ties HERD's controls to a recognized framework. Two of its building blocks have shipped (encrypted-at-rest secrets and group-based access grants); the audit trail and SSO above are the other two. Aspirational, not a near-term deliverable.

Product direction:

- **SAML 2.0 single sign-on.** ADR 0016 covers OIDC only and defers SAML to a follow-up. A SAML provider would be a fourth authentication method beside local, LDAP, and OIDC. No issue yet.
- **Multi-tenancy and team workspaces.** Organizational isolation so independent teams share one instance without seeing each other's resources, building on the shipped group-based access grants. It would add a tenant identifier to every service's schema and has no design yet.
- **General extension system.** A broader surface for third-party or internal add-ons: custom validators, workflow automations, and integrations beyond device drivers. Webhooks, notification channels, and driver-published schemas have shipped; no first extension point has been chosen.
- **Federated labs.** Connect several HERD instances into one searchable view. Its first dependency does not exist: the `/api/v1` facade serves reservations and webhooks only, with no inventory, calendar, or topology read.
- **Mobile views.** Dedicated mobile layouts for the topology and reservation flows. Tables scroll horizontally instead of reflowing and the Administration menu is built for hover (it also opens on a tap), so this is a project of its own and not a first issue.

## How this maps to today

For the full list of shipped capabilities and how to use them, see [FEATURES.md](FEATURES.md), the [docs](docs/) directory (architecture, drivers, AI providers, roles, operations), and the hosted user manual linked from the README.
