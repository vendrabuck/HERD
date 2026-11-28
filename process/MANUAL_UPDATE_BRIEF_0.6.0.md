# Brief for Claude Design: bring the HERD manual up to v0.6.0

Repo: vendrabuck/HERD, branch main at c4590e55 (0.7.0.dev0). The manual is
the HTML under `docs/manual/` (21 pages plus `assets/`), published at
<https://vendrabuck.github.io/HERD/manual/>. Its last content edit was
1ec8dd76 (2026-10-01, the `device_cabled` delete refusal in
`troubleshooting.html`). Since the 0.5.0 refresh (commit 81ab3557, 2026-09-15)
only two manual edits landed, both in `troubleshooting.html` line 153 (134f15a8
for issue #900 and 1ec8dd76 for issue #940); both stay as they are. Since then
v0.6.0 was released: tag `v0.6.0` at commit 3216868f, release date
2026-10-01, GitHub Release
<https://github.com/vendrabuck/HERD/releases/tag/v0.6.0>. The manual still
presents 0.5.0 as the current release everywhere.

Scope: `docs/manual/**` only. Do not touch code, tests, CHANGELOG.md, or other
docs. Deliver as one PR on a branch named `docs/manual-0-6-0`. Model the whole
change on commit 81ab3557 ("docs(manual): update manual to 0.5.0", 18 files):
one new release page plus the nav repoint across every page.

Unlike 0.5.0, this release changed screens that existing manual pages already
draw: the Reservations list, the Inventory page, the reservation detail modal,
the header, and the admin menu. So the work is Part A (new page), Part B
(repoint, 17 existing files), Part C (twelve fills), and Part D (nine
corrections: seven to text this release made wrong, two to older errors found
while preparing this brief). Part E lists what was checked and
left alone, and the changes a manual reader cannot see.

Sources of truth, in priority order: the running UI (a seeded 0.6.0 stack is at
https://localhost), `frontend/src/` for exact labels, `CHANGELOG.md` section
`## [0.6.0] - 2026-10-01` (eleven summary bullets, then `### Delivery detail`
with 44 entries), `FEATURES.md`, `docs/USER_GUIDE.md`, `docs/ADMIN_HANDBOOK.md`,
`docs/ROLES.md`, `docs/DRIVERS.md`, `docs/AI_ASSISTANT.md`, `docs/AI_GENERATE.md`,
`docs/AI_PURPOSE_CLASSIFICATION.md`, `docs/BULK_IMPORT_EXPORT.md`,
`docs/EXTERNAL_API.md`, `docs/SIMULATED_LAB_GUIDE.md`.

## Working rules (standing project rules)

- Branch from main, one branch `docs/manual-0-6-0`, one PR, opened as a normal
  PR against main. Do not merge it: Lane or his reviewing session merges on
  green. Do not rebase or force-push once it is open; add commits.
- Commit only files under `docs/manual/`. Never stage environment files, key or
  certificate files, editor or assistant working folders, `frontend/node_modules`,
  or anything the local ignore rules hide. Stage by file name, never
  `git add -A`.
- Commit messages and the PR body carry no `Co-Authored-By` trailer and no
  Claude or Anthropic attribution of any kind. Commits are authored under
  Lane's git identity from the local config.
- Write the PR body to `docs/PR_BODY.md` (it is ignored, local only) and open
  the PR with `gh pr create --body-file docs/PR_BODY.md`; never commit that
  file. Do not run `gh auth` in any form; on an auth error stop and report.
- Prose rules for every sentence you write into the manual or the PR: plain
  declarative sentences built from concrete facts; no em-dashes; no filler
  enthusiasm ("excited", "leverage", "delve", "seamless"); no tidy three-item
  parallel constructions for their own sake; no bullet glyphs, arrows,
  box-drawing, or emojis anywhere in a tracked file. Existing page voice wins
  over your own.
- Every claim about the UI must be checked against the running stack at
  https://localhost or the source under `frontend/src/`; do not describe a
  control you have not seen. If something in this brief contradicts the running
  UI, follow the UI and say so in the PR body.
- Do not touch the CHANGELOG, FEATURES.md, or any doc outside `docs/manual/`
  even if you find an error there; list it in the PR body instead.
- Do not spawn subagents for the work; do it in one pass so there is one writer
  in the checkout.
- Security wording: describe the hardening items in the plain "hardened" terms
  the changelog uses. Three security advisories were published with this
  release; link the repository's advisories list
  (<https://github.com/vendrabuck/HERD/security/advisories>) once, from the
  release page as Part A says, and do not copy identifiers, severity ratings,
  or advisory text into the manual or the PR.

## Conventions you must keep

- Manual "screenshots" are hand-built inline HTML/CSS mockups
  (`<figure class="shot">` with the `hd-*` classes in `assets/app.css`), never
  images. New visuals follow that pattern. Classes that already exist and are
  useful here: `hd-filters`, `hd-search`, `hd-select`, `hd-chip`, `hd-btn`,
  `hd-table`, `hd-top` (with `.right` and `.bell`), `hd-modal`, `hd-badge`.
  Where no class fits (a checkbox, a sort chevron, a help icon), use a small
  inline style or inline SVG in the pattern the neighbouring mockup uses.
- Every page shares the same top nav strip and (on release pages) a sidebar
  TOC. The nav's release link is repeated on all 18 non-legacy files (the 16
  ordinary pages plus the two newest release pages).
- Style rules for every file: no em-dashes (use colon, semicolon, comma), no
  box-drawing characters, no bullet glyphs or arrows in text (use `-`, `,`, the
  word `to`), no emojis. Password placeholders are literally `password`.
- Voice: second person, plain, task-first, same as the existing pages.
- Links to the repo use `https://github.com/vendrabuck/HERD/...`; relative links
  stay relative. Every link must resolve.

## A. New page: `release-0-6-0.html`

Copy the structure of `release-0-5-0.html` exactly (159 lines): the `.relmeta`
style block, sidebar TOC (`#what`, `#highlights`, `#quality`, `#boundaries`,
`#manual`), dek paragraph, relmeta strip, the "Read this before you file a bug"
box, `#what`, "What shipped" card grid, "Also in this release" list,
architecture continuity paragraph, "Quality bar" list, "Known boundaries" list,
the feature-gate warn callout, "Reading the manual against this release"
section, footer pagenav (back link to `release-0-5-0.html`, then Home). The
`<title>` is `HERD Manual: Release 0.6.0`; the h1 is `Release 0.6.0`. Sidebar
"Shared reference" list gains the 0.6.0 entry (active) above 0.5.0.

Relmeta strip values:

- Version: v0.6.0
- Released: 2026-10-01
- Tagged commit: 3216868f

Dek (facts to carry, in your own plain sentences): 0.6.0 makes the
reservations and inventory screens do more (sort, bulk Cancel and Release,
Cancel before activation, inventory filters, Classify now), makes reservation
status writes race-safe, makes device delete refuse while wiring depends on the
device, makes AI generation pick devices that can be wired, and shows what
version every part of the stack is running.

"What shipped" cards (link each to the manual page named; keep the existing
card markup and the link-text style with the trailing arrow entity that
`release-0-5-0.html` uses):

1. Reservations list that works at scale: the Owner, Status, Period, and Purpose
   headings sort (a third click returns to the default order) and the choice
   persists; a booking can be cancelled before it is active; tick rows and use
   Cancel selected or Release selected with one confirmation that says what
   will be skipped and why; an admin can cancel another user's reservation, and
   only the owner can release. Link `user-reservations.html#list` and
   `user-reservations.html#status`.
2. Reservation status writes are race-safe: a cancel that lands while a
   reservation is being created can no longer leave it ACTIVE and never wired;
   a future PENDING booking no longer touches device status; back-to-back
   bookings of one exclusive device no longer leave it AVAILABLE under a live
   booking; a dynamic instance that teardown retired is never brought back by a
   late create. Link `user-reservations.html#status`.
3. Inventory page filters: Status, Template (shown with vendor and model), and
   Topology selects beside the name search, a Clear filters control, choices
   that persist, and an expanded device row that stays open when the list
   refreshes under it. Link `admin-equipment.html#inspect`.
4. Device delete refuses while wiring depends on the device: as a transit hop
   on a live reservation's wiring, or while any cable still names it. No force
   option. Link `troubleshooting.html#admin` and `admin-equipment.html#inspect`.
5. AI generation picks devices that can be wired, and the assistant keeps its
   record: generation asks cabling which candidate devices are connected and
   searches for an assignment that satisfies every proposed link; commit checks
   wireability before the reservation is created; a reservation assistant turn
   whose tools already ran is kept when the model returns no text, times out,
   or the provider fails. Link `user-ai.html#commit`, `troubleshooting.html#ai`,
   and `user-assistant.html#write`.
6. Version and build visibility: the version shows on the login page and beside
   the HERD wordmark in the header; an admin About page lists the version,
   build, and build date of the frontend and all twelve services and flags
   skew; the header help icon opens this manual. Link `quickstart.html#nav` and
   `admin-setup.html#about`.

"Also in this release" list (one sentence each, no new links needed unless
shown):

- An admin can classify one reservation's purpose from its detail modal with
  Classify now. Link `user-reservations.html#detail`.
- Hardened: the auth service authorizes on the lower of the token's role claim
  and the account's current role; the database and message-broker host ports
  bind to loopback only; a device node on a topology canvas keeps a fixed set
  of fields and never `field_data`; the execution service checks each
  reservation lifecycle event with the reservations service before acting;
  every device-scoped inventory read and the topology import follow device-group
  visibility; every CSV export neutralizes spreadsheet formulas. Add one
  sentence after the list item: three of these were published as security
  advisories with this release (link the advisories list above), and a
  deployment on 0.5.0 or older should upgrade, run `make migrate-cabling` so
  stored canvases are scrubbed, and rotate any device password that was saved
  on a topology canvas by an admin.
- Config apply is refused on a driver whose connection type cannot configure,
  and a driver's raw exception text no longer reaches a stored error. Link
  `admin-health-config.html#schedule`.
- Outbound webhooks (the `/api/v1` facade) can subscribe to
  `device.health_transition`.
- Message-bus consumers retry on an explicit delay schedule and keep a message
  alive while its handler runs, so a slow webhook receiver is not handed the
  same event twice.
- A streamed assistant answer always ends in a done or an error frame.
- A new task-oriented guide to the simulated-hardware tiers:
  `docs/SIMULATED_LAB_GUIDE.md` (link
  `https://github.com/vendrabuck/HERD/blob/main/docs/SIMULATED_LAB_GUIDE.md`).
- Developer facing: the frontend toolchain floor (npm 11.11.0) is pinned, an
  opt-in AI generation evaluation harness exists, the JSON log formatter emits
  every extra field with key-name redaction, and the unused
  `HEALTH_POLL_MINIMUM_INTERVAL_SECONDS` setting was removed.

Architecture continuity paragraph: unchanged from `release-0-5-0.html` line 99.

Quality bar: use the same bullet list shape as `release-0-5-0.html` lines 105
to 113, with these figures, measured at the tagged commit by the full
`make everything` run on 2026-10-01. Do not reuse 0.5.0's numbers.

- 6,169 backend unit tests across the 13 suites plus the repo-root suite;
  backend line coverage 96.7% with every service at 95% or higher.
- 1,636 frontend tests via vitest, 91.0% line coverage.
- 12 OpenAPI contract snapshot suites.
- 230 cross-service integration tests: 221 run in the main pass, the 6
  LDAP-mode tests run in their own phase against the gate stack, and 3 are
  gated on an AI provider or other stack configuration.
- 178 end-to-end browser tests, run twice: 130 on the unseeded stack (48
  device-gated skips) and 174 again on the seeded stack with no unexpected skip
  allowed (4 exempt).
- 41 live-LDAP tests against the checked-in `infra/ldap-test` directory,
  hard-required in the gate.
- 28 driver tests against real FRRouting and Nokia SR Linux nodes in the
  checked-in lab, run on every pull request and inside the gate.
- Nine Postgres-live suites against the gate database, hard-required: the seven
  from 0.5.0 plus two new this release, the reservation status compare-and-swap
  race (issue #899) and the sort-by-status ordering (issue #902).
- Load: 20 simulated users for one minute, 655 requests, 0 failures, median
  13 ms, p95 250 ms.

One more structural fact is new in CI: the frontend job reruns `npm install`
and fails on any `package-lock.json` diff (issue #885). Keep the same closing
sentence about the release gate and branch protection.

Known boundaries: carry forward the 0.5.0 list (`release-0-5-0.html` lines 121
to 130) verbatim EXCEPT the bullet "Classify now for a single reservation is an
API call in this release", which closed in this release: drop it and, in the
intro sentence under `#boundaries`, say it closed in 0.6.0 and link
`user-reservations.html#detail` (the same pattern `release-0-2-0.html` line 112
uses). Add these, each grounded in the changelog or the source:

- A delete right after a cancel can still precede execution's asynchronous
  teardown of the reservation's wiring. The delete guard answers 503 when
  cabling or reservations cannot be asked. (Changelog, issue #900.)
- Webhook delivery is at-least-once: a crash while a POST is in flight can
  still deliver the same event again. (Changelog, issue #911;
  `docs/EXTERNAL_API.md`.)
- Classify now can show on a finished reservation that is not yet eligible: a
  reservation that ended before classification existed and was never swept or
  backfilled answers "This reservation is not ready to be classified." Run
  Classify history first. (`frontend/src/lib/reservationStatus.ts` lines 28 to
  43; `frontend/src/lib/purposeClassify.ts` line 12.)
- The Period column sorts by start time; there is no heading that sorts by end
  time in the UI, only the API. (`ReservationsPage.tsx` lines 39 to 44.)
- The About page's skew check is strong evidence on a clean tagged build and
  weak on a dirty one: the build string names the commit an image was built
  from, not its bytes. (`docs/ADMIN_HANDBOOK.md` line 228.)

`#manual` section: "This manual now documents 0.6.0"; link `release-0-5-0.html`
as the record of the previous release, which in turn links 0.4.0 and earlier.

## B. Repoint the existing pages (17 files)

`ls docs/manual/*.html` shows 21 files today; 22 with the new page. Of the
21, 17 change and four do not (`release-0-4-0.html`, `release-0-3-0.html`,
`release-0-2-0.html`, `release-0-1-0.html`: each already points at the release
that demoted it, so leave them, exactly as the 0.5.0 refresh left
`release-0-3-0.html`).

Every ordinary page's top nav: the "Release 0.5.0" link becomes "Release 0.6.0"
and points at `release-0-6-0.html`. Exact lines today:

| File | Lines | What changes |
|---|---|---|
| admin-equipment.html | 23 | nav link |
| admin-health-config.html | 23 | nav link |
| admin-ldap-sync.html | 23 | nav link |
| admin-purpose-review.html | 23 | nav link |
| admin-reporting.html | 23 | nav link |
| admin-setup.html | 23 | nav link |
| quickstart.html | 23 | nav link |
| troubleshooting.html | 23 | nav link |
| user-ai.html | 23 | nav link |
| user-assistant.html | 23 | nav link |
| user-live-editing.html | 23 | nav link |
| user-notifications.html | 23 | nav link |
| user-reservations.html | 23 | nav link |
| user-topology.html | 23 | nav link |
| glossary.html | 32, 109 | nav link; "Release / version" entry: badge `v0.6.0`, "This manual documents 0.6.0", link to the new page |
| index.html | 53, 185 to 188 | nav link; the release card in "When you're stuck" (href line 185, h3 line 187, dek line 188) retitled Release 0.6.0 with a fresh one-line dek (reservations sorting and bulk actions, inventory filters, wireable AI generation, version visibility) and linking the new page |
| release-0-5-0.html | 31, 49, 60, 140, 149 to 150 | demote to a historical record exactly the way `release-0-4-0.html` was demoted for 0.5.0 |

Demoting `release-0-5-0.html`, modeled on `release-0-4-0.html` lines 31, 49
to 50, 60, 139, and 148 to 149:

- Nav (line 31): the link becomes "Release 0.6.0" pointing at
  `release-0-6-0.html` with `class="active"` (the historical pages keep the
  active class on the current release's nav entry).
- Sidebar shared-reference list (line 49): the active entry in the sidebar is
  the page's own, so 0.5.0 keeps `class="active"` there and a plain "Release
  0.6.0" link is added above it, as `release-0-4-0.html` lines 49 to 50 do for
  0.5.0.
- Dek (line 60): keep the 0.5.0 description and end with "The current release
  is 0.6.0, which this manual now documents." with 0.6.0 linked.
- `#manual` paragraph (line 140): "now describes 0.6.0; this page is kept as the
  record of 0.5.0", name the 0.4.0 page as the record of the release before
  it, and add the sentence about deployments still on 0.5.0, matching
  `release-0-4-0.html` line 139.
- Footer pagenav (lines 149 to 150): keep the back link to 0.4.0 and replace
  the Home link with a forward link "Release 0.6.0" to `release-0-6-0.html`,
  matching `release-0-4-0.html` lines 148 to 149. Leave the "API only in this
  release" and boundary wording in the 0.5.0 page body alone: it is the record
  of 0.5.0.

## C. Content that is missing

Twelve fills. For each: the page, the place, what to write, and the facts with
their source. Where a fill needs a new mockup, update the existing figure
rather than adding a second one.

C1. `user-reservations.html`, `#list` (lines 59 to 99), the list mockup,
caption, and legend. Bring the mockup to the real column set and add the
sorting and selection behavior. Facts from `ReservationsPage.tsx`:

- Real columns, left to right (lines 396 to 441): a select-all checkbox
  (accessible name "Select all reservations on this page"), ID, Owner, Status,
  Topo ID, Topology, Devices, Period, Purpose, and an unlabeled actions column.
  The manual mockup (line 80) shows only ID, Status, Topology, Devices, Period,
  Purpose and no checkbox; add the checkbox column, Owner, and Topo ID. Rows
  show Period as "start to end" in locale dates (line 150).
- Sortable headings are Owner, Status, Period, and Purpose (lines 409 to 439);
  ID, Topo ID, Topology, and Devices are plain. Period sorts by start time.
  First click on a heading sorts ascending, the second descending, the third
  clears back to the default order, which is newest created first (lines 54 to
  58, 231 to 241). The active heading shows an up or down chevron. Changing
  the sort returns to page one (line 232). The choice persists per page, kept
  with the user's saved preferences (changelog, issue #844). Status sorts
  alphabetically by status name (changelog, issue #902).
- Selection bar (lines 337 to 377), shown only when at least one row is
  ticked: "N selected", then the buttons "Cancel selected" and "Release
  selected", then "Clear selection". When no ticked row qualifies, that
  action's button is disabled and a note beside it says why, for example
  "None of the selected reservations can be cancelled: 1 already finished, 2
  not yours." (`lib/reservationBulk.ts` lines 71 to 87 and 131 to 137).
- Confirmation (`lib/reservationBulk.ts` lines 98 to 128): the dialog title is
  "Cancel Reservations" or "Release Reservations"; the confirm button reads
  "Cancel 3 reservations" or "Release 1 reservation"; the dismiss button reads
  "Keep reservations" or "Do not release". The text counts what will run:
  "Cancel 3 reservations? This releases their devices and cannot be undone.",
  or when some are skipped, "Cancel 2 of the 3 selected reservations? ... 1
  will be skipped: 1 already finished." Release says "This ends them early and
  frees their devices." The skip reasons are "already finished", "not active"
  (Release only), and "not yours".
- Result (`lib/reservationBulk.ts` lines 153 to 189; `ReservationsPage.tsx`
  lines 291 to 295): a success toast "Cancelled 3 reservations"; with failures
  an error toast "Cancelled 2, failed 1" plus ": <reason>" when every failure
  has the same server reason. Succeeded rows leave the selection; failed rows
  stay ticked. The selection is dropped when the page, the sort, or the "All
  reservations" view changes (lines 248 to 261). There is no bulk endpoint:
  the page runs the ordinary per-reservation cancel and release for each row
  (changelog, issue #843).
- Per-row buttons (lines 112 to 113, 158 to 181): a green "Release" and a red
  "Cancel" text button, shown by the same rule as the bulk actions. Cancel asks
  first: title "Cancel Reservation", text "Cancel this reservation? This
  releases its devices and cannot be undone.", buttons "Cancel reservation" and
  "Keep reservation" (lines 182 to 194).
- Admins only: an **All reservations** checkbox beside the page title and
  count (lines 315 to 327) switches the list from your own bookings to
  everyone's and returns to page one. This is what the Owner column is for.
  The manual does not mention it today; add it to the mockup and the legend.
- Legend additions: Owner and the other sortable headings; the checkbox column;
  the selection bar; the admin All reservations checkbox. The existing legend
  items 1 to 4 stay.

C2. `user-reservations.html`, `#detail` legend (lines 232 to 240): add one
legend item for **Classify now**. Facts from
`components/reservations/ReservationDetailModal.tsx` and `lib/`:

- It is an admin-only button on the Details tab, directly under the Purpose
  category row and any "AI suggested" line (ReservationDetailModal.tsx lines
  129 to 132, 300 to 310); while the request runs it reads "Classifying...".
- It appears on a COMPLETED, CANCELLED, or FAILED reservation that has no
  suggestion (`lib/reservationStatus.ts` lines 28 to 54). A reservation that
  ended before classification existed and was never swept or backfilled shows
  the button and answers with the not-ready message below.
- Outcomes: success toast "Suggested category: <category name>."
  (`lib/purposeClassify.ts` lines 29 to 33); a plain sentence for each other
  outcome: timeout "Classification timed out. Check that the AI orchestrator
  is responding, then try again."; transient "The AI orchestrator could not
  take the request right now. Check its status, then try again."; failed
  "Classification failed. Check the AI orchestrator logs, then try again.";
  forbidden "The AI orchestrator refused the request. Check that its internal
  token matches this service." (lines 17 to 27). Refusals: "Purpose
  classification is disabled." (line 7); "This reservation already has a
  suggestion. Review it on the Purpose Review page." (line 9); "This
  reservation is not ready to be classified." (line 12). On either 409 the
  button goes away.
- It calls the same endpoint `admin-purpose-review.html` already documents
  (`#backfill`, line 136).

C3. `admin-equipment.html`, `#inspect` (lines 101 to 140): the Inventory page
filters and the expanded-row fix. Update the mockup (line 112, a table with no
filters) with a filter row above it, then add a paragraph before the existing
"Rows per page" paragraph. Facts from `InventoryPage.tsx` and
`lib/inventoryFilters.ts`:

- Above the table: the name search ("Search devices by name...", line 515),
  then three labeled selects: **Status** with All, AVAILABLE, RESERVED,
  OFFLINE, MAINTENANCE (`inventoryFilters.ts` lines 7 to 12); **Template** with
  All plus each template shown as its name followed by vendor and model in
  parentheses (`InventoryPage.tsx` lines 213 to 216, 528 to 535), which is how
  vendor and model become filterable; **Topology** with All, PHYSICAL, CLOUD
  (line 13).
- A **Clear filters** button appears whenever a filter or the search is
  active and resets all of them (lines 544 to 553). When nothing matches, the
  table says "No devices match the current filters." with a Clear filters link
  (lines 610 to 620), instead of the plain "No devices found".
- Filters combine with each other and with the search; any change returns to
  page one; the count beside "All Devices" is the matching total
  (`docs/USER_GUIDE.md` lines 148 to 156). The choices are remembered between
  visits; a remembered status, topology, or template that no longer exists
  falls back to All (changelog, issue #842).
- Expanded rows: when the list changes (a search finishing, a filter change),
  a row that is still listed stays open and only rows that left the list close
  (`InventoryPage.tsx` lines 388 to 399, issue #938). Add this to the sentence
  at line 103. Selection for bulk delete is still cleared on a list change.

C4. `admin-equipment.html`: a short "Deleting a device" paragraph after the
"Rows per page" paragraph (line 140), the page currently never mentions
deleting. Facts: an admin deletes from the device page (**Delete**, confirm
dialog titled "Delete Device", `DevicePage.tsx` lines 168 and 339) or from the
Inventory page by ticking rows and using **Delete Selected** (confirm dialog
"Delete devices", `InventoryPage.tsx` lines 561 and 659 to 661). A device is
refused while a live reservation holds it or carries its wiring as a transit
hop (`device_in_use`), or while any cable still names it (`device_cabled`);
the device page keeps the refusal on screen with an **Open Connections** link
(`DevicePage.tsx` lines 184 to 194), for example "Device is still cabled (2
connections) and cannot be deleted. Remove its cables first."
(`frontend/src/api/inventory.ts` lines 298 to 305); a bulk delete reports
"Deleted 1, failed 2. 2 still cabled: remove their cables first"
(`InventoryPage.tsx` line 445). There is no force option, and the delete also
refuses when HERD cannot ask cabling or reservations. Link
`troubleshooting.html#admin`, which already explains both refusals (Part E).

C5. `admin-equipment.html`, `#bulk` (lines 202 to 215), and
`admin-reporting.html`, `#csv` (lines 136 to 150): two facts about exports and
imports. Sources: changelog entries for issues #910 and the topology import
hardening; `docs/BULK_IMPORT_EXPORT.md` lines 215 to 258.

- Every CSV HERD writes puts a single leading quote on a free-text cell whose
  first character (ignoring leading spaces) is `=`, `+`, `-`, `@`, a tab, or a
  carriage return, so a spreadsheet shows literal text instead of running it.
  Covered: the free-text columns of the topology export (topology, device, and
  port names), the device and template exports, and the utilization report's
  CSV (owner and fleet device names). Not covered: fixed enumerations, numbers,
  and the JSON `field_data` and `sections` columns. The three importers
  (topologies, devices, templates) strip that one quote again, only when a
  trigger character follows, so an exported `=1+1` round-trips and a name that
  starts with an apostrophe for its own reasons is left alone. The Reporting
  page's by-template CSV, built in the browser, follows the same rule.
- A non-admin importing topologies sees a device outside their groups the same
  way as one that does not exist: the row is rejected with `unresolved device
  names: <names>`, in a dry run and a real run, and nothing is stored for it.
  An admin is unfiltered. If HERD cannot check visibility, the import fails
  with a 503 and processes nothing. (`bulk_service.py` line 421;
  `docs/BULK_IMPORT_EXPORT.md` lines 267 to 283.)

C6. `quickstart.html`, `#nav` (lines 160 to 200) and the sign-in mockup
(`#tour`, lines 60 to 90): the help icon and the version. Facts from
`components/layout/AppLayout.tsx`, `pages/LoginPage.tsx`, `lib/links.ts`:

- In the header's right-hand group, left of the bell, a question-mark-in-circle
  icon (accessible name "Help", `AppLayout.tsx` lines 236 to 245) opens the
  published manual at https://vendrabuck.github.io/HERD/manual/ in a new tab
  (`lib/links.ts` line 5). Add it to the nav mockup and as legend item 7 with a
  matching `data-anno` pin (legend currently has six items, lines 188 to 195).
- The version appears as a small gray `v<version>` (for example `v0.6.0`)
  beside the HERD wordmark in the header (`AppLayout.tsx` lines 46 to 49) and
  on the sign-in card beside the title (`LoginPage.tsx` lines 41 to 44). Add it
  to both mockups and one sentence under the sign-in legend. The header and the
  login page show only the version; the build and build date are on the admin
  About page (C7).
- Update the `glossary.html` "Release / version" entry (Part B) with one clause:
  the version shows beside the wordmark in the header and on the sign-in
  screen.

C7. `admin-setup.html`: a new short section `#about` between Step 6
(`#dynamic`) and the closing "That's the whole chain" rule (line 278), titled
for checking versions after setup and after every upgrade; add a seventh
entry to the sidebar TOC after line 38, in the same markup as the six numbered
entries before it, numbered 7, reading "Check versions", linking `#about`. Facts from `pages/admin/AboutPage.tsx`, `api/about.ts`, `lib/appVersion.ts`,
and `docs/ADMIN_HANDBOOK.md` line 228:

- Open it from **Administration** then **About**, the last item in that menu
  (`AppLayout.tsx` lines 216 to 227); the page is admin only. It has a heading
  "About", a **Refresh** button (lines 25 to 33), a Frontend card with Version,
  Build, and Build date (lines 37 to 51), and a Services table with columns
  Service, Version, Build, Build date, Status (lines 54 to 66) and twelve rows:
  ACL, AI Orchestrator, Auth, Cabling, Config, Execution, Integration,
  Inventory, Notifications, Reservations, Secrets, User Profile
  (`api/about.ts` lines 54 to 65).
- Status is one of "reachable", "unreachable" (the service did not answer), or
  "invalid response" (it answered 200 with a body that is not a version record,
  such as a proxy's error page; the row shows dashes and no skew badge, issue
  #874) (`AboutPage.tsx` lines 124 to 132).
- A row turns amber with a "differs" badge when its version or its build differs
  from the frontend's. A build of `dev` is never flagged. The backend's
  `0.6.0.dev0` and the frontend's `0.6.0-dev` count as the same; so do
  `0.6.0rc1` and `0.6.0-rc.1`, while `0.6.0rc1` and `0.6.0-rc.2` do not
  (`lib/appVersion.ts` lines 88 to 105). A build date reads like
  "2026-09-19 22:18 UTC" (line 114).
- After an upgrade, check it before filing a bug: a "differs" row usually means
  that service was not rebuilt; `ai-orchestrator`, `notifications`, `acl`, and
  `user-profile` have no dev mount and need `docker compose up -d --build
  <service>`. `make version` on the host prints the build string a fresh build
  would carry. One honest limit: on a dirty build the match is weak evidence
  (`docs/ADMIN_HANDBOOK.md` line 228).

C8. `admin-health-config.html`, `#schedule` (lines 183 to 200), after the
bulleted list: config apply is gated on the driver contract, and driver error
text is sanitized. Sources: changelog entries for issues #839, #840, and #870;
`docs/DRIVERS.md` lines 28 to 60.

- Applying or scheduling a config on a device whose driver's connection type
  does not include `configure` is refused with 409 `driver_cannot_configure`,
  naming the connection type and the driver, before any job is created. Today
  only the Management connection type can configure. The scheduled-apply loop
  checks again when a job fires, against the device's current driver. Creating,
  listing, reading, and restoring config versions are not gated on any
  connection type (a Layer 2 or Layer 3 version stores intent). A configure
  on a device with no driver at all is refused by the execution service with
  409 `device_has_no_driver`, before any run is recorded.
- When a driver raises, the stored error on a run or an apply job is only
  `driver raised <ExceptionClassName>`; a driver that cannot be loaded stores
  `driver load failed: <ExceptionClassName>`; a driver process that dies with no
  structured error stores `driver process exited with status N`. The full text
  goes to the execution service log, tagged with the run id. A driver that
  returns its own failure (`success` false) keeps its own message.

C9. `troubleshooting.html`, `#ai` (lines 113 to 127): two new accordions in the
existing `details.accordion` markup. Sources: `lib/errors.ts` lines 98 to 114,
`components/topology-editor/AIDialog.tsx` lines 70 to 95,
`components/topology-editor/AICommitDialog.tsx` lines 14 to 26 and 126 to 135,
`docs/AI_GENERATE.md` lines 40 to 75 and 142.

- Generation toast listing role pairs: the generator could not find devices
  that can be wired for every proposed link. The toast shows a sentence (for
  example "The lab has no cabled path for 1 proposed connection; the topology
  cannot be built from the devices currently available.") then one "source role
  to target role" line per pair. Nothing is saved. Cause: no cable is recorded
  between available devices of those two kinds. Fix: an admin records the
  cabling (Administration, Connections; see
  `admin-equipment.html#cabling`), or you ask again with different devices. The
  generator already re-asks the model up to two times (`AI_GENERATE_MAX_REPAIRS`,
  default 2) before this appears.
- Commit toast "Commit failed: cannot wire this topology": after Accept, the
  commit checks the saved topology before it creates the reservation. The toast
  lists "<source role> to <target role>: <reason>" with the reason in plain
  words: "no cable path", "device not found", "cannot connect two elements
  directly", or "element has no available port". The just-created topology is
  deleted and no reservation exists. Fix the cabling, or reject and regenerate.
  If cabling cannot answer, the commit fails with an error toast instead.

C10. `user-assistant.html` (the accordion at lines 236 to 247, "The tab is
missing, or a question errored out") and `troubleshooting.html` line 128 (the
timed-out accordion): one added bullet and one sentence for the fallback
answers. Sources: `ai_client.py` lines 88 to 107; `docs/AI_ASSISTANT.md` lines
166 to 170; changelog entries for issues #848, #871, #903.

- If the assistant ran tools (so something may already have changed) and then
  returned no text, you get this answer: "I ran the steps above but did not
  produce a summary. Check the tool results for what was done, or ask me
  again." If it ran tools and then timed out, the provider failed, or the tool
  budget ran out, you get: "The steps above ran, but the assistant could not
  finish this reply. Review the results before continuing." In both cases the
  turn is kept, the tool panel shows what ran, and a config version the
  assistant proposed is kept too. Read the panel before asking again.
- If no tool had run, a timeout or provider failure is still an error and
  nothing from that turn is saved, as the page already says.
- Add to the existing "times out" bullet that a timeout after tools ran now
  keeps the turn, as above.

C11. `admin-setup.html`, driver section, line 138 (the paragraph about
`frr_l3` and `srl_l2`): add one sentence pointing to the new how-to,
`https://github.com/vendrabuck/HERD/blob/main/docs/SIMULATED_LAB_GUIDE.md`: it
compares the three simulated-hardware tiers (the mock drivers, the checked-in
lab of real operating systems, and the unbuilt external simulator) and walks
through building and reserving a topology on the lab in the UI. Keep the
existing `docs/NOS_LAB.md` link.

C12. `user-reservations.html`, `#status` and the Cancel vs Release callout are
corrected in D1 to D3; this item covers the one new fact that belongs in prose
rather than in a correction: a future PENDING booking holds no device. An
exclusive device is RESERVED only while a reservation in PENDING_PROVISION or
ACTIVE holds it; conflict checks still stop an overlapping booking, and a
device is released only when no other live reservation holds it. Add one
sentence to the PENDING row of the status table (line 165). Sources: changelog
entries for issues #897 and #898.

## D. Corrections to existing text

D1. `user-reservations.html`, status table (lines 163 to 172) and the list
mockup (lines 81 to 83). The "Who moves it" cells for PENDING (line 165, "Auto
to ACTIVE at start time") and PENDING_PROVISION (line 166, "Auto, on create")
leave out that you can Cancel them now. Cancel is offered on PENDING,
PENDING_PROVISION, and ACTIVE (`lib/reservationStatus.ts` lines 21 to 23, issue
841). Add "You can Cancel" to both cells, keep ACTIVE's "You can Release early"
and add Cancel there too. In the mockup, the PENDING and PENDING_PROVISION rows
(lines 82 and 83) have an empty actions cell; give them the red Cancel text
button as row 81 already has.

D2. `user-reservations.html`, the "Cancel vs Release" callout (line 177). Add who
may act: the owner or an admin may cancel (an admin sees Cancel on another
user's reservation); only the owner may release, so an admin who does not own it
sees no Release (`lib/reservationStatus.ts` lines 56 to 83; `docs/ROLES.md`
lines 29 to 31; changelog, issues #841 and #843). The CANCELLED row (line 169)
already says "You or an admin"; leave it.

D3. `user-reservations.html`, the detail-modal mockup footer (line 224). It
shows `Edit Resources` and `Cancel reservation` buttons in the footer. In the
real modal, Release (green, owner of an ACTIVE reservation) and Cancel (red)
sit in a bar at the bottom of the Details tab and read "Release" and "Cancel";
"Cancel reservation" is the confirm button of the dialog that Cancel opens, and
Edit Resources, Edit topology, and View as-built sit at the right end of the tab
bar (`ReservationDetailModal.tsx` lines 197 to 238 and 349 to 370, 432 to 436).
Redraw the footer with the real Release and Cancel buttons and move Edit
Resources to the tab bar.

D4. `user-reservations.html`, "Exclusive" card (line 186): "Status flips to
RESERVED while held." Make it precise: while a reservation that is being
provisioned or ACTIVE holds it; a future PENDING booking does not flip it
(issues #897 and #898). The ACTIVE status row (line 167) is already correct.

D5. `admin-purpose-review.html`, line 136, last sentence: "There is no button for
this on the page yet: in this release it is an API call only." This is now
wrong. Replace it with: an admin can also classify one finished reservation
from its detail modal with **Classify now** (link
`user-reservations.html#detail`); the Purpose Review page itself lists only
reservations that already carry a suggestion and says "A finished reservation
with no suggestion can be classified from its detail view on the Reservations
page." (`PurposeReviewPage.tsx` lines 120 to 123). Keep the API paragraph. In
`#where` (line 60), add one clause pointing to the same button. Do not change
`release-0-5-0.html` for this: it is the record of 0.5.0.

D6. `user-topology.html`, line 274: "export as JSON if you need a byte-for-byte
copy of a topology that includes elements." Since this release a device node on
a canvas keeps only a fixed set of fields (id, name, topology type, connection
type, status, template name, template icon, and the topology-template role) at
every write and every read, including the export, and device `field_data` is
never stored or returned (changelog, the canvas hardening entry). Change "a
byte-for-byte copy of a topology that includes elements" to "a complete copy of
a topology that includes elements" and add one clause that device nodes in an
export carry only those fields.

D7. `glossary.html` line 109 and `index.html` lines 185 to 188: covered in
Part B (release entry and card). Listed here so the reviewer sees them as
corrections of text that is now stale, not only as nav edits.

D8. `admin-health-config.html`, line 159, the **Restore** item: "Re-applies an
older version as a new apply." This is wrong and predates 0.6.0. Restore
creates a new config version copied from the older one and marked as restored
from it; it is a draft and nothing is applied to the device until you apply
it (`services/inventory/app/routers/device_configs.py` lines 358 to 374).
Rewrite the first sentence to say that, and keep the rest of the item (the
reservation guard, the 409, the 503) as it is. Check line 134's section for
any other sentence that says a restore applies, and fix it the same way.

D9. `user-reservations.html`, line 92, the list caption: "Your bookings, newest
behaviour on top." The word "behaviour" is a stray; the default order is
newest created first. Make it "Your bookings, newest on top." and, once C1
is in, add that a heading click changes the order.

## E. Verified as already correct, and changes a manual reader cannot see

E1. Verified in the current manual; do not re-do (page and lines checked):

- `troubleshooting.html` lines 152 to 153, "Can't delete a device": already
  describes `device_in_use` including the transit-hop case and the short wait
  after a cancel, and `device_cabled` with the count, the device page link to
  Connections, and "no force option". Covers issues #900 and #940. Compare its
  wording against `api/inventory.ts` lines 259 to 305 and leave the prose.
- `user-reservations.html` line 167 (ACTIVE flips exclusive devices to
  RESERVED), line 177 ("Cancel stops a future or current reservation"), line
  244 (Edit Resources, removed devices go back to AVAILABLE): consistent with
  the new status rules.
- `user-assistant.html` lines 183 to 184: "If it stalls badly it times out with
  a message rather than hanging" matches issue #946, where a streamed turn now
  always ends in a done or an error frame.
- `user-notifications.html` lines 116 to 127 and 181: the "Webhook" channel and
  the "device health changed" toggle belong to the notifications service
  (`SettingsPage.tsx` lines 19 to 32), not to the integration service's
  outbound webhook subscriptions that issue #831 extends. No manual page covers
  the `/api/v1` facade, so nothing to edit.
- `user-live-editing.html` lines 300 to 316 (seven retry outcomes, background
  retry) and `admin-setup.html` line 138 (the two NOS drivers): unchanged by
  this release apart from C11.
- `release-0-5-0.html` boundary bullet "Classify now ... API call" and its card
  05: correct as the 0.5.0 record; only the demotion edits in Part B apply.

E2. Not visible to a manual reader: no manual page change, mention on the
release page summary at most (issue numbers are the changelog's):

| Issue | Change | Where it shows on the release page |
|---|---|---|
| #944 | execution consumer loop no longer swallows a shutdown cancel; live exactly-once webhook test | not mentioned |
| #911 | consumers keep a message alive while a handler runs | "Also": consumers line |
| #895 | explicit NAK delay schedule, `NATS_ACK_WAIT_SECONDS` setting | "Also": consumers line |
| #896 | dynamic-instance ledger is forward-only | card 2 |
| #899 | every status transition is a compare-and-swap | card 2 |
| #898 | back-to-back bookings and release ordering | card 2 and C12 |
| #905 | start log passes `context_keys`, not the context | not mentioned |
| #906 | stale comments; L1 retry tests read the row back | not mentioned |
| #872 | JSON log formatter emits every extra with redaction | "Also": developer line |
| #880 | removed the dead `HEALTH_POLL_MINIMUM_INTERVAL_SECONDS` | "Also": developer line |
| #885 | npm floor and lockfile gate | "Also": developer line; quality bar |
| #849 | three AI settings now reach the container | not mentioned |
| #826 | opt-in AI generation evaluation harness | "Also": developer line |
| #831 | webhook subscription to `device.health_transition` | "Also" |
| #909 | device-scoped inventory reads follow group visibility; a hidden device answers like an unknown one | "Also": hardened line |
| #708 | database and broker host ports bind to loopback | "Also": hardened line |
| auth role | authorization uses the lower of claim and database role | "Also": hardened line |
| lifecycle events | execution checks each lifecycle event with reservations | "Also": hardened line |
| #946 | streamed assistant turn always ends in done or error | "Also"; E1 |

Coverage ledger: every one of the 44 Delivery detail entries is accounted for
above exactly once: in Part C (sort and bulk, issues #843, #844, #902 in C1,
issue #822 in C2, issues #842 and #938 in C3, #940 and #900 in C4 with E1, CSV #910 and
topology import in C5, #846 and #960 in C6, #874 and #846 in C7, #839 and #840
and #870 in C8, #828 and #827 in C9, #848 and #871 and #903 in C10, the
simulated lab guide in C11, #897 in C12), in Part D (#841 in D1 to D3, #897 in
D4, #822 in D5, the canvas hardening in D6), in Part E1 (#946, #831), or in the
E2 table.

## F. Definition of done

- `grep -l "release-0-5-0" docs/manual/*.html` lists only `release-0-6-0.html`
  (its backlink), `release-0-5-0.html` itself, and `release-0-4-0.html`'s
  historical forward link; every other page links `release-0-6-0.html`.
- `grep -n "0\.5\.0" docs/manual/*.html` outside `release-0-*.html` finds
  nothing stale (the glossary entry and the index card now say 0.6.0).
- A byte scan of every touched file finds no em-dash, en-dash, box-drawing
  character, bullet glyph, arrow character, or emoji. HTML entities that the
  existing pages already use for dashes and arrows stay as they are in lines
  you do not change.
- Every `href` in `docs/manual/` resolves (a link checker over the directory),
  including the new anchors `#about` in `admin-setup.html` and the six anchors
  the release cards use.
- Visual QA in a browser: the new page, the demoted 0.5.0 page, `index.html`,
  and one repointed page each render with the nav's active state correct; the
  updated Reservations list mockup, the Inventory mockup, and the quick-start
  nav mockup render without horizontal overflow at phone width.
- Every UI string in Part C is checked once against the running stack; any
  mismatch is reported in the PR body.
- PR body lists every page touched, cites the brief part for each change, and
  states which Part E items were verified unchanged.
