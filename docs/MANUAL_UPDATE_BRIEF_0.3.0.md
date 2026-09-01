# Brief for Claude Design: bring the HERD manual up to v0.3.0

Repo: vendrabuck/HERD, branch main at 1beffbd8 (0.4.0.dev0). The manual is the
HTML under `docs/manual/` (16 pages plus `assets/`), published at
<https://vendrabuck.github.io/HERD/manual/>. Its last content edit was
1aed191d (2026-08-29). Since then v0.3.0 was released: tag `v0.3.0` at commit
21a315f6, release date 2026-08-30, GitHub Release
<https://github.com/vendrabuck/HERD/releases/tag/v0.3.0>. The manual still
presents 0.2.0 as the current release everywhere.

Scope: `docs/manual/**` only. Do not touch code, tests, CHANGELOG.md, or other
docs. Deliver as one PR on a branch named `docs/manual-0-3-0`. Model the whole
change on PR #485 (commit 8f090553), which did the same job for 0.2.0: one new
release page plus a 16-file repoint.

Sources of truth, in priority order: the running UI (a seeded 0.3.0 stack is at
https://localhost), `frontend/src/` for exact labels, `CHANGELOG.md` section
`## [0.3.0] - 2026-08-30` (summary bullets, then `### Delivery detail` with
`#### <area>` subsections), `FEATURES.md`, `docs/USER_GUIDE.md`,
`docs/ADMIN_HANDBOOK.md`, `docs/AI_PROVIDERS.md`, `docs/OPERATIONS.md`.

## Conventions you must keep

- Manual "screenshots" are hand-built inline HTML/CSS mockups
  (`<figure class="shot">` with the `hd-*` classes in `assets/app.css`), never
  images. New visuals follow that pattern.
- Every page shares the same top nav strip and (on release pages) a sidebar
  TOC. The nav's release link is repeated on all 16 files.
- Style rules for every file: no em-dashes (use colon, semicolon, comma), no
  box-drawing characters, no bullet glyphs or arrows in text (use `-`, `,`,
  the word `to`), no emojis. Password placeholders are literally `password`.
- Voice: second person, plain, task-first, same as the existing pages.
- Links to the repo use `https://github.com/vendrabuck/HERD/...`; relative
  links stay relative. Every link must resolve.

## A. New page: `release-0-3-0.html`

Copy the structure of `release-0-2-0.html` exactly (146 lines): the `.relmeta`
style block, sidebar TOC (`#what`, `#highlights`, `#quality`, `#boundaries`,
`#manual`), dek paragraph, relmeta strip, `#what`, "What shipped" card grid,
"Also in this release" list, architecture continuity paragraph, "Quality bar"
list, "Known boundaries" list, "Reading the manual against this release"
section, footer pagenav.

Relmeta strip values:

- Version: v0.3.0
- Released: 2026-08-30
- Tagged commit: 21a315f6

"What shipped" cards (link each to the manual page named):

1. LDAP directory group sync (ADR 0011): directory groups map to HERD groups,
   sync now and on an interval, run history, a deactivation and reactivation
   sweep, all on the new Administration > LDAP Sync page. Link the new
   `admin-ldap-sync.html` (section C1).
2. Fork version preview, diff, and restore in the live-editing history panel:
   preview any saved version read-only, diff two versions or a version against
   the draft, restore a version to the draft for the next Commit. Link
   `user-live-editing.html#fork`.
3. Network element objects: a non-device canvas node (VLAN segment, Subnet,
   External cloud, Patch trunk) that many device ports attach to, saved with
   the topology and validated on fork save. Link `user-topology.html#elements`.
4. Multi-port wiring: the canvas wiring dialog and the admin multi-connect
   dialog stage many lines between two devices with per-line L1/L2/L3 and
   "Connect 1:1 in order", including same-device (loopback) pairing. Link
   `user-topology.html#connect` and `admin-equipment.html#cabling`.
5. Reconcile hardening: a stale connection left by an ended reservation can no
   longer strand a VLAN allocation or dodge a rebuild (phrase like the 0.2.0
   card "failure paths that cannot eat wiring").
6. Production durability: under `make prod` JetStream stream data now survives
   container recreates, and every service image installs exactly what
   `uv.lock` pins.

"Also in this release" list:

- Inventory table: a Rows per page selector (25, 50, 100, 200) that is
  remembered across sessions.
- Deleting a device that any reservation still holds is refused with
  `device_in_use`; deleting a secret that a hypervisor references is refused
  with `secret_in_use`.
- The Hypervisors page shows an amber "Deleted secret <id>" marker when a
  hypervisor's secret has been removed.
- `GET /api/ai/status` reports `degraded: true` with a `reason` when the AI
  provider is configured but cannot be constructed, so "no Use AI button" now
  has two explanations.
- A seeded end-to-end pass in the release gate that fails on any unexpected
  skip, which is how the inventory Next-click bug below was found.
- Fixed: clicking Next on the inventory page right after it loaded could snap
  back to page 1.

Quality bar (measured at the tagged commit by the full `make everything` run):

- 4,498 backend unit tests across the 13 suites plus the repo-root suite;
  backend line coverage 97.1% with every service at 94% or higher.
- 1,211 frontend tests via vitest, 89.7% line coverage.
- 12 OpenAPI contract snapshot suites.
- 192 cross-service integration tests (182 run, 10 gated on LDAP mode or an AI
  provider).
- 163 end-to-end browser tests, run twice: 124 on the unseeded stack and 159
  again on the seeded stack with no unexpected skip allowed.
- 41 live-LDAP tests against the checked-in `infra/ldap-test` directory,
  hard-required in the gate.
- Load: 20 simulated users for one minute, 691 requests, 0 failures, median
  14 ms, p95 240 ms.
- Same closing sentence as 0.2.0 about the release gate and branch protection.

Known boundaries: carry forward "AI recipe authoring is dark by default" and
"driver packages are trusted code" verbatim; add "network element provisioning
(anchored VLANs) is a later phase; elements are canvas objects today"; DROP the
0.2.0 bullet about dynamic-resource canvas placement, which shipped.

`#manual` section: "This manual now documents 0.3.0"; link
`release-0-2-0.html` as the record of the previous release, which in turn
links 0.1.0.

## B. Repoint the existing pages (16 files)

- Every page's top nav: the "Release 0.2.0" link becomes "Release 0.3.0" and
  points at `release-0-3-0.html`.
- `index.html`: the release card in "When you're stuck" retitles to Release
  0.3.0 with a fresh one-line dek and links the new page.
- `glossary.html` line 95: the "Release / version" entry badge to `v0.3.0`,
  text to "This manual documents 0.3.0", link to the new page.
- `release-0-2-0.html`: demote to a historical record exactly the way commit
  8f090553 demoted `release-0-1-0.html`: dek becomes "kept as the record of
  what 0.2.0 shipped; the current release is 0.3.0, which this manual now
  documents", the `#manual` paragraph likewise, nav `active` moves to the
  0.3.0 entry, sidebar shared-reference list gains the 0.3.0 entry above it,
  footer pagenav forward-link goes to `release-0-3-0.html`.
- `release-0-1-0.html`: no change.

## C. Content that is missing

C1. New page `admin-ldap-sync.html` (add it to the admin track on
`index.html` and to the nav wherever the other admin pages appear):

- Where: Administration > LDAP Sync (`/admin/ldap-sync`). Admin only. The
  page opens even when sync is off and says so.
- Preconditions: HERD in LDAP mode (`AUTH_METHOD=ldap`), a service account
  (anonymous bind is refused), and `ldap_group_sync_enabled` for the interval
  loop; without it only Sync now works.
- Mappings: a directory group DN maps to exactly one HERD group and vice
  versa. Creating a mapping to a group that is already mapped is refused.
  A mapping whose directory group currently has no members is accepted with a
  warning, not rejected.
- Sync now: one run at a time; a second click while a run is in progress
  reports "A sync run is already in progress" (or "on another replica").
- What a run does: adds and removes HERD group memberships to match the
  directory; a member the directory cannot fully describe (missing email or
  username) is skipped, and when that happens removals for that group are
  suppressed so an incomplete read never deletes memberships. A directory
  outage skips the whole group rather than guessing. Deactivated users are
  invisible to sync in both directions.
- Deactivation sweep: users no longer present in the directory are
  deactivated with sync provenance; they are reactivated automatically when
  they reappear, but only if sync was what deactivated them. A manual
  activate or deactivate on the Users page always clears that provenance.
- Run history: each run lists counts and per-item skips with reasons; the UI
  shows a run as stale after 30 minutes; a run orphaned by a crash is marked
  failed automatically. History is kept 90 days by default when the interval
  loop runs; a sync-now-only deployment keeps rows until an admin clears them.
- Mockup: the mappings table plus a run-history row, hand-built like the
  other admin pages.
- Sources: `docs/ADMIN_HANDBOOK.md` LDAP section, `docs/design/0011-ldap-group-sync.md`,
  `frontend/src/pages/admin/LdapSyncPage.tsx` (check the exact page name and
  labels in `frontend/src/routes.tsx`).

C2. `admin-equipment.html#inspect`: describe the Rows per page selector on the
inventory table (25, 50, 100, 200; remembered across sessions; the Page X of Y
indicator and Prev/Next).

C3. `admin-equipment.html#cabling`: one sentence on loopback pairing: picking
the same device on both sides pairs its free ports adjacently (first with
second, third with fourth), an odd leftover stays unpaired, and fewer than two
free ports shows "Need at least two free ports to pair".

C4. `admin-equipment.html` (near device delete) or `troubleshooting.html#admin`:
deleting a device that any non-terminal reservation holds is refused; the
error names the reservation ids; release or complete them first. There is no
force option.

C5. `admin-setup.html#dynamic`: two lines. Deleting a secret that any
hypervisor references is refused with `secret_in_use` (and refused outright if
inventory is unreachable); re-point or delete the hypervisor first. If a
secret is deleted anyway by another path, the Hypervisors page shows the
hypervisor's secret as an amber "Deleted secret <first 8 chars>" until you
pick a live secret.

C6. `troubleshooting.html#ai` and `user-ai.html#have`: "no Use AI button" now
has two causes: the provider is not configured, or it is configured but
cannot be constructed (bad base URL, unreadable CA file, bad key shape). An
admin can tell them apart with `GET /api/ai/status`: `enabled: false,
degraded: true, reason: <error class name>`; the detail message never leaves
the service, so check the ai-orchestrator logs for it. The status is cached
for 30 seconds.

C7. `user-live-editing.html#fork`: add a mockup of the history panel with
Preview, Diff, and Restore, since the prose exists but no visual does. Keep
the "restore stages the draft; the next Commit to reservation applies it"
nuance; restore never reconciles by itself.

C8. `glossary.html`: three new terms. Fork (a reservation's private copy of
its topology, with its own version history, preview, diff, and restore).
Network element (a non-device canvas object that device ports attach to; four
types; saved with the topology; never provisioned today). Directory group sync
(the LDAP-to-HERD group mapping reconcile, see admin-ldap-sync).

## D. Corrections to existing text

- `admin-reporting.html:53` says "if you don't see Reporting in the nav,
  you're not an admin". Wrong: Reporting always appears in the nav for every
  signed-in user; the page itself denies non-admins. `quickstart.html:185`
  already has the right wording; match it.
- `release-0-2-0.html` quality-bar figures (3,600 / 184 / 151 / 580) stay as
  the historical record of 0.2.0; do not edit them in place. The refreshed
  figures belong on the 0.3.0 page only.

## E. Verified as already correct (do not re-do)

Fork Preview/Diff/Restore prose, network elements section, the multi-port
wiring dialog and Quick connect, the admin Create Connection dialog with its
Multi default and Single toggle, the Wiring tab and its retry outcomes, the
"Commit to reservation" label, the top-nav mockup, all relative and asset
links, all numeric defaults (30-minute session, 30-second health-poll floor,
24-hour assistant idle expiry, 50 dynamic instances, 30-day reporting window).

## F. Definition of done

- Every page's nav points at 0.3.0; `grep -l "release-0-2-0" docs/manual/*.html`
  returns only `index.html` (via the 0.3.0 page chain), `release-0-3-0.html`,
  and `release-0-2-0.html` itself.
- The repo's banned-character check (em-dash, bullet glyph, ASCII arrows,
  box-drawing) finds nothing under docs/manual.
- Every href resolves (relative files exist; the v0.3.0 tag URL is live).
- Open the pages in a browser once; mockups render with the shared CSS.
- PR body lists the pages touched and links this brief's sections.
