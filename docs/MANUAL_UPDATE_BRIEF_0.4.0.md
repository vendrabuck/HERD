# Brief for Claude Design: bring the HERD manual up to v0.4.0

Repo: vendrabuck/HERD, branch main at fc990e1e (0.5.0.dev0). The manual is the
HTML under `docs/manual/` (18 pages plus `assets/`), published at
<https://vendrabuck.github.io/HERD/manual/>. Its last content edit was
6e304090 (2026-09-04, the transit-gear column on the reporting page). Since
then v0.4.0 was released: tag `v0.4.0` at commit 497ef978, release date
2026-09-05, GitHub Release <https://github.com/vendrabuck/HERD/releases/tag/v0.4.0>.
The manual still presents 0.3.0 as the current release everywhere.

Scope: `docs/manual/**` only. Do not touch code, tests, CHANGELOG.md, or other
docs. Deliver as one PR on a branch named `docs/manual-0-4-0`. Model the whole
change on PR #667 (commit 597653a9), which did the same job for 0.3.0: one new
release page plus the nav repoint across every page.

Sources of truth, in priority order: the running UI (a seeded 0.4.0 stack is at
https://localhost), `frontend/src/` for exact labels, `CHANGELOG.md` section
`## [0.4.0] - 2026-09-05` (seven summary bullets, then `### Delivery detail`
with `#### <area>` subsections), `FEATURES.md`, `docs/USER_GUIDE.md`,
`docs/ADMIN_HANDBOOK.md`, `docs/ROLES.md`, `docs/AI_PURPOSE_CLASSIFICATION.md`,
`docs/ENV_VARS.md`.

## Conventions you must keep

- Manual "screenshots" are hand-built inline HTML/CSS mockups
  (`<figure class="shot">` with the `hd-*` classes in `assets/app.css`), never
  images. New visuals follow that pattern.
- Every page shares the same top nav strip and (on release pages) a sidebar
  TOC. The nav's release link is repeated on all 18 files.
- Style rules for every file: no em-dashes (use colon, semicolon, comma), no
  box-drawing characters, no bullet glyphs or arrows in text (use `-`, `,`,
  the word `to`), no emojis. Password placeholders are literally `password`.
- Voice: second person, plain, task-first, same as the existing pages.
- Links to the repo use `https://github.com/vendrabuck/HERD/...`; relative
  links stay relative. Every link must resolve.

## A. New page: `release-0-4-0.html`

Copy the structure of `release-0-3-0.html` exactly (151 lines): the `.relmeta`
style block, sidebar TOC (`#what`, `#highlights`, `#quality`, `#boundaries`,
`#manual`), dek paragraph, relmeta strip, `#what`, "What shipped" card grid,
"Also in this release" list, architecture continuity paragraph, "Quality bar"
list, "Known boundaries" list, "Reading the manual against this release"
section, footer pagenav.

Relmeta strip values:

- Version: v0.4.0
- Released: 2026-09-05
- Tagged commit: 497ef978

"What shipped" cards (link each to the manual page named):

1. Lab purpose classification (ADR 0013): a closed taxonomy of lab purposes
   per reservation, an AI-suggested category at booking time and again when
   the reservation ends, an admin Purpose Review page to accept or dismiss
   suggestions, and purpose reporting at device and user level that counts
   transit gear on a reservation's paths, with CSV downloads. Link
   `admin-purpose-review.html` and `admin-reporting.html#tables`.
2. A full multi-pass code review of the codebase closed 21 findings. The
   ones you can see: a fork commit can no longer wire a device that is not
   part of the reservation (the editor adds it to the reservation first; an
   error names the devices), group member details are admin-only, a device's
   config-version history follows device visibility, and the cabling list
   shows a non-admin only connections touching devices they can see. Link
   `user-live-editing.html#commit`, `admin-setup.html#groups`,
   `admin-health-config.html#config`, `admin-equipment.html#cabling`.
3. Events publish within milliseconds. The transactional outbox now wakes on
   write instead of waiting for its relay tick, so provisioning, notifications,
   and webhooks start right after the change that caused them. Link
   `user-notifications.html`.
4. AI-proposed network elements: the topology generator can place VLAN
   segments, subnets, external clouds, and patch trunks and attach devices to
   them; they appear as ghost nodes for review like devices, and committing
   keeps them. Link `user-ai.html#ghost` and `user-topology.html#elements`.
5. Fork integrity under concurrency: two saves can no longer claim the same
   physical port, a restore racing a save can no longer lose the restore, and
   the activation snapshot honors the same port exclusivity as a save. Phrase
   like the 0.2.0 card "failure paths that cannot eat wiring". Link
   `user-live-editing.html#conflicts`.
6. Scheduled config applies re-check authority when they fire: a job whose
   creator has since lost manage rights or the active reservation on the
   device resolves as skipped instead of applying, and a job cannot be
   scheduled more than 30 days out. Link `admin-health-config.html#schedule`.

"Also in this release" list:

- The purpose classifier runs on its own background task, so a slow AI
  provider never delays reservation activation or expiry, and a rate limit no
  longer counts against a reservation's classification attempts.
- Download CSV for every purpose reporting section.
- Reporting no longer truncates at 500 devices.
- The superadmin account can no longer be deactivated from the Users page.
- Uploads to the AI generator are rejected the moment they cross the file
  count or size cap, before the upload is read.
- Provider and internal error text no longer leaks into AI error responses.
- The Traefik dashboard is bound to the host's loopback address only.
- Fixed: cancelling or releasing a reservation whose device was deleted in the
  meantime now completes instead of retrying forever.

Quality bar (measured at the tagged commit by the full `make everything` run;
fill every figure from that run's log, do not reuse 0.3.0's):

- 4,837 backend unit tests across the 13 suites plus the repo-root
  suite; backend line coverage 96.9% with every service at
  95% or higher.
- 1,297 frontend tests via vitest, 90.2% line coverage.
- 12 OpenAPI contract snapshot suites.
- 204 cross-service integration tests (194 run, the
  rest gated on LDAP mode or an AI provider).
- 163 end-to-end browser tests, run twice: 128 on the unseeded stack (35
  device-gated skips) and 159 again on the seeded stack with no unexpected
  skip allowed (4 exempt).
- 41 live-LDAP tests against the checked-in `infra/ldap-test` directory,
  hard-required in the gate.
- Four Postgres-live suites against the gate database, hard-required: the
  LDAP sync reconciler, advisory locks, the fork restore-versus-save race, and
  the new two-session fork port-claim race.
- Load: 20 simulated users for one minute, 692 requests,
  0 failures, median 10 ms, p95 230 ms.
- Same closing sentence as 0.3.0 about the release gate and branch protection.

Known boundaries: carry forward all five 0.3.0 bullets verbatim (anchored VLAN
provisioning is a later phase; manual wiring retry is not in the external
API; a failed VLAN removal is logged, not blocked on; AI recipe authoring is
dark by default; driver packages are trusted code). Add two:

- Purpose classification is off by default. It needs a configured AI provider
  and `AI_PURPOSE_CLASSIFICATION_ENABLED`; a suggestion never writes the
  confirmed category by itself, an owner or an admin does.
- Config-version history for a device outside your groups now answers
  "Device not found", the same as the device page. That is the visibility
  rule catching up, not a missing device.

`#manual` section: "This manual now documents 0.4.0"; link
`release-0-3-0.html` as the record of the previous release, which in turn
links 0.2.0 and 0.1.0.

## B. Repoint the existing pages (18 files)

- Every page's top nav: the "Release 0.3.0" link becomes "Release 0.4.0" and
  points at `release-0-4-0.html`.
- `index.html`: the release card in "When you're stuck" (line 185) retitles
  to Release 0.4.0 with a fresh one-line dek and links the new page.
- `glossary.html` line 103: the "Release / version" entry badge to `v0.4.0`,
  text to "This manual documents 0.4.0", link to the new page.
- `release-0-3-0.html`: demote to a historical record exactly the way
  `release-0-2-0.html` was demoted (its dek reads "kept as the record of what
  0.2.0 shipped ... the current release is 0.3.0, which this manual now
  documents"): dek and the `#manual` paragraph say the current release is
  0.4.0, nav `active` moves to the 0.4.0 entry, sidebar shared-reference list
  gains the 0.4.0 entry above it, footer pagenav forward-link goes to
  `release-0-4-0.html`.
- `release-0-2-0.html` and `release-0-1-0.html`: no change.

## C. Content that is missing

C1. `user-live-editing.html#commit`: when you commit a fork whose canvas
includes a device that is not part of the reservation, HERD adds that device
to the reservation before saving, and a device you removed from the canvas is
removed from the reservation after the save. If the add is refused (the device
is booked elsewhere, or you are not allowed to add it), the commit fails with
"These devices are not part of the reservation: <ids>" and nothing is wired.
Admins get the same rule. Verify the exact order against the running UI and
`frontend/src/pages/TopologyEditorPage.tsx` (`handleCommitToReservation`).

C2. `user-ai.html#ghost`: a proposal can now include network elements (the
same four types as the canvas palette) with devices attached to them. They
arrive as ghost nodes beside the device ghosts; review and commit them the
same way. HERD, not the model, picks which device port each attachment uses,
in port-name order, skipping ports the proposal already used on that device.
Extend the existing proposal mockup with one element ghost if it fits.

C3. `admin-setup.html#groups`: users can list group names; only an admin or
superadmin can open a group and see its members and their emails. One
sentence, next to the "New registrations auto-join Not Grouped" line.

C4. `admin-health-config.html#config` and `#diff`: the version list, the diff,
and a version's detail follow device visibility: a device outside your groups
answers "Device not found", exactly like the device page. Admins see every
device. One or two sentences.

C5. `admin-equipment.html#cabling` (and a cross-reference from
`user-topology.html#connect`): a non-admin's connections list contains only
connections touching at least one device they can see; the page total counts
the same set. Admins see the fleet. If inventory cannot answer the visibility
lookup, the list fails closed with "Could not verify device visibility;
connections were not returned. Retry the request." Add that message to
`troubleshooting.html#admin`.

C6. `admin-health-config.html#schedule`: two sentences. A scheduled apply
re-checks, when it fires, that its creator still has manage rights or an
active reservation on the device; if not, the job resolves as skipped with the
error "creator no longer authorized for this device". `scheduled_for` may be
at most 30 days out (`APPLY_JOB_MAX_HORIZON_DAYS`); later is refused when you
schedule.

C7. `user-notifications.html`: one sentence near the bell paragraph (line 57):
events now reach the notification service within milliseconds of the change;
the bell's unread count still refreshes every 30 seconds.

C8. `glossary.html`: check for and add if absent: "Purpose category" (the
confirmed lab purpose on a reservation, from a closed taxonomy, inherited by
every device in it including transit gear) and "Transit gear" (a switch or
router a reservation's wiring passes through without being reserved; counted
under the reservation's purpose in the by-device report). "AI-suggested
purpose" already exists at line 101; link the new entries to it.

## D. Corrections to existing text

- `release-0-3-0.html` quality-bar figures stay as the historical record of
  0.3.0; do not edit them in place. The refreshed figures belong on the 0.4.0
  page only.
- Any page that says all authenticated users can see all cabling, or that a
  user can view a group's members, is now wrong; grep for "any authenticated"
  and "members" and fix the sentence where you find one.

## E. Verified as already correct (do not re-do)

The Purpose Review page (`admin-purpose-review.html`), the purpose sections
and transit column on the reporting page, the LDAP Sync page, the fork
history panel with Preview, Diff, and Restore, the network elements section
of the topology page, the wiring and multi-connect dialogs, the top-nav
mockup, all relative and asset links, and all numeric defaults (30-minute
session, 30-second health-poll floor, 24-hour assistant idle expiry, 50
dynamic instances, 30-day reporting window).

## F. Definition of done

- Every page's nav points at 0.4.0; `grep -l "release-0-3-0" docs/manual/*.html`
  returns only `release-0-4-0.html` (the previous-release link) and
  `release-0-3-0.html` itself, plus `release-0-2-0.html` if its forward link
  is left pointing at 0.3.0 as the historical chain.
- The repo's banned-character check (em-dash, bullet glyph, ASCII arrows,
  box-drawing) finds nothing in what you added.
- Every href resolves (relative files exist; the v0.4.0 tag URL is live).
- Open the pages in a browser once; mockups render with the shared CSS.
- PR body lists the pages touched and links this brief's sections.
