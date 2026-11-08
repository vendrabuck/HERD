# Brief for Claude Design: bring the HERD manual up to v0.7.0

Repo: vendrabuck/HERD, branch main at `<main commit after the 0.8.0.dev0 bump>`
(0.8.0.dev0). The manual is the HTML under `docs/manual/` (22 pages plus
`assets/`: 16 ordinary pages and six release pages), published at
<https://vendrabuck.github.io/HERD/manual/>. Its last content edit was a98d7d45
(2026-10-15, `troubleshooting.html` line 56). The manual was last refreshed to
0.6.0 by commit b7b16d17 (2026-10-02) and its review fixes 4ac05c9d. Since then
eight spot edits landed, each from a code PR or an accuracy sweep, and all of
them stay as they are: a250fb96 (`user-ai.html` line 185), 8db0036e
(`user-live-editing.html` line 196), 5a368931 (`user-live-editing.html` line
197), 77ce2dc8 (`user-notifications.html` lines 108, 167, 177, 182), 3d1ddf95
(`troubleshooting.html` line 159, `user-ai.html` line 97), 6bb7e88a
(`troubleshooting.html` lines 137 to 142, `user-ai.html` line 110,
`user-live-editing.html` line 316), and a98d7d45 (`troubleshooting.html` line
56). Since then v0.7.0 was released: tag `v0.7.0` at commit 9fc48ba0, release
date 2026-11-08, GitHub Release
<https://github.com/vendrabuck/HERD/releases/tag/v0.7.0>. The manual still
presents 0.6.0 as the current release everywhere.

Scope: `docs/manual/**` only. Do not touch code, tests, CHANGELOG.md, or other
docs. Deliver as one PR on a branch named `docs/manual-0-7-0`. Model the whole
change on commit b7b16d17 ("docs(manual): update manual to 0.6.0", 18 files):
one new release page plus the nav repoint across every page.

This release is a hardening release (its changelog calls it that), so most of
its 177 delivery entries are invisible to a manual reader. It still changed
screens that existing manual pages already draw: the Inventory page, the
Reservations list, the header, Settings, and the Hypervisors form. It also added
three screens or sections the manual never described: the Topologies list page,
the Profile section of Settings, and the Administration Users page. So the work
is Part A (new page), Part B (repoint, 17 existing files), Part C (nine fills),
and Part D (eight items: four to text this release made wrong or stale, D1, D2,
D7, and D8; three to older errors found while preparing this brief, D3, D4, and
D5; and one addition to a paragraph this release extended, D6). Part E lists
what was checked and left alone, and the changes a manual reader cannot see.

Sources of truth, in priority order: the running UI (a seeded 0.7.0 stack is at
the address the supervisor gives you; on this project's host that is
https://192.168.1.27, never localhost), `frontend/src/` for exact labels,
`CHANGELOG.md` section `## [0.7.0] - 2026-11-08` (eight summary bullets, then
`### Delivery detail` with 177 entries), `FEATURES.md`, `docs/USER_GUIDE.md`,
`docs/ADMIN_HANDBOOK.md`, `docs/ROLES.md`, `docs/TROUBLESHOOTING.md`,
`FRESH_SETUP.md`, `docs/EXTERNAL_API.md`, `docs/BULK_IMPORT_EXPORT.md`.

## Working rules (standing project rules)

- Branch from main, one branch `docs/manual-0-7-0`, one PR, opened as a normal
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
  declarative sentences built from concrete facts; no em-dashes or en-dashes; no
  filler enthusiasm ("excited", "leverage", "delve", "seamless"); no tidy
  three-item parallel constructions for their own sake; no bullet glyphs,
  arrows, box-drawing, or emojis anywhere in a tracked file. Existing page voice
  wins over your own.
- Every claim about the UI must be checked against the running stack or the
  source under `frontend/src/`; do not describe a control you have not seen. If
  something in this brief contradicts the running UI, follow the UI and say so
  in the PR body.
- Do not touch the CHANGELOG, FEATURES.md, or any doc outside `docs/manual/`  even if you find an error there; list it in the PR body instead.
- Do not spawn subagents for the work; do it in one pass so there is one writer
  in the checkout.
- Security wording: two security advisories are addressed by this release (the
  token-settings check and the template password masking, both named in the
  changelog summary). Describe those two items in the plain "hardened" and
  "checked at startup" terms the changelog uses. The advisories are addressed,
  not published, at the time of this brief: do not link an advisories page, and
  do not copy identifiers, severity ratings, or advisory text into the manual or
  the PR.

## Conventions you must keep

- Manual "screenshots" are hand-built inline HTML/CSS mockups
  (`<figure class="shot">` with the `hd-*` classes in `assets/app.css`), never
  images. New visuals follow that pattern. Classes that already exist and are
  useful here: `hd-card`, `hd-filters`, `hd-search`, `hd-select`, `hd-field`,
  `hd-label`, `hd-input`, `hd-btn`, `hd-table`, `hd-top`, `hd-modal`,
  `hd-modal-bg`, `hd-badge`, `hd-tabs`. There is no class for the new left
  filter panel; build it as a two-column flex row (a 240px card, then the list)
  from `hd-card`, `hd-field`, `hd-label`, and `hd-select`, with a small inline
  style where no class fits, as the neighbouring mockups do. Where no class fits
  a checkbox, a sort chevron, or a help icon, copy the inline pattern the
  Reservations mockup in `user-reservations.html` (lines 72 to 112) uses.
- Every ordinary page shares the same top nav strip and release pages add a
  sidebar TOC. The nav's release link is repeated on all 18 non-legacy files
  (the 16 ordinary pages plus the two newest release pages).
- Style rules for every file: no em-dashes or en-dashes (use colon, semicolon,
  comma), no box-drawing characters, no bullet glyphs or arrows in text (use
  `-`, `,`, the word `to`), no emojis. Password placeholders are literally
  `password`. HTML entities that existing pages already use for dashes and
  arrows stay as they are in lines you do not change; do not add new ones.
- Voice: second person, plain, task-first, same as the existing pages.
- Links to the repo use `https://github.com/vendrabuck/HERD/...`; relative links
  stay relative. Every link must resolve.

## A. New page: `release-0-7-0.html`

Copy the structure of `release-0-6-0.html` exactly (167 lines): the `.relmeta`
style block (line 10 onward), sidebar TOC (`#what`, `#highlights`, `#quality`,
`#boundaries`, `#manual`, lines 36 to 43), dek paragraph, relmeta strip (line
63), the "Read this before you file a bug" box (line 70), `#what` (line 75),
the "What shipped" card grid (line 82), "Also in this release" list (line 91),
the architecture continuity paragraph (line 103), `#quality` (line 105),
`#boundaries` (line 121), the feature-gate warn callout (line 141), `#manual`
(line 146), and the footer pagenav (line 154 onward: back link to
`release-0-6-0.html`, then Home). The `<title>` is `HERD Manual: Release 0.7.0`;
the h1 is `Release 0.7.0`. Sidebar "Shared reference" list gains the 0.7.0
entry (active) above 0.6.0.

Relmeta strip values:

- Version: v0.7.0
- Released: 2026-11-08
- Tagged commit: 9fc48ba0

Source-of-record line under the strip: the
<https://github.com/vendrabuck/HERD/releases/tag/v0.7.0> release notes and
`CHANGELOG.md`, same markup as `release-0-6-0.html` line 68.

Dek (facts to carry, in your own plain sentences): 0.7.0 is a hardening release
that also gives accounts a profile and gives the list pages a left filter panel.
Services refuse to start on a blank or unusable token key, template password
defaults are masked from non-admins, and a stack on a shared host no longer
reads another stack's containers. The Topologies and Reservations lists search
and filter, the Topologies list sorts and deletes several rows at once, dialogs
open centred, and toasts appear at the bottom centre.

"What shipped" cards (keep the existing card markup and the link-text style
with the trailing arrow entity that `release-0-6-0.html` uses; the grid is
two columns, six cards):

1. Accounts have profiles: every account has a required display name and an
   optional contact email and phone number. You edit yours in the Profile
   section of Settings; an admin edits any account from Administration, then
   Users, with **Edit profile**. The header shows your name, with your username
   on hover. Email notifications go to the contact email when one is set. Link
   `user-notifications.html#profile` and `admin-setup.html#users`. (Changelog,
   issue #1252.)
2. List pages have a filter panel: the Inventory filters moved into a left
   **Filters** panel with no change in behaviour; the Reservations list can be
   searched and filtered by Status, Category, and Period, saved per user; the
   Topologies list gains search, an Owner filter, sortable headings, and
   multi-select delete. Link `user-reservations.html#list`,
   `user-topology.html#open`, and `admin-equipment.html#inspect`. (Issues #957,
   #958, #959.)
3. Token settings are checked when a service starts: a blank, placeholder, or
   unusable `AUTH_SECRET_KEY`, or an `AUTH_ALGORITHM` other than HS256, HS384,
   or HS512, stops the service with a message naming the variable. Link
   `troubleshooting.html#admin`. (Issue #1186.) One sentence after the card
   text: two security advisories are addressed by this release, this check and
   the template password masking under "Also"; per the security wording rule,
   no link and no identifiers.
4. A stack shares its host more safely: `HERD_GATEWAY_BIND` publishes the
   gateway on one address, each stack's Traefik reads only its own compose
   project, `make up` refuses to start while another project holds ports 80 or
   443, and `make certs` writes a ten-year self-signed gateway certificate. Link
   `troubleshooting.html#admin` and `quickstart.html#signin`. (Issues #1256,
   #1240, #1239.)
5. Reservation edits follow the rules create follows: an edit that races a
   cancel, release, or auto-complete keeps nothing and answers 409; an edit
   cannot stretch a reservation past `RESERVATION_MAX_DURATION_SECONDS`; a
   device added to a PENDING reservation is judged by the window conflict check;
   the provisioning timeout no longer restarts on a purpose edit; the calendar
   answers 503 to a non-admin instead of showing everyone's bookings when
   device visibility cannot be verified. Link `user-reservations.html#detail`
   and `user-reservations.html#calendar`. (Issues #994 to #1000.)
6. Interface fixes: modal dialogs open centred (Tailwind 4 had pinned them to
   the top-left corner in every release through 0.6.0), toasts appear at the
   bottom centre where they no longer cover the topology editor's Save button,
   and the Settings page scrolls again. No link. (Issues #979, #942, #1260.)

"Also in this release" list (one sentence each; links only where shown):

- An admin can pause and resume an outbound webhook subscription with
  `PATCH /api/v1/webhooks/{id}` and the body `{"is_active": false}` or
  `{"is_active": true}`; a paused subscription receives no event and nothing is
  replayed on resume. Link
  `https://github.com/vendrabuck/HERD/blob/main/docs/EXTERNAL_API.md`. (Issue
  #1078.)
- A hypervisor has a **Bookable by device group** setting, and its **Enabled**
  flag is now enforced: a non-admin sees and books a hypervisor's dynamic
  templates only through a device group their user groups hold a permission on,
  and a booking on a disabled hypervisor is refused. Link
  `admin-setup.html#dynamic`. (Issues #1053, #1033.)
- A topology cannot be deleted while a PENDING, PENDING_PROVISION, or ACTIVE
  reservation uses it, and a port cannot be deleted or renamed while a cable
  names it. Link `user-topology.html#open` and `admin-equipment.html#inspect`.
  (Issues #977, #1023.)
- Hardened: non-admin template reads mask password-field defaults the way
  device reads mask password values; the sandbox child for driver packages
  starts in Python's isolated mode and puts the package on its path only after
  its resource limits are set; uploaded tar archives with an oversized header
  record are refused at the header block; the e2e Selenium container publishes
  on 127.0.0.1 only; the config login limits failed attempts and answers 429;
  webhook destinations must resolve to public addresses; a non-admin reservation
  owner's execution run list shows only runs on devices they can see.
- Import dry runs are full rehearsals for devices, templates, and topologies: the
  dry-run report matches the committing report row for row; the template export
  and import carry a `hypervisor_name` column; the device export leaves out
  dynamic-instance devices. Link `admin-equipment.html#bulk`. (Issues #1017,
  #1064, #1024, #1068.)
- Wiring and dynamic instances recover from failures that used to strand them:
  VLAN numbers are unique across everything reachable through the current
  cabling, the background wiring retry no longer stalls on `FAILED` rows it will
  never drive, and an upstream 401 or 403 during an `INTERNAL_API_TOKEN`
  rotation is retried instead of recorded as a missing device. (Issues #1003,
  #1190, #1203, #1215.)
- Auth boots on a stack upgraded in place before `make migrate`; a reservation
  that lands `FAILED` notifies its owner; the buffered assistant route answers
  502 `Assistant call failed` instead of a plain 500. (Issues #1261, #1077,
  #1280.)
- Developer facing: tokens are signed and verified with PyJWT through one helper
  (`herd_common.jwt_codec.decode_access_token`, 10 second leeway); existing
  Hypervisor recipes must accept `instance_ref=None`; three developer guides,
  `docs/specs/DEVELOPMENT.md`, `docs/specs/TESTING.md`, and
  `docs/specs/CONVENTIONS.md`; a test-effectiveness sweep read every test file
  and landed its fixes; `make everything` now runs the ci.yml checks it used to
  skip. (Issues #1186, #937, #1134 to #1148.)

Add one upgrade-note paragraph after the list, as the 0.6.0 page did for its
advisories. Facts, from the changelog upgrade note and `docs/TROUBLESHOOTING.md`:
after pulling 0.7.0, recreate Traefik (`make up`, or `docker compose up -d`)
before anything restarts it; a Traefik container created before 0.7.0 still
mounts the deleted `infra/traefik/traefik.yml`, and restarting it fails with a
`not a directory` mount error. A stack whose `AUTH_SECRET_KEY` is unset, blank,
still the placeholder, or not usable as an HMAC key now refuses to start,
naming the variable; if a configuration was saved in the config UI, fix the
value there. This release adds migrations (for example auth 0009 and inventory
0022 and 0024, and a reservations column, `provision_started_at`), so run
`make migrate` after pulling.

Architecture continuity paragraph: unchanged from `release-0-6-0.html` line
103.

Quality bar: use the same bullet list shape as `release-0-6-0.html` lines 107
to 118, with these figures, measured at the tagged commit by the full
`make everything` run on 2026-11-08. Do not reuse 0.6.0's numbers. Intro line:
"Measured at the tagged commit by the full `make everything` run on
2026-11-08:".

- 9,158 backend unit tests passed across the 13 service suites (7,898) plus the
  repo-root suite (1,260), with 112 skipped; backend line coverage 97.0%
  (24,625 statements, 741 missed) with every service at 95% or higher.
- 2,030 frontend tests via vitest in 166 test files, run a second time under
  `TZ=America/Los_Angeles` with the same result; coverage is 94.43% lines,
  92.99% statements, 86.15% branches, and 92.28% functions.
- 12 OpenAPI contract snapshot suites.
- 271 cross-service integration tests: 259 run in the main pass, 3 model-dependent
  assistant tests run in their own pass after it, the 6 LDAP-mode tests run in
  their own phase against the gate stack, and 3 are gated on an AI provider or
  other stack configuration.
- 206 end-to-end browser tests, run twice: 148 passed and 58 device-gated
  skipped on the unseeded stack, and 202 passed again on the seeded stack with
  no unexpected skip allowed (4 exempt).
- 41 live-LDAP tests against the checked-in `infra/ldap-test` directory,
  hard-required in the gate.
- 28 driver tests against real FRRouting and Nokia SR Linux nodes in the
  checked-in lab, run on every pull request and inside the gate.
- Fifteen Postgres-live suites against the gate database, hard-required, 69
  tests in all.
- Load: 20 simulated users for one minute, 660 requests, 0 failures, median
  13 ms, p95 120 ms.

Closing sentence: keep `release-0-6-0.html` line 119 (the release gate and
branch protection) but replace its first sentence, which was about the 0.6.0
npm lockfile check, with: "New in the gate this release: `make everything` runs
the ci.yml checks it used to skip, among them `uv lock --check`, the frontend
suite under `TZ=America/Los_Angeles`, and the `package-lock.json` sync check
(changelog, the Delivery detail entry "`make everything` runs the ci.yml checks
it used to skip")." Keep the rest of the line as it
is.

Known boundaries: carry forward the 0.6.0 list (`release-0-6-0.html` lines 125
to 138, fourteen bullets) verbatim; none of them closed in this release. Change
only the intro sentence under `#boundaries` to say these are limits of 0.7.0.
Add these, each grounded in the changelog, a doc, or the source:

- The generated gateway certificate is self-signed, so every client warns until
  you trust `server-chain.crt` by hand; macOS 10.15 and later refuse a TLS
  server certificate valid for more than 825 days even when trusted, so a
  ten-year certificate still warns there until you issue a shorter one.
  (`FRESH_SETUP.md` lines 82 to 88.)
- The phone number on a profile is stored and shown only: nothing calls or
  texts it. (`frontend/src/components/profile/ProfileForm.tsx` line 92;
  changelog, issue #1252.)
- Pausing a webhook subscription is an API call only; no screen exposes it, and
  resuming does not replay the events it missed. A delivery already under way
  when you pause finishes and is recorded. (`docs/EXTERNAL_API.md` lines 277 to
  286.)
- A hypervisor created before 0.7.0 starts admin-only until an admin assigns a
  device group, so a non-admin sees none of its dynamic templates until then.
  (Changelog, issue #1053.)
- Hypervisor recipes written before 0.7.0 must accept `instance_ref=None`,
  because ending a reservation now runs a keyed destroy for a dynamic instance
  whose create never reported a handle. (Changelog, issue #937.)

`#manual` section: "This manual now documents 0.7.0"; link `release-0-6-0.html`
as the record of the previous release, which in turn links 0.5.0 and earlier.
The GitHub Release link goes to `releases/tag/v0.7.0`.

## B. Repoint the existing pages (17 files)

`ls docs/manual/*.html` shows 22 files today; 23 with the new page. Of the 22,
17 change and five do not (`release-0-5-0.html`, `release-0-4-0.html`,
`release-0-3-0.html`, `release-0-2-0.html`, `release-0-1-0.html`: each already
points at the release that demoted it, so leave them, exactly as the 0.6.0
refresh left `release-0-4-0.html`).

Every ordinary page's top nav: the "Release 0.6.0" link becomes "Release 0.7.0"
and points at `release-0-7-0.html`. Exact lines today:

| File | Lines | What changes |
|---|---|---|
| admin-equipment.html | 23 | nav link |
| admin-health-config.html | 23 | nav link |
| admin-ldap-sync.html | 23 | nav link |
| admin-purpose-review.html | 23 | nav link |
| admin-reporting.html | 23 | nav link |
| admin-setup.html | 23, 287, 293, 294, 295, 303 to 307, 321 | nav link; the About mockup (see below) |
| quickstart.html | 23, 72, 93, 171, 190 | nav link; every `v0.6.0` becomes `v0.7.0` |
| troubleshooting.html | 23 | nav link |
| user-ai.html | 23 | nav link |
| user-assistant.html | 23 | nav link |
| user-live-editing.html | 23 | nav link |
| user-notifications.html | 23 | nav link |
| user-reservations.html | 30 | nav link (this page's nav sits two lines lower than the rest) |
| user-topology.html | 23 | nav link |
| glossary.html | 32, 109 | nav link; "Release / version" entry: badge `v0.7.0`, "This manual documents 0.7.0", link to the new page |
| index.html | 53, 185 to 188 | nav link; the release card in "When you're stuck" (href line 185, h3 line 187, dek line 188) retitled Release 0.7.0 with a fresh one-line dek (accounts have profiles, list pages have a filter panel, services check their token settings at startup) and linking the new page |
| release-0-6-0.html | 31, 49, 61, 148, 157 to 158 | demote to a historical record exactly the way `release-0-5-0.html` was demoted for 0.6.0 |

The `admin-setup.html` About mockup (lines 285 to 310) is a fixture, not a
description: set the wordmark version (line 287), the Frontend card Version and
Build (lines 293 and 294), and the ACL, Auth, and Config rows (lines 303, 305,
307) to `0.7.0` and `v0.7.0`. Set the build date in lines 295, 303, 305, and 307
to `2026-11-08 18:20 UTC`, an illustrative value the page invents, as the
existing `2026-10-01 14:05 UTC` was. Line 304, the amber AI Orchestrator row
that demonstrates a "differs" badge, currently reads `0.5.0` and `v0.5.0`;
change both to `0.6.0` and `v0.6.0` so the row still shows exactly one release
of skew and the page does not pretend a 0.5.0 service sits beside a 0.7.0
frontend. Line 321 (the legend's skew examples) uses `0.6.0.dev0`, `0.6.0-dev`,
`0.6.0rc1`, `0.6.0-rc.1`, and `0.6.0-rc.2`; change each to the `0.7.0` form.

Demoting `release-0-6-0.html`, modeled on `release-0-5-0.html` lines 31, 49 to
50, 61, 141, and 150 to 151:

- Nav (line 31): the link becomes "Release 0.7.0" pointing at
  `release-0-7-0.html` with `class="active"` (the historical pages keep the
  active class on the current release's nav entry).
- Sidebar shared-reference list (line 49): the active entry in the sidebar is
  the page's own, so 0.6.0 keeps `class="active"` there and a plain "Release
  0.7.0" link is added above it, as `release-0-5-0.html` lines 49 to 50 do.
- Dek (line 61): the page opens with "The version this manual documents." Replace
  that opening with "The sixth tagged release of HERD, kept as the record of
  what 0.6.0 shipped." (match the wording of `release-0-5-0.html` line 61) and
  end the dek with "The current release is 0.7.0, which this manual now
  documents." with 0.7.0 linked.
- `#manual` paragraph (line 148): "now describes 0.7.0; this page is kept as the
  record of 0.6.0", name the 0.5.0 page as the record of the release before it,
  and add the sentence about deployments still on 0.6.0, matching
  `release-0-5-0.html` line 141.
- Footer pagenav (lines 157 to 158): keep the back link to 0.5.0 and replace the
  Home link with a forward link "Release 0.7.0" to `release-0-7-0.html`,
  matching `release-0-5-0.html` lines 150 to 151. Leave the boundary wording in
  the 0.6.0 page body alone: it is the record of 0.6.0.

## C. Content that is missing

Nine fills. For each: the page, the place, what to write, and the facts with
their source. Where a fill needs a mockup, update the existing figure rather
than adding a second one, except C1, which has no figure today.

C1. `user-topology.html`, `#open` (line 64): the Topologies list page. The
manual never describes the page you reach from the **Topology** nav tab; line 66
says "pick a saved topology or hit **New topology**" and the existing figure
(lines 68 to 122) is the editor. Insert a new paragraph and a small figure
between line 66 and the existing figure, with a short legend, and change line
66's "New topology" to the real button text **New Topology**
(`frontend/src/pages/TopologyPage.tsx`). Facts from `TopologyPage.tsx`,
`lib/topologyFilters.ts`, `lib/topologyBulk.ts`, and `lib/errors.ts`:

- Layout: the heading **Topologies** with the matching total beside it, the
  **New Topology** button and the import and export control at the right, then a
  left **Filters** panel (a labelled region) and the table beside it; on a
  narrow window the panel stacks above the table (`ListFilterPanel.tsx`, lines 12 to 18). The panel holds a **Search topologies** box (placeholder "Search by
  name...", matches anywhere in the name, ignoring case), an **Owner** select
  (All, Mine), and a **Clear filters** button that appears only while a filter
  or search text is active (`TopologyPage.tsx` lines 408 to 421).
- Columns, left to right (lines 478 to 521): a select-all checkbox (accessible
  name "Select all topologies on this page"), Name, Owner, Created, Updated,
  and an unlabeled actions column holding **Clone** and, where you may delete,
  **Delete**. Name, Owner, Created, and Updated sort. The first click sorts
  ascending, the second descending, the third returns to the default order,
  most recently updated first (`topologyFilters.ts` lines 19 to 22 and 77 to 85).
  Changing the sort returns to page one.
- Search text, the Owner choice, and the sort are saved per user and restored on
  the next visit; a saved value that is no longer valid falls back to the
  default (`topologyFilters.ts` lines 1 to 8; `docs/USER_GUIDE.md` lines 174 to
  179). An empty result reads "No topologies match these filters." when a filter
  is on, else "No topologies yet. Create one to get started." (lines 464 to
  468).
- Multi-select delete (lines 423 to 450): tick rows; a bar above the table shows
  "N selected", **Delete selected**, and **Clear selection**. You can delete a
  topology you created; an admin or superadmin can delete any
  (`topologyBulk.ts` lines 14 to 25). The confirmation is titled **Delete
  Topologies**; its text counts what will run and what is skipped, "Delete 2 of
  the 3 selected topologies? This permanently deletes them and their canvas data
  and cannot be undone. 1 will be skipped: not yours."; the confirm button reads
  "Delete 2 topologies" and the dismiss button "Keep topologies" (`topologyBulk.ts` lines 59 to 80). When no ticked row qualifies, **Delete selected** is
  disabled and a note says "None of the selected topologies can be deleted: 2 not
  yours." (`topologyBulk.ts` lines 82 to 84). The row's own **Delete** opens a dialog titled "Delete
  topology?" ("This will permanently delete this topology and its canvas data.",
  `TopologyPage.tsx` lines 620 to 628).
- Result (`topologyBulk.ts` lines 87 to 92): a success toast "Deleted 3 topologies"; with
  failures an error toast "Deleted 2, failed 1" plus ": <reason>" when every
  failure has the same reason. A row the server refused stays ticked, with "Not
  deleted: <reason>" under its name (`TopologyPage.tsx` line 125). The selection
  is dropped when the page, search, owner filter, or sort changes (lines 258
  to 280).
- A topology a PENDING, PENDING_PROVISION, or ACTIVE reservation references
  cannot be deleted: the server answers 409 `topology_in_use` and the toast or
  row note reads "In use by 2 reservations. Cancel them or wait for them to end,
  then delete again." (`lib/errors.ts` lines 183 to 215; changelog, issue #977;
  `docs/ROLES.md` line 1156). COMPLETED, CANCELLED, and FAILED reservations do not
  block. Link `user-reservations.html#list`.

C2. `user-reservations.html`, `#list` (lines 67 to 127): the filter panel.
Bring the list mockup (lines 72 to 112) to the 0.7.0 layout and add the filter
facts to the legend. Facts from `ReservationsPage.tsx` lines 444 to 520,
`lib/reservationFilters.ts`, `docs/USER_GUIDE.md` line 66, and the changelog
entry for issue #959:

- The page now has a left **Filters** panel beside the table, with: a **Search
  reservations** box (placeholder "Purpose or reservation ID...", matches the
  purpose text ignoring case; a term of 8 or more hex digits also matches a
  reservation ID prefix); **Status** (All, Pending, Pending provision, Active,
  Completed, Cancelled, Failed, sentence case, not the badge text); **Category**
  (All, Unclassified, then each configured purpose category); **Period** (All,
  Upcoming, Current, Past: Upcoming starts now or later, Current has started and
  not ended, Past has ended, by time only, whatever the status); and, for admins
  only, the **All reservations** checkbox at the bottom of the panel. A **Clear
  filters** button shows only while something is active.
- Filters combine with each other, with the sort, and with All reservations. Any
  filter change returns to page one and clears the ticked rows. The search and
  filters are saved per user and restored on the next visit; a saved status,
  category, or period that is no longer valid falls back to All and is never
  sent. When nothing matches the list says "No reservations match the current
  filters." with a Clear filters link (`ReservationsPage.tsx` lines 574 to 586).
- The tab strip above keeps **My Reservations** or **All Reservations** (the
  label follows the checkbox), **Calendar**, the count, and **New Reservation**
  (lines 446 to 468); the checkbox is no longer in that strip (see D1).
- Add a legend item for the panel and one for the saved choices; update the
  caption (line 113) so it mentions the filters.

C3. `user-notifications.html`, `#settings` (lines 136 to 184) and `#profile`
(lines 191 to 197): the Profile section of Settings. The real Settings page has
two cards, **Profile** above **Notifications** (`SettingsPage.tsx` lines 49 to 82
and 130 to 137); the manual's mockup (lines 143 to 178) shows only
Notifications. Add a Profile card above the Notifications card in the figure,
and one legend item. Facts from `components/profile/ProfileForm.tsx` and
`docs/USER_GUIDE.md` lines 229 to 237:

- The card shows your **Sign-in email** and **Username** read-only (they are
  your login identity), then three fields: **Name** (required; "Name is
  required." appears under it when blank), **Contact email** (optional, hint
  "Where email notifications go. Leave blank to use the sign-in email."), and
  **Phone** (optional, hint "Stored and shown only. HERD does not call or text
  this number."), then a **Save profile** button, disabled until a field
  changed. A save shows the toast "Profile saved" or the server's reason.
- Name is 1 to 200 characters and need not be unique. Phone allows digits,
  spaces, `+`, `-`, `(`, `)`, at least one digit, up to 32 characters. A blank
  optional field clears it. LDAP accounts edit the same three fields; nothing is
  read from the directory for them (changelog, issue #1252).
- Email notifications go to the contact email when one is set, else the sign-in
  email, within about 30 seconds (the contact cache).

C4. `admin-setup.html`: a short unnumbered subsection `#users` after the
user-groups paragraph at line 197 and before Step 5 (line 199), with an
unnumbered sidebar TOC entry "Users and profiles" after entry 4 (line 36)
linking `#users`. The manual never mentions the Users page. Facts from
`pages/admin/UsersPage.tsx`, `components/admin/UserManagementTable.tsx`, and
`components/admin/EditProfileDialog.tsx`:

- Open **Administration**, then **Users** (`AppLayout.tsx` lines 202 to 214); the
  page is headed **User Management**. The table columns are Name, Username,
  Email, Role, Created, Profile, and, for a superadmin only, Action. Name shows
  the display name, else the username.
- **Edit profile** on a row opens a centred dialog titled **Edit profile** with
  the same form as Settings (Name, Contact email, Phone, **Save profile**,
  **Cancel**). An admin or superadmin edits any account's profile, active or
  not. A save toasts "Profile saved" and closes the dialog. The sign-in email
  and username are shown read-only; no screen changes them.
- A user who names any account but their own through the API gets 404 `User not
  found`, the same answer as an unknown id (changelog, issue #1252;
  `docs/ROLES.md` line 1970).
- The Action column's **Promote to admin** and **Demote to user** buttons
  already match the roles table at lines 88 to 100; do not change that table.

C5. `quickstart.html`: the Name field and the name in the header.

- `#register` (lines 117 to 119): the Register form has Email, Username, **Name**
  ("Shown to other users. Defaults to your username."; prefilled from the
  username until you type there), and Password (`RegisterPage.tsx` lines 49 to
  93). Add one sentence to line 119 naming the Name field and its default.
- `#nav` legend (lines 188 to 196) and mockup (lines 165 to 182): the header's
  right-hand group ends with **Settings**, your display name (your username shows
  when you hover it), and **Logout** (`AppLayout.tsx` lines 247 to 271). Add a
  legend item 8 and a matching `data-anno` pin on the `<span class="u">` (line
  179), which carries no pin today.

C6. `admin-setup.html`, `#dynamic` (lines 210 to 278): two hypervisor settings.
The Register Hypervisor mockup (lines 215 to 232) lacks the **Bookable by device
group** select. Add it between **Secret** and **Enabled**, and add legend
items. Facts from `pages/admin/HypervisorsPage.tsx` lines 258 to 295 and
340 to 372, `docs/ADMIN_HANDBOOK.md` lines 84 and 85, and the changelog entries
for issues #1053 and #1033:

- **Bookable by device group**: options are **Admins only** (the default) and
  each device group. Hint: "Users whose user groups have a permission on this
  device group can see and book the dynamic templates that use this hypervisor.
  Admins can always book them." A hidden template answers exactly like an unknown
  one. Every hypervisor that existed before 0.7.0 starts admin-only until an
  admin assigns a group. The Hypervisors table gains a **Bookable by** column.
- **Enabled**: while unticked, a booking of a template on that hypervisor is
  refused with a 422 naming it, and execution creates no instance on it for a
  booking taken earlier. The table's **Enabled** column reads Yes or No.
- Fix the shotcap (line 233) to list the two columns that now exist, and fix the
  step-6 prose at line 211 only if it contradicts this (it does not today).

C7. `troubleshooting.html`: three admin and two login accordions, in the
existing `details.accordion` markup. Sources: `docs/TROUBLESHOOTING.md` lines
39 to 75, `FRESH_SETUP.md` lines 57 to 90, and the changelog entries for issues
#1186, #1240, #1256, #1239, and #1126.

- `#admin` (after the "Can't delete a driver" accordion, lines 161 to 162): "A service
  exits at startup naming `AUTH_SECRET_KEY` or `AUTH_ALGORITHM`". Cause: the
  key is unset, blank, still the `.env.example` placeholder, or not usable as an
  HMAC secret; `AUTH_ALGORITHM` accepts only HS256, HS384, or HS512. Fix: put a
  random value (`openssl rand -hex 32`) in `.env`, or in the config UI if a
  configuration was saved there, then `make up`. A value under 32 bytes starts
  with a warning.
- `#admin`: "`make up` refuses to start, or `https://localhost` is refused". Two
  causes: another compose project holds host port 80 or 443 (the message names
  it; use that stack or run `make gate-down`, which purges its database), or
  `HERD_GATEWAY_BIND` in `.env` publishes the gateway on one address only, so
  `localhost` is refused by design and the address to browse is the one `make
  gateway-url` prints.
- `#admin`: "Traefik fails to restart with a `not a directory` error after an
  upgrade". Cause and fix from the changelog upgrade note: recreate Traefik with
  `make up` or `docker compose up -d` before restarting it; if the error already
  happened, `sudo rmdir infra/traefik/traefik.yml`.
- `#login` (after line 62): "The browser warns about the certificate". Expected
  on an install that serves HERD with the self-signed certificate `make certs`
  generates, or at an address the certificate does not name; an admin trusts
  `server-chain.crt` on each client or installs a CA-signed chain
  (`FRESH_SETUP.md` step 3). Do not tell users to change anything.
- `#login`, extend the "not configured" accordion at line 56 with one sentence:
  the config page answers `Too many failed login attempts; try again in N
  seconds` after repeated wrong passwords and keeps Sign in disabled for that
  wait; a wrong password alone still reads `Invalid config password` (changelog,
  issue #1126; `lib/errors.ts` lines 398 to 410).

C8. `admin-equipment.html`, after the "Deleting a device" paragraph (line 151):
one sentence on ports. An admin deletes a port from the device page's Ports
section (**Delete**, confirm dialog titled **Delete Port**); a port a cable
still names is refused with "Port is still cabled (2 connections) and cannot be
deleted. Remove its cables first." and the API answers 409 `port_cabled`, or 503
when cabling cannot answer (`components/devices/PortsSection.tsx` lines 143 to
152 and 320 to 325; `lib/errors.ts` lines 232 to 245; changelog, issue #1023).
Do not describe renaming a cabled port: the API refuses it too, but the page
shows only its generic save error. Also in `#bulk` (lines 213 to 262): add to the
"Always dry-run first" step (line 219) that the dry-run report is a full
rehearsal, so it matches the report of the real run row for row (changelog,
issues #1017 and #1064; `docs/BULK_IMPORT_EXPORT.md` lines 94 to 100), and to
the paragraph at line 256 that the template export and import carry a
`hypervisor_name` column resolved by name (an unknown name rejects the row,
naming it) and that the device export leaves out dynamic-instance devices
(`docs/BULK_IMPORT_EXPORT.md` lines 20, 45, and 189 to 196).

C9. `user-notifications.html` line 200, the saved-choices callout: it says only
Inventory filters and page size are saved. Add that the Topologies and
Reservations searches, filters, and sorts are saved too (C1, C2), as one
sentence. Also add one clause to line 194's profile bullet pointing at C3.

## D. Corrections to existing text

D1. `user-reservations.html`, the All reservations checkbox. It is drawn in the
tab strip (line 82, `data-anno="7"`) and described as "A checkbox beside the tabs
and the count" (line 123), and line 118 points at "item 7". In 0.7.0 the
checkbox sits at the bottom of the left Filters panel (`ReservationsPage.tsx`
lines 507 to 518; changelog, issue #959: "with the admin 'All reservations'
toggle moved into the panel"). Move it in the mockup, rewrite item 7 to say
where it is, and fix line 118's cross-reference if the numbering changes when C2
adds legend items. The behaviour (switch to everyone's bookings, return to page
one, the Owner column) is unchanged.

D2. `admin-equipment.html`, the Inventory mockup and its legend. Lines 113 to
119 draw the filter controls as a horizontal row above the table, and legend
item 4 (line 145) is titled "The filter row". Since 0.7.0 they are in the left
**Filters** panel (`InventoryPage.tsx` lines 484 to 520), labelled **Search
devices** (placeholder still "Search devices by name..."), then **Status**,
**Template**, **Topology**, then **Clear filters** as a full-width button at the
bottom while active; on a narrow window the panel stacks above the table. The
selects, options, persistence, and empty text are unchanged (changelog, issue
#957: "no behavior change"), so keep the rest of item 4 as it is. Redraw the
mockup as panel plus table, retitle item 4 "The Filters panel", and update the
shotcap (line 138) from "under the filter row".

D3. `user-notifications.html`, line 195: "Changing your password: use the profile
page if your version exposes it." HERD has no password-change screen or route;
`docs/USER_GUIDE.md` line 239 says so and the changelog (issue #1252, Docs)
records that guide's own correction. Rewrite the bullet: HERD has no password
change; LDAP users change theirs in the directory; local accounts ask an admin.
The next bullet (line 196, no self-serve reset) stays. Line 194 ("email,
username, role, and group memberships") becomes: sign-in email, username, name,
optional contact email and phone, role, and group memberships.

D4. `user-notifications.html`, the event label. The mockup row (line 168) and
the legend text (line 182) say "Device health changed"; the real label is
**Device health changes** (`SettingsPage.tsx` line 20). The label predates 0.7.0
(commit 12299911). Correct both.

D5. `admin-health-config.html`, lines 188 to 190 and 193. The text says a
schedule "fire[s] automatically when a linked reservation goes ACTIVE" and "it
runs when that reservation activates". That is not what the code does: a
`reservation_id` is accepted at schedule time only for a reservation that is
ACTIVE now, holds the device, and, for a non-admin, belongs to the caller; any
other id is a 422, and an unanswerable check is a 503 "Could not verify the
reservation; nothing was scheduled. Retry the request." The job then fires at its
`scheduled_for` time and only while that reservation is still active
(`services/inventory/app/routers/apply_jobs.py` lines 5 to 6 and 73 to 106;
changelog, issue #1104). Rewrite line 188 and the "Schedule against a
reservation" bullet (line 190) to say that. The Schedule form on the device page
has only a **Schedule for (optional)** time and no reservation picker
(`components/device-config/DeviceConfigSection.tsx` lines 231 to 300), so say the
reservation link is an API and assistant feature. Line 193 (authority re-checked
at fire time) is correct; keep it. Add one sentence: the immediate **Apply now**
needs an explicit `manage` grant for a non-admin; a reservation owner without a
grant schedules the apply instead (changelog, issue #1092), which matches
`user-ai.html` line 185.

D6. `admin-health-config.html`, line 196 (the `driver_cannot_configure`
paragraph). Add that the device page now shows the refusal in words: Apply now
and Schedule show the 409's sentence and the driver's name, a refused restore
lists up to three blocking reservations, and other plain-text refusals (403,
422, 503) show as given, instead of the old generic `Apply request failed` and
`Restore failed` (`DeviceConfigSection.tsx`, `lib/errors.ts`; changelog, issue
#1098). Keep the existing sentences.

D7. `quickstart.html` line 100, the certificate sentence. It says the browser
"may warn about the certificate on a local install, that's expected". Keep it
true and add a pointer: the warning stays until an admin trusts the generated
certificate on your machine, and the troubleshooting accordion from C7 explains
it (link `troubleshooting.html#login`). Do not describe `make certs` here; this
is the user quick start.

D8. `glossary.html` line 109 and `index.html` lines 185 to 188: covered in Part
B (release entry and card). Listed here so the reviewer sees them as
corrections of text that is now stale, not only as nav edits.

## E. Verified as already correct, and changes a manual reader cannot see

E1. Verified in the current manual; do not re-do (page and lines checked):

- `user-live-editing.html` lines 196 and 197: the fork save names the lines it
  could not wire and a draft-check line turns red with the reason on hover;
  consistent with changelog issues #1007 and #1066.
- `user-notifications.html` lines 108, 167, 177, 182: the **Reservation failed**
  event and toggle already match issue #1077 (commit 77ce2dc8). Only the label
  spelling in D4 is wrong.
- `troubleshooting.html` lines 164 to 165 ("Can't delete a device") and
  `admin-equipment.html` line 103 (an expanded row stays open when the list
  changes) are unchanged by this release; #957 kept the same row-expansion and
  selection rules.
- `user-ai.html` line 185 ("Needs admin, or a manage grant on each device"):
  consistent with issue #1092.
- `user-assistant.html` lines 236 to 247: the errored-turn accordion describes
  the streaming route the web page uses. Issue #1280 changed only the buffered
  `POST /reservations/{id}/assistant` route, which the UI does not call.
- `admin-health-config.html` lines 196 and 197 (gating and sanitized driver
  errors): still true; only D6 adds to them.
- No manual page states where a toast appears or where a dialog opens. A grep of
  the 16 ordinary pages and the 0.6.0 page for "top right", "top-right",
  "top-left", "bottom right", "corner", "centred", and "centered" finds only the
  bell heading (`user-notifications.html` line 55, "Top-right corner", which is
  the header bell and is still right), the editor palette "in the corner"
  (`user-topology.html` lines 66 and 135), and the editor's view-only mini-map
  "in the bottom-right" (`user-topology.html` line 457). A separate grep for
  "toast" finds only result messages, none about placement. The dialog and
  toast changes (issues #979, #942) need no edit beyond the release page.
- The mockups' own modals (`hd-modal-bg`) are the manual's CSS, not the app's;
  the centred-dialog fix does not touch them.
- `quickstart.html` lines 78 to 88: the wrench and the "not configured" banner
  are unchanged.
- The Settings scroll fix (issue #1260) and the `/config` and `/templates/new`
  bottom padding (issue #988) change nothing a page describes.

E2. Not visible to a manual reader: no manual page change, mention on the
release page at most (issue numbers are the changelog's). The rest of the 177
Delivery detail entries fall in these groups, none needing a manual edit:

| Issue or group | Change | Where it shows on the release page |
|---|---|---|
| #1186 | PyJWT through `herd_common.jwt_codec`, 10 second leeway | "Also": developer line |
| #1180, #1279, #1192 and the tgz, driver-metadata, driver-parse entries | sandbox isolated mode; bounded tar and metadata reads; `driver.py` parsed, never imported | "Also": hardened line |
| #964 | e2e Selenium ports on 127.0.0.1 | "Also": hardened line |
| #1134 to #1148, #1158, #1169, #1270 | test-effectiveness sweep, 31 test-change entries | "Also": developer line; quality bar |
| #1203, #1215, #1190, #1204, #937, #1152, #1003 | execution and wiring recovery, VLAN uniqueness | "Also": wiring line |
| #994 to #1000 | reservation edit rules, `provision_started_at`, calendar 503 | card 5 |
| #1078 | webhook pause and resume | "Also" |
| #1017, #1064, #1024, #1068 | import rehearsals, template `hypervisor_name`, device export | "Also"; C8 |
| #1053, #1033 | hypervisor device group and Enabled | "Also"; C6 |
| #977, #1023 | topology and port delete refusals | "Also"; C1; C8 |
| #1261 | auth boots before `make migrate` | "Also" |
| #1280 | buffered assistant route 502 | "Also" |
| #1126 and the config-service entries | config login limit, `PUT /settings` allowlist, CORS without credentials | "Also": hardened line; C7 |
| #1104, #1092, #1098 and the config-apply entries | schedule and apply authorization, refusal wording | D5, D6 |
| #1268, #1263, #1100 | NOS lab image and healthcheck, device-config e2e and load | not mentioned |
| docs entries (18) and spec entries | `docs/specs/` guides and as-built corrections | "Also": developer line |
| #1256, #1240, #1239 | `HERD_GATEWAY_BIND`, Traefik discovery, `make certs` | card 4; C7 |

Coverage ledger: the changelog's own summary counts the 177 entries as 115 fixes,
31 test changes, 18 documentation changes, 4 behavior changes, 4 additions, 1
security change, 3 list-page features, and the upgrade note. The user-visible
ones are in Parts A, C, and D above; the 31 test changes and 18 documentation
changes need no manual page; the remaining fixes are in the E2 table by group.
If a reviewer finds a Delivery detail entry that changes text on a screen and is
in none of these places, add it to the PR body and fix it; do not drop it.

## F. Definition of done

- `grep -l "release-0-6-0" docs/manual/*.html` lists only `release-0-7-0.html`
  (its backlink), `release-0-6-0.html` itself, and `release-0-5-0.html`'s
  historical forward link; every other page links `release-0-7-0.html`.
- `grep -n "0\.6\.0" docs/manual/*.html` outside `release-0-*.html` finds
  nothing stale (the glossary entry, the index card, the quickstart version
  mockups, and the About mockup now say 0.7.0, except the deliberate `0.6.0`
  skew row at `admin-setup.html` line 304).
- A byte scan of every touched file finds no em-dash, en-dash, box-drawing
  character, bullet glyph, arrow character, or emoji. HTML entities that the
  existing pages already use for dashes and arrows stay as they are in lines
  you do not change.
- Every `href` in `docs/manual/` resolves (a link checker over the directory),
  including the new anchor `#users` in `admin-setup.html` and the card anchors
  the new release page uses.
- Visual QA in a browser: the new page, the demoted 0.6.0 page, `index.html`,
  and one repointed page each render with the nav's active state correct; the
  updated Reservations list mockup, the Inventory mockup, the new Topologies
  figure, and the Settings mockup render without horizontal overflow at phone
  width (the filter panel stacks above the table there).
- Every UI string in Parts C and D is checked once against the running stack or
  the source line cited; any mismatch is reported in the PR body.
- PR body lists every page touched, cites the brief part for each change, and
  states which Part E items were verified unchanged.
