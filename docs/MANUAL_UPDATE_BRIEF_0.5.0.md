# Brief for Claude Design: bring the HERD manual up to v0.5.0

Repo: vendrabuck/HERD, branch main at ab2f3d70 (0.6.0.dev0). The manual is the
HTML under `docs/manual/` (20 pages plus `assets/`), published at
<https://vendrabuck.github.io/HERD/manual/>. Its last content edit was
d99b079f (2026-09-14, the seven-valued retry vocabulary). Since then v0.5.0
was released: tag `v0.5.0` at commit a41f11e1, release date 2026-09-15, GitHub
Release <https://github.com/vendrabuck/HERD/releases/tag/v0.5.0>. The manual
still presents 0.4.0 as the current release everywhere.

Scope: `docs/manual/**` only. Do not touch code, tests, CHANGELOG.md, or other
docs. Deliver as one PR on a branch named `docs/manual-0-5-0`. Model the whole
change on commit 1037af21 (PR for 0.4.0, "docs(manual): update manual to
0.4.0"), which did the same job: one new release page plus the nav repoint
across every page.

Unlike 0.4.0, most 0.5.0 feature content is ALREADY in the manual, because it
landed with each feature: the Routing intent section of `user-topology.html`
(lines 320 to 366), the assistant's Search docs and Read doc rows in
`user-assistant.html` (lines 155 to 171), and the retry outcomes paragraph in
`user-live-editing.html` (line 302). Part E lists what to verify and leave
alone. The work is Part A (new page), Part B (repoint), and three fills in
Part C.

Sources of truth, in priority order: the running UI (a seeded 0.5.0 stack is
at https://localhost), `frontend/src/` for exact labels, `CHANGELOG.md` section
`## [0.5.0] - 2026-09-15` (eight summary bullets, then `### Delivery detail`
with seven `#### <area>` subsections), `FEATURES.md`, `docs/USER_GUIDE.md`,
`docs/TOPOLOGY_EDITOR.md`, `docs/AI_ASSISTANT.md`,
`docs/AI_PURPOSE_CLASSIFICATION.md`, `docs/DRIVERS.md`, `docs/NOS_LAB.md`.

## Working rules (standing, from the project's CLAUDE.md and Lane's git policy)

- Branch from main, one branch `docs/manual-0-5-0`, one PR, opened as a
  normal PR against main. Do not merge it: Lane or his reviewing session
  merges on green. Do not rebase or force-push once it is open; add commits.
- Commit only files under `docs/manual/`. Never stage `.claude/`, `CLAUDE.md`,
  `.env`, `spark-ca.pem`, `frontend/node_modules`, or anything the local
  `.gitignore` hides. Stage by file name, never `git add -A`.
- Commit messages and the PR body carry no `Co-Authored-By` trailer and no
  Claude or Anthropic attribution of any kind. Commits are authored under
  Lane's git identity from the local config.
- Write the PR body to `docs/PR_BODY.md` (it is gitignored, local only) and
  open the PR with `gh pr create --body-file docs/PR_BODY.md`; never commit
  that file. Do not run `gh auth` in any form; on an auth error stop and
  report.
- Prose rules for every sentence you write into the manual or the PR: plain
  declarative sentences built from concrete facts; no em-dashes; no filler
  enthusiasm ("excited", "leverage", "delve", "seamless"); no tidy
  three-item parallel constructions for their own sake; no bullet glyphs,
  arrows, box-drawing, or emojis anywhere in a tracked file. Existing page
  voice wins over your own.
- Every claim about the UI must be checked against the running stack at
  https://localhost or the source under `frontend/src/`; do not describe a
  control you have not seen. If something in this brief contradicts the
  running UI, follow the UI and say so in the PR body.
- Do not touch the CHANGELOG, FEATURES.md, or any doc outside `docs/manual/`
  even if you find an error there; list it in the PR body instead.
- Do not spawn subagents for the work; do it in one pass so there is one
  writer in the checkout.

## Conventions you must keep

- Manual "screenshots" are hand-built inline HTML/CSS mockups
  (`<figure class="shot">` with the `hd-*` classes in `assets/app.css`), never
  images. New visuals follow that pattern.
- Every page shares the same top nav strip and (on release pages) a sidebar
  TOC. The nav's release link is repeated on all 18 non-legacy files.
- Style rules for every file: no em-dashes (use colon, semicolon, comma), no
  box-drawing characters, no bullet glyphs or arrows in text (use `-`, `,`,
  the word `to`), no emojis. Password placeholders are literally `password`.
- Voice: second person, plain, task-first, same as the existing pages.
- Links to the repo use `https://github.com/vendrabuck/HERD/...`; relative
  links stay relative. Every link must resolve.

## A. New page: `release-0-5-0.html`

Copy the structure of `release-0-4-0.html` exactly (157 lines): the `.relmeta`
style block, sidebar TOC (`#what`, `#highlights`, `#quality`, `#boundaries`,
`#manual`), dek paragraph, relmeta strip, `#what`, "What shipped" card grid,
"Also in this release" list, architecture continuity paragraph, "Quality bar"
list, "Known boundaries" list, "Reading the manual against this release"
section, footer pagenav.

Relmeta strip values:

- Version: v0.5.0
- Released: 2026-09-15
- Tagged commit: a41f11e1

"What shipped" cards (link each to the manual page named):

1. Layer 3 routing intent (ADR 0014): declare routes on a Layer 3 switch node
   in the topology editor, import them from the switch's saved config, and
   HERD validates them against the switch when you save and drives them when
   the reservation activates, ahead of any routes in the device's config
   version. Routes can name a virtual router (VRF) when the driver supports
   it, and a route's interface must sit on a wired port unless the switch's
   config marks it logical. Link the Routing intent section of
   `user-topology.html` (the `<h2 class="sec">` that precedes line 320; it
   carries the section's id) and `user-live-editing.html#fork` for the
   "Routing changed on <device>" line in the version diff (line 112).
2. Drivers for real network operating systems: HERD now ships two driver
   packages an admin can upload as they are, `frr_l3` (FRRouting as a Layer 3
   switch) and `srl_l2` (Nokia SR Linux as a Layer 2 switch), both proven
   against a checked-in lab of the real operating systems on every change.
   A driver must now report a device's rejection of a command as a failure,
   so a refused change can never show as applied. Link
   `admin-setup.html#drivers` (or whatever the driver-upload anchor is named).
3. The assistant can read the manual: two new read-only tools, Search docs and
   Read doc, let the reservation assistant consult this manual, any document
   folders the operator adds, and allowlisted web pages, and cite what it
   found instead of guessing. Link `user-assistant.html#sees` (the section
   holding the tools table).
4. Wiring retry cannot double-drive: a manual Retry and HERD's own background
   retry now claim a failed connection before touching the device, so a
   switch never receives the same command twice, and a stale retry can never
   resurrect a connection you released. The Retry toast gains a seventh
   outcome, "already retrying", for a row the background channel already had
   in hand. Link `user-live-editing.html#wiring` (the retry paragraph at line 302
   sits under it).
5. Classify one reservation now: an admin can ask for a purpose suggestion for
   a single ended reservation without waiting for the background sweep or
   running the whole Classify history backfill. API only in this release
   (see Part C1 for the exact wording). Link `admin-purpose-review.html`.
6. Commit waits for the fork to load: in live editing the Commit to
   reservation button is disabled until the reservation's fork canvas has
   loaded, so a fast click can no longer save an empty version and fail the
   device-set update. Link `user-live-editing.html#commit`.

"Also in this release" list:

- Non-admins see only their own devices when a topology is validated or a
  path is found: a device outside your groups reports as missing, and a
  hidden switch on a path shows as a hidden hop.
- A driver that declares `supports_vrf` receives the virtual router on route
  changes; a route naming a VRF on any other driver is refused and shown in
  the Wiring tab, never silently dropped.
- Seeding a stack with demo data moved to the `seedtools` command
  (`python -m seedtools full`); the old script and shell wrappers are gone.
  Admin and developer facing; one sentence.
- Nightly test failures now keep a screenshot, page, console log, and
  traceback per failed browser test.
- Fixed: a wiring retry that finished after you released the connection no
  longer re-creates it as failed.

Quality bar (measured at the tagged commit by the full `make everything` run;
fill every figure from these numbers, do not reuse 0.4.0's):

- 5,443 backend unit tests across the 13 suites plus the repo-root suite;
  backend line coverage 96.8% with every service at 95% or higher.
- 1,378 frontend tests via vitest, 90.4% line coverage.
- 12 OpenAPI contract snapshot suites.
- 221 cross-service integration tests (211 run, the rest gated on LDAP mode
  or an AI provider).
- 164 end-to-end browser tests, run twice: 128 on the unseeded stack (36
  device-gated skips) and 160 again on the seeded stack with no unexpected
  skip allowed (4 exempt).
- 41 live-LDAP tests against the checked-in `infra/ldap-test` directory,
  hard-required in the gate.
- New this release: 28 driver tests against real FRRouting and Nokia SR Linux
  nodes in the checked-in lab, run on every pull request and inside the gate.
- Seven Postgres-live suites against the gate database, hard-required: the
  LDAP sync reconciler, advisory locks, the outbox wake-on-write, the fork
  restore-versus-save race, the two-session fork port-claim race, the Layer 3
  route key width, and the new two-channel wiring retry claim race.
- Load: 20 simulated users for one minute, 665 requests, 0 failures, median
  10 ms, p95 170 ms.
- Same closing sentence as 0.4.0 about the release gate and branch protection.

Known boundaries: carry forward all seven 0.4.0 bullets verbatim (anchored
VLAN provisioning is a later phase; manual wiring retry is not in the external
API; a failed VLAN removal is logged, not blocked on; AI recipe authoring is
dark by default; driver packages are trusted code; purpose classification is
off by default; config-version history answers "Device not found" outside your
groups). Add three:

- A route that names a virtual router is driven only by a driver that declares
  `supports_vrf`; on any other driver the route is refused as
  `l3_vrf_unsupported` and stays visible in the Wiring tab until the intent or
  the driver changes. The shipped `frr_l3` driver declares it.
- Classify now for a single reservation is an API call in this release; the
  Purpose review page has no button for it yet.
- The assistant's web sources are off by default (`AI_DOCS_WEB_ENABLED`) and
  limited to operator-allowlisted https prefixes; the built-in manual and any
  configured folders are on.

`#manual` section: "This manual now documents 0.5.0"; link
`release-0-4-0.html` as the record of the previous release, which in turn
links 0.3.0 and earlier.

## B. Repoint the existing pages (18 files)

- Every page's top nav: the "Release 0.4.0" link becomes "Release 0.5.0" and
  points at `release-0-5-0.html`.
- `index.html`: the release card in "When you're stuck" retitles to Release
  0.5.0 with a fresh one-line dek and links the new page.
- `glossary.html` "Release / version" entry: badge to `v0.5.0`, text to "This
  manual documents 0.5.0", link to the new page.
- `release-0-4-0.html`: demote to a historical record exactly the way
  `release-0-3-0.html` was demoted for 0.4.0: dek and the `#manual` paragraph
  say the current release is 0.5.0, nav `active` moves to the 0.5.0 entry,
  sidebar shared-reference list gains the 0.5.0 entry above it, footer pagenav
  forward-link goes to `release-0-5-0.html`.
- `release-0-3-0.html`, `release-0-2-0.html`, `release-0-1-0.html`: no change.

## C. Content that is missing

C1. `admin-purpose-review.html`, next to the "Classify history" backfill text
(lines 133 to 138): one short paragraph. An admin can classify a single ended
reservation right away with `POST
/api/reservations/admin/purpose-review/{reservation_id}/classify`. It runs
the same classifier the background sweep uses, for that one reservation, and
answers with the outcome (`ok` with the new suggestion, or `timeout`,
`transient`, `failed`, `forbidden`) as a 200; 503 when purpose classification
is off; 409 `not_eligible` while the reservation has not ended; 409
`already_suggested` when a suggestion already exists (dismiss or override it
first). It ignores the attempt cap on purpose, so it is the way to retry one
exhausted reservation without running Classify history, which retries every
exhausted row. Say plainly there is no page button yet. Source:
`docs/AI_PURPOSE_CLASSIFICATION.md` lines 162 to 217.

C2. `admin-setup.html`, driver upload section (lines 104 to 142): two or three
sentences. HERD ships two driver packages for real network operating systems
under `drivers/` in the repo, `frr_l3` (FRRouting, Layer 3 Switch) and
`srl_l2` (Nokia SR Linux, Layer 2 Switch); upload either as it is. A driver
must report a device's rejection of a command as a failure, so a refused
change is never shown as applied; a driver that supports virtual routers says
so with `supports_vrf` in its metadata. One sentence that the packages are
exercised against a lab of the real operating systems in the project's own
tests, linking `docs/NOS_LAB.md` on GitHub. Source: `docs/DRIVERS.md` line 81
and lines 118 to 254.

C3. `troubleshooting.html`, the live-editing or wiring entries: add
`l3_vrf_unsupported`: a route that names a virtual router was refused because
the switch's driver does not declare `supports_vrf`; the route stays in the
Wiring tab as failed. Fix by removing the virtual router from the route or
uploading a driver that supports it. Also add one line for the Retry toast's
"already retrying": HERD's background retry was working on that row when you
pressed Retry, so it was left alone; check the Wiring tab again in a minute.
Verify the toast text against `frontend/src/api/reservations.ts` line 500.

## D. Corrections to existing text

D1. `user-live-editing.html` line 302: the retry outcomes sentence paraphrases
the seventh outcome. Make it name the toast wording, "already retrying",
matching `docs/USER_GUIDE.md` line 107.

D2. Anywhere a page says "six outcomes" or lists six retry outcomes, make it
seven.

## E. Verified as already correct (do not re-do)

- `user-topology.html` Routing intent section (lines 320 to 366): panel,
  route-count badge, "Import from device config", validation reasons, virtual
  router and interface wiring rules. Verify the labels against
  `frontend/src/components/topology-editor/RoutingPanel.tsx` (line 292 for the
  import button) and leave the prose.
- `user-assistant.html` tools table (lines 155 to 171): Search docs and Read
  doc rows, "nine read-only tools", on by default.
- `glossary.html` line 67 "Routing intent" entry.
- `user-live-editing.html` line 112, the "Routing changed on <device>" diff
  line.

## F. Definition of done

- `grep -l "release-0-4-0" docs/manual/*.html` lists only `release-0-5-0.html`
  (its backlink), `release-0-4-0.html` itself, and `release-0-3-0.html`'s
  historical link; every other page links `release-0-5-0.html`.
- A byte scan of every touched file finds no em-dash, box-drawing character,
  bullet glyph, arrow, or emoji.
- Every `href` in `docs/manual/` resolves (a link checker over the directory).
- Visual QA in a browser: the new page, the demoted 0.4.0 page, `index.html`,
  and one repointed page each render with the nav's active state correct.
- PR body lists every page touched, cites the brief part for each change, and
  states which Part E items were verified unchanged.
