# Changelog

## [Unreleased]

- Auth: the single sign-on routes are present and dark (#37, ADR 0016 phase 3). `services/auth/app/routers/oidc.py` adds `GET /oidc/login` (302 to the identity provider, setting the `herd_oidc_flow` binding cookie), `GET /oidc/callback` (always a 302: `/login/sso` with the one-time `herd_oidc_handoff` cookie on success, else `/login?sso_error=` `cancelled`, `unavailable`, or `failed` per the ADR's outcome table, failing the claimed flow row), and `POST /oidc/exchange` (the handoff cookie and an empty JSON body for the `POST /login` token body plus `next`, once; 401 `Single sign-on session expired; sign in again` otherwise, also for any unexpected exception after the body is validated). An unexpected exception in any of the three is logged by class name only, never with a traceback or its message. Outside `AUTH_METHOD=oidc`, which `AUTH_METHOD` still refuses, each answers 404 `Single sign-on is not enabled` before reading any input, so no deployment can reach them. Visible today: the new public `GET /login-options` answers in every mode, and its `registration_open` is false in LDAP mode, where `POST /register` already answers 409. In OIDC mode `POST /register` answers 409 and `POST /login` refuses every account unless `OIDC_BREAK_GLASS_LOCAL_SUPERADMIN` is true, which admits an active local superadmin only; every answer costs one bcrypt. A new `oidc` account's username follows the owner's option C: the username claim when it fits `^[A-Za-z0-9][A-Za-z0-9._@+-]{0,99}$`, else the email when it does, else the first login is refused as `username_invalid` (or `username_missing` when neither the claim nor the email was present, which the earlier `email_missing` refusal always preempts through the callback); provisioning also refuses any other username as `username_invalid`. `OIDC_REDIRECT_URI` is now invalid when its path holds a character outside letters, digits, `-._~/%`, since the path becomes both cookies' unquoted Path attribute and a `;` there would inject a cookie attribute. The lifespan logs the OIDC startup warnings. Tests: `services/auth/tests/test_oidc_routes.py` (every row of the callback outcome table, every exchange failure, both cookies' attributes, the D13 refusals over HTTP, the catch-alls, and log hygiene over every record), `test_login_options.py`, `test_oidc_break_glass.py`; the log-hygiene check of `test_oidc_service.py`'s `idp` fixture now reads the setup and call records, where before it read `caplog.records` in teardown, which holds only the teardown phase, so it checked nothing. ADR 0016 records option C with its refusal table, what the binding and handoff cookies do not defend against (a tossed `herd_oidc_flow` or `herd_oidc_handoff` from a sibling subdomain signs the victim in to the attacker's account), and the as-built details; the identity-and-access specification gains section 8.19 (IAM-OIDC-15 to IAM-OIDC-23); the auth contract snapshot gains the four routes and four schemas.
- Build: Dependabot no longer proposes a Node major for the frontend's build image. The `docker` block of `.github/dependabot.yml` ignores `version-update:semver-major` for `node`, beside the existing Python, Postgres, and NATS ignores, because ci.yml's frontend job runs Node 22 only and a runtime major is moved by hand. `tests/unit/test_dependabot_runtime_majors.py` reads the `docker` and `docker-compose` blocks, finds the images each one tracks in its directories, and fails when a node, python, postgres, or nats image is tracked without a semver-major ignore, or is tracked by no block; `docs/specs/DEVELOPMENT.md` lists the Node major among the hand bumps.
- Docs: accuracy sweep after the 2026-11-11 to 11-13 merges. README.md counts over 11,000 backend unit tests, over 2,400 frontend tests, 273 integration tests, and 221 e2e tests (the current CI and nightly runs), and sixteen Postgres-live suites (the OIDC flow store's joined in #1363), calls the manual searchable (#1298), and links `NOTICE` and the Code of Conduct beside `LICENSE`; docs/specs/DEVELOPMENT.md also counts sixteen Postgres-live suites. PLANNED_FEATURES.md says the OIDC design is ADR 0016 (Proposed) with phases 1 and 2 merged dark and single sign-on not yet usable; FEATURES.md mentions the manual search and says local accounts sign in by email. SECURITY.md counts five published advisories, two of them for issues fixed in v0.7.0. The manual's quickstart says a sign-in lands on the Topology page (or the page that sent you to sign in), not the Reservations page; the quickstart and troubleshooting pages no longer say the Register button is greyed out in LDAP mode (it opens the form, and the submit answers `Local registration is disabled; this deployment uses LDAP authentication.`), and the troubleshooting page's signed-out entry describes the shared session across tabs (#1316) and the return to the page after sign-in (#1333) instead of advising to clear local storage and claiming a key rotation forces every user to sign in again; the reservations page names the short id button as the keyboard way into the detail view (#1317); the search index is regenerated. docs/TROUBLESHOOTING.md no longer says the PDF reader has no memory bound (#1202), describes the shared refresh in its expired-token item, and its notifications-schema patch no longer names the `herd` role or grants to it (#1325); docs/OPERATIONS.md says a busy PDF reader answers 503, not 400; docs/USER_GUIDE.md names the **Sign in** button and the Email field's acceptance of a login name (#1335); docs/ADMIN_HANDBOOK.md's role table no longer lists changing your own password, which no route does; docs/GAPS.md no longer says `frontend/src/hooks/` does not exist or that `ErrorBoundary` has no suite of its own (#1344), and counts 71 e2e files and 221 tests.
- Tests: `tests/e2e/test_inventory_saved_search_late_load_playwright.py` waits for the preferences GET to reach its route handler instead of asserting it is already held when the Inventory filters appear. The app sends that GET only after `/auth/me` answers, and the page renders its filters without waiting, so the assertion raced the round trip and failed the 2026-11-12 nightly.
- Tests: the hostile-PDF table in `services/ai-orchestrator/tests/test_extractor_pdf.py` runs with a 20 second wall clock instead of 8 beside its 4 second CPU limit. On a busy CI runner a CPU-starved child (200k rectangles) reached the 8 second deadline before its 4 CPU seconds, failing main's CI; the CPU limit now always ends a CPU-bound read first, and a deadline still means a stuck child.
- Tests: two hostile-PDF shapes in `services/ai-orchestrator/tests/test_extractor_pdf.py` (a form drawn 10,000 times, 450k fresh names) accept either a bounded read or the CPU refusal. Both finish inside the shipped limits and sit at the test table's lowered 4 second CPU limit, so a CI runner faster than the development host ended one of them with its text and failed the test.
- Tests: the suites run on pytest 9.2, pytest-asyncio 1.4, and FastAPI 0.162 (the grouped dependency update). Every pytest configuration ignores, by its exact text, pytest-asyncio 1.4's warning that `asyncio_default_fixture_loop_scope` is unset; setting that option would move the event loop of the session-scoped async fixtures in `tests/integration`, so it stays unset until those fixtures name their loop scope. `tests/unit/test_e2e_failure_artifacts.py` passes the same filter to its inner pytest run, which has no configuration file. Two tests that listed `app.routes` (`services/integration/tests/test_webhooks_router_direct.py`, `services/inventory/tests/test_device_config_write_rules.py`) now also walk FastAPI 0.162's included-router entries, which keep the routes and their full paths behind `effective_candidates()`.
- Fixed: no uploaded PDF can make the ai-orchestrator service use memory or CPU past fixed ceilings enforced by the kernel, and every other upload type is read under a concurrency cap (#1202). Every PDF is now read in a child process (`python -I -B app/services/_pdf_child.py`) that sets `RLIMIT_AS` 512 MiB, `RLIMIT_CPU` 20 s, `RLIMIT_FSIZE` 0, `RLIMIT_NOFILE` 32, and `RLIMIT_CORE` 0 on itself before it reads the upload or imports pdfplumber, so the kernel enforces them on every code path of the parser; a limit that does not apply fails the read closed. The service gives the child an empty environment and only its three pipes, keeps at most the budget's worth of its output, kills its whole process group at 30 s and on every other way out (including a cancelled request), and reads the outcome from the exit status, the signal, and one strict status line. Refusals are fixed 400s (memory, ended by the kernel at its memory or hard CPU limit, CPU, deadline, parse failure by class name), a reader failure is a 500; no file text reaches a response or a log. At most two PDF children run at once and one per user (a second PDF read by the same user in flight is refused at once), the reads of every other type run in the service under a pool of two, and each pool lets at most eight requests wait 30 s before answering 503. The JSON and tgz reads stay in the service process, bounded by the upload cap and that pool rather than by a kernel limit (a 5 MiB JSON array can take about 32 times its size). A PDF read refused at a resource limit is charged 20,000 tokens against the user's AI quota when one is set, so five exhaust a 100,000-token day. Before, a 204 KB PDF used 460 MiB of the service and an 833-byte one could build a 3 GB dict. Honest PDFs read the same text and `truncated` flag as before, and the walk now stops at the page that fills the character budget. The JSON path stops pretty-printing at the budget (a 91.8 KB upload of nested arrays printed 82.8 million characters) and passes a document nested past the recursion limit as raw text instead of answering 500. Compose gives ai-orchestrator `mem_limit: 2048m` (two PDF children, two in-process reads at their measured worst, the uploads of the 20 requests that may hold or wait for a reader, and the service); its existing `restart: unless-stopped` brings the service back if the kernel kills it at that limit. Tests: `test_extractor_pdf.py` runs 23 hostile shapes through the real child and tests each parent protection against stand-in children, `test_extractor_json.py` covers the JSON bounds, and route tests cover the 400, 500, and 503 answers. The gate and CI stacks configure no AI provider, so `/generate` answers 503 there before it reads an upload and no stack test reaches this path; the memory limit and restart policy are not exercised by any test. `docs/specs/ai-features.md` gains AI-UPLOAD-19 to AI-UPLOAD-25.
- Chore: the empty, unreferenced `.ai/mcp/mcp.json` is deleted, and the nightly workflow sets its compose project name once (#1313). The `full-stack` job's `env:` block now sets `COMPOSE_PROJECT_NAME: herd` for every step, where before only the LDAP-mode and NOS lab feature steps passed `COMPOSE_PROJECT_NAME=herd` and `docker compose build` and `up -d` relied on the checkout directory being named `HERD`; the value is the one that derivation gave, so every step targets the same project as before, and a renamed checkout can no longer split them. The NOS and LDAP lab compose calls are unaffected (the Makefile pins their projects with `-p`). `tests/unit/test_nos_lab_ci_wiring.py` now requires the job-level value and refuses a step that sets another.
- Fixed: `infra/postgres/init.sql` no longer names the role or the database `herd`, so a first boot with another `POSTGRES_USER` or `POSTGRES_DB` initializes completely (#1325). The official postgres image runs init files through psql with `ON_ERROR_STOP=1`, as `POSTGRES_USER` connected to `POSTGRES_DB`; with any other user the first `GRANT ... TO herd` stopped the container after the schemas were created, and the restart skipped the rest of the file. The eleven GRANTs are gone (the connecting role owns the schemas it creates, so they granted nothing), and the database search path is set by a `DO` block that formats `ALTER DATABASE` with `current_database()`; for the default `herd` names the resulting schemas, owner, and search path are the same as before. The three "stub, ready for future implementation" comments now say what the acl, user_profile, and cabling schemas hold. `tests/unit/test_postgres_init.py` fails when an init file that `docker-compose.yml` mounts names a role or a database, or when `init.sql` stops creating the eleven schemas or changes the search path; it also refuses `COMMENT ON DATABASE`, `REASSIGN OWNED BY`, `DROP OWNED BY`, `FOR ROLE`, and a quoted literal passed as a `format()` argument. `tests/integration/test_postgres_init_applied.py` checks, against the running stack's Postgres, that the database-level search path exists (only the file's last statement sets it) and that the connected user owns the eleven schemas. Upgrade note: init files run only on a fresh Postgres volume, so existing stacks are unaffected and need nothing.
- Ops: container logs are bounded and the database pools fit Postgres (#1352). Every service in every compose file (`docker-compose.yml`, the override's `selenium`, and the LDAP and NOS lab files) now uses the `x-logging` anchor: the `json-file` driver with `max-size: "10m"` and `max-file: "5"`, at most 50 MB per container, where before a container's log grew without bound. `herd_common.database.make_database` now sets `pool_size`, `max_overflow`, and `pool_pre_ping` from three new `HerdBaseSettings` fields, `DB_POOL_SIZE` (default 5), `DB_MAX_OVERFLOW` (default 10), and `DB_POOL_PRE_PING` (default true), passed by every service's `app/database.py` and wired through `docker-compose.yml` for the eleven database services; the size defaults are SQLAlchemy's own, now explicit, and `pool_pre_ping` is new. SQLite URLs get only `pool_pre_ping`, because SQLAlchemy's in-memory SQLite pool refuses size arguments. The eleven services can open 11 x (5 + 10) = 165 connections, over Postgres' default `max_connections` of 100, so the compose postgres service now runs with `-c max_connections=200`, leaving 35; each extra execution replica adds 15. `tests/unit/test_compose_capacity.py` fails on a compose service without the logging block, on a database service without the three variables, when the compose defaults exceed `max_connections`, when docs/ENV_VARS.md or docs/OPERATIONS.md stops stating the arithmetic those defaults give, and when a service's `make_database` call does not pass the three settings; `services/common/tests/test_database.py` checks the engine's pool for a Postgres URL and the SQLite branch. Upgrade note: both changes apply only to recreated containers. `make up` (or `docker compose up -d`) recreates every container whose configuration changed, postgres included; a `docker compose restart` keeps the old log options and the old `max_connections`.
- Build: the image build contexts leave out local artifacts (#1301). Without a `.dockerignore`, the frontend image's `COPY . .` copied the host's `node_modules`, `dist`, `coverage`, and `.env*` files over the image's own `npm ci` install, so a local `.env.local` could be baked into the served bundle, and every service build sent the whole checkout as its context. `frontend/.dockerignore` excludes those (keeping `.env.example` and `.npmrc`, which the Dockerfile needs), and the root `.dockerignore` excludes everything except `pyproject.toml`, `uv.lock`, `services/`, and `docs/manual/`, and inside those excludes virtualenvs, caches, coverage output, `node_modules`, `dist`, env files, keys, and certificates. `tests/unit/test_dockerignore.py` checks, for every compose build, that each COPY source exists and that no tracked file under it is excluded by the ignore file Docker applies, and that the two files exclude the artifacts above; it evaluates the rules with Docker's matching semantics, without Docker.
- Build: every image HERD builds from or runs names an exact version, and Dependabot proposes the bumps (#1300). `traefik:latest` is now `traefik:v3.7.18`, the newest v3 release, which no published Traefik advisory affects (several fixed v3 advisories have their fix only in the 3.7 line); the twelve service images take uv `0.13.7` (what `ghcr.io/astral-sh/uv:latest` already resolved to, and outside every published uv advisory range) instead of `latest`, and CI's `astral-sh/setup-uv` steps pass the same `version: "0.13.7"`, so `uv lock --check` and the image-versus-lock check run the uv the images run; the frontend serves from `nginx:1.30.6-alpine` (the stable line) instead of the retired `nginx:1.25-alpine`; and `python:3.12-slim`, `node:22-alpine`, `postgres:16-alpine`, and `nats:2.10-alpine` are `3.12.16-slim`, `22.23.4-alpine`, `16.15-alpine`, and `2.10.29-alpine`. Every upstream image also carries its index digest (`name:tag@sha256:...`), the LDAP test directory's `osixia/openldap:1.5.0` included. The service Dockerfiles take the uv binary from a named stage (`FROM ghcr.io/astral-sh/uv:0.13.7@sha256:... AS uv`, then `COPY --from=uv`), because Dependabot reads only `FROM` lines. `.github/dependabot.yml` replaces the `pip` ecosystem with `uv`, which regenerates `uv.lock` in the same PR, adds `docker` (the service and frontend Dockerfiles) and `docker-compose` (the root, `infra/ldap-test/`, and `infra/nos-test/` compose files), and groups minor and patch updates into one PR per ecosystem. It does not propose: a Postgres major, a NATS minor or major, a Python minor or major, an nginx minor, or any Selenium image, and it does not read the FRR lab Dockerfile; `docs/specs/DEVELOPMENT.md` lists why. Every workflow action is pinned by full commit SHA with its version in a trailing comment. `tests/unit/test_image_pins.py` fails on a `FROM`, `COPY --from=`, or compose `image:` that uses `latest`, has no tag, has a tag that does not start with an exact `MAJOR.MINOR.PATCH` version (PostgreSQL's two-part `16.15` is the one listed exception), or names a pre-release; an earlier build stage, `scratch`, and the image of a compose service with its own `build:` pass. It and the other repo-root guards that find Dockerfiles and compose files read the tracked files (`git ls-files`), so an untracked local file on a developer host is not judged. `tests/unit/test_uv_pins.py` fails when the twelve service Dockerfiles name different uv or Python references, or when a workflow's `setup-uv` step does not pass the images' uv version. `tests/unit/test_docs_nats_cli_form.py` now accepts any `nats:2.10.<patch>-alpine` pin. Upgrade note: a running container keeps its old image until it is recreated. `make up` (or `docker compose up -d`) recreates every service whose image changed; recreate Traefik to pick up the pinned release (`docker compose up -d traefik`), never restart it.
- Tests: three e2e corrections after the first seeded browser run of the 2026-11-11 frontend changes (203 passed, 9 failed, all in tests). `tests/e2e/test_purpose_classify_playwright.py` locates the Classify now button by its exact name: a reservation row's button is now named with the reservation's purpose (#1317), and the test's own purpose text, "e2e classify now ...", matched the loose locator. `tests/e2e/test_reservation_detail.py::test_reservation_detail_modal_opens` scopes its title wait to the open dialog: with a document title per route (#1317) the unscoped text match resolved first to the page's `<title>`, which is never visible. `tests/e2e/test_topology_editor_canvas_identity_playwright.py` (new with #1315, never run before) releases its held device batch call through the route handler instead of removing the handler first, which left the route already handled.
- Tests: two e2e assertions corrected after the first browser run of the session and login change (#1316, #1333). `tests/e2e/test_login.py::test_login_failure_shows_error` waited for the old toast text and now waits for "Invalid credentials"; `tests/e2e/test_multi_tab_session_playwright.py` no longer asserts that two access tokens differ, because tokens minted for one account within the same second are identical, and the refresh tokens already prove both rotations. In that run the two-tab test passed every step before that assertion: the second tab refreshed with the rotated token and both tabs stayed signed in.
- Fixed: a refresh in one browser tab no longer signs the user out of the other open tabs (#1316). Each tab read the stored tokens once, at load, so a second tab refreshed with the refresh token the first tab had already rotated, auth refused it (a used token has no grace window), and the second tab cleared the tokens every tab shares. On a 401 the client now decides on what storage holds at that moment: when storage already holds another access token of the same account than the one that was refused, the request is retried with it and no refresh is sent; a stored token of another account is never sent, and the tab is signed out instead. A refresh and a logout hold the Web Lock `herd-auth-refresh` where the browser offers Web Locks (secure contexts), so they run one at a time across tabs; a tab waits for the lock at most 20 s, then goes on without it. A refused refresh polls storage for up to 2 s for another tab's write before it ends the session, with or without the lock (a lock grant and another tab's write are not ordered across processes), so a revoked session signs out up to 2 s later; the shared tokens are cleared only when storage still holds the refused token. A refreshed pair is stored only when storage still holds the token that was sent, otherwise it is revoked: within one tab, and across tabs when Web Locks exist, a refresh in flight never undoes a logout, while without Web Locks a logout landing between that check and the write can still be overwritten. Logout waits for the tab's refresh in flight and revokes the refresh token storage holds inside the lock, not the one in the tab's memory. A refresh that fails without a 401 (no answer, a timeout, a 5xx) keeps the tokens, and the refresh request times out after 15 s. Each tab follows the others' `storage` events, so a login, refresh, or logout in one tab reaches the rest (a logout signs every tab out); a tab that processes another account's login late signs out first and then adopts it. A tab whose storage is blocked, or whose own write to it failed once, works from memory and never adopts a stored pair. Every sign-out, whatever its cause, empties the query cache and resets the preferences store (`frontend/src/queryClient.ts`), so the next account in the tab never sees the previous one's data. `docs/specs/identity-and-access.md` IAM-UI-1 to IAM-UI-4, IAM-UI-11, IAM-UI-12, IAM-UI-15, and IAM-UI-16 state the rules; `frontend/src/test/api/clientMultiTab.test.ts` stages the interleavings with two store and client instances over one storage, `frontend/src/test/sessionReset.test.tsx` walks each path into the signed-out state, and `tests/e2e/test_multi_tab_session_playwright.py` runs two tabs of one browser context against a real stack.
- Fixed: the login page names why a sign-in failed, and a sign-in returns to the page that needed it (#1333). Only a 401 says the credentials are wrong; no answer (including no answer within the new 15 s client timeout), a 404 from the gateway, and any 5xx say `Could not reach HERD. Try again in a moment.`; a 422 names the length limits, a 429 asks for a wait, any other status says the answer was unexpected, and no server text is shown. `AuthGuard` records the path, query, and hash a signed-out visitor asked for, and the account last signed in in the tab, including after a session ends mid-page; the login page and `GuestGuard` go there after sign-in only for the same account (or after a first visit), and only when it is a same-origin path starting with one `/` (no `//`, backslash, scheme, or control character, and not `/login` or `/register`); anything else goes to `/topology`. The inputs carry `autocomplete` `username` and `current-password`, and the icon-only configuration link has an accessible name. IAM-UI-5, IAM-UI-9, and IAM-UI-13 state the rules.
- Fixed: a directory user whose login name has no `@` can submit the login form (#1335). The identifier field was `type="email"`, so the browser refused a plain name in LDAP mode, where the identifier is the directory login name (IAM-LDAP-2). It is now a text field, still labeled `Email` with placeholder `you@example.com` (local accounts sign in by email), with `autocomplete="username"`, `inputmode="email"`, no client-side email check, and the helper line "Directory accounts: enter your directory login name." tied to it with `aria-describedby`; the backend validates the value in both modes, and the request body still sends it as `email`. A wrong password now reads "Invalid credentials. Check your email or login name and your password." The field keeps its `login-email` id, so no e2e selector changed. The page does not yet know the active mode (that waits for `GET /api/auth/login-options`, ADR 0016), so the Register link still shows in LDAP mode. IAM-UI-14 states the rule.
- Fixed: a request refused with FastAPI's default 422 no longer blanks the web app, and a page that fails while rendering no longer takes the header and navigation with it (#1314, #1344). 35 error handlers in 19 frontend files trusted the shape of `response.data.detail`: 25 through the `detail?: string` cast the issue named, and 10 through an untyped axios read, the assistant stream's `fetch` body, or a cast of the detail's own fields (the config save's `errors`, the fork save's `message`, the version restore's `reservations`). They handed a 422's list of objects to `toast.error` or to JSX, which threw, or interpolated it as `[object Object]`; the editor's Save as Template with a name over 100 characters was one such path. Every one now goes through `errorDetail` in `frontend/src/lib/errors.ts`, whose new core `detailText` cuts a plain string to 1000 characters and formats a validation list as `field: message` per entry: the location's non-blank parts joined with dots, without a leading `body`, `query`, `path`, `header`, or `cookie` source unless that source is the whole location (`body: Field required`); at most three entries named, then `(and N more)`; cut to 300 characters; the echoed `input` never shown. Any other non-string shape, an empty or blank string included, is the caller's fallback. `profileSaveErrorText` joins its labelled list through the same bounded helper, and it and `deleteDeviceErrorMessage` fall back for a blank string detail. Three surfaces that showed other error text now show fixed copy: a config version create (`Could not save the config version. Try again.`, never axios's `Request failed with status code 500`), the reporting page's error strip, and the assistant's error and status frames, whose fields are type-checked. A version restore refused with a string detail (a 403, a 404) shows that text, and a blocked 409 whose entries carry no id shows `Restore failed` instead of an unchanged dialog. A `no-restricted-syntax` block in `frontend/eslint.config.js` refuses, outside `lib/errors.ts` and the tests, a `detail` typed `string` inside an `as` cast, a `satisfies` clause, or a type argument, a dotted or computed `.data.detail` access, and destructuring `detail` from `.data`; `src/test/lint/errorDetailLintRule.test.ts` runs the real config over the refused forms, the allowed forms, and the documented misses. The Save as Template name input takes at most 100 characters. `AppLayout` wraps its routed outlet in `RouteErrorBoundary`, keyed on the pathname plus a retry count: a page that throws shows `This page could not be displayed` with Try again, Reload page, and Go to Topology (not on `/topology` itself) under the intact header, Try again remounts the route, and navigating to another route renders it normally. Behavior change: any pathname change, a route parameter alone included, now remounts the routed page, resetting its local state, focus, and scroll position; a query-string or hash change does not. The root boundary in `App.tsx` stays as the last resort, now also holds the `Toaster` (a toast that throws shows the last-resort page instead of unmounting the app), and offers Reload page instead of a Try again that re-rendered the same failure; no fallback shows the error's message any more, it goes to the console only. Three frontend tests that pinned the old fallback for a list detail now expect its text, two now pin the fallback with a list entry that has no `msg`, one with an object, and the root boundary test asserts the thrown message is absent. New tests: a shape table and the string and list bounds in `src/test/lib/errors.test.ts`; `TopologyEditorPage.ErrorText.test.tsx` and `GroupsPage.validationToast.test.tsx` feed a real 422 list through MSW into an inline error and a real Toaster, and pin the restore refusal text; `RouteErrorBoundary.test.tsx` covers containment, the fixed text, Try again, recovery by navigation, and which navigations remount; `App.test.tsx` makes a toast throw. Integration and load tests do not apply; no browser e2e was added.
- Fixed: the topology editor can no longer save one topology's canvas into another (#1315), and the fork commit summary reports the routes a save built and released (#1321). The canvas store is global and nothing cleared it, so opening topology B after A showed A's canvas under B's name until B's hydration returned, and indefinitely when B's fetch failed, with Save enabled the whole time; a Save there wrote A's wiring into B as B's newest version. The page now remounts its editor for every topology id and `reservationId`, so a version preview, restore target, AI proposal, or open dialog never carries over to another canvas; each editor clears the store in a layout effect when it mounts and when it unmounts, and the store clears itself when the session signs out (a token refresh clears nothing). Until the editor's own load lands a "Loading canvas..." status covers the canvas; Save, Reserve, Save as Template, Clear canvas, History, and Use AI are disabled, and drops, new connections, AI proposals, dragging, selecting, and the Delete key are refused, so an edit made during the load is never silently replaced. A request token guards the load's deferred writes and every later wholesale canvas replacement (version preview, exit, and restore, and the fork history's), so one that resolves after its editor unmounted, including a late fork hydration after leaving live edit, is dropped. A failed topology fetch outside live edit shows "Topology not found" (404) or "Could not load this topology" with a way back to the list, and no canvas. The commit toast adds a `Routes released N, built M` line from the save's `l3_routes_released` and `l3_routes_built` when either is above zero; a response without them reads as 0. `frontend/src/test/pages/TopologyEditorPage.CanvasIdentity.test.tsx` drives twenty sequences without resetting the store between steps (A then B cached and uncached, in place and through the list page; B's fetch failing with 503 and 404; late hydrations after navigating to C, to an empty topology, after unmount, and after leaving live edit; A, B, and back to A; a version preview on A followed by B, and one resolving after it; the same topology between plain mode and live edit; a drop during the load; a remount; logout then another user; a new empty topology; entering live edit; StrictMode's double mount), and `ForkSaveResultToast.test.tsx` covers L1-only, L2-only, routes-only, mixed, all-zero, and older responses. `tests/e2e/test_topology_editor_canvas_identity_playwright.py` holds the device batch call with `page.route`, opens A, moves to B in the app, and reads both topologies back after a Save. Integration and load tests do not apply.
- Fixed: list rows and the Administration menu work from the keyboard (#1317, WCAG 2.1.1 and 4.1.2). On Inventory, Templates, Topology, User Groups, and Device Groups the name cell is a link to the item, and on Reservations the short id is a button that opens the detail dialog, named from the id, the action, and the purpose cut to 60 characters (`11111111, open reservation: fw test`); the row click stays as a mouse convenience through `rowClick` (`frontend/src/lib/rowNavigation.ts`), which ignores clicks on the link, checkboxes, and row actions, so the link never navigates twice. The Administration menu is a disclosure button with `aria-expanded` and `aria-controls` that opens on click, Enter, or Space, lists ordinary links reached with Tab, and closes on Escape (focus returns to the button), an outside click, focus leaving it, or a route change; hover still opens it, a click while hover-opened keeps it open, and the mouse leaving a hover-opened panel that holds keyboard focus keeps it open instead of dropping focus to the page. Its eleven links come from one array, `ADMIN_NAV` in `frontend/src/lib/adminNav.ts`, and `frontend/src/test/routes.test.tsx` fails when that array and the AdminGuard route group disagree; which role sees which link is unchanged. The layout gains a skip link and a `<main>` landmark around the routed page, and each page under the layout sets `document.title` (`Inventory - HERD`) from `ROUTE_TITLES` in `frontend/src/lib/routeTitles.ts`, restoring the original title when the layout unmounts. Two keyboard gaps outside list navigation remain: the device rows in the reservation edit-devices dialog and the expandable device rows in the reservation inventory tab are still click-only.
- Fixed: every dialog is named by its own title (#1318). `Modal`, `ConfirmDialog`, and `BulkImportExport` hardcoded their title ids (and `ConfirmDialog` its description id), and closed dialogs stay mounted, so pages with several dialogs (the topology editor, the device config section, one `ConfirmDialog` per reservation row) had duplicate ids and each `aria-labelledby` resolved to the first title in the document; the ids now come from `useId()`. The Create Connection modal on the admin Connections page still mounts only while open, now for the reasons that remain (a fresh form per open, and no hidden copies of its device search inputs or Cancel button for a first-match e2e locator to land on); its comment no longer cites the shared id. The e2e tests that selected `#modal-title`, `#confirm-dialog-title`, or `#bulk-import-title` now assert the open dialog's accessible name with `to_have_accessible_name`. The unused `open_admin_menu` helper in `tests/e2e/conftest.py` is removed: nothing called it, and its wait also matched the always-visible Templates link.
- Auth: the single sign-on flow store and federated identities, present and dark (#37, ADR 0016 phase 2). Migration 0010 adds the `oidc_login_flows` and `federated_identities` tables; `services/auth/app/services/oidc_flow_service.py` begins, claims, completes, fails, and exchanges a sign-in flow, each step one conditional update on the expected status, storing only SHA-256 hashes of the state, binding, and handoff secrets and sanitizing the return path; `resolve_federated_user` and `create_oidc_user` in `auth_service.py` provision `auth_source="oidc"` accounts keyed by (issuer, subject) and refuse, without linking, any login whose email or username another account holds, compared case-insensitively and, on Postgres, under advisory locks so the refusal also holds between concurrent first logins; it returns an account or nothing for any claim content, refusing a NUL, control, format, or unpaired surrogate character, surrounding whitespace, or a malformed email with a fixed reason. The flow store requires 43-character state, binding, and handoff secrets, and its prune skips locked rows and never fails a sign-in. No route calls any of it and `AUTH_METHOD` still accepts only `local` and `ldap`, so nothing an operator can reach changes. Tests: unit suites for the flow store (every status crossed with every operation, live and expired), provisioning (every collision against local, LDAP, and OIDC accounts), and the migration (create_all and the migration build the same tables), plus `test_oidc_flow_service_live_pg.py` in the gates' Postgres-live phase for the races. `docs/specs/identity-and-access.md` gains rules IAM-OIDC-1 to IAM-OIDC-14.
- Manual: search across the published manual (#1298). Every page of `docs/manual/` has a search box in its header (`/` focuses it) whose results list the page, the section heading, and a snippet with the matched words marked, each linking to the section's anchor. Every query word is required, the last one as a prefix, case-insensitively, and a dotted version such as `0.3.0` is one word that ranks its own release page first; otherwise heading matches rank above body matches, and earlier release pages rank after current pages. Release pages are labeled as release notes. The index is prebuilt: `scripts/build_manual_search_index.py` (stdlib only, `make manual-index`) writes `docs/manual/assets/search-index.js`, 240,744 bytes for 23 pages and 189 sections, as a script assigning a global; no page loads it until the reader first focuses or types in the search box, when `manual.js` adds it as a script element, so search also works from `file://` and no request is made while a query runs. Results are built from text nodes, never parsed as markup, and the result count is announced through a live region. The UI mockups (`.shot-stage`) are not indexed. The 33 glossary entries without an id gained one, and the seven step headings of `admin-setup.html` that its sidebar links are sections of their own. The header nav now collapses behind the menu toggle at 980 px instead of 720 px: between those widths its links, the wordmark, and the search box do not fit on one row of the bar (on main the links already spilled out of the bar from 724 to 844 px). `tests/unit/test_manual_search_index.py` fails when the committed index is stale, when a page loads the index itself or does not load `manual.js`, when an index anchor is missing from its page or a sidebar target is not an index anchor, or when an id repeats on a page; `frontend/src/test/manual/manualSearch.test.ts` tests the matching and ranking function and the rendered results; `tests/e2e/test_manual_search_playwright.py` drives a real browser against a local static server, with no stack, including a header sweep from 721 to 1100 px. Integration and load tests do not apply: the manual is static pages with no service surface.
- Docs: accuracy sweep after v0.7.0. SECURITY.md names v0.7.0 as the latest release; README.md counts eight locust user classes (`DeviceConfigUser` joined in #1100); docs/ENV_VARS.md no longer says the bound-address certificate warning lasts until #1239 lands, and docs/GAPS.md aims its certificate check at the certificate `make certs` writes instead of a committed CA; docs/USER_GUIDE.md places the Inventory search and filters in the left filter panel (#957); docs/ARCHITECTURE.md, docs/DRIVERS.md, docs/AI_GENERATE.md, and docs/TROUBLESHOOTING.md describe the 1 MiB tar header bound as the header check #1279 made it, not a per-read bound; and `docs/specs/identity-and-access.md` section 13 records the three documents issue #1010 named as corrected instead of still disagreeing. Three older errors found on the way: docs/ADMIN_HANDBOOK.md no longer tells the superadmin to change a password HERD cannot change, docs/GAPS.md names the auth health route `/api/auth/health`, and `docs/specs/DEVELOPMENT.md` gives the gate stack's address as the gateway URL rather than localhost.
- Tests: `tests/unit/test_manual_release_pages.py` guards the manual's release pages against facts that expire when the next release ships. It fails when a page in `docs/manual/` does not carry exactly one release link in its header, pointing at the newest release page (by integer version) with the text `Release X.Y.Z`; when a release page's sidebar does not list every release page newest first with exactly one entry, its own, marked active; when a demoted release page's dek does not link the release immediately after it; or when a demoted release page contains one of four known expiring phrasings ("The current release is", "now describes", "now documents", "The version this manual documents"). It knows only those four phrasings and judges no other prose. `docs/specs/DEVELOPMENT.md` now states the release-page rule and the three edits a release makes to the old manual pages, and `docs/specs/TESTING.md` lists the guard.
- Tests: `test_wiring_changed_reconcile.py::test_stale_wiring_writer_does_not_clobber_concurrent_winner` no longer fails about once in sixty runs at its step-2 check that a writer is mid-drive (#1238). The cause was the background retry tick, not the product: when the tick fetched the switch before the test armed the slow fail knob, it held the FAILED row's claim with the unarmed, fast context, the test's one manual retry answered `in_progress`, and nothing drove the row again inside the 6 s poll. Step 2 now fires the manual retry again while no login run is in flight and the latest call answered `in_progress` for the row, at most once per 0.25 s poll and only within 10 s of the first call, each call restarting the 6 s login watch; every call is awaited, and the check still fails when no writer logs in, now listing each retry answer for the row.
- Tests: the e2e harness makes the next login timeout diagnosable (#1266, still open; these changes name the flake, they do not remove it). The failure-artifact hook in `tests/e2e/conftest.py` now also writes for a SETUP-phase failure, under a `setup-` prefixed directory: the five files for every page or driver created during that test's setup (`pw_page` and the Selenium fixtures register theirs), else for what the test's resolved fixtures hold (`pw_page`, the open pages of `pw_contexts`, or a Selenium driver), else traceback, an empty console log, and a meta.txt that says no page or driver was registered or resolved for the test; the registry is emptied after every phase. Every page.html has the value of each password input redacted, `ADMIN_PASSWORD` and the password the login helpers receive print as `'[redacted]'` in traceback arguments and locals, and the Selenium console log is now captured (the sessions ask for the browser log and the hook reads it with the getLog command, which `webdriver.Remote` needs; before, every Selenium console.log was empty). `pw_login` clicks inside `expect_response` on `POST /api/auth/login`, fails at once with the status and the password-redacted body on a non-200, and only then waits for /topology with `wait_until="commit"`; `_do_login` names a login form that never rendered, and a post-login timeout names the page's URL and the login response statuses the page received. The `logged_in_browser` and `admin_browser` fixtures register `driver.quit` as a finalizer before they log in (and `browser` before anything else can raise), so a login that raises no longer leaks one of the Selenium container's three sessions. `tests/unit/test_e2e_failure_artifacts.py` pins all of it with fakes, autospecced Selenium mocks, and one inner pytest run in a subprocess under `--tb=short` and `--tb=long -l`, no browser.
- Docs: accuracy sweep after the 2026-11-09 merges. docs/TROUBLESHOOTING.md and docs/ENV_VARS.md no longer say e2e failure artifacts are written only for a call-phase failure: they name the `setup-` directories, and TROUBLESHOOTING.md says page.html has password values redacted and that the Selenium console log is now read (it no longer calls it best-effort and possibly empty). docs/MANUAL_TESTING.md no longer implies the conftest's Selenium fixtures leak a session when a login raises. `docs/specs/DEVELOPMENT.md` says the release-page rule takes precedence over the older manual briefs' Part B. `docs/specs/TESTING.md` lists the `nos-dialect` job among the per-PR CI jobs and no longer calls the repo-root `tests/unit/` suite small or limits it to the seed script, the drivers, and the fetch-retry helper.
- License: `LICENSE` is now the verbatim Apache License 2.0 text. The previous file was headed Apache 2.0 but differed from the canonical text (section 4 carried MIT-style grant wording, section 8 said "exemplary" where the license says "consequential", and the appendix was missing), so GitHub reported the license as Other. The project has always been offered under Apache 2.0, as the README states; the copyright line moves to a new `NOTICE` file.
- Fixed: one VLAN allocation whose `delete_vlan` cannot read its switch or template from inventory no longer stops the release of the allocations after it. A `TransientUpstreamError` from that read escaped the orphaned-allocation release, so the allocation in hand stayed released with its `defined_switch_ids` untrimmed and every later orphaned allocation in the pass stayed `ACTIVE` with no memberships, at the tail of each reservation's L2 apply (the reconcile nacked, the terminal teardown stopped before L3) and in the L2 retry's release of superseded rows. The release now catches that error per allocation: the switches whose delete succeeded before it leave `defined_switch_ids`, the failing switch and the ones not yet tried stay listed, the pass goes on with the next allocation, and one line is logged at `ERROR` with action `vlan_delete_upstream_failed` (`error_class`, `vlan_assignment_id`, `reservation_id`, `vlan_id`, `switch_ids`; the class name only, never the error's text), and by owner decision the allocation is released rather than kept `ACTIVE` with the definition left on those switches never re-driven (keeping a switch in a known-good state is the L2 driver's job, docs/DRIVERS.md). Two retry results change with it: a manual retry of a superseded row in that situation now answers `superseded` where it answered 503, and when the failing read is at the tail of one reservation's L2 apply inside the retry tick, that reservation is no longer logged with `wiring_retry_reservation_upstream_failed` (its rows were already driven and `skipped_upstream` stays 0; the line logged is `vlan_delete_upstream_failed`). The retry tick's `wiring_retry_superseded_release_upstream_failed` catch stays as a backstop that no `TransientUpstreamError` reaches today. Any other exception still propagates (#1209).
- Auth: the first pieces of OpenID Connect single sign-on are present and dark (ADR 0016 phase 1, #37); no route uses them and `AUTH_METHOD` still refuses `oidc`, so nothing an operator can reach changes. `herd_common.jwt_codec.decode_id_token` is a second named decode beside `decode_access_token`: one asymmetric algorithm from the nine in `ID_TOKEN_ALGORITHMS` (never `none`, never HS, so an HMAC token keyed by the client secret or the public key is refused before any decode), exact `iss`, `aud` membership, `exp`, `iat`, `iss`, `aud`, `sub`, and `nonce` required, and a 60 s leeway for ID tokens only (an owner decision; HERD's access tokens keep 10 s). `tests/unit/test_jwt_settings_wiring.py` admits it as a second `DIRECT_DECODE_ALLOWED` entry and admits the one header read that picks the provider's key as the first `UNVERIFIED_READ_ALLOWED` entry. The auth service gains `app/services/oidc_service.py` (discovery and JWKS cached an hour and never on failure, an unknown signing key refetching the JWKS at most once a minute, the PKCE S256 authorization URL, the `client_secret_basic` code exchange, and ID-token verification with the `azp`, nonce, subject, key-suitability, and RFC 9068 `typ` rules, an RSA provider key under 2048 bits refused, a 10 s overall deadline on each provider request, and only `OidcUnavailableError` or `OidcRejectedError` raised for anything the provider sends) and the twelve `OIDC_*` settings with their compose lines, `.env.example` lines, and a docs/ENV_VARS.md section; `OIDC_BREAK_GLASS_LOCAL_SUPERADMIN` is a field only until phase 3. `pyjwt[crypto]` is now auth's runtime dependency and common's dev dependency (`cryptography` was already locked). `herd_common.logging.setup_logging` now sets the `httpcore` logger to INFO in every service, so its DEBUG lines, which carry outbound response headers, stay out of the log even at `LOG_LEVEL=DEBUG`.

## [0.7.0] - 2026-11-08

- A hardening release. The 177 entries under Delivery detail are 115 fixes, 31 test
  changes, 18 documentation changes, 4 behavior changes, 4 additions, 1 security change, 3
  list-page features, and the upgrade note for the Traefik change.
- Token settings are checked when a service starts. Every service that checks access
  tokens validates `AUTH_SECRET_KEY` and `AUTH_ALGORITHM` as its settings load, so a blank
  or placeholder key, or one that cannot serve as an HMAC secret, stops the service with a
  message naming the variable instead of signing tokens with it. Tokens are signed and
  verified with PyJWT instead of python-jose, through one helper,
  `herd_common.jwt_codec.decode_access_token`, which passes exactly one algorithm and a 10
  second leeway (#1186). Non-admin template reads now mask password-field defaults the way
  device reads mask password values. The settings check and the template masking are the
  fixes for the two security advisories addressed by this release.
- Untrusted input is bounded earlier. A driver package's files no longer run in the
  sandbox child before its resource limits apply: the child starts in Python's isolated
  mode and puts the package on its path only after the limits are set (#1180). An uploaded
  tar whose pax extended header or GNU long name is over 1 MiB is refused at the header
  block, before any of the record is read; the earlier per-read bound did not hold on
  Python 3.13.18, which reads such a record in 1 MiB chunks (#1279). The e2e Selenium
  container publishes its WebDriver and noVNC ports on 127.0.0.1 only (#964). The buffered
  reservation assistant route answers 502 without the exception's text when a turn fails
  with an exception class it does not name, where it used to answer a plain 500 (#1280).
- A stack shares its host more safely. `HERD_GATEWAY_BIND` publishes the gateway on one
  host address (#1256). Each stack's Traefik now discovers only its own compose project's
  containers, and `make up`, `make dev`, and `make prod` refuse to start while another
  compose project publishes port 80 or 443; recreate Traefik after pulling, as the upgrade
  note below says. `make certs` writes a self-signed gateway certificate valid for ten
  years, so a fresh clone is no longer served Traefik's built-in certificate, and the TLS
  store names that pair as its default (#1239). Auth boots on a stack upgraded in place
  before `make migrate` when the new release adds a column to `users` (#1261). `make
  everything` runs the ci.yml checks it used to skip, among them `uv lock --check`, the
  frontend suite under `TZ=America/Los_Angeles`, and the `package-lock.json` sync check.
- Accounts have profiles, and the list pages have a filter panel. Every account has a
  required display name and an optional contact email and phone number, which the account
  edits on Settings and an admin edits from Admin > Users (#1252). List pages share a left
  filter panel (#957). The Topologies page gains search, an Owner filter, sortable
  columns, and multi-select delete (#958), and the Reservations page can be searched and
  filtered by Status, Category, and Period, with the choices saved per user (#959).
- Webhook subscriptions can be paused, and several interface defects are fixed. An admin
  can pause and resume a webhook subscription with `PATCH /api/v1/webhooks/{id}`, keeping
  its delivery ledger; a paused subscription receives no event and nothing is replayed on
  resume (#1078). Modal dialogs open centred again; Tailwind 4's base styles had pinned
  them to the top-left corner in every release through 0.6.0 (#979). Toasts appear at the
  bottom centre, where they no longer cover the topology editor's Save button (#942), and
  the Settings page scrolls again (#1260).
- Reservation edits follow the rules create follows (#994 to #1000). A device edit commits
  under a status guard, so a PATCH that races a cancel, release, auto-complete, or
  activation keeps nothing and answers 409, and inventory is written only for an `ACTIVE`
  reservation (#994). An edit can no longer stretch a reservation past
  `RESERVATION_MAX_DURATION_SECONDS` (#995), and a device added to a `PENDING` reservation
  is judged by the window conflict check (#999). Both provisioning backstops time from a
  new `provision_started_at` stamp, so a purpose edit no longer restarts the timeout
  (#997). The calendar answers 503 to a non-admin when device visibility cannot be
  verified, instead of showing every user's reservations (#1000).
- Wiring and dynamic instances recover from failures that used to strand them. VLAN
  numbers are unique across everything reachable through the current cabling, transit
  switches included, so a cable change no longer lets two reservations share a number in
  one fabric (#1003). The background wiring retry no longer stalls on a ledger whose
  oldest `FAILED` rows it will never drive (#1190), and one reservation's failed upstream
  read no longer stops the retry for every other reservation in that layer (#1204).
  Execution no longer reads a 401 or 403 from inventory as a missing device or template,
  so during an `INTERNAL_API_TOKEN` rotation a live switch's wiring rows are retried
  instead of pinned `FAILED` (#1203), and a reservation lifecycle event refused the same
  way is retried instead of dropped (#1215). Ending a reservation runs a keyed destroy for
  a dynamic instance whose create never reported a handle, and the #896 compensation path
  uses it too; existing Hypervisor recipes must accept `instance_ref=None` (#937). A
  ledger write that loses the FAILED-to-ACTIVE compare-and-swap returns the winner's row
  instead of raising `MissingGreenlet` (#1152).
- Tests prove what they claim. A test-effectiveness sweep read every test file in the
  repository, verified 75 high-severity findings by mutation, and landed the fixes (#1134
  to #1148, #1158); it also found the ledger defect above (#1152). Three Playwright tests
  order their read-backs by the HTTP response of the action they wait on instead of a
  toast an earlier action left on screen (#1169). The assistant integration tests that
  judge the local model's own tool choices carry an `ai_model` marker and run in a
  separate invocation after every deterministic integration test, so one model miss no
  longer leaves the rest of the suite unrun (#1270). The NOS lab's SR Linux healthcheck
  waits for an SSH banner (#1263), and the FRR image builds again with its openssh pin
  moved to the version Alpine 3.16 still carries (#1268).

### Delivery detail

- Fixed: an uploaded tar with an oversized pax extended header or GNU long name is refused again on Python 3.13.18 (#1279). The bound from #1179, #1180, and #1192 was a per-read limit, `herd_common.bounded_tar.BoundedTarStream` refusing any single read over `MAX_TAR_READ_BYTES` (1 MiB), which worked because tarfile on 3.12 reads such a header's whole record in one call. Python 3.13.18 reads it in 1 MiB chunks (tarfile's `_safe_read`, gh-151497), every chunk passed the bound, and a driver package or AI generate tgz declaring an 8 MiB record was read into memory in full instead of being refused, and in the GNU long-name case extraction started. The bound is now a header check: `BoundedTarInfo` refuses an extended header (pax `x` and `g`, Solaris `X`, GNU `L` and `K`) whose record, rounded up to its 512-byte block, is over `MAX_TAR_READ_BYTES`, at the header block and before any of the record is read, whatever size of reads tarfile makes. Every uploaded tar is opened through one helper, `open_bounded_tar`, which passes that class (ai-orchestrator's `_BoundedTarInfo` now extends it) and keeps the per-read bound as the second line; the refusals are unchanged: execution's `PackageLimitError`, inventory's 422, and ai-orchestrator's 400, each with the same detail. HERD's images and CI run 3.12, which was not affected; the five tests per service that failed on 3.13.18 pass on both.
- Added: `make certs` generates the gateway's TLS certificate, so a fresh clone is no longer served Traefik's built-in `TRAEFIK DEFAULT CERT` (#1239). The repository never tracked `infra/traefik/certs/server.key`, so the tracked chain could not load anywhere but a host that had supplied its own key, and `dynamic.yml` set no default certificate, so a client connecting by IP address (no SNI) got Traefik's own even there. `make certs` (`scripts/make_certs.sh`) writes `server.key` and a self-signed `server-chain.crt` valid for ten years, naming `localhost`, `127.0.0.1`, the `HERD_GATEWAY_BIND` address, and any `CERT_SANS` entries, only when the key is absent: with both files present it changes nothing, with the key present and the certificate missing it issues a certificate for that key, and `CERT_FORCE=1` regenerates both. `make up`, `dev`, `prod`, and the gate's stack start run it first, and CI's integration job and nightly, which start their stacks with a bare `docker compose up`, run it as a step before that; `dynamic.yml` names the pair as the default certificate too, so a pair that does not load now means no TLS handshake at all rather than the built-in certificate. The tracked root, intermediate, and leaf certificates are removed (no consumer; their key was never public), and `infra/traefik/certs/README.md` keeps the directory. Trusting the certificate stays a manual step (FRESH_SETUP.md step 3). `tests/unit/test_make_certs_wiring.py` pins it through real `make` in scratch checkouts; spec rule OPS-GUARD-5.
- Fixed: the buffered reservation assistant route (`POST /reservations/{id}/assistant`) no longer answers FastAPI's plain 500 when the turn fails with an exception class it does not name (#1280). It now does what the streaming route already did: before any write tool landed, the turn is rolled back, its spent tokens are metered, and the route answers 502 `Assistant call failed`; after a write landed, the turn is kept and answered 200 with `incomplete: "ai_error"`. Neither the response nor the stored rows carry the exception's text; the failure is logged once as `ai_assistant_unexpected_error` with its class and traceback, as the streaming route logs it (spec rule AI-TURN-9).
- Fixed: one miss by the local model in an assistant integration test no longer stops the rest of the integration suite and the gate (#1270). `make everything` failed on 2026-11-06 when the 9B model behind the host's vLLM bridge answered `test_assistant_answers_a_herd_how_to_from_the_manual` from its `search_docs` results without the `read_doc` the test asserts, and `make test-integration` runs pytest with `-x`, so every later integration test went unrun. The three tests that judge the model's own tool-call choices (`test_assistant_loop_response_shape_against_live_stack`, `test_assistant_write_flow_discovers_schema_first`, and the docs-lookup test) now carry a registered `ai_model` marker; `make test-integration` runs `-m "not ai_model"` and keeps `-x`, and the new `make test-integration-ai` runs `-m ai_model` without `-x` right after it in both `master` and `everything`, so a model miss still fails the gate but only after every deterministic test has reported. The docs-lookup test also asks its question once more when, and only when, the first turn searched the documentation without error and did not read the manual page; a second miss in a row fails, and a non-200, a turn with no `search_docs` call, or an erroring tool still fails on the first turn. `tests/unit/test_ai_model_marker_wiring.py` requires the marker on every integration test that calls `ai_provider_configured` and reads `tool_calls`, and pins the registration, both pytest commands, and both gates' calls. nightly.yml runs the whole directory with no `-m` and is unchanged.
- Docs: three new developer guides, `docs/specs/DEVELOPMENT.md` (install, run, gates, git and CI, migrations, releases), `docs/specs/TESTING.md` (writing and running tests at every level, the test effectiveness rules, the e2e and integration gotchas, the pg-live suites), and `docs/specs/CONVENTIONS.md` (how the code is organized per area, the one helper for each job, what must not be reintroduced). They carry the operating rules that until now lived only in an untracked local file, so every clone has them. They live beside the specifications but are not specifications: they have no template sections and no rule identifiers, and the spec drift guard (`tests/unit/test_spec_references.py`) checks only their repository paths and relative links. `docs/specs/ai-features.md` lists #1280 (the buffered assistant route's plain 500) and the tgz CPU-time limit, `device-configuration.md` the driver loader's pax-header bound, `inventory.md` the outage-order asymmetry of three older reads, and `topology.md` the label-only node decision.
- Fixed: the NOS lab FRR image builds again (#1268). `infra/nos-test/frr/Dockerfile` pinned `openssh=9.0_p1-r5`, and Alpine 3.16's repository (the pinned frrouting/frr base image is Alpine 3.16.2) now carries only `openssh-9.0_p1-r6`, so every fresh build failed with `openssh-9.0_p1-r6: breaks: world[openssh=9.0_p1-r5]` and `make nos-up` stopped there; the advisory `nos-dialect` job and the nightly NOS lab feature tests failed with it, while hosts with a cached image never rebuilt the layer. The pin is now `openssh=9.0_p1-r6`, the version an unpinned install resolves to on the same base image, as the Dockerfile's bump procedure says.
- Fixed: auth boots on an upgraded-in-place stack before `make migrate` when the new release adds a column to `users`. The startup seed's existence check, `superadmin_exists`, selected the whole `User` model, so on a schema stamped at the previous release (startup never runs `create_all` there, #419) it named the columns revision 0009 added (`full_name`, `contact_email`, `phone`), the SELECT failed with `column users.full_name does not exist`, auth exited at startup, and `make up` ended with the auth container unhealthy; under `make prod` the container crash-looped, so `make migrate-auth` (a `docker compose exec`) raced the restarts. The seed's three existence checks now select the id only: `superadmin_exists` (with `LIMIT 1`), the new `email_registered` for the configured superadmin email, and the new `group_name_exists` for the "Not Grouped" group, so no column a later release adds is read at boot. `docs/OPERATIONS.md` now states the column case and the `make prod` remedy, `docker compose -f docker-compose.yml run --rm <svc> alembic upgrade head`, and `docs/TROUBLESHOOTING.md` lists the symptom (#1261).
- Fixed: the first SR Linux test of the advisory `nos-dialect` job no longer fails with an SSH `ConnectionResetError` during the banner exchange right after the lab boots (#1263). The SR Linux healthcheck proved the baseline landed but never opened an SSH connection, so `make nos-up` (`docker compose up --wait`) could return while sshd still reset new connections. The healthcheck now also runs `infra/nos-test/srl/ssh_ready.sh`, which must read an `SSH-` banner from port 22, and every test-side netmiko session under `tests/nos_lab/` opens through one helper, `tests/nos_lab/_lab_ssh.py`'s `open_lab_session`, which retries a transport failure at most three times two seconds apart (an authentication failure is never retried) and names the attempt count and the last exception class when it gives up. `tests/unit/test_nos_lab_ssh_readiness.py` pins both healthchecks' SSH probes, runs the probe against fake servers, and pins the helper.
- Fixed: the Settings page scrolls again (#1260). The app shell clips every page (`AppLayout` puts the page in `flex-1 overflow-hidden`), so a page scrolls only when its own outermost element is a scroll container; SettingsPage had none, which was harmless until #1258's Profile section pushed the Notifications section below the fold, where its event toggles and Save button could not be reached by wheel, scrollbar, or keyboard (the three Selenium settings round trips failed with `ElementNotInteractableException`). The page now wraps its content in `h-full overflow-y-auto`, and a vitest source scan, `src/test/pages/pageScrollContainer.test.tsx`, fails any page the route table renders under `AppLayout` whose main render is not an `h-full` scroll container, with an exact, commented allowlist (the three full-screen pages outside the layout, and the topology editor, whose canvas owns its scrolling).
- Fixed: a 401 or 403 from reservations no longer drops a reservation lifecycle event unrun. Execution corroborates every `reservation.created`, `.updated`, `.cancelled`, `.completed`, `.failed`, `.provision_requested`, and `.wiring_changed` against reservations' `GET /internal/{id}` before it runs the handler, and `_verify_reservation_event` read any answer below 500 that was neither 200 nor 404 as not corroborated, so `process_reservation_message` ACKed the message and skipped the handler with no NAK and no dead letter. Reservations answers a wrong internal token with 403, so during an `INTERNAL_API_TOKEN` rotation a `reservation.cancelled`, `.completed`, or `.failed` was silently discarded, leaving the reservation's wiring rows `ACTIVE` and its dynamic instances running after it ended. Only a 404 (`reservation not found`) and a 200 whose status does not corroborate the event are still ACKed unrun as `nats_event_unverified`; the corroboration rule itself is unchanged. Every other answer (a 401, 403, 409, a 422 for a malformed id, a 5xx) raises `TransientUpstreamError` with the text `verify reservation event: upstream <status>` (the 404-only status check is now one function, `_internal_record_found`, which the #1203 device and template reads share), so the event is NAKed with backoff and runs once reservations answers, and a misconfiguration that outlasts the five deliveries (about 81 seconds at the defaults) is dead-lettered and can be replayed. The same check inside a dynamic create (the re-read after the `CREATING` row is committed) now NAKs on such an answer instead of abandoning the event as if the reservation had ended (#1215).
- Fixed: a 401 or 403 from inventory or the secrets service on execution's latest-config, hypervisor, or secret read no longer reads as missing. `_fetch_latest_config`, `_fetch_hypervisor`, and `_fetch_secret_value` returned `None` for any answer below 500 other than 200, and both services answer a wrong internal token with 403, so during an `INTERNAL_API_TOKEN` rotation a dynamic-instance teardown left the row live with `recipe_config_missing` and never destroyed the VM, a dynamic-instance create was dead-lettered on its first delivery as a missing hypervisor or secret (the reservation `FAILED` instead of retried), an L3 switch with no routing intent was skipped as having no routes while the fork version was stamped (its routes never installed), and a switch whose intent needed re-validation was recorded `FAILED` with the misleading `l3_switch_unconfigured`. A 401 or 403 on any of the three now raises `TransientUpstreamError` with the text `fetch latest config for device <id>: upstream 403` (or `fetch hypervisor <id>: ...`, `fetch secret <id>: ...`), through one check, `_raise_if_token_refused`, so the event is NAKed and driven again once the service answers: the dynamic instance's ledger row and the refused switch's route row are left unchanged and the fork version unstamped. As with a 5xx, what the same wiring pass did before the read stays as it was left (the L1 and L2 layers, another L3 switch already settled, a stale build row already parked for release, which the retry channels remove). Every other answer keeps its reading by decision: a 404, 409, or 422 still means missing, since a misread on these reads degrades one connection rather than a whole desired set, unlike the device and template reads (#1203). In both wiring retry channels, a FAILED L3 row that records no routes and whose configured-routes read is refused is now skipped for that pass with every column unchanged instead of released as having no routes: the tick leaves it for a later tick, and a manual retry reports it `still_failed` where it reported `released` (#1216).
- Added: `HERD_GATEWAY_BIND` publishes the gateway on one host address (#1256). `docker-compose.yml` publishes Traefik's ports 80 and 443 on `${HERD_GATEWAY_BIND:-0.0.0.0}`, read from the shell environment or `.env`; the dashboard stays on `127.0.0.1:8080`. Set it to one address (for example the wired NIC of a host that also has wifi) and the stack answers there only, with `localhost` and every other interface refused. Unset keeps every interface, so CI, nightly, and other clones behave as before, with one difference: the explicit `0.0.0.0` publishes on IPv4 only, where the bare `"80:80"` and `"443:443"` also bound `[::]`. Nothing in the gates uses the IPv6 listener (`curl https://localhost` and Chromium fall back from `::1` to `127.0.0.1`). The Makefile reads the same value (the environment or command line first, else the last `HERD_GATEWAY_BIND=` line of `.env`) and derives the host its harnesses address: `localhost` for unset, empty, or `0.0.0.0`, otherwise the value. It exports `HERD_BASE_URL` (`https://<host>/api`) and `E2E_HOST_BASE_URL` (`https://<host>`) as defaults, so `make seed`, `test-integration`, `test-contract`, `test-e2e`, and `test-load` follow it while a value already in the environment wins, and the gate's health wait, its load phase, the `make everything` banner, the `make logs` note, and `scripts/check_gateway_ports_free.sh`'s remedies (given the host as `HERD_GATEWAY_HOST`) name it. `make test-load` now drops a trailing `/api` from `HERD_BASE_URL` for locust's `--host`. `make gateway-url` prints the derived URL. A browser at a bound IP address warns about the certificate until #1239 lands.
- Added: every account has a profile (#1252): a required display name (`full_name`, 1 to 200 characters, not unique), an optional contact email, and an optional phone number (digits, spaces, `+`, `-`, `(`, `)`, at least one digit, at most 32 characters; stored and shown only, nothing calls or texts it). The account edits its own on Settings (`PATCH /api/auth/me/profile`), and an admin or the superadmin edits any account's, active or not, from Admin > Users (`PATCH /api/auth/users/{id}/profile`). That route is self or admin by the effective role: a user naming any id but their own gets 404 `User not found`, byte-identical to an unknown id and decided before any read of the named account. Both routes accept only the three fields (any other key is 422), read an empty contact email or phone as clearing it, refuse a null or empty name with a 422 naming the field, record the caller in `modified_by`, and log one `user_profile_updated` line carrying the changed field names, never values. `GET /api/auth/me` and `GET /api/auth/users` report the three fields; the header shows the name; register takes an optional name, prefilled from the username, and stores the username when it is omitted. Auth migration 0009 adds the columns and backfills every existing account's name from its username. The internal contact route notifications calls answers the contact email when one is set, else the sign-in email, with its shape unchanged, so email notifications follow it within the 30 s contact cache. LDAP accounts edit the same fields; nothing is read from the directory for them. Specified as IAM-PROFILE-1 to IAM-PROFILE-11 in `docs/specs/identity-and-access.md`.
- Docs: `docs/USER_GUIDE.md` no longer says a password-change route exists; HERD has none (#1252).
- Fixed: a running stack is no longer changed by another compose project on the same host. Each stack's Traefik read every container on the shared Docker socket, so with the seeded gate stack a `make everything` leaves serving `https://localhost` still up, `make up` added the dev stack's `config` and `frontend` containers to the gate Traefik's routes, on a network it cannot reach, and about half of the page's requests hung. Traefik's static configuration is now the `traefik` service's command in `docker-compose.yml` (`infra/traefik/traefik.yml` is gone, since Traefik ignores every other flag once a configuration file is given, which had also left the dev override's `--log.level=DEBUG` inert), with `--providers.docker.constraints` admitting only containers labelled with the stack's own `com.docker.compose.project` and `--providers.docker.network` naming that project's network; both read `${COMPOSE_PROJECT_NAME:?...}`, so an empty project name refuses to render the file instead of matching nothing. `make up`, `make dev`, and `make prod` now refuse to start while another compose project publishes host port 80 or 443, naming it and, for a gate stack, the two ways out (use it, or `make gate-down` then `make up`; `scripts/check_gateway_ports_free.sh`); `make logs` says which project is serving when the dev project has none, and the `everything` closing banner says `make up` is refused while the gate stack runs. `FRESH_SETUP.md` and `docs/OPERATIONS.md` no longer claim the repository's certificate chain works out of the box: its private key is not tracked (#1239). The guard compares against the project name `docker compose config` reports, so a checkout directory with `_` or `.` in its name, or a `COMPOSE_PROJECT_NAME` in `.env`, never refuses its own stack, and it fails closed when compose cannot answer. A gate run (`make master`, `make everything`) runs the same check before it starts its stack, so it can no longer send its seed, integration, and e2e traffic into another checkout's serving stack. Docker Compose 2.27.1 or later is now required (it sets `COMPOSE_PROJECT_NAME` for interpolation, compose-go #628).
- Upgrade note for the change above: after pulling it, recreate Traefik (`make up`, or `docker compose up -d`) before anything restarts it. A Traefik container created before the change still mounts the deleted `infra/traefik/traefik.yml`; restarting it (`docker compose restart`, a Docker daemon restart, a reboot) fails with a `not a directory` mount error and leaves a root-owned directory at that path, which `sudo rmdir infra/traefik/traefik.yml` removes. `make restart` now recreates such a Traefik before it restarts anything.
- Changed: access tokens are signed and verified with PyJWT instead of python-jose (#1186). Every service verifies through one helper, `herd_common.jwt_codec.decode_access_token`, which passes exactly one algorithm and a 10 s `leeway` (`ACCESS_TOKEN_LEEWAY_SECONDS`) for clock skew between the auth service and a verifying service: a token stays valid up to 10 s past its `exp`, and an `iat` up to 10 s in the future verifies (PyJWT refuses a future `iat`, python-jose did not). `enforce_minimum_key_length` stays off, so a key under 32 bytes still verifies tokens and PyJWT emits `InsecureKeyLengthWarning`; startup still logs the short key. The three decode sites (`make_auth_dependencies`, auth's `verify_access_token`, notifications' `get_bearer_credentials`) and the settings round trip in `herd_common.auth_settings` call the helper, catch `jwt.PyJWTError`, and keep their 401 `Could not validate credentials` and the `jwt_key_unusable` log line, now naming `InvalidKeyError`. Token format and claims are unchanged, so a session issued before the upgrade stays valid. One startup rule moved: a JSON-shaped `AUTH_SECRET_KEY` (an array, a string, or an object without a `kty` member) now loads as an ordinary HMAC secret, while a JSON Web Key (an object with `kty`) is still refused with `AUTH_SECRET_KEY cannot be used as an HMAC secret; ...`. The other key shapes jose refused are still refused with that message: PyJWT refuses a complete PEM block and an ssh key at the start of the value, and `herd_common.auth_settings` refuses the partial shapes PyJWT would accept itself, before the round trip (`_is_key_shaped`: any value containing `-----BEGIN `, so a lone BEGIN line or a truncated block, and any value whose stripped form starts with `ssh-` or `ecdsa-sha2-`); a value that cannot be encoded as UTF-8 (a lone surrogate) gets the same message instead of a codec error. The repository guard `tests/unit/test_jwt_settings_wiring.py` now reads PyJWT's spellings (`jwt.decode`, `decode_complete`, `jwt.api_jwt`, `jwt.api_jws`, `PyJWT()` and `PyJWS()` instances, `get_unverified_header`, the algorithm registry) and fails on a PyJWT decode anywhere but the helper and the config service's own session decode (`DIRECT_DECODE_ALLOWED`), each matched by file, qualified function path (`outer.inner`, `Class.method`), and the number of decode calls it holds. Dependencies: `pyjwt>=2.15.0` replaces `python-jose[cryptography]` in common, auth, and notifications and joins the `dev` extras of ai-orchestrator, cabling, integration, and secrets (which import it only in tests; herd-common brings it into every image), and acl, execution, inventory, reservations, and user-profile drop it; `ecdsa`, `rsa`, `six`, and `python-jose` leave `uv.lock`, while `cryptography` stays for secrets, netmiko's `paramiko`, and `pdfminer-six`. Test suites sign with a 64-character hex key, so no suite logs the short-secret warning any more.
- Fixed: a 401 or 403 from inventory no longer pins an existing switch's wiring rows as not found. Execution's `_fetch_device` and `_fetch_template` returned `None` for any answer below 500 other than 200, and every wiring layer reads that `None` as a switch (or template) gone from inventory: it recorded the switch's rows `FAILED` with `recorded hop unresolvable: switch <id> not found` and no attempt counted, a reason no retry channel drives, so a teardown or build that ran while inventory refused the internal token (an `INTERNAL_API_TOKEN` rotation) left an existing switch's rows pinned for good, and on an ended reservation its connections possibly still live on the hardware. Both reads now judge inventory's answer through one function, `_inventory_record_or_none`: only a 404 means not found, and every other answer that is not 200 (a 401, 403, 409, 422, or 5xx) raises `TransientUpstreamError` with the text `fetch device <id>: upstream <status>` (or `fetch template <id>: ...`), as does a 200 whose body is not a JSON object (`...: upstream answered with a malformed body`). The event is NAKed with backoff and no row is pinned: a `wiring_changed` records nothing for the switch and a terminal teardown leaves its `ACTIVE` rows as they were. In the retry tick, the rows of a reservation whose apply hit the error are left unchanged and counted in `skipped_upstream`, and a build row whose endpoint read fails during the intent re-check reads back `still_failed`; the manual retry answers 503. The rows are driven again once inventory answers, within the NAK window (five deliveries, about 81 seconds at the defaults; after that the event is dead-lettered and can be replayed). A dynamic instance's template read follows the same rule, so a 401 or 403 there is retried, where before a create dead-lettered the event as a missing template and a teardown left the instance's row live with `recipe_config_missing`. The consumer's other reads (a switch's latest config, a hypervisor, a secret) are unchanged (#1203).
- Fixed: no driver package file runs in the sandbox child before the resource limits apply. The child is now started as `python -I -B _runner.py ...` (Python's isolated mode, no bytecode written) with no `PYTHONPATH` or `PYTHONDONTWRITEBYTECODE` in its environment, and `_runner.py` puts the package directory and then `_deps` on `sys.path` itself (`_put_package_on_path`) only after `apply_rlimits`, right after its own directory and ahead of the standard library, the order `PYTHONPATH` gave. Before, the package directory was on `PYTHONPATH` from interpreter start, so a package `sitecustomize.py` ran at startup and a package `json.py` replaced the standard library for the runner's own `import json`, both with no limit applied (a probe saw `RLIMIT_AS` `(-1, -1)`); a package `sitecustomize.py` is now never imported, and a package `json.py` is never imported because the runner's standard library `json` stays in `sys.modules`. The optional `requirements.txt` install runs as `python -I -B -m pip install ...` for the same reason. A Python subprocess a driver starts itself no longer inherits the package path through `PYTHONPATH`; the driver must pass it explicitly. A driver that imports a sibling module, a `lib.<module>`, or a module vendored under `_deps` works as before (#1180, item 1).
- Tests: device configuration is now driven in a browser and under load (#1100). `tests/e2e/test_device_config_apply_playwright.py` runs four Playwright journeys on a device it creates on the checked-in `drivers/frr_mgmt` package, each read back through the API: Apply now (the failed run is answered, shown as `Apply failed: <error>`, and recorded as the version's `last_apply_run_id`), Schedule (the job is stored pending at the chosen time and shown in the Scheduled applies panel until the scheduler fires it and records the run), Cancel from the panel (the job reads back `cancelled`, never fired), and the dry-run review (it waits for the real dry run, shows the run's transcript, and Confirm queues a pending real job that the test cancels at once). The device page offers no dry run, so its two pushes go to a device with no `HERD_ip`, which netmiko refuses before it opens a socket; the review is reached through the reservation assistant with its status and turn answered in the page, under either assistant build. `tests/load/locustfile.py` gains `DeviceConfigUser`, which writes, reads, and diffs config versions, reads the driver's config schema through inventory, and lists apply jobs on one device of its own that it deletes at the end; it never applies or schedules.
- Fixed: one reservation whose inventory or fabric read fails no longer stops the background wiring retry for every other reservation in the same layer. A `TransientUpstreamError` raised inside one reservation's apply (inventory's read of a switch or its template, the L2 fabric lookup while re-resolving an allocation, the L3 route apply) escaped the layer's retry, so the tick left every selected row of that layer unchanged, release rows and the reservations it had not reached yet included: 20 release rows on a switch whose inventory read failed hid a newer row on a healthy switch on every tick. Each of the three layer retries now catches that error per reservation: only that reservation's rows of the layer stay as they were, with no attempt counted and no claim left, and the rest of the batch is driven, in selection order. The `wiring_retry_tick` log line gains `skipped_upstream`, the rows left unchanged this way across the three layers (also counted in `still_failed` and `rows_retried`), and each such reservation is logged once at `WARNING` with action `wiring_retry_reservation_upstream_failed` (`reservation_id`, `layer`, `error_class`, `rows_unchanged`; the error's class name only, never its text). The L2 retry also catches that error from its release of the allocations its superseded rows left orphaned, after it has read back its rows, so the outcomes stand; that failure is logged once with action `wiring_retry_superseded_release_upstream_failed`. An exception these catches do not take still drops the layer for the tick, and the manual retry still answers 503 for the same errors (#1204).
- Changed: `make everything` runs the ci.yml checks it used to skip: `uv lock --check` before `uv sync` (so a stale `uv.lock` fails the gate instead of being rewritten in place; `make master-quick`, and so `make master`, gains this check too), the frontend suite a second time under `TZ=America/Los_Angeles` (new target `make test-frontend-tz`), the `package-lock.json` sync check after the frontend build, and the image-versus-lock check on the freshly built ai-orchestrator image (new target `make check-image-lock`); `tests/unit/test_gate_ci_parity_wiring.py` pins each one. `make master-clean` now builds the gate stack's own images with `--no-cache` too (before, only the dev-project build skipped the cache), and its image purge removes this checkout's dev and gate images by compose project label instead of every `herd-*` image, so the NOS lab's built image and other checkouts' images are no longer deleted. `make clean` (and so the gates) no longer purges caches and coverage files under `.claude/`.
- Fixed: a `.tgz` context file uploaded to AI generate can no longer drive the ai-orchestrator process to gigabytes of memory from inside the entry and byte ceilings. More than 64 KiB of pax extended headers and GNU long names in front of one member is refused with 400 `The tgz archive has more than 65536 bytes of pax extended headers and GNU long names in front of one member; nothing was extracted`, more than 4 KiB of pax global headers in the archive with 400 `The tgz archive has more than 4096 bytes of pax global headers; nothing was extracted`, and a sparse member of any GNU format with 400 `The tgz archive has a sparse member, which is not supported; nothing was extracted`, each before the data that would have been held is read, and the walk no longer keeps every member it has passed. Before, a 250 KB upload of one pax global header of just under 1 MiB and 1,000 empty members peaked at 1.9 GiB resident (tarfile copies the global header's records into every member and kept them all), a 20 KB upload holding a GNU sparse 1.0 map of ten million numbers peaked at 976 MiB, and 2,000 small global headers in a 695 KB upload ran out of a 6 GiB address space; each is now refused in under a millisecond with no growth. `git archive` output (one 52-byte global header), PAX archives with long UTF-8 paths, and GNU archives of 10,000 members still extract the same text. A text member that the character budget cuts inside a UTF-8 character now decodes as UTF-8 up to the cut; before, the whole member fell back to Latin-1. A non-integer `GNU.sparse.realsize`, which tarfile would convert with a bare `int()`, is refused as a sparse member rather than escaping as a 500. A tgz whose gzip stream is whole but whose tar header is bad where a member's own header should be still yields the members before it, as it always did; a bad header that follows a pax or GNU long-name header is the 400 `ReadError`. Both header limits count each header's declared size rounded up to its 512-byte block, the bytes tarfile reads and parses (#1201).
- Fixed: the background wiring retry no longer stops for a whole ledger once that ledger holds a batch of `FAILED` rows it will never drive. The tick selected the oldest `FAILED` rows under the attempts cap and only then set aside the ones with a pinned not-retryable reason and the build rows of ended (frozen) reservations; setting a row aside does not change it, so `WIRING_RETRY_BATCH_SIZE` (20) of them at the old end of a ledger filled every batch and no newer retryable row in that ledger was retried by any later tick. The two tests now sit in the three due queries as one shared predicate (`wiring_due.tick_would_drive`: the reason prefixes compared case-sensitively, an absent or empty reason retryable, a reservation with no wiring state row not frozen, release-direction rows of a frozen reservation still selected), so a batch holds no row those two rules set aside. A build row whose intent cannot be read while cabling or inventory is unreachable is still selected and left unchanged with no attempt counted, so until that upstream answers again it can delay newer rows in its ledger; that is a known limit by decision. No row is rewritten and there is no migration. The `wiring_retry_tick` log line gains `excluded_not_retryable` and `excluded_frozen`, the `FAILED` rows under the cap that the selects leave out across the three ledgers, and stays at `INFO` while either is above zero; `skipped_not_retryable` and `skipped_frozen` keep their meaning (rows selected and then set aside by the same two rules, which now happens only to a reservation frozen after the select) and are normally 0. The manual retry and its outcomes are unchanged (#1190).
- Fixed: a `.tgz` context file uploaded to AI generate is walked within two fixed ceilings, and the extraction of uploaded files runs in a worker thread instead of on the service's event loop. An archive of more than 10,000 members is refused with 400 `The tgz archive has more than 10000 entries; nothing was extracted` at the member past the ceiling, and one the reader would have to read or skip past 100 MiB of, decompressed, with 400 `The tgz archive extends past 104857600 bytes uncompressed; nothing was extracted` before the tar reader reads the data past the ceiling (the gzip layer decompresses up to its 8 KiB read buffer ahead of the position); the bound is on the decompressed stream position, so member data, skipped members, pax extended headers, and GNU long names all count. Before, an 894 KB upload of 400,000 empty members took 7.7 s and raised the ai-orchestrator process from 57 MiB to 226 MiB of resident memory, and a 1 MiB upload holding one skipped member of 1 GiB of zeros took 2.1 s, with every other request to the service waiting, `/health` included; they now take 0.2 s and under a millisecond. A tgz with several hundred pax extended headers or GNU long names in a row, which makes Python's tar reader recurse past its limit, answers 400 `Failed to read tgz archive (RecursionError)`; before, a 1.8 KB upload of 400 consecutive pax headers was an unhandled 500. An archive under both ceilings yields the same text as before (#1195). A tgz member name that is not valid UTF-8 enters the extracted text with U+FFFD in place of each undecodable byte; before, the name carried a lone surrogate, the provider request could not be encoded, and the request answered 502 `AI returned no usable response` without reaching the provider (#1196).
- Fixed: a `.tgz` context file uploaded to AI generate whose pax extended header or GNU long name is over 1 MiB is refused with 400 `The tgz archive has a tar header record over 1048576 bytes (a pax extended header or GNU long name); nothing was extracted` before the header is read into memory. Before, a 65 KB upload declaring a 64 MiB header made the ai-orchestrator process allocate about 128 MiB, and any signed-in user could send one when a provider is configured. The extractor reads the archive through `herd_common.bounded_tar`, as the two driver readers do, and reads each text member in 64 KiB chunks, so a member larger than 1 MiB still extracts and an honest archive yields the same text as before. A tgz that is truncated, holds corrupt compressed data, or carries a checksum the reader finds wrong now answers 400 `Failed to read tgz archive (ReadError)`, as a body that is not gzip always did; before, those three escaped as a 500 (#1192).
- Fixed: the driver upload reads `driver_metadata.json` within a bound: an entry larger than 1 MiB, by its declared size or by what a capped read returns, is refused with 422 `driver_metadata.json is larger than 1048576 bytes; nothing was stored`, and a `.tar.gz` whose pax extended header or GNU long name is over 1 MiB is refused with 422 before the header is read into memory. Before, a small, highly compressible metadata entry or header expanded whole in the inventory process. Nothing is stored on a refusal, on create or on file replace. The bounded tar stream moved from execution into `herd_common.bounded_tar`, which both services use; execution's refusals and their wording are unchanged (#1180, item 3).
- Fixed: a `driver_metadata.json` that is not UTF-8, or is nested deeply enough that the JSON parser raises `RecursionError`, reads as no capability at load, as a malformed file already did, with a warning naming only the exception class; before, the error escaped the load as a transient failure, so a package that can never change was retried forever (#1189).
- Changed: every service that checks access tokens validates its token settings when its Settings model loads, through one shared module, `herd_common.auth_settings`. An `AUTH_SECRET_KEY` that is blank (empty, white space, or only invisible characters such as a zero-width space) or a placeholder (any value that, trimmed, starts with `<` and ends with `>`, such as the `.env.example` line) is refused with `AUTH_SECRET_KEY is unset, blank, or still a placeholder; set it in .env, or in the config UI if a configuration was saved there, to a random value, for example the output of openssl rand -hex 32`; under compose a line missing from `.env` arrives blank and gets the same message, while a service process started outside compose with no `SECRET_KEY` at all fails with pydantic's `secret_key Field required`. A value jose cannot sign and verify an HS256 token with (a PEM public key, certificate, or ssh public key, or a JSON-shaped value) is refused with `AUTH_SECRET_KEY cannot be used as an HMAC secret; set it in .env, or in the config UI if a configuration was saved there, to a random value, for example the output of openssl rand -hex 32`. A value under 32 bytes starts and logs one warning giving its byte length. `AUTH_ALGORITHM` accepts only `HS256`, `HS384`, or `HS512` (`AUTH_ALGORITHM must be one of HS256, HS384, HS512`); the RS* algorithms `docs/ENV_VARS.md` listed are no longer accepted. ai-orchestrator no longer carries a built-in secret default, and a Settings load error no longer prints the refused value. The config service's save (`PUT /settings`) and first-boot bootstrap refuse a blank or placeholder `AUTH_SECRET_KEY` and another `AUTH_ALGORITHM` with the same messages and write nothing, since a saved `config.json` outranks `.env` (OPS-CONFIG-23). The three token decode sites (`make_auth_dependencies`, auth's `verify_access_token`, and notifications' `get_bearer_credentials`) answer 401 for any error jose raises, a key error included, and log a key error once at ERROR by class name (`jwt_key_unusable`). A repository guard, `tests/unit/test_jwt_settings_wiring.py`, checks that every jose decode passes exactly one algorithm with signature verification on, following names across the scanned modules, and that every service's Settings model uses the shared types (IAM-JWT-4, IAM-JWT-5). Upgrade note: a stack whose `AUTH_SECRET_KEY` is unset, blank, still the placeholder, or not usable as an HMAC key now refuses to start, naming the variable; if a configuration was saved in the config UI, fix the value there. `AUTH_ALGORITHM` must be `HS256`, `HS384`, or `HS512`.
- Tests: CI's `frontend` job runs the frontend unit suite a second time with `TZ=America/Los_Angeles`, a zone that observes daylight saving time, so a date bug that only shows outside UTC fails a pull request; the repository guard `tests/unit/test_ci_frontend_tz_wiring.py` requires that step to set `TZ` in its `env:` to a non-UTC zone with daylight saving time and to run the same vitest command as the UTC step.
- Fixed: a `driver.py` the parser itself cannot get through (a very long or deeply nested expression, which raises `RecursionError` or `MemoryError` inside `ast.parse`) is a permanent load failure, `driver load failed: DriverPackageError`, like any other unparseable `driver.py`. Before, it was classed as transient, so wiring retries and recipe redeliveries kept loading it, and each attempt left its extraction directory in the driver cache; a failed load now removes the directory whether a package check returns errors or raises (#1178).
- Fixed: a `.tar.gz` driver package whose pax extended header or GNU long name declares more than 1 MiB is refused with `driver load failed: PackageLimitError` before the header is read into memory; before, a 389 KB upload declaring a 400 MB header raised the execution process's memory by about 500 MiB (#1179).
- Fixed: a driver's required method must be bound to something that could be callable: `status = None`, a string, number, or container literal, an annotation with no value (`status: object`), and a `def` removed by a later `del` no longer pass the load and then fail every health poll in the sandbox. A binding under a condition or in a `for` target still passes by decision (#1179).
- Fixed: the recipe validator reads and parses each package file once, as bytes, so its structural and policy sections agree on a `driver.py` with a coding cookie, and an extraction failure reports `Failed to extract package: <ClassName>` without the temporary path a tar filter error carried (#1179, #1180).
- Tests: the repository guards refuse inputs their Limits did not name (#1181). The integration teardown guard finds a device, port, or topology delete whose URL carries a query string or fragment, is built by `+=`, is a `for` loop or comprehension target, or is unpacked from a tuple, or that goes through a name bound to `getattr(client, "delete")` or a lambda wrapper, and fails an exempt function whose count shrank as well as one whose count grew. The Postgres-live gate guard refuses any `exit` or `exec` word in the recipe ahead of a suite or of a gate's call, an honest `|| exit 1` included; a `;` after a `fi`, `done`, or `esac` that closes around a suite; a variable assigned on a suite command other than `HERD_TEST_PG_REQUIRED` and `HERD_TEST_PG_DSN`; `PYTEST_ADDOPTS` on any make-level line of the Makefile outside a comment; a target-specific `SHELL` for the phase or a gate; and any assignment or `-C` on a gate's call. A recipe line with an unterminated quote fails with a message naming the line. The execution consumer start test pairs `add_consumer` and `pull_subscribe` by durable name, so it fails when the durable bound is not the one created. The heartbeat guard counts a `nonlocal` rebinding and a `match` capture as a second binding. The specification guard refuses a fixture named `test_*`, an `it.fails` or `test.fails` title, and a call that starts inside a string or template literal. The compose guard's `.env.example` check requires each active `AI_*` key to be set on the same container key to exactly `${KEY}` or `${KEY:-default}`. Each guard's module docstring lists, under Limits, what its scan still cannot see, now including the teardown scan reading names without regard to statement order and its dropped-answer check seeing only a bare expression statement, and OPS-NATS-12 names the `ack_wait` settings the heartbeat scan reads.
- Tests: three Playwright e2e tests no longer wait on a toast an earlier action left on screen. The fork restore-then-save test orders its read-back by the save request's own HTTP response (`pw_click_for_response` in `tests/e2e/conftest.py`) and asserts the fork save toast by its exact version. The config edit-and-restore test orders its read-back by the settings PUT response and, before saving a second time, waits for the earlier `Configuration saved` toast to clear (`pw_toast_gone`). The device config version cycle orders each version create by its POST response and waits for the earlier `Config version created` toast to clear before the second create; its read-back after Restore is ordered by a poll until the version count reaches three. The fork test lost that race in the 2026-10-11 nightly with `assert 2 == 3`; the product had saved the version correctly (#1169).
- Tests: the dynamic-resources integration test for a failed keyed destroy reads the ledger row after an ordering anchor (a later dynamic reservation reaching `ACTIVE`, which the one sequential consumer handles only after the teardown event) and no longer after a 3 second sleep, and checks that the failed destroy was not driven a second time (#1158).
- Tests: integration tests send every device and port a test deletes in its own teardown through the checked teardown helpers, which fail the test on a refused delete; `delete_devices_checked` and `delete_port_checked` join `delete_device_checked`, and four tests whose cleanup relied on the test body's own cancel now cancel the reservation first. A repository guard, `tests/unit/test_integration_teardown_contract.py`, fails on a device, port, or topology delete in `tests/integration/` that bypasses the helpers, under the call and URL spellings its module docstring lists (it follows a URL through one local wrapper and does not resolve one built from data it cannot see), with a per-function allowlist for the tests whose subject is the delete's answer (#1158).
- Tests: the VLAN integration test for a cable change between two joins forces the collision: reservation A's stored allocation is moved onto reservation B's preferred number before B joins, and the two stored fabric ids are read back as different, so a rule that compares stored fabric ids fails it (#1158).
- Tests: the wiring retry pollers of the L1, L2, L3, and ledger-teardown integration tests share one helper that, when the retry answer no longer names the row, accepts the row the test saw `FAILED` from wiring-status by id (`ACTIVE` or `RELEASED`), so the background retry tick converging it first no longer times the test out; a release that converged must also show a `SUCCESS` run of its release action, so a row settled with no driver call is not taken for a driven release. The helper and the two teardown helpers are pinned stack-free in `tests/unit/` (#1158).
- Tests: five more repo-root guards read the behavior they name instead of text near it. The Postgres-live gate guard counts a suite only when the `_gate-pg-live-tests` recipe holds a pytest command that names the file, runs in the suite's service directory, carries `HERD_TEST_PG_REQUIRED=1` on that command, passes no argument outside a short list of options that cannot narrow or skip a run, and sits where its failure fails the recipe with no `exit` ahead of it (a name in a comment, on a commented-out line, in an `echo`, or ahead of `|| true` no longer counts); it also checks that `master` and `everything` each run the phase through `$(MAKE)`, `$(SUBMAKE)`, or `make` with no option that stops it or hides its failure. The consumer heartbeat guard requires a call to the shared `process_batch_with_heartbeat` with `heartbeat_interval` of the configured ack wait, at least one place that sets `ack_wait`, and the configured value at every `ack_wait` setting its scan reads. The build-args guard requires each `ARG` once and one `ENV` after both in the stage that ships, and no `RUN`, `COPY`, or `ADD` after them. The compose settings guard requires a field's whole value to be exactly `${VAR}` or `${VAR:-default}` under its own variable or a recorded rename (`DATABASE_URL` must interpolate its three `POSTGRES_*` variables), not any `${`. The specification guard requires a cited Python test to be a `test*` function that pytest's default rules collect from a `test_*.py` or `*_test.py` file, a cited frontend test to be the complete title of an `it`, `test`, or `describe` call in a `*.test.*` or `*.spec.*` file that is not commented out, `.skip`, or `.todo`, and an enforcing symbol to appear outside comments. Each guard's module docstring lists, under Limits, what its scan cannot see and what it refuses conservatively. Each false-pass spelling is a detector case, the current tree passes every scan, and one specification citation that named a title prefix is corrected (#1158).
- Tests: the notifications, integration, and execution consumer start tests read `add_consumer` and `pull_subscribe` from one ordered call log, so they fail when a durable is bound before `ensure_consumer` creates or updates it, and the notifications and integration tests assert the stream each durable is created on; separate per-call lists passed in either order, and a membership check passed with the two streams exchanged (#1158).
- Docs: README.md states the measured backend unit test count (over 7,900 across the 13 services and the repo-root suite), OPS-NATS-6 cites the three consumer start tests that pin `ensure_consumer` ahead of `pull_subscribe`, and the teardown guard entry above names the limits its module docstring states.
- Docs: OPS-NATS-12, AI-STREAM-5, and the #1070 entry describe the repository guards as #1145 widened them, ADR 0011 gains an as-built amendment for the sync-now concurrency test (#1134), and README.md and GAPS.md carry the current e2e, integration, and Postgres-live suite counts.
- Tests: inventory tests whose input an earlier check refused now reach the rule they name: the template-validation HTTP tests send a port template valid except for the field under test and assert the exact 422 message, the non-admin device list test grants both devices so only the forced `dut_only` hides the infrastructure one, and the two apply-scheduler gate tests stub the creator re-check and assert `reservation not currently active` (#1138).
- Tests: nine ai-orchestrator tests now fail under the regression they name: the tgz budget break, the apply-jobs renderer reading only count and names, the pathfind client failing closed on a transport error, a non-200, a non-JSON body, and a missing `results` list (new client-level cases plus a route test through the real client), the buffered 504 leaving no conversation or message row, the JSON extractor's indent and key order, the committer refusing a non-boolean `valid`, oldest-first history eviction, transcript truncation dropping the oldest turn, and the status probe recovering only after its cache window (#1143).
- Docs: the specifications say which spellings the heartbeat and cancel-scope guards match, that the login form stays enabled while the config status is loading or unreachable, and `docs/GAPS.md` lists the three suites no GitHub workflow runs (#1144).
- Tests: the frontend absence checks wait for pending work before asserting nothing happened, so the five "Cancel issues no delete" page tests (device group detail, device groups, grants, groups, inventory bulk delete) fail when Cancel also deletes, and `ConfirmDialog`'s own test pins that Cancel and Escape never call `onConfirm`. The AI assistant streaming test reads the live bubble mid-stream, the recipe panel close test reads every field back, the Routing panel and `useDeviceConfigVersion` pin the lazy config fetch, the grants filter test checks a partial id is never sent, the stale routing problem test reuses the node id across topologies, the topology delete failure test reads the dialog's `open` state, the fork restore test restores from inside a preview, the auth store tests import a fresh store, and `getWeekRange` asserts local calendar fields so it passes in every time zone (#1142).
- Fixed: a reservation that lands `FAILED` notifies its owner on every channel they enabled: the message is titled `Reservation failed` and names the reservation by its short id and device count, never a failure reason. The event is on by default, including for users whose saved preferences predate it, and the Settings page gains a `Reservation failed` toggle (#1077).
- Added: an admin can pause and resume a webhook subscription with `PATCH /api/v1/webhooks/{id}` and the body `{"is_active": false}` or `{"is_active": true}`, keeping its delivery ledger; any other field is 422. A paused subscription receives no later event and nothing is replayed on resume (#1078).
- Fixed: the notifications webhook channel signs through `herd_common.webhooks`, the helper integration's registered webhooks use, instead of a local copy; the bytes, headers, and signature a receiver sees are unchanged (#1079).
- Fixed: the test webhook sink, served only when `WEBHOOK_TEST_SINK_ENABLED` is on, refuses a POST that came through the gateway with 403, and its hit count requires an admin token, so it adds no unauthenticated route to `/api/v1` (#1107).
- Tests: the webhook ledger's dead-row retry, its concurrent-insert race, the subscription delete cascade (on Postgres and on SQLite with foreign keys on), and the preferences write that replaces `channels` whole are pinned (#1081).
- Tests: the reservations tests for the extension's non-exclusive skip, its inventory-failure fallback to all-exclusive, and the purpose reconciler's oldest-first drain now arrange a competing booking or a reversed insertion order that only the named rule resolves, and the Postgres PATCH race records an inventory write when it lands, so only the PATCH's own revert can leave the added device `AVAILABLE` (#1136).
- Tests: the notifications channel-isolation test drives `handle_event` with the production dispatchers and a transport error on email, chat, or webhook in turn, and the missing `device_name` test checks the exact title and body (#1135).
- Fixed: a scheduled config apply that names a `reservation_id` is refused with 422 unless that reservation is `ACTIVE`, holds the device (reservations' internal by-device list), and, for a non-admin, belongs to the caller; an admin is exempt from ownership only. Before, a caller who owned any active reservation of the device could name an unrelated one, and the job's fire-time check then followed the wrong reservation. An unanswerable check is 503 `Could not verify the reservation; nothing was scheduled. Retry the request.` (#1104).
- Tests: pins for device-configuration rules that had none: the write routes skip device visibility, the default restore label, no version delete or edit and the cascade from a device delete, the schedule's time checks first, the exact dry-run support refusal, a confirm that repeats no schedule-time check, and `POST /execute` passing ports, arguments, and the dry-run flag as sent; plus an integration test that schedules a dry run on `drivers/frr_mgmt`, waits for the scheduler to fire it, reads the simulated transcript, and confirms it. The dev and gate stacks run the apply scheduler every 2 seconds (#1100).
- Fixed: execution checks a driver package's `Driver` class and required methods by parsing `driver.py`, never by importing it, so no package code runs in the execution process; the load path and the recipe validator share one check (`driver_structure_errors`). `Driver` must be a plain top-level class statement (an assignment, an import, or a nested class is refused); methods inherited from a top-level base class in `driver.py` count, and a method that could come from code the parser cannot read is left to the sandboxed call. A `driver.py` whose top-level code raises now fails its first driver call instead of the load (#1114); for a dynamic-resource recipe that is `login`, so the event is retried and dead-lettered at the fifth delivery rather than the first (#279). The parse-based check is not the import-based one in either direction: it refuses a method that exists only because it was assigned onto a base class or added by rebinding the class (`Driver = add(Driver)`), and accepts a method bound only in code that never runs, a decorated or `setattr` class whose methods are missing, and a `driver.py` that parses but does not compile, each of which then fails its first sandboxed call (#1179).
- Fixed: extracting a driver package refuses an archive with more than 10,000 entries or more than 100 MiB declared uncompressed before writing anything, for `.zip` and `.tar.gz` alike, so a small, highly compressible upload can no longer fill the driver cache volume; the load records it as `driver load failed: PackageLimitError` (#1115). The ceilings bound disk, not memory: a tar's pax or GNU long-name header was still read whole into memory until #1179.
- Fixed: the config login limits failed attempts. From a source address's third consecutive failure it waits before its next attempt (1 second, doubling to 60 seconds), and `CONFIG_LOGIN_MAX_ATTEMPTS` failures (default 20) from any sources within `CONFIG_LOGIN_LOCKOUT_SECONDS` (default 300) make every source wait that long; a waiting login is answered 429 with `Retry-After`, a success clears the counts, and each wait that begins logs one `config_login_locked` line with the scope and source address.
- Fixed: the config page's `PUT /settings` writes only keys `CONFIG_SCHEMA` declares: a body naming any other key is 422 `Unknown settings: <sorted keys>` and writes nothing; `GET /settings` shows schema keys only, and a key placed in `config.json` by hand outside the schema is carried over unchanged by a save (#1109).
- Fixed: the config service's CORS policy keeps its wildcard origin and no longer allows credentials, so no origin is answered with `Access-Control-Allow-Credentials` (#1111).
- Fixed: webhook destinations must resolve to public addresses. A `target_url` whose host resolves to a loopback, link-local, private, shared, multicast, or unspecified address, or does not resolve, is refused at registration with 422 `target_url must resolve to a public address` and is checked again before every delivery (a `failed` ledger row with `destination not allowed`, nothing sent). The new `WEBHOOK_ALLOWED_HOSTS` setting (hostnames or CIDRs, empty by default) admits named internal destinations. Redirects are not followed, the ledger's `last_error` carries only `upstream answered HTTP <status>` or `delivery failed (<ClassName>)`, and the address predicate is shared with the assistant's documentation fetch through `herd_common.public_address`.
- Fixed: the `/api/v1/reservations/{reservation_id}` routes validate the id as a UUID, so a malformed id is a 422 from the facade and never reaches reservations as a different route; the published contract `docs/api/v1-openapi.json` gains `format: uuid` on the parameter (#1105).
- Fixed: a non-admin reservation owner's execution run list (`GET /runs?reservation_id=`) and run transcript read (`GET /runs/{id}/commands`) include only runs on devices the caller can see under device-group visibility, resolved once per request from inventory with the caller's token; a run on any other device answers the transcript read exactly as an unknown run, and a lookup inventory cannot answer is 503 with no rows. Admins are unfiltered.
- Fixed: a driver method's keyword arguments reach the sandbox child through an owner-only temp file instead of the command line, and an execution run stores them masked (credential-named keys and the text after credential keywords such as `password`, `secret`, `community`, `key`, and `md5` in command strings). Inventory's apply paths send the config version id, and a retry of a masked run reads the configuration back from that version, refusing with 409 when the run names none or the version no longer matches.
- Fixed: `GET /device-health/{device_id}` answers a non-admin with a device's snapshot only when the device is inside their device-group visibility; any other device answers byte for byte as an id with no snapshot, and an unanswerable lookup is 503 (#1108).
- Fixed: a manual `POST /execute` that names a `reservation_id` is refused with 422 before anything runs unless the reservation holds the device and, for a non-admin, belongs to the caller, so a run is never filed under a reservation that did not hold its device (#1112).
- Fixed: a non-admin `configure` on execution's `POST /execute` whose acl check answers 200 with a JSON body that is not an object (a list, a string, a number, or null) gets the pinned 403 `Admin access or device manage grant required` instead of a 500; execution now asks acl through `herd_common.acl.user_has_grant`, the one closed-by-default reader (execution half of #1096).
- Fixed: confirming a dry-run config apply (`POST /apply-jobs/{id}/confirm`) checks the caller's authority on the job's device before the two 409s, so a caller without authority gets 403 whatever the job's kind or status and learns only that the job exists; an unknown job is still 404 first (#1113).
- Fixed: the device export, CSV and JSON, leaves out dynamic-instance devices (devices carrying a `request_id`), which another instance rejected with `Template is not a device template` and a re-import on the same instance wrote back over the live instance device; every other device still exports and round-trips (#1068).
- Fixed: the config page answers a 429 from the login attempt limit with `Too many failed login attempts; try again in N seconds` (the server's sentence when `Retry-After` is missing or not a whole number) and keeps Sign in disabled for that wait, instead of the wrong-password toast; a 401 still reads `Invalid config password` (#1126).
- Fixed: in a live reservation edit, a line the fork's draft autosave reports in `invalid_edges` turns red on the canvas with the reason on hover (for example `no cable path on the chosen ports` for a line on two cabled ports no cable joins), and a bundle holding such a line goes red; the mark clears when a later autosave answers otherwise or the canvas is replaced, a commit refreshes it from `constrained_edges_skipped`, and it does not block the commit (#1066).
- Tests: the cabling write-boundary scrub tests read the stored fork, topology, version, and template rows instead of a GET that strips again, and seed dirty rows directly for clone, export, fork create, and instantiate; the topology version-retry tests lose a real race after the max read and prove the retry re-applies name, canvas, and modified_by; the version cascade test counts rows with SQLite foreign keys on; and the connection, topology, pathfind, fabric, and port-cable seeds disable the device-group guard explicitly instead of passing through its fail-open branch (#1139).
- Tests: the config service's Save and Restart skip list is pinned against a literal list of the five containers, and `restart_services` is shown to skip each one beside a restarted service (#1140).
- Tests: the CORS origin parsing test drives `add_cors_middleware` and reads the origin list it registered, and the `DATABASE_URL` precedence test builds its source over a settings class that declares `database_url`, with a control that the derived URL appears when the variable is unset (#1141).
- Docs: `docs/OPERATIONS.md` describes `/health` as a liveness answer that checks no dependency, a Postgres outage and a wiped config volume as the services actually handle them, and the `nats-data` volume; `FEATURES.md` and `docs/ENV_VARS.md` say the config service logs plain text, WARNING and higher only (#1087).
- Docs: `docs/DRIVERS.md` records a timed-out driver run as `TIMEOUT`, `docs/ROLES.md` lists the run list's `created_after` and `created_before` filters, the config-schema extraction docstring states what its subprocess bounds, and ADR 0002 gains an as-built note on the proxy's access and the driver cache (#1099).
- Docs: `docs/ROLES.md` lists every `/api/v1` and `/api/notifications` route by role, and `docs/ARCHITECTURE.md` names the facade's `GET /reservations/{id}/wiring-status` (#1080).
- Docs: `docs/OPERATIONS.md`, `docs/ENV_VARS.md`, and `SECURITY.md` state that the generated config-page password written to the container log stays valid until it is changed (#1110).
- Docs: README.md, FEATURES.md, SECURITY.md, ARCHITECTURE.md, DRIVERS.md, ENV_VARS.md, OPERATIONS.md, TROUBLESHOOTING.md, ADMIN_HANDBOOK.md, and ADR 0003 describe the 2026-10-08 fixes as built (the bounded NATS connect and the shared event decoder, notifications caches that keep only answers, secrets key rotation across replicas, Save and Restart errors by class name, the per-load driver cache, the immediate apply's `manage` rule, scheduled-apply transcripts, and dry-run retries).
- Docs: ROLES.md, ADMIN_HANDBOOK.md, AI_ASSISTANT.md, ARCHITECTURE.md, DRIVERS.md, ENV_VARS.md, OPERATIONS.md, TROUBLESHOOTING.md, EXTERNAL_API.md, IMPROVEMENTS.md, FEATURES.md, SECURITY.md, `.env.example`, and ADRs 0002 and 0015 describe the later 2026-10-08 changes as built (execution run reads and device health limited to visible devices, masked run arguments and the retry 409s, manual execute's reservation scope, the config login limits and schema-only saves, the webhook destination rule and the upgrade step it needs, VLAN delete skips cabling cannot judge, topology import dry runs, and the device export leaving out instance devices), and TROUBLESHOOTING.md gains entries for each new refusal.
- Docs: README.md, FEATURES.md, ENV_VARS.md, OPERATIONS.md, TOPOLOGY_EDITOR.md, USER_GUIDE.md, and TROUBLESHOOTING.md describe the 2026-10-09 changes as built (webhook pause, the failed-reservation notice and every default-on event, the test sink closed to the gateway, the config page's login wait, which red lines block a live-edit commit, the scheduled apply's reservation rule and the schedule route's reservation reads, and the dev apply scheduler interval), README.md names the payload `event_id` as the outbound dedupe key, and TROUBLESHOOTING.md gains an entry for a live-edit line on unjoined ports.
- Tests: four repo-root guards catch every spelling CLAUDE.md says they catch, not only the one that caused the original bug: an own NATS heartbeat under an import alias or as a hand-rolled `in_progress` loop; a `yield` inside a timeout scope reached through `import asyncio as aio` or a scope bound to a variable; a raw request body tested for a substring across two lines, through `str()`, `.find()`, or `.startswith()`, or in a nested `tests/e2e` directory; and a `COPY . ./`, `COPY --chown=... . .`, `ADD . .`, or `COPY . /app` ahead of `npm ci` in the frontend Dockerfile. Each missed spelling is a detector case, and the current tree passes every widened scan (#1145).
- Tests: auth tests now let the rule they name decide the outcome: directory group removal suppression is pinned for a `missing_username` skip as well as `missing_email`, the concurrent refresh test races two refreshes inside one read-to-consume window, the local-login refusal of an LDAP account is tested with a real password hash, the paged email enumeration asserts the cookie it sends back, and the blank and partial `SUPERADMIN_*` seed tests read the database the seed writes to; a test that asserted a set literal is removed (#1146).
- Tests: execution tests that never reached the branch they name now do: a stale validation stamp on a route the current config refuses drives nothing, the route ledger's FAILED-to-ACTIVE race runs two real `record_route_active` calls, the VLAN allocation's IntegrityError retry is reached by a competitor committed inside the race, a crashing health poll is shown not to back off the tick, a stale `wiring_changed` version is a no-op against a non-empty intended set, a 5xx ownership answer is a 403, and create failures are shown to NAK rather than dead-letter (#1137).
- fix(execution): a ledger write that loses the FAILED-to-ACTIVE compare-and-swap to a concurrent writer (`record_l1_connect`, `record_l2_membership_active`, `record_route_active`) returns the winner's row and logs `stale reattempt writer skipped`, as documented, instead of raising `MissingGreenlet` while re-reading a row its rollback had expired; the stored row was never harmed (#1152).
- Tests: integration checks that held without the effect they name now prove it. Absence reads wait for an ordering anchor (a later wired or dynamic reservation, the reservation's own `expiring_soon` notification, a second health event, or execution's `last_applied_fork_version` stamp) instead of a fixed sleep; the wiring retry pollers return nothing unless the wanted outcome was seen; the ACL tests build a group holding the integration user and read `allowed`; the VLAN uniqueness test forces two reservations onto one preferred number; the stale-replay test delivers its replay while the ledger and the intended set disagree; and the facade release, admin visibility, device-existence, webhook event-type, topology import, commit, execute-scope, and JWT expiry tests assert the exact answer (#1147).
- Tests: Selenium tests that a static label satisfied now read the element the behavior changes: exact toast text for the config, connection, and driver refusals (the driver one also reads the driver count back), the superadmin role cell, the inventory filtered empty state, the palette search removing one device's card, the active tab and the Details panel, and the calendar header label; the LDAP login test fails instead of skipping and defaults to `ldapit-eng1`; the live-edit and reservation detail tests open their own row; the clone test reads the clone back by name; two hollow duplicates are removed; and two vitests pin the palette's `search` parameter and the calendar day view (#1148).
- Tests: the LDAP sync-now concurrency test accepts the second request being refused or accepted after the first run ended, and in the second case proves the two runs did not overlap; it no longer fails when a small run finishes before the second request arrives (#1134).

- Fixed: reservations, execution, notifications, and integration start without a NATS broker. The first connect is bounded (`herd_common.jetstream.connect_nats`, five retries two seconds apart) and then raises into each service's logged-and-continue branch; an established connection still reconnects without limit (#1083).
- Fixed: a NATS event whose body is JSON but not an object (a list, a string, a number) is dead-lettered and acknowledged by the execution, integration, and notifications consumers, as undecodable JSON already was, instead of escaping unsettled and being dropped after the delivery cap with no DLQ copy (#1074).
- Fixed: a secret delete whose inventory reference lookup answers 200 with a body that is not a JSON list of objects answers the fail-closed 503 and keeps the secret, instead of a 500 (#1084).
- Fixed: a data-key rotation in one secrets replica binds every other replica with no restart: a key version a process has not seen is loaded from the key table on first use, and writes use the newest version in the table. Rotations are serialized by an advisory lock, a collision answers 409 instead of 500, and an unloadable key version answers a fixed 503 (#1085).
- Fixed: the config service's Save and Restart reports a Docker failure by the exception class only; the exception text goes to the log (#1086).
- Fixed: the notifications service no longer caches a failed admin-list, preferences, or contact lookup. The fallback covers only the event that hit the failure and the next event asks again, so an outage no longer silences admin health notifications for a minute or ignores an opt-out for 30 seconds (#1075).
- Fixed: config version numbers stay unique per device under concurrent writes: the unique `(device_id, version_number)` index is declared on the model so a fresh schema has it, migration 0023 creates it where missing (renumbering any duplicates above the device's maximum), and a create or restore that collides retries, answering 409 after five attempts (#1095).
- Fixed: cancelling a scheduled config apply is a compare-and-swap on a pending row, the same guard as the scheduler's claim, so a cancel that loses to a claim answers 409 with the status it found and a claim that loses fires nothing (#1088).
- Fixed: the stale-running sweep measures a running apply job from its claim (new `claimed_at`, migration 0024) instead of its scheduled time, so a job claimed late is no longer re-queued and fired a second time while its claimer is still firing it (#1089).
- Fixed: the immediate and scheduled config applies judge execution's answer by one rule (success only for a JSON object whose run status is `SUCCESS`) and move the device's current config pointer through one helper, so a scheduled real apply that succeeds now moves it and a dry run never does (#1094).
- Fixed: a scheduled config apply tags its execution run with the job's reservation, so the reservation owner can read the dry-run transcript and find the run in the reservation's run list (#1090).
- Fixed: the immediate config apply requires an explicit `manage` grant from a non-admin, the same rule execution applies, and refuses anyone else up front with 403 `manage grant required on this device for an immediate apply (a reservation owner can schedule the apply instead)`; a reservation owner without a grant schedules the apply instead (#1092).
- Fixed: a 200 whose JSON body is not an object from acl or reservations during a device-config permission or reservation check, or from execution during a published-schema read, is treated as an unusable answer (no grant, a 503 at schedule time, a skipped job, the registry schema) instead of raising into an unhandled 500 or a `scheduler crash` (#1096).
- Fixed: a failed config apply stores and returns `execution service unreachable (<ClassName>)` or `execution answered HTTP <status>` (plus the message of a structured refusal such as `driver_cannot_configure`) instead of exception text or execution's raw answer; the raw text is logged (#1093, inventory side).
- Fixed: retrying a failed or timed-out execution run repeats its mode: every run records a boolean `dry_run` in `input_params`, a retried dry run is called with `dry_run=True`, and a run written before that record existed is refused with 409 instead of being retried for real (#1091).
- Fixed: two first loads of one driver at once both succeed: each extracts into its own directory and the cache row is written insert-or-nothing and read back, so the loser adopts the winner's row; and a run interrupted by an unexpected exception ends `FAILED` with `execution failed: <ClassName>` instead of staying `PENDING` or `RUNNING` (#1097).
- Fixed: execution's 503 for a device or template it cannot read from inventory carries an upstream status, a class name, or a malformed-body note instead of the exception text with the internal URL, and a 200 whose body is not a JSON object is that 503 too (execution half of #1093).
- Fixed: the device page's configuration section shows a refusal in words: Apply now and Schedule show the `driver_cannot_configure` 409's sentence and the driver's name, a refused restore lists up to three blocking reservations, and any plain-string detail (a 403, a 422, the fail-closed 503) shows as given, instead of `Apply request failed`, `Restore failed`, or an object passed to the toast. The Apply dialog and the scheduled-applies panel gain tests (#1098).
- Fixed: the notification bell's Delete is a sibling of the notification's own button instead of a button nested inside it, so each is a separate keyboard stop with its own name, and the notification list query no longer runs for a signed-out visitor (#1076).
- Fixed: a topology import dry run is a full rehearsal: every row runs through the same create and update path as a committing import inside a transaction that is rolled back, so a new topology named twice in one file reports `create` then `update` on a dry run, as the commit does, and the dry-run report matches the committing report row for row (#1064).
- Fixed: when a freed VLAN number's allocation is released, `delete_vlan` is skipped only when another live allocation holding the number reaches the current cabling component of a switch the number is defined on, judged as allocation judges it, instead of by comparing stored fabric ids that a cable change makes stale; when cabling cannot answer, that delete is skipped and the definition is left in place (#1065).
- Fixed: reservations reports `purpose_classification_pending` as false once the purpose sweep's attempts reach `PURPOSE_CLASSIFY_MAX_ATTEMPTS`, so the idle-conversation sweeper releases the transcripts of a reservation the sweep has given up on; the admin backfill reset makes it pending again (#1067).
- Fixed: non-admin template reads mask password-field defaults the way device reads mask password values, and the template editor's Default input for a password field no longer shows the value on screen.
- Tests: the Selenium inventory search test (`test_inventory_search_filters_results`) now restores the admin's saved inventory filter. It reads the baseline before touching the page, still waits for its erased search to read back empty, then writes the baseline back in a finally block and fails when the read-back differs; before, it left an empty search in place of whatever filter was saved (#1070 follow-up).
- Tests: the e2e suite no longer leaks the admin's saved inventory search between tests. Every inventory test that types a search waits for the preference PATCH whose parsed body saves exactly that search (one shared predicate, `pw_prefs_patch_carries` in `tests/e2e/conftest.py`, replacing a substring match that an earlier, longer search satisfied) before it restores the baseline, and then reads the preference back and fails if it differs. The inventory filters and toast placement tests clear a leftover filter through the Filters panel's control instead of failing on it, and a unit test refuses a substring test on a raw request body anywhere under `tests/e2e` (an AST scan since #1145 that follows the body through a variable, `str()`, `.decode()`, or `or ""`, and flags `in`, `find`, `startswith`, and the like; a parsed body is not flagged) (#1070).
- Docs: README.md, FEATURES.md, PLANNED_FEATURES.md, ARCHITECTURE.md, the user, admin, topology editor, and operations guides, and GAPS.md describe the 2026-10-07 fixes as built (instance-device and dynamic-template visibility, the enforced hypervisor flag, the port cabling guard, reservation edit and calendar rules, reachability-scoped VLAN allocation, the assistant's device scope and transcript retention), and ADRs 0004, 0006, 0009, and 0013 gain dated as-built amendments.
- Fixed: the fork save lists every line whose chosen ports no cable path joins in a new `constrained_edges_skipped` field (it still answers 200 and never wires other ports), and the editor's save toast names each such line by device and port and stays until dismissed (#1007).
- Fixed: a directory-group mapping create the directory cannot validate answers 503 with the fixed detail `Directory unavailable, mapping not validated`; the directory error text goes to the log message only (#1009).
- Fixed: every reservation assistant tool that takes a device id accepts only the reservation's own devices; any other id is refused at dispatch with `<argument> is not a device of this reservation` before any downstream call, for read and write tools alike, and an unreadable device list refuses every device-scoped call (#1054).
- Fixed: the assistant's web documentation fetch resolves `.` and `..` path segments, literal or percent-encoded, before the allowlist prefix match and on every redirect target, and refuses hosts resolving into the shared address space 100.64.0.0/10; corpus search no longer indexes a symlink that leaves the corpus root (#1055).
- Fixed: while purpose classification reads assistant transcripts, the idle-conversation sweeper keeps a conversation whose reservation is not terminal or is terminal with classification still pending, read from reservations' internal status (new `purpose_classification_pending` field), and keeps it when that answer is unavailable (#1039).
- Docs: `docs/DRIVERS.md` states that a Hypervisor recipe's returned `field_data` is stored as given and redacted only under the template's password keys, so a credential must go under such a key or into the secrets service (#1056).
- Fixed: a device or template import dry run is a full rehearsal: every row runs through the same create and update checks as a committing import inside a transaction that is rolled back, so the dry-run report matches the committing report row for row (#1017).
- Fixed: a port delete, and a port update that changes the name, answer 409 `port_cabled` (`connection_count`, `connection_ids`) while a cabling connection names the port, and 503 when cabling cannot answer, instead of leaving the cable naming a port that no longer exists; cabling gains the internal `GET /connections/internal/by-port` lookup (#1023).
- Fixed: the template export and import carry a `hypervisor_name` column resolved by name, so a dynamic template round-trips into another instance; an unknown hypervisor name rejects the row naming it (#1024).
- Fixed: VLAN numbers are unique across everything reachable through the current cabling, transit switches included: allocation compares cabling's current fabric answer for the new switches and for every switch another live allocation is anchored at, so a cable change no longer lets two reservations share a number in one fabric or gives one reservation two numbers in it; allocations are serialized by an advisory lock, and a fabric lookup cabling cannot answer now defers the allocation (NAK, retry tick keeps the row FAILED, manual retry 503) instead of substituting a one-switch fabric (#1003).
- Fixed: a non-admin can see and book a dynamic template only when its hypervisor names a device group one of their user groups holds a permission on (new `device_group_id` on hypervisors, migration 0022, "Bookable by device group" on the Hypervisors page); a hidden template answers exactly like an unknown one, and every existing hypervisor starts admin-only until an admin assigns a group (#1053).
- Fixed: a hypervisor's Enabled flag is enforced: a booking of a template on a disabled hypervisor is refused with a 422 naming it, and execution creates no instance on it for a booking taken earlier (log action `dynamic_instance_hypervisor_disabled`) (#1033).
- Fixed: the owner of a booking of physical devices plus dynamic instances can open their instance device and change the reservation's physical devices while keeping it; the edit judges topology-type uniformity over the booked devices only, and inventory grants visibility of an instance device through the owner's live reservation (new internal `GET /internal/held-devices` in reservations) (#1030).
- Fixed: a filter, page size, or sort changed on the Inventory, Reservations, or Topologies page before the saved preferences finished loading no longer saves an empty search over the saved one; the store holds such writes until the load settles, applies only what the user changed on top of the loaded values, and saves the result once (#985).
- Fixed: the device page's Save sends only the fields the admin changed since clicking Edit, and Edit starts from the current device, so a rename no longer writes back a stale status over one provisioning set meanwhile (#1020).
- Fixed: the topology editor opens a stored node that has no device (the seeded "BROKEN" topologies) instead of crashing into the error boundary; the node renders as "Unknown device" and a new connection to it is refused with a toast (#989).
- Fixed: `/config` and `/templates/new` carry bottom padding so their last controls can scroll clear of a stack of toasts (#988).
- Fixed: a bulk group member add naming a user id with no account answers 404 `Users not found: <ids>` and adds nobody, instead of an unhandled 500 (#1009).
- Docs: the dead-letter inspection, replay, and purge commands in `docs/OPERATIONS.md` run the `nats` CLI from a `natsio/nats-box` container, since the `nats` service image has no CLI, and say when a replay is meaningful (#991).
- Fixed: inventory answers 422 or 503 where a 500 escaped: a bulk port create whose last generated name exceeds 255 characters, a device or template import file that is not UTF-8, and a non-JSON 200 from reservations during a device delete or config restore (#1022).
- Fixed: a device-group bulk add naming a device that does not exist answers 422 `Devices not found: <ids>` and adds nothing, instead of a 500 (#1019).
- Fixed: a device import that updates an existing device writes only the columns the row carries, so omitted `field_data`, poll interval, status, and topology type keep their stored values; an explicit null on a NOT NULL device field is a 422 naming the field, and only a real name clash reads as one (#1016).
- Fixed: the device page's user group names are resolved across every page of auth's group list, not only the first 50 groups (#1021).
- Fixed: template and driver updates hold the template-driver connection-type contract: a template update cannot clear a required driver or hypervisor (422), and a driver's connection type cannot change in a way that breaks a template using it (409) (#1018).
- Fixed: a dynamic-instance teardown that hits a transient recipe package download
  failure (a replica with no cached copy while inventory or package storage is briefly
  unreachable) now NAKs the terminal event so the redelivery destroys the instance; it
  used to acknowledge the event with the instance still running and nothing retrying it.
  A broken package still leaves the row live with `dynamic_instance_keyed_destroy_failed`
  (#1029).
- Fixed: a dynamic create can no longer leak an instance and its device after the
  reservation's teardown has run. Each request re-reads the reservation after its ledger
  row is committed and creates nothing once it is no longer `PENDING_PROVISION` (log
  action `dynamic_instance_create_skipped_reservation_ended`), and the create path reads
  ledger rows scoped to its own reservation. When a create loses its `ACTIVE` flip to
  teardown, the compensating destroy now always runs, and a device delete that fails is
  logged as `dynamic_instance_compensation_device_left` instead of being reported as a
  clean undo (#1028).
- Fixed: the dynamic recipe flows act on a `login` that returns `{"success": false}`: a
  create stops before `create_instance` and retries, teardown and the compensating
  destroy run no `destroy_instance` and keep the ledger row live, and recipe run rows are
  `FAILED` whenever the recipe reported failure, as physical runs are. A device delete
  that fails after a successful teardown destroy now logs
  `dynamic_instance_device_delete_failed` with the device and the row's real status
  (#1027).
- Tests: pins for dynamic-resources rules that had none, among them secret redaction in
  recipe run rows across a full create and teardown cycle and a live test of a failed
  recipe login; the integration redelivery test's docstring now says it proves the
  corroboration gate (#1032).
- Fixed: saving a topology as a template no longer turns network elements into device roles (a template stored that way reads and instantiates with the stray role ignored); a taken template name is a 409 from every template write route, never a 500; template names and descriptions take the topology bounds (#1005).
- Fixed: topology CSV export writes the ports chosen in the editor instead of canvas handle names, and CSV import writes them back where the fork save reads them, so a round trip keeps port assignments (#1006).
- Fixed: topology validation judges an edge's chosen ports by the fork save's rule and reports `no_port_path` for ports with no cable path, instead of calling valid an edge the save builds nothing for (#1007).
- Fixed: a non-admin owner's fork read redacts hops on devices outside their visibility, and `GET /connections/{id}` answers a cable with no visible end with the same 404 as an unknown id (#1008).
- Fixed: the expiration sweep's restart backstop, which returns a physical-only reservation stranded in `PENDING_PROVISION` to `PENDING`, now also releases that reservation's exclusive devices through the holder-aware release filter, so a later cancel or elapsed-window failure no longer leaves them `RESERVED` with no holder (#993).
- Fixed: both provisioning backstops measure `PROVISION_TIMEOUT_SECONDS` from the new `reservations.provision_started_at` column, stamped when a row enters `PENDING_PROVISION` (migration 0017), instead of from `updated_at`, so a purpose-category edit no longer restarts the timeout; rows without a stamp fall back to `updated_at` (#997).
- Fixed: a reservation that fails during create because the inventory flip gave up is now stamped for purpose classification like every other terminal transition, and its revert of partially reserved devices uses the holder check (#996).
- Tests: new tests pin the 50-request dynamic cap (backend and frontend), owner-only `GET /{id}`, `PATCH /{id}`, and release for non-owner admins and users, and the sweep's `PENDING` claim against a concurrent cancel on a real Postgres (`test_sweep_claim_cancel_race_live_pg.py`, added to the live-Postgres gate phase) (#998).
- Fixed: every streamed assistant turn ends in exactly one `done` or `error` frame; an unexpected exception, including a database error while saving the turn, now ends in `error` "Assistant call failed" (or `done` with `incomplete` when a write already landed) instead of a stream with no terminal frame (#1037).
- Fixed: an inventory or reservations failure during generation or a first assistant turn answers a pinned 503 instead of a 500, and generate checks the provider, the quota, and the uploads before it reads inventory (#1035).
- Fixed: an AI request that ends in an error after reaching the provider is metered against `AI_DAILY_TOKEN_QUOTA` with the tokens its calls reported, for generation, the assistant, recipe drafting, purpose classification, and identity suggestions (#1034).
- Fixed: model output and upstream error text no longer reach logs, error details, or assistant tool results; the identity route logs only the failing fields, and tool and commit errors name a status or an exception class (#1036).
- Fixed: AI generation refuses a proposal whose resolved devices mix physical and cloud types, as a repairable mistake and then a structured 422 `topology_mixed_types`, and reports each device's resolved type (#1038).
- Tests: tests for the AI rules that had none, and the specification's "Pinned by" entries updated (#1040).
- Docs: the AI commit dialog's config label says admin or a device manage grant, and three stale lines in TROUBLESHOOTING.md and ADR 0009 match the code (#1041).
- Fixed: a driver package download that fails while wiring is applied (inventory or
  package storage briefly unreachable on a cold driver cache) no longer strands the
  connection. L1, L2, and L3 rows used to be parked under the non-retryable
  `recorded hop unresolvable` reason that only a fork re-save cleared, and on the release
  side (a teardown included) nothing ever drove them again. A transient load failure now
  records `driver load failed: <ClassName>` with one attempt, and both retry channels
  drive the row again in its own direction; a broken package (`DriverPackageError`) stays
  non-retryable. The stored text carries only the class name (#1002).
- Fixed: removing a Layer 3 routing intent that the drive gate refused before anything was applied now falls back to the switch's configured routes. The refused row records that nothing is installed, so the save and the retry tick drive the configured routes (or release the row when there are none) instead of pinning an empty route set ACTIVE and reporting a login and logout with no route call as reconnected; a switch that had an applied set still keeps it (#1004).
- Fixed: a Layer 3 route change that partly fails no longer leaves routes on the switch.
  The failed pin now records every route that may still be installed (the previous
  routes whose removal did not confirm plus the routes the change tried to add); a
  terminal teardown parks such a pin release-direction and removes those routes, and a
  later save or retry that rebuilds the switch removes the ones its intent drops before
  configuring the intent (#1001).
- Docs: corrected documentation, docstrings, and comments that the code contradicts, found while writing the specifications; ADR 0004 and ADR 0015 gain dated as-built addenda instead of rewritten decisions (#1010, #1025, #1031, #1041).
- Docs: specifications under `docs/specs/`, one per feature area, each rule tied to the code that enforces it and the test that pins it. The first is `docs/specs/reservations.md`; `tests/unit/test_spec_references.py` fails when a specification names a path, symbol, test, link, or section that no longer exists.
- Fixed: editing a reservation's devices obeys the inventory hold rule. The edit commits under a status guard, so a PATCH that races a cancel, release, auto-complete, or activation keeps nothing and answers 409; inventory is written only after the commit and only on an `ACTIVE` reservation, with three attempts, and removed devices are released through the holder-aware filter (#994).
- Fixed: a PATCH can no longer stretch a reservation past `RESERVATION_MAX_DURATION_SECONDS`; the new end time is judged from the stored start by create's own check and wording (400) (#995).
- Fixed: adding a device to a `PENDING` reservation follows create's rule for a future window: the window conflict check decides, not whether the device is `AVAILABLE` right now (#999).
- Fixed: the reservations calendar answers 503 for a non-admin when device visibility cannot be verified, instead of showing every user's reservations; admins are unaffected (#1000).
- Fixed: ending a reservation no longer leaks a dynamic instance whose create failed,
  timed out, or lost its process after touching the hypervisor. Teardown used to treat a
  ledger row with no `instance_ref` as "nothing was created" and mark it `DESTROYED`
  without calling the recipe. It now runs a keyed destroy, `destroy_instance` with
  `instance_ref=None` and `HERD_request_id` in the context, and retires the row only when
  that destroy succeeds; a failed or raising keyed destroy leaves the row `CREATING`,
  acknowledges the event, and logs `dynamic_instance_keyed_destroy_failed` with the
  request and reservation ids (see `docs/TROUBLESHOOTING.md` for finding the instance and
  retrying). The same keyed destroy now covers the #896 compensation path. A
  `create_instance` that reports success without an `instance_ref` is a failed create
  (log action `dynamic_instance_create_missing_ref`) instead of an `ACTIVE` row nothing
  could destroy. The execution consumer now corroborates `reservation.provision_requested`
  like the other lifecycle events: it runs only while the reservation is
  `PENDING_PROVISION`, so a late redelivery cannot create an instance for an ended
  reservation; this makes provisioning depend on reservations being reachable, and an
  outage that outlasts the NAK schedule dead-letters the event so the provision timeout
  fails the reservation with no create attempted (`docs/TROUBLESHOOTING.md`). Teardown
  retires a row only by a compare-and-swap on the state it actually destroyed, so a create
  that lands on another replica mid-teardown is torn down too instead of being leaked
  (log action `dynamic_instance_teardown_contended` if that keeps happening), and a
  create that fails after teardown retired its row runs a keyed compensating destroy. A
  `create_instance` success without an `instance_ref` now dead-letters on its first
  delivery. The package validator judges `create_instance` and `destroy_instance` by the
  consumer's own rule (an explicit `"success": true`, and a non-empty `instance_ref` from
  create). AI-drafted recipes are validated against a new dry-run step,
  `destroy_instance (no instance_ref)`, and the drafting prompt states the requirement
  (#937).
  UPGRADE NOTE for authors of existing Hypervisor recipes: `destroy_instance` is now
  called as `destroy_instance(instance_ref=None)` for an instance whose create never
  reported a handle. Accept `instance_ref=None`, find the instance by the name
  `create_instance` derives from `HERD_request_id`, destroy it, and return
  `{"success": True}` when no such instance exists; never raise or fail just because the
  ref is missing. Also make sure `create_instance` always derives that name and returns a
  non-empty `instance_ref` on success. A recipe that requires a ref leaves such rows
  `CREATING` with the log action above on every teardown. Watch for the opposite trap too:
  a recipe that passes `None` into its hypervisor API and reads the "not found" as an
  already-absent success would let HERD retire a row whose instance still exists. There
  is no capability flag; the keyed destroy is part of the Hypervisor contract
  (`docs/DRIVERS.md`).
- Changed: toasts appear at the bottom centre of the window instead of the top right. At the
  top right a toast covered the topology editor's Save button and the header controls, and
  because a toast waits while the pointer rests on it, the pointer left on Save after a click
  kept the toast up and Save unclickable. Hovering a toast still holds it open; durations and
  styles are unchanged. A new Playwright test measures that no toast, single or three deep,
  covers a control on the topology editor or the inventory list at 1280x720 and 1920x1080
  (#942).
- Fixed: a topology can no longer be deleted while a reservation still uses it.
  `DELETE /cabling/topologies/{id}` is refused with 409
  `{"error": "topology_in_use", "reservation_ids": [...]}` (sorted) while any reservation
  in PENDING, PENDING_PROVISION, or ACTIVE references the topology; COMPLETED, CANCELLED,
  and FAILED reservations do not block. Before this, deleting the topology behind a future
  booking made that booking activate later with an empty canvas and no wiring, and nothing
  reported it. The check asks reservations' internal by-topology lookup and fails closed: if
  reservations cannot answer, or answers in an unexpected shape, the delete is a 503 and
  nothing is deleted. It runs after the existing 404 and 403, so a caller who may not
  delete the topology learns nothing about its bookings. There is no force flag and no
  cascade; cancel the reservations or let them end first. The Topologies page explains the
  refusal on the row and in the toast ("In use by N reservations. Cancel them or wait for
  them to end, then delete again."). The edit lock on a reserved topology keeps its
  fail-open behavior. Known limit: a reservation created in the instant between the check
  and the delete can still land on a deleted topology (#977).
- Fixed: on the Inventory page, a saved search could be lost when the user changed the
  Status, Template, or Topology filter within about 300 ms of the saved preferences
  arriving after a page load. The filter change saved an empty search in place of the
  saved one. A saved search now applies to the device list as soon as it loads, without
  waiting for the search debounce, and every later save carries it. Only typed text
  still goes through the debounce. The Topologies page already worked this way and now
  has tests that pin it (#982).
- Security: the e2e Selenium container now publishes its WebDriver endpoint (4444) and
  its noVNC browser view (7900) on 127.0.0.1 only. Neither port authenticates, and both
  used to listen on every host interface while the container was up (during
  `make test-e2e` and the gate runs), so anything that could reach the host could start
  a browser session inside the stack's network or watch the one a test was driving. The
  e2e suite connects from the host on localhost and is unaffected; to watch a run from
  another machine, forward 7900 over ssh. A unit test now holds every port the dev
  override publishes to the same loopback rule as the base compose file (#964).
- Fixed: modal dialogs now open centred instead of pinned to the top-left corner of the
  window. Tailwind 4's base styles set every element's margin to zero, which removed the
  browser's own centring for native dialogs; the three dialog components (`Modal`,
  `ConfirmDialog`, and the bulk import dialog) now set it explicitly. Every release
  through 0.6.0 was affected. A dialog taller than the window still scrolls inside itself,
  so no control moves out of reach (#979).
- The Reservations page can be searched and filtered from the left filter panel: a search
  on purpose (case-insensitive; 8 or more hex digits also match a reservation id prefix),
  Status, Category (a configured purpose category or Unclassified), and Period (Upcoming,
  Current, Past), with the admin "All reservations" toggle moved into the panel. The list
  endpoint `GET /reservations/` gained `search`, a repeatable `status`,
  `purpose_category` (`none` for unset), and half-open `starts_after`, `starts_before`,
  `ends_after`, and `ends_before` bounds; every filter applies inside the rows the caller
  may already see, `total` is the filtered total, `all=true` stays admin-only, and a bad
  value (including an unknown category or a timestamp without an offset) is a 422. The
  choices are saved in `savedFilters.reservations`, a stale saved value falls back to All
  and is never sent, any filter change returns to page one and clears the bulk selection,
  and a filtered-empty state offers Clear filters. The `/api/v1` facade is unchanged and
  still forwards only `skip` and `limit` (#959).
- The Topologies page gets search, an Owner filter (All, Mine), sortable Name, Owner,
  Created, and Updated headings, and multi-select delete. `GET /cabling/topologies`
  gains `search` (case-insensitive name substring), `owner` (`mine` or `all`), `sort_by`,
  and `sort_dir`, validated as enums (a bad value is a 422), with `total` counting the
  filtered set and an id tiebreak; text sorts case-insensitively and identically on
  SQLite and Postgres. The filters and sort persist per user (`savedFilters.topologies`,
  `extras["sort:topologies"]`) and a stale saved value falls back to the default. Delete
  is gated by one predicate matching the backend rule (creator, admin, or superadmin),
  shared by the row button and the bulk action; the bulk action skips rows that are not
  yours, says so in the confirmation, and keeps refused rows selected with the server's
  reason (#958).
- List pages get a shared left filter panel (`ListFilterPanel` in
  `frontend/src/components/ui/`): a labeled `Filters` region beside the table that holds
  the search box, the page's filter controls, and a Clear filters control shown only when
  a filter is active, stacking above the table on a narrow viewport. The panel owns
  layout and labels only. The Inventory page's search and its Status, Template, and
  Topology filters moved into it with no behavior change: the same query parameters,
  saved filter, stale-value fallback, and row expansion and selection rules (#957).

## [0.6.0] - 2026-10-01

- Reservation status writes are race-safe. Every status transition is a compare-and-swap
  through one helper, so only the writer that wins the row stages the event, flips
  inventory, or calls cabling (#899). A PENDING reservation holds no device and writes no
  inventory status (#897), back-to-back bookings no longer leave a shared exclusive device
  AVAILABLE under a live booking (#898), and the dynamic-instance ledger is forward-only,
  so a row retired by teardown is never brought back by a late create (#896).
- Security hardening. The auth service authorizes on the lower of the JWT role claim and
  the account's current database role; the compose file binds the NATS and Postgres host
  ports to loopback (#708); a device node on a canvas is reduced to a fixed allowlist at
  every write and read, so `field_data` is never stored or returned; execution
  corroborates each reservation lifecycle event with the reservations service before
  acting on it; every device-scoped inventory read and the topology import go through the
  device-group visibility check (#909, and the #763 follow-up); and every CSV writer
  neutralizes spreadsheet formulas (#910).
- NATS consumers behave as configured. Retry timing comes from an explicit NAK delay
  schedule instead of a `backoff` list that silently shortened `ack_wait` (#895), all
  three consumers heartbeat a message while its handler runs so a slow webhook receiver
  is not handed the same event twice (#911, #944), and `ack_wait` is the setting
  `NATS_ACK_WAIT_SECONDS`.
- Deleting a device is refused while wiring depends on it: as a transit hop on a live
  reservation's fork (#900, `device_in_use`) or while any cable still names it (#940,
  `device_cabled`). There is no force flag, and an unreachable upstream refuses too.
- AI topology generation picks devices that can be wired. Resolution asks cabling which
  candidate pairs are reachable and searches for an assignment that satisfies every
  proposed edge (#828), commit checks wireability before the reservation is created
  (#827), and an opt-in evaluation harness scores how often a proposal resolves (#826).
- The reservation assistant keeps the record of what it did. A turn whose tools already
  ran is persisted when the model returns no text, times out, or the provider fails
  (#848, #871, #903), and a streamed turn always ends in one `done` or `error` frame
  (#946).
- Config apply is gated on the driver contract in inventory and again in execution
  (#839, #870), raw driver exception text no longer reaches a row or a response (#840),
  and the JSON log formatter emits every `extra=` key with key-name redaction (#872).
- Version and build visibility (#846): every service answers `GET /version`, images are
  stamped with the git build identifier, `make version` prints it, and the admin About
  page lists all twelve services and flags version skew (#874).
- Reservations and inventory pages: the reservations list sorts (#844, #902), a booking
  can be cancelled before it is active (#841), rows can be selected for a bulk Cancel or
  Release (#843), the inventory page filters by status, template, and topology (#842)
  and keeps an expanded row open across a refetch (#938), an admin can classify one
  reservation's purpose from its detail modal (#822), and the header help icon opens the
  published manual (#960).
- Outbound webhooks can subscribe to `device.health_transition` (#831).
- Added `docs/SIMULATED_LAB_GUIDE.md`, pinned the npm floor the frontend lockfile needs
  (#885), and removed the dead `HEALTH_POLL_MINIMUM_INTERVAL_SECONDS` setting (#880).

### Delivery detail

- Fixed the header help icon (the `?` beside the notification bell) to open the
  published user manual at <https://vendrabuck.github.io/HERD/manual/> (issue
  #960). It used to open a repository document. The address lives once as
  `MANUAL_URL` in `frontend/src/lib/links.ts`; the link still opens in a new tab
  with `rel="noopener noreferrer"`.
- Added multi-select Cancel and Release to the Reservations page (issue #843).
  Each row has a checkbox and the header has a select-all for the current page;
  a selection bar offers Cancel selected and Release selected. There is no bulk
  endpoint: the page fans out over the existing per-id cancel and release calls,
  and the backend's status compare-and-swap decides each row. Eligibility is per
  row through `canCancelAs` and `canReleaseAs` in `lib/reservationStatus.ts`,
  the one rule now behind the detail modal, the per-row buttons, and the bulk
  actions, matching the backend: the owner or an admin may cancel (issue #340),
  only the owner may release. An admin now sees Cancel on another user's
  reservation, and Release no longer shows on a row the caller does not own.
  One confirmation per action states how many reservations will be acted on
  and how many will be skipped and why. Succeeded rows leave the selection,
  failed rows stay selected and the toast reports "Cancelled N, failed M" with
  the server's reason when every failure shares one. The selection is dropped
  when the page, sort, or all-reservations view changes. Pure logic lives in
  `lib/reservationBulk.ts`; `tests/e2e/test_reservations_bulk_playwright.py`
  proves the effect through API read-back.
- Added column filters to the Inventory page (issue #842). Status, Template, and
  Topology selects sit beside the name search and use the device list's existing
  `status`, `template_id`, and `topology_type` parameters, so there is no backend
  change. Each has an All option that sends no parameter. The Template options show
  the template name with its vendor and model, which is how vendor and model are
  filterable. Filters compose with the search and pagination (any change returns to
  page one, and the total is the filtered total), a Clear filters control resets all
  of them, and a filtered-empty result says so instead of showing the no-devices
  state. The choices persist in `savedFilters.inventory` as
  `{search, status, template_id, topology_type}` with a filter at All omitted, so an
  old `{search}` object still works; an unknown saved status or topology, or a
  template id that no longer exists, falls back to All and is never sent.
- Fixed the admin device DELETE leaving stale cabling connections behind (issue
  #940, the plain-cable half split out of #900). A cabled device with no live
  reservation deleted with 204 and every `Connection` row that named it kept a
  dead device UUID that pathfinding still routed through. Cabling's internal
  `GET /internal/forks/by-device/{device_id}` now also returns
  `connection_count` (true total, a loopback counts once) and `connection_ids`
  (sorted sample, capped at 10), and `assert_device_deletable` refuses with
  `409 {"error": "device_cabled", "connection_count", "connection_ids"}` after
  the `device_in_use` decision, which wins when both apply. A cabling build
  without the new keys reads as the existing 503, fail closed; there is no
  force flag. The device page keeps the refusal on screen with the count and a
  link to Connections, and a bulk delete on the inventory page says remove the
  cables first. Integration fixtures now route device teardown through a strict
  helper that removes leftover cables and fails with the refusal body instead of
  leaking the device.
- Fixed execution's consumer loop swallowing a shutdown cancel, and added a live
  exactly-once test for a slow webhook receiver (issue #944). Execution kept an
  inline copy of the heartbeat whose `try: await heartbeat / except
  CancelledError: pass` could not tell a cancel aimed at the consumer from the
  heartbeat's own, so a shutdown landing just after a batch settled left the loop
  running and `stop_nats_consumer` waiting forever. Execution now runs its
  batches through the shared `process_batch_with_heartbeat`, with a regression
  test through the real loop. `NATS_ACK_WAIT_SECONDS` is now a setting (default
  30, minimum 2) in execution, notifications, and integration, with the
  heartbeat derived as half of it by `herd_common.jetstream.heartbeat_interval`.
  The dev/test stack pins integration to 4 s, and the test webhook sink gains a
  bounded `delay_ms` and a per-event hit counter, so a live test holds a fan-out
  open past ack_wait and asserts that the receiver got one POST and that a second
  consumer waiting on the durable was handed no redelivery. The second consumer
  is what makes the test able to fail: JetStream hands a timed-out message only
  to a pull request that is waiting, so a lone consumer never receives its own
  redelivery and the duplicate the heartbeat prevents needs a peer replica.
- Added a **Classify now** action to the reservation detail modal (issue #822),
  the UI for the on-demand purpose classification trigger from issue #808. An
  admin sees it on a COMPLETED, CANCELLED, or FAILED reservation with no
  suggestion; it calls `POST /admin/purpose-review/{id}/classify` and renders
  the outcome: a toast naming the suggested category on `ok`, a plain sentence
  for each other outcome, the disabled message on 503, and a refetch that
  removes the stale button on either 409. The gate is one predicate,
  `canClassifyPurpose`, beside `canCancel` and `canRelease`. The Purpose Review
  page, which lists only rows that already have a suggestion, gained a sentence
  pointing at the new button. Covered by vitest and a Playwright test that
  asserts the UI against the real backend answer and an API read-back.
- Fixed the assistant stream losing its terminal frame when the overall
  deadline fired while a frame was being handed to a slow client (issue #946).
  The route yielded SSE frames from inside `asyncio.timeout`, so the
  cancellation landed in the consumer, the timeout handler never ran, and the
  stream ended with neither `done` nor `error` and no incomplete-turn save. The
  route now keeps one absolute deadline and bounds only the wait for the next
  event, yielding each frame after that scope closes, and closes the inner
  generator on every exit. A structural unit test fails on any `yield` inside
  `asyncio.timeout`, `asyncio.timeout_at`, or `asyncio.TaskGroup` under
  `services/*/app`.
- Fixed integration and notifications redelivering a message whose handler was
  still running (issue #911). Only execution sent `in_progress`, so a webhook
  fan-out outlasting the 30 s `ack_wait` (several slow POSTs plus backoff) was
  redelivered and the receiver got the same event twice. The heartbeat is now
  one shared implementation, `herd_common.jetstream.keep_messages_alive`, with
  `process_batch_with_heartbeat` wrapping each fetched batch; both integration
  durables and notifications' two durables use it, and each service pins its
  `NATS_HEARTBEAT_SECONDS` below its `ack_wait`. Execution's behavior is
  unchanged. Delivery stays at-least-once: a crash during a POST can still
  redeliver (`docs/EXTERNAL_API.md` says so).
- Fixed an expanded inventory row collapsing when the list changes under it
  (issue #938). Typing a search and expanding a row before the debounced
  filtered list arrived used to close the row, because every list change
  cleared the expanded set. The page now drops only the rows that left the
  list; rows still listed stay open. The bulk selection is still cleared on a
  list change. The seeded e2e pass gains a deterministic regression test, and
  the topology editor's L3 routing e2e test now moves the pointer off the toast
  stack before each Save (react-hot-toast pauses a toast while hovered).
- Fixed the admin device DELETE guard ignoring cabling (issue #900, live-wiring
  half). A switch that was only a transit hop on a saved fork sat in no
  `reservation_devices` row, so it deleted with 204 and its cross-connects were
  stranded with no driver left to release them. Cabling gains the internal
  `GET /internal/forks/by-device/{device_id}` (reservation ids of non-archived
  forks whose hops touch the device), and inventory's single helper
  `assert_device_deletable` asks it beside reservations. The 409 `device_in_use`
  detail keeps `reservation_ids` (now the sorted union) and adds
  `transit_reservation_ids`; either upstream failing returns 503, fail closed.
  The device page names transit holders in its toast. A delete right after a
  cancel can still precede execution's asynchronous teardown. Refusing a delete
  while plain cabling connections reference the device (`device_cabled`) is
  deferred to its own issue.
- Made the dynamic-instance ledger forward-only (issue #896). `set_instance_ref`
  and `mark_active` are now compare-and-swap updates that never touch a
  DESTROYED row, and `provision_requested` refuses a DESTROYED row
  (`dynamic_instance_resurrection_refused`). If teardown retires the row while
  `create_instance` is in flight, the instance just created is destroyed again
  and its device deleted, so nothing is orphaned.
- The execution "Starting driver execution" log now emits `context_keys` (the
  sorted key names) instead of the whole redacted device context (issue #905),
  so a text-typed secret under a neutral key no longer reaches the JSON log.
- Corrected stale comments in execution that claimed JSONFormatter drops
  unlisted extras (issue #906).
- Made the L1 manual-retry integration tests read the row back again (issue #906):
  a reported `reconnected` or `released` outcome no longer substitutes for
  wiring-status showing the row ACTIVE or RELEASED.
- Fixed the reservation assistant losing the record of a write (issues #903,
  #904, #906). `propose_config_change` now records a `config_version_created`
  side effect, so a later timeout or provider failure persists the turn
  instead of rolling back a config version that exists (it does not set
  `pending_apply`). Both transports now persist a completed turn outside the
  overall deadline, so the deadline can no longer interrupt the commit or
  append the same segments twice. The closing text of an incomplete turn now
  ignores errored tool calls when deciding which landed actions were
  recorded, and `ai_assistant_incomplete_after_tools` carries `reason` as its
  own log field.
- Fixed `sort_by=status` on the reservations list ordering by the Postgres enum's
  storage order instead of alphabetically (issue #902). The ORDER BY now casts
  the status column to a string, so every dialect sorts by status name, matching
  the SQLite unit test and the UI. A new live Postgres suite,
  `test_reservation_sort_live_pg.py`, pins both directions and is part of
  `_gate-pg-live-tests`.
- Made every reservation status transition a compare-and-swap (issue #899).
  The immediate create path wrote ACTIVE (or FAILED) with an UPDATE keyed by
  id alone after seconds of inventory calls, so a cancel committed in that
  window was overwritten, leaving a zombie ACTIVE reservation that never
  wires and has both a cancelled and a created event. Create, scheduled
  activation, cancel, release, and the sweep's auto-complete now go through
  one `_claim_status_transition` (a conditional `UPDATE ... WHERE status IN
  (...)`; `_claim_provision_transition` is a thin wrapper) and stage events,
  flip inventory, and call cabling only when the rowcount is 1. A create or
  scheduled activation that loses reverts exactly the devices it flipped
  (skipping any a newer booking holds) and returns the row as the winner left
  it. No HTTP status code or error message changed. A new live Postgres suite,
  `test_reservation_status_cas_live_pg.py`, races create against cancel and
  auto-complete against release, and is part of `_gate-pg-live-tests`.
- Fixed back-to-back bookings leaving a shared exclusive device AVAILABLE under
  an ACTIVE reservation (issue #898). In one expiration tick the successor's
  activation ran before the predecessor's release, and both are absolute
  inventory writes. The sweep now releases completed rows' devices before
  activating claimed rows, and every release (cancel, release, provision
  failure, timeout, auto-complete) skips a device another PENDING_PROVISION or
  ACTIVE reservation still holds, logging `release_skipped_device_held`. The
  activation claim also requires `end_time > now`: a PENDING row whose window
  already elapsed is moved to FAILED (`reservation_window_elapsed`), stages
  `reservation.failed`, and makes no inventory or fork call.
  A scheduled activation whose inventory flip fails reverts to PENDING and
  also releases the row's exclusive devices (holder-aware), since a same-tick
  predecessor's release had skipped them while the row held them.
- Stopped PENDING reservations from writing inventory status (issue #897).
  Since #132 a future booking touches no inventory status until activation,
  but cancelling a PENDING booking, or PATCH-removing a device from one,
  still released every exclusive device to AVAILABLE (flipping a device that
  another ACTIVE reservation held), and PATCH-adding a free device marked it
  RESERVED for a booking that starts later. An exclusive device is now
  RESERVED in inventory if and only if a reservation in PENDING_PROVISION or
  ACTIVE holds it, so `cancel_reservation` and `update_reservation` write
  inventory status only when the row's status before the transition holds
  devices. Conflict checks and the "device must be AVAILABLE to add" refusal
  are unchanged.

- Hardened three inventory reads that skipped the issue #718 device-group
  visibility gate (issue #909): `GET /device-groups/device/{id}`,
  `GET /devices/{id}/apply-jobs`, and `GET /apply-jobs/{id}`. Any
  authenticated user could confirm a hidden device exists, read its
  device-group and user-group membership, and read its config-apply
  history, including driver-returned error text. All three now apply the
  same non-admin group-visibility gate as `GET /devices/{id}` and
  device_configs.py's config-version reads: a device outside the caller's
  groups 404s with the identical detail its own unknown-id 404 already
  used (`device-groups/device/{id}`'s existing id-bearing detail is reused
  rather than replaced with a second phrasing), and `GET /apply-jobs/{id}`
  gates on the job's own device_id, answering a hidden device's job with
  the same 404 a missing job gets. Admins stay unfiltered. The gate itself
  moved out of device_configs.py into a shared `check_device_read_visibility`
  helper in `app/services/device_visibility.py` so all three call sites,
  plus device_configs.py's own three reads, share one implementation.
- Hardened `POST /api/cabling/topologies/import` against reopening the #763
  visibility oracle through a batched side door. Topology import is open to
  any authenticated user, and it used to resolve every device name through
  the unfiltered internal inventory lookup and run the full topology
  validator against the unredacted canvas regardless of caller, so a
  non-admin could confirm a hidden device's name, learn whether two hidden
  devices are physically connected, and probe a hidden Layer 3 switch's
  interface and VRF configuration through the per-row reject reason, with
  `dry_run=true` making it free of side effects. The import route now
  resolves the caller's device visibility once per request the same way
  `POST /topologies/{id}/validate` does (issue #763's shared
  `resolve_caller_visibility`): admins are unfiltered and never trigger the
  lookup, and an unanswerable inventory lookup fails closed with a 503 and
  writes nothing. For a non-admin, a canvas device name that resolves to a
  device outside their visibility is folded into the row's existing
  `unresolved device names` reason, so a hidden device and a nonexistent one
  produce an identical rejection; a device reference that carries a raw id
  with no name at all (bypassing name resolution) is redacted from the canvas
  before validation runs, so it can only ever surface as the existing
  `missing_device` reason and never reaches the Layer 3 pass. A non-dry-run
  row that references a hidden device is rejected and stores nothing. CSV
  import parses into the same canvas shape before this gate runs, so it is
  covered identically.
- Hardened every CSV writer against spreadsheet formula injection (issue #910).
  A CSV cell whose text began with `=`, `+`, `-`, `@`, a tab, or a carriage
  return was written as given and evaluated as a formula, not literal text,
  when the file was opened in a spreadsheet: the most exposed writer was
  cabling's topology export, whose topology, device, and port names are
  free text any authenticated user controls (they come from the stored
  canvas, not inventory), so a regular user could plant a formula that ran
  when an admin opened the export. A new shared helper,
  `herd_common.csv_safety.csv_safe_cell`, prefixes a single quote onto a
  text cell whose first character (ignoring leading spaces) is one of those
  triggers, the OWASP-recommended neutralization; it is applied to every
  free-text column of the topology CSV export, the inventory device and
  template CSV exports, and the reservations utilization CSV export
  (owner_name and fleet device name), while a fixed enumeration, a
  formatted number or boolean, and a JSON-encoded blob column are left
  untouched. The topology, device, and template CSV importers apply the
  inverse, `csv_safe_cell`'s exact opposite `csv_unsafe_cell`, which strips
  exactly one leading quote and only when a trigger character still follows
  it, so an exported value round-trips through import byte-for-byte and a
  name that legitimately begins with an apostrophe is never mangled. The
  Reporting page's browser-built "by template" CSV export gained the same
  neutralization via its own `csvSafeCell`, applied before its existing
  RFC 4180 quoting.
- Hardened the topology editor and cabling service against persisting device
  credentials on a canvas. The editor used to store the whole inventory Device
  record on each device node, including `field_data`, which can carry a
  clear-text device password; cabling stored and returned whatever canvas a
  caller sent, with no narrowing on what a device node could hold. Every
  cabling write boundary (topology create/update/clone/import, every fork
  write, template create/update/instantiate, and version restore) now reduces
  a device node's `data.device` to a fixed allowlist (id, name, topology_type,
  connection_type, status, template_name, template_icon, plus the
  topology-template placeholder key `role`) before a row is written, and the
  export route strips again on read as a second layer. The frontend narrows
  the same way before a save is ever sent, though the server-side strip is
  what actually enforces it. A one-time data migration scrubbed every
  already-stored canvas.
- Hardened execution's NATS consumer against a forged reservation lifecycle event.
  It used to treat any event on `herd.reservations.*` as authoritative: a message
  claiming `reservation.cancelled`, `.completed`, or `.failed` froze a reservation's
  wiring and tore down its ledgers and dynamic instances, a `reservation.updated`
  destroyed dynamic instances for whatever `removed_device_ids` it named, and a
  `reservation.created` or `.wiring_changed` acted on whatever it claimed, all
  without asking reservations whether any of it was true. `process_reservation_message`
  now corroborates each of these six events against reservations' own record of the
  row (`GET /internal/{id}`) before running any handler: a terminal event needs any
  terminal status, `reservation.created` needs PENDING_PROVISION or ACTIVE,
  `reservation.updated` needs any non-terminal status (it is staged outside the
  ACTIVE-only branch of the device-set PATCH), and `reservation.wiring_changed`
  needs ACTIVE specifically. A mismatch, or a reservation
  that does not exist, acks the message without running the handler; an unanswerable
  check (a reservations outage) NAKs for retry rather than proceeding on missing
  information. `reservation.provision_requested` is unaffected: it already enforces
  its own preconditions server-side.
- Hardened `docker-compose.yml`: the NATS (4222, 8222) and Postgres (`POSTGRES_PORT`,
  default 5433) host port bindings now listen on loopback only, matching the
  Traefik dashboard's existing loopback bind (issue #708). NATS runs with no broker
  authentication, so both were reachable from the LAN or the open internet on a host
  with no firewall in front of it. Traefik's plain HTTP/HTTPS listeners are unchanged.
- Fixed the auth service authorizing against a caller's database role instead of
  their JWT's role claim. The API-token exchange (`POST /tokens/exchange`) already
  clamped an issued JWT's role claim to the lesser of the token's role and the
  owning principal's current database role, but every authorization check inside
  the auth service itself (`require_role`, the token-minting rank checks in
  `POST /tokens`) read `current_user.role`, the database row, and ignored that
  clamp entirely. A user-role API token minted for a superadmin account therefore
  passed every admin and superadmin gate in the auth service, including minting a
  full superadmin token from it, undoing the exchange's demotion. Every service
  other than auth already authorized against the claim (`herd_common.auth`), so
  only the auth service itself was affected. Authorization inside the auth service
  now uses the effective role, the lower of the claim and the database role
  (`app.dependencies.auth.effective_role`); a missing, empty, or unrecognized role
  claim is treated as `user`. A login or refresh JWT still carries the account's
  role at issue time and behaves exactly as before; the only change in outcome is
  that a token whose claim is lower than the account's current database role, or an
  account demoted after a token was issued, is now held to the lower role until
  that token expires, as intended. See `docs/ROLES.md` for the updated rule.
- Fixed the three NATS consumers' (execution, notifications, integration) retry timing
  (issue #895). Measured against nats-server 2.10.29, a `ConsumerConfig.backoff` list
  silently made the server replace the configured `ack_wait` with `backoff[0]` (1
  second instead of the intended 30), and `backoff` itself only ever timed
  ack-timeout redeliveries, never a NAK, so a bare `msg.nak()` on a transient error
  redelivered immediately: five NAKs landed within about a second and the #317
  in-progress heartbeat protected nothing. `ConsumerConfig` no longer carries
  `backoff`; a transient-error NAK now passes an explicit `delay=` from a new
  `NATS_NAK_BACKOFF_SECONDS` setting (comma-separated seconds, default
  `1,5,15,60,120`, shared across all three services; a short schedule is pinned in
  the dev/test compose override), via the shared `herd_common.jetstream.nak_delay`
  helper. Every durable is now also created OR UPDATED to match its `ConsumerConfig`
  at boot (`herd_common.jetstream.ensure_consumer`), since `pull_subscribe` alone
  only creates a durable that does not yet exist and otherwise binds to whatever
  config the server already has, which would have silently kept a `make prod`
  durable's stale `backoff` forever across this fix's rollout.
- Added `docs/SIMULATED_LAB_GUIDE.md`, a task-oriented guide to HERD's three
  simulated-hardware tiers (the hardware-free mock drivers, the checked-in NOS lab,
  and the unbuilt external network-simulator): a table of what each can and cannot
  do and when to reach for it, a numbered NOS lab quick start covering both the
  Makefile commands and the UI steps to build a topology, add a Layer 3 route on
  the real FRR node, reserve it, watch it go ACTIVE, verify on the box, cancel, and
  detach, a mock-driver field_data knob reference, a test-tier comparison, and
  troubleshooting for the known lab traps (the `[FACTORY]` prompt issue, the VRF
  kernel-module dependency, the credential-export gotcha). `docs/NOS_LAB.md` now
  points readers who want the how-to at the new guide instead of its own build log.
- Pinned the npm version the frontend expects (issue #885). A plain `npm install` in
  `frontend/` on an npm older than 11.11.0 silently strips the `libc` array that a newer
  npm (including Dependabot's) writes into optional-platform lockfile entries, rewriting
  `package-lock.json` by 18 lines with no `package.json` change: a diff every contributor
  or agent had to notice and hand-discard before committing real work. `frontend/package.json`
  now declares `engines.node`/`engines.npm`, and `frontend/.npmrc` sets `engine-strict=true`
  so an incompatible npm refuses instead of silently drifting the lockfile. CI's frontend
  job installs npm 11.17.0 (matching Dependabot's npm_and_yarn updater) before `npm ci`,
  and a new gate step reruns `npm install` and fails on any `package-lock.json` diff,
  mirroring the backend job's `uv lock --check`. The frontend Docker image is unaffected:
  its build stage copies only `package.json`/`package-lock.json` before installing, so
  `.npmrc` is not present yet and engine-strict never applies there.
- The reservations list can now be sorted (issue #844). `GET /api/reservations/` gains
  `sort_by` (an explicit allowlist: `start_time`, `end_time`, `status`,
  `purpose_category`, `user_id`, `created_at`; an unlisted value 422s) and `sort_dir`
  query params; the default (`created_at` desc) reproduces the previous ordering
  exactly, and every `ORDER BY` now tiebreaks on `id` for stable pagination across
  ties. Visibility narrows the row set before the sort ever runs, so sorting by a
  field like `user_id` cannot surface a row outside what the caller could already see.
  The Owner, Status, Period, and Purpose column headings on the reservations table are
  now sort controls (aria-sort plus a direction chevron; a third click clears back to
  the default order), and the chosen sort persists per page alongside the existing
  page-size preference.
- Outbound webhooks can now subscribe to `device.health_transition` (issue #831). The
  integration service's NATS consumer previously bound only `herd.reservations.*`, so a
  device health transition published by execution's health scheduler on `HERD_HEALTH`
  (subject `herd.health.status_changed`) could never reach a registered webhook, even
  though `WebhookSubscription.event_types` accepted any string. A second durable pull
  consumer now binds `herd.health.*` (its own durable name and DLQ subject
  `herd.health.dlq.integration`, batch size 1 per the #648 rule), started through the
  same schema-readiness gate as the reservations consumer and dispatching to the same
  handler, since both streams' payloads carry the event name under the same `event`
  key. `device.health_transition` is now accepted by the registration validator and
  documented in `docs/EXTERNAL_API.md`.
- `JSONFormatter` now emits every `extra=` key on a log record instead of a fixed
  eleven-key allowlist that silently dropped the rest (issue #872). The stdlib's
  reserved `LogRecord` attributes are computed once at import time so anything else
  passed through `extra=` reaches the container log; an extra whose name collides
  with one of the formatter's own envelope keys (`timestamp`, `level`, `service`,
  `logger`, `message`, `exception`) is renamed `extra_<name>` instead of overwriting
  it. A key that looks credential-shaped (password, secret, token, api key,
  authorization, cookie, kek, credential, jwt, bearer, private key, ssh key, SNMP
  community) is redacted to `"[redacted]"`, recursively through nested dicts and
  lists (depth-capped at 8), with narrow carve-outs for token-counting fields
  (`input_tokens`, `output_tokens`, `token_id`, `token_count`) so those are not swept
  up as false positives; a value that defeats `json.dumps(default=str)` is replaced
  with a placeholder for that key alone rather than losing the whole log line. Two
  follow-up fixes closed fail-open gaps found on review: execution's "Starting driver
  execution" log line now passes only the method-kwargs' key names, never the values
  (for a `configure` action the kwargs are the device config itself, which can carry
  passwords in free text no key-name pattern can catch), and redaction now recurses
  into nested structures rather than only the top level of an extra.
- Removed the dead `HEALTH_POLL_MINIMUM_INTERVAL_SECONDS` setting from the execution
  service (issue #880). The variable was declared with a default and documented in
  `docs/ENV_VARS.md`, but it was never passed through by `docker-compose.yml` and
  nothing in the health scheduler read it: it never had any effect, in a container or
  otherwise. The real floor on `poll_interval_seconds` remains inventory's hardcoded
  `MIN_POLL_INTERVAL_SECONDS`. An operator who had set this variable loses nothing by
  its removal, since it never changed any behavior.
- The admin About page now tells an unreachable service apart from one that answered
  with a malformed body (issue #874): a `200` whose body fails the `ServiceVersion`
  shape check (a proxy's HTML error page, a truncated JSON body) used to render as
  "reachable" and compute a version-skew "differs" badge against garbage data. It now
  gets its own "invalid response" state, with dashed version/build/build-date cells
  and no skew badge. Version comparison also now recognizes release-candidate versions
  in both the backend's PEP 440 spelling (`0.6.0rc1`) and the frontend's semver
  spelling (`0.6.0-rc.1`), comparing rc numbers for equality while still treating rc,
  dev, and final as distinct pre-release kinds. The dead `ReservationPanel` component
  (nothing imported it) was also removed.
- The reservation assistant no longer discards a turn's tool writes when the turn
  fails after they already landed (issue #871): a timeout, an unreachable provider, or
  any other `AIError` used to roll the whole turn back once a write tool had already
  run, hiding the only record of what happened. The route now checks whether any tool
  produced a side effect before rolling back; if one did, it persists what completed
  and returns 200 with the real `tool_calls`, `pending_apply` when applicable, a fixed
  fallback answer, and a new `incomplete` field naming the reason (`timeout`,
  `provider_unavailable`, or `ai_error`). A turn with no tool run and no text still
  returns 502 and rolls back as before.
- Execution now refuses a driver action its driver's own contract does not support,
  instead of only failing deep inside the sandbox (issue #870). `POST /execute` and
  `POST /execute/internal` reject a `configure` action against a connection type
  outside `CONFIGURE_CONNECTION_TYPES` with the same structured 409
  `driver_cannot_configure` shape inventory's apply endpoints already used, and reject
  ANY action against a device with no resolvable driver with a 409
  `device_has_no_driver`, both before any run row is created. This closes the gap left
  by inventory's own #839 gate, which the AI commit path (`committer.py`) bypasses by
  posting `configure` straight to `/execute`. Driver LOAD failures (a bad archive, a
  missing `Driver` class, an import error) are now sanitized the same way method-raise
  failures already were: the stored and returned error is the fixed string `driver
  load failed: <ExceptionClassName>`, with the full text going only to the execution
  service log.
- Reservations can be cancelled from the UI before they are active (issue #841): the
  reservations table and the reservation detail modal gated BOTH Release and Cancel on
  `status === "ACTIVE"`, so a PENDING or PENDING_PROVISION reservation had no Cancel
  button even though the API allows it. The gates now live once as `canCancel` and
  `canRelease` in `frontend/src/lib/reservationStatus.ts` and match the backend's rule
  exactly: cancel on every non-terminal status, release on ACTIVE only.
- The reservation assistant no longer reports failure for a turn whose tools already ran
  (issue #848): when the model ends a turn with no text AFTER at least one tool was
  dispatched, the assistant returns 200 with a fixed fallback answer, the real
  `tool_calls`, and `pending_apply` when a schedule call succeeded, and persists the
  turn. It used to return 502 and roll the conversation back while the tools' writes
  stayed in inventory. A no-text turn with no tool run still returns 502 and rolls back.
- `AI_GENERATE_MAX_REPAIRS`, `AI_RESOLVER_CANDIDATES_PER_TEMPLATE`, and
  `AI_RESOLVER_MAX_SEARCH_STEPS` now reach the ai-orchestrator container (issue #849).
  They were listed in `.env.example` and documented, but `docker-compose.yml` never
  passed them through, so setting them in `.env` did nothing. A repo-root unit test
  fails when an active `AI_*` key in `.env.example` is missing from the compose block.
- Version and build visibility (issue #846). Every service now reports its real
  version: `FastAPI(version=...)` reads the package version from its own
  `pyproject.toml` through `herd_common.version.service_version`, replacing the
  hardcoded `0.1.0` that eleven services had carried since the first release, so each
  `/docs` page and `/openapi.json` is correct and a release bump needs no second edit.
  Every service answers an unauthenticated `GET /version` with `service`, `version`,
  `build`, and `build_date`. The integration service keeps its `1.0.0` facade
  contract version, and its `/version` is excluded from the published `/api/v1`
  OpenAPI document. The build string is `git describe --tags --always --dirty`
  (`v0.5.0` at a release commit, otherwise `vX.Y.Z-N-g<hash>`, where N is the number
  of commits since the tag), computed on the host by the Makefile and passed to every
  image as a late build arg so the dependency layers stay cached; the build date is
  the HEAD commit date, not the wall clock, so an unchanged commit produces unchanged
  images. A bare `docker compose build` outside the Makefile reports `dev`. `make
  version` prints both values. The frontend shows its version on the login page and
  beside the HERD wordmark, and a new admin-only About page (`/admin/about`, last item
  in the Administration menu) lists the version, build, and build date of the frontend
  and of all 12 services, marks a service that does not answer as unreachable in its
  own row, and flags a row whose version or build differs from the frontend's; it
  treats the PEP 440 and semver spellings of one pre-release (`0.6.0.dev0` and
  `0.6.0-dev`) as the same, and never flags a `dev` build. See `docs/ENV_VARS.md` for
  `HERD_BUILD` and `HERD_BUILD_DATE`.
- Gated device-config apply on driver capability and stopped leaking raw driver
  exception text (issues #839 and #840): inventory's two apply entry points
  (`POST /devices/{id}/config-versions/{vid}/apply` and `.../schedule`) now refuse with
  a structured 409 `driver_cannot_configure` when the device's driver connection type
  has no `configure` in its contract (today, every type except Management), checked
  right after authorization and before any call to execution or job row creation; the
  apply_scheduler background loop carries the same check at fire time for jobs queued
  before this change or a driver swapped after scheduling. Config-version
  create/list/read/restore are unaffected on every connection type. New
  `herd_common.device_config.CONFIGURE_CONNECTION_TYPES` /
  `connection_type_supports_configure`, kept in parity with execution's
  `REQUIRED_METHODS` by a dedicated test. The AI assistant's `schedule_config_apply`
  tool surfaces the 409's plain-English message instead of a dict repr. Separately, a
  driver method (or its instantiation, or driver loading) that RAISES now stores and
  returns only the exception class name (e.g. `"driver raised AttributeError"`), never
  the message, on an execution run and an apply job's `error` field; the full text
  still goes to the execution service log, tagged with `run_id`. A driver that RETURNS
  a failure verdict (`{"success": false, ...}`) is unaffected and keeps its own
  message. The sandbox reads only the LAST stderr line for the runner's structured
  exception record, so a warning or a library log record ahead of it (the normal case
  when a network driver cannot reach its device) no longer defeats the sanitizing, and
  a child that fails with no structured line stores the pinned `driver process exited
  with status N` rather than its raw stderr, which is logged instead. See
  `docs/DRIVERS.md` for the full rule and the `frr_l3`/`frr_mgmt` pairing example.
- AI topology generation gained cabling-aware device resolution (issue #828, PR #836):
  the resolver (`services/ai-orchestrator/app/services/resolver.py`) assigns concrete
  devices to proposed roles with a deterministic backtracking search over candidates
  the cabling graph can actually connect, checked through cabling's `POST
  /pathfind/batch`, replacing the previous first-N-available assignment. An infeasible
  proposal gets a corrective re-prompt naming the unconnectable template pairs before
  failing with a structured 422 `topology_unconnectable`. A stricter per-port capacity
  bound was tried and removed before merge: it refused topologies HERD's own validator
  accepts. New settings `AI_RESOLVER_CANDIDATES_PER_TEMPLATE` (default 8) and
  `AI_RESOLVER_MAX_SEARCH_STEPS` (default 5000).
- Commit-time wireability fail-fast for AI-generated topologies (issue #827, PR #835):
  the committer calls cabling's `POST /topologies/{id}/validate` against the just-saved
  canvas, before the reservation is created, raising a structured 422
  `topology_unwireable` (or a 503 when cabling cannot answer) instead of only
  surfacing the problem later through reservations' generic connectivity check.
  Generation also now rejects a self-loop edge and a duplicate device-to-device edge
  as repairable mistakes. The repair cap is now the setting `AI_GENERATE_MAX_REPAIRS`
  (default 2), replacing a hardcoded single retry.
- Added an opt-in evaluation harness for AI topology generation (issue #826, PR #833):
  `tests/ai_eval/` scores the generator's end-to-end wireability pass rate against a
  running, seeded stack with an AI provider configured. Skips unless
  `HERD_AI_EVAL=1`; never runs in a gate or CI. Run with `make ai-eval`.

## [0.5.0] - 2026-09-15

- Shipped first-class Layer 3 routing intent end to end (ADR 0014, issue #34, phases 1
  to 4): routes declared on a Layer 3 switch node in the topology editor, validated at
  save against the switch's config with fourteen refusal reasons, resolved into
  `fork_l3_routes` on activation and save, and driven by the execution service in
  precedence over config-version routes. The three follow-ups closed too: virtual
  routers reach the driver contract behind a `supports_vrf` capability (#755), attachment
  is checked at the interface, not just the switch (#756), and a non-admin's canvas is
  redacted to their visible devices before validation and pathfinding (#763).
- Shipped the emulated-gear test tier (ADR 0010, phases 0 to 3b): a checked-in NOS lab
  (FRRouting and Nokia SR Linux, images pinned by digest), the first two real driver
  packages, `frr_l3` (Layer 3 Switch) and `srl_l2` (Layer 2 Switch), a binding driver
  contract that a device rejection is a failure, and two CI tiers (issue #785): the
  dialect suites on every PR and inside `make everything`, the via-stack feature suites
  nightly.
- The reservation assistant can consult documentation (ADR 0015, issue #31): `search_docs`
  and `read_doc` over the built-in manual, operator corpora, and allowlisted https
  sources, advertised only when a source is enabled and refused at the dispatch boundary
  otherwise.
- Wiring retry hardening: retry failure writes are by row identity (#814), the two retry
  channels claim a row before driving it so a driver is never called twice for one row
  (#817, a seventh retry outcome `in_progress`), and the nightly uploads per-test e2e
  artifacts on failure (#813).
- Seeding moved into the `seedtools` package with a subcommand CLI (`full`, `acl`, `frr`,
  `nos`; issue #791), replacing the root script and both shell wrappers.
- Purpose classification gains an admin per-reservation trigger,
  `POST /admin/purpose-review/{id}/classify` (#808), the operator's retry for one row
  without a global backfill.
- Consolidations and fixes from the 2026-09-12 review of the driver batch (#778 to #784),
  `GET /connections` on the shared visibility helper (#809), and the frontend test runner
  on vitest 5 with a temporary jest-dom type shim (#806).
- fix(frontend): the live-edit Commit button is disabled until the reservation fork
  has hydrated; an early click saved an empty fork version and failed the device-set
  update with 422 (#824, PR #832).

### Delivery detail

#### Layer 3 routing intent

- Layer 3 routing intent is now checked at the INTERFACE, not just the switch
  (issue #756, ADR 0014 addenda X-J and X-K). A `Layer 3 Switch` config's
  `interfaces` entries take two optional fields, `kind` (`physical`, the
  default, or `logical`) and `port` (the HERD inventory port name a physical
  interface maps to, defaulting to the interface's own name). A route out of a
  physical interface whose port carries no resolved hop is refused with a new
  reason, `l3_interface_unwired`, at the fork save gate, at topology and
  reservation-create validation, and again at execution's drive-time
  re-validation, all through the one shared `herd_common.l3_validation`. A
  `logical` interface (a loopback, an SVI, a dummy interface enslaved to a
  virtual router) is exempt and the switch-level `l3_switch_unattached` check
  is unchanged. Existing configs are read the strict way, every interface
  physical with its port equal to its name, so a switch whose OS interface
  names differ from its HERD port names needs `port` declared.
- Virtual routers now reach the Layer 3 driver contract (issue #755, ADR 0014
  addenda X-G, X-H, X-I). `configure_route` and `remove_route` take a
  `virtual_router` keyword, and a driver opts in by declaring `supports_vrf` in
  its `driver_metadata.json`; execution passes the keyword only to a declaring
  driver, because every Layer 3 signature ends in `**_` and a non-declaring one
  would swallow it in silence and install the route in the default table.
  A switch whose driver does not declare it still has its VRF routes refused
  with `l3_vrf_unsupported` and no driver call. A route's virtual router is now
  validated against the switch's config (`l3_unknown_virtual_router`,
  `l3_interface_outside_virtual_router`, `l3_interface_bound_to_virtual_router`),
  by one shared `herd_common.l3_validation` the cabling save gate and execution's
  drive-time re-validation both import instead of the two hand-synced copies they
  carried. `drivers/frr_l3` renders the VRF form and now reports a route FRR
  accepted but never installed as a failure; `drivers/mock_l3` records the VRF;
  and the checked-in FRR lab node boots with a real VRF fixture so the live
  suites can prove the success case, not just the refusal.
- Shipped phase 1 of first-class Layer 3 routing intent (ADR 0014, issue #34):
  cabling migration 0011 adds `fork_l3_routes`, a canvas parser reads
  `data.l3.routes` on Layer 3 Switch device nodes, both fork write paths (fork
  save and fork-on-activation) reconcile the resolved route set under the
  existing fork row lock, and `/topologies/{id}/validate` and `/validate/internal`
  gain an `invalid_routes` pass with ten refusal reasons (malformed shape, not a
  router, unconfigured, unattached, duplicate route, and five per-route IP/interface
  checks), fail-closed with a 503 `l3_config_unavailable` on an inventory outage.
  Reservations' create-time gate folds route problems into its existing
  unreachable-topology error, and the fork's resolved routes ride the internal
  and user-facing fork GET. Inventory gained an internal batch device-type
  lookup (`POST /internal/devices/batch`) to serve the validation pass.
  Editor and execution-side consumption are phases 2 and 3, shipped below.
- Shipped phase 2 of first-class Layer 3 routing intent (ADR 0014, issue #34):
  the topology editor's canvas contract gains `data.l3` on a device node
  (`L3RouteIntent[]`, persisted through fork commit, autosave, and restore
  preview like any other node data), a new Routing panel that appears when a
  single Layer 3 Switch device node is selected (add/edit/remove routes,
  import the switch's latest device-config routes with a confirm-to-replace
  when rows already exist), a route-count badge on the canvas node that turns
  red on a validation problem, and the editor's first call to
  `POST /topologies/{id}/validate`, fired after a plain topology save when the
  canvas carries any routing intent. Fork-save (409 `l3_intent_invalid`/422
  `l3_intent_malformed`/503 `l3_config_unavailable`) and reservation-create
  (422 `topology_routing_intent_invalid`) refusals render the same per-node
  badge and per-row reasons. The fork version diff reports a route-set change
  on an otherwise-unchanged device node ("Routing changed on <device>: +A
  routes, -B routes"), comparing destinations in the server's canonical form
  so a canonicalization-only difference is never reported as a change nobody
  made. Execution-side consumption (phase 3) shipped below.
- Shipped phase 3 of first-class Layer 3 routing intent (ADR 0014, issue #34):
  execution now consumes a fork's resolved routing intent instead of always
  reading a switch's latest config version. Precedence is per switch: intent,
  when the fork carries it, IS the route list; otherwise the pre-phase-3
  config-version fallback is unchanged. A switch that stays adjacent across a
  fork save now reconciles its route-set DELTA against changed intent (removes
  before adds, one login/logout, the pin advancing only on full success), the
  one deliberate exception to the "pinned, never re-derived" rule; an
  inter-switch trunk hop that the adjacency derivation would otherwise skip
  still counts when either end carries intent. Before any intent-driven route
  drives, it is re-validated against the switch's CURRENT config when its
  validation stamp is missing or stale, and any route naming a `virtual_router`
  refuses the whole switch until the driver contract gains VRF support (issue
  #755): either way the switch lands a FAILED pin with the refusal reason and
  no driver call, never a partial drive. The retry channel drives a
  still-adjacent switch's CURRENT intent, not a stale FAILED row's own routes,
  when intent is present. The four-field route identity packing moved to
  `herd_common.l3_route_identity.route_identity_key` (issue #757, closed),
  shared byte-identical between cabling's `fork_l3_routes.route_key` and
  execution's fork-route-to-pin comparisons. `tests/load/locustfile.py` gained
  a `RoutedTopologyValidator` user class exercising the L3 validate pass under
  load. No schema change on the execution side.
- Fixed issue #758: `fork_l3_routes.route_key` was `String(400)`, which legal
  routing intent could overflow (a 64-character non-ASCII `virtual_router` and
  the default `json.dumps` escaping inflated past 400 characters), 500ing a
  fork save and, on activation, stranding the reservation with no fork at any
  layer. `route_key` now packs with `ensure_ascii=False` and the column widens
  to `Text` (cabling migration 0012).
- Fixed issue #759: the reservations-to-cabling fork SAVE forward used the
  default 10s timeout while cabling's L3 save gate may run up to its own 12s
  deadline, so a save could time out on the reservations side and 503 the user
  while cabling went on to commit it. The save forward now uses a 20s budget,
  matching the headroom already given to the validate forward.
- Docs and manual for phase 4 of first-class Layer 3 routing (ADR 0014, issue
  #34): with all three delivery phases merged, `PLANNED_FEATURES.md`'s entry
  flips from `Partial` to `Shipped` with a phase-by-phase summary and PR
  numbers, and `FEATURES.md` gains a Topology-section bullet describing the
  Routing panel (route table, Import from device config, the route-count
  badge) plus a corrected one-sentence precedence statement (fork routing
  intent, else the switch's latest config version) on the existing Layer 3
  provisioning bullet. `docs/ARCHITECTURE.md` had one stale "not yet built"
  sentence left over from when phases 2 and 3 were still in flight, corrected
  to say both shipped. The published manual's `user-topology.html` gains a
  Routing panel mockup in the wiring section, `user-live-editing.html#fork`
  gains one sentence on the fork-diff "Routing changed" line, and
  `glossary.html` gains a Routing intent term.

#### Emulated-gear tier and drivers

- Wired the NOS lab dialect tier into `make everything` (Lane's decision,
  2026-09-12): it now runs `make nos-test-dialect` as its own phase, after the
  live LDAP auth phase and before frontend coverage, so a full local gate also
  proves the real SR Linux and FRR dialects. `make master` is unchanged and
  does not run it; the via-stack feature tier stays nightly-only either way.
  `tests/unit/test_nos_lab_ci_wiring.py` pins both sides of the asymmetry.
- Split the NOS lab suites into two CI tiers (issue #785). The four dialect
  suites, which drive one driver against one lab node with no HERD stack, run
  on every pull request from ci.yml's new advisory `nos-dialect` job; the two
  via-stack feature suites, which drive a real SR Linux and a real FRR node
  through HERD's own reservation path, run in nightly after the seed step and
  before the load test. Both go through new Makefile targets,
  `make nos-test-dialect` (boots the lab if it is not already up, tears down
  only what it started) and `make nos-test-feature` (attaches the lab, seeds
  it, always detaches), so a workflow and a local run share one recipe.
  `tests/unit/test_nos_lab_ci_wiring.py` fails if a new file under
  `tests/nos_lab/` belongs to neither tier. Neither target is wired into
  `make master` or `make everything`.
- Tightened the two `tests/nos_lab/` via-stack feature tests so they prove what
  they claim (issue #782). The FRR Layer 3 test now runs a fork SAVE with a
  CHANGED route set, which is the only path that exercises `gate_l3_intent`,
  the staged `reservation.wiring_changed`, and execution's route-set delta
  reconcile, and it verifies both halves of that delta on the real router; its
  rejection test pins the device's own `% Unknown command:` wording instead of
  any non-empty error string; every destination assertion now matches the FULL
  prefix (the old `split("/")[0]` form matched a leaked neighbouring prefix);
  and a non-200 from `GET /execution/runs` now fails loudly instead of being
  swallowed into "the route was never configured". The SR Linux Layer 2 test
  now activates over an edgeless canvas and adds the edge in the fork save, so
  the membership is attributable to the save; its device baseline is taken
  before the reservation exists and scoped to the two ports and the allocated
  VLAN rather than "no mac-vrf anywhere"; and its device cleanup checks its
  exit status and reports the `sr_cli` stderr. Both files now probe their
  preconditions from a session-scoped fixture, so collection touches neither
  the network nor docker.
- Closed four rejection-classification gaps in the FRR drivers (issue #779)
  and the shared `HERD_port` parsing defect (issue #780). `drivers/frr_mgmt`
  and `drivers/frr_l3` now anchor the benign "already absent" carve-out at the
  START of the device's line instead of testing for it anywhere in the line:
  FRR quotes the offending command back inside `% Unknown command: ...`, so a
  config line containing that phrase had its genuine rejection reported as
  success (verified live on the NOS test lab node). `frr_mgmt.configure()` now
  classifies its `write memory` output instead of assuming it worked, since a
  failed save prints no `%` line and exits 0; it requires a
  `configuration saved to ...` line and reports `{"success": False}` with the
  offending save line otherwise, and its failure payload calls the commands
  `attempted` rather than `applied`. `frr_l3` got `frr_mgmt`'s
  collect-and-partition scanner, so `remove_route` reports the first GENUINE
  line and treats a response as `already_absent` only when every `%` line in
  it is benign. Both drivers now parse `HERD_port` in `_connect()` rather than
  the constructor, treating blank or missing as 22 and raising
  `DriverError("HERD_port must be an integer")` otherwise, so a device with a
  present-but-empty `port` field degrades to `{"reachable": False}` instead of
  failing the sandbox before any method runs. `docs/DRIVERS.md` gained the
  anchoring and persist-classification rules, the Hypervisor
  `create_instance`/`destroy_instance` exception to "an absent key stays
  success", and a note that `frr_l3` deliberately does not persist its
  reservation-scoped routes.
- Fixed `drivers/srl_l2` applying part of a batch it reported as failed, and
  committing configuration staged by an earlier call that died (issue #778). SR Linux
  stages every `set`/`delete` in a per-user private candidate that only `commit stay`
  applies, and that candidate outlives the SSH session: netmiko keeps sending after a
  rejected line, so the old unconditional `commit()` applied the valid PREFIX of a
  rejected batch (proven live: `admin-state enable` landed on a port HERD believed
  untouched while the driver returned `success: False`), and any exception path left
  lines staged for the next call's commit to pick up through the device. `_apply` now
  discards the candidate at entry, returns failure on a set-time rejection before it
  ever commits, discards on a commit refusal, and discards in a `finally` on exception
  paths. The module docstring's "shared session" rationale is replaced with the
  persistent-candidate fact (the sandbox runs one process per action, so there is no
  shared session) and gains the best-effort limitation block `docs/DRIVERS.md`
  requires. Also (issue #780, `srl_l2` only) an optional `HERD_port` that is blank now
  means 22 instead of raising `ValueError` from the constructor, and a non-integer
  raises `DriverError("HERD_port must be an integer")` from `_connect`, so `status()`
  still degrades to `{"reachable": False}` for health polling. New live regression
  tests in `tests/nos_lab/test_srl_l2_driver_live.py` verify the device independently
  through a separate `docker exec nos-test-srl sr_cli` session.
- Pinned the NOS lab and Selenium images by digest (issue #783): SR Linux
  (`26.7.2-519`), the FRR base image (no matching version tag; digest only),
  and `selenium/standalone-chrome` (`4.43.0-20260404`), plus the FRR
  Dockerfile's `apk add openssh` version; added `--wait-timeout 120` to the
  Selenium `up --wait` recreate; tightened the Makefile `down`-line test
  regex to catch `@`/`-`/`VAR=value`-prefixed and conditional spellings;
  made `nos-detach` report per-container attachment and exit non-zero on a
  real disconnect error; fixed `scripts/seed_nos_lab.sh`'s misleading
  `SEED_NOS=1` message on its default `--nos-only` path; hardened
  `infra/nos-test/frr/start.sh` (`set -e`, an sshd-up check) and extended the
  FRR healthcheck to also probe port 22.
- Shipped phase 3b of the emulated-gear test tier (ADR 0010), the Layer 2
  counterpart of phase 3a's Layer 3 proof: `tests/nos_lab/test_srl_l2_via_stack_live.py`
  proves HERD derives a Layer 2 VLAN membership from a reservation's wiring and
  configures it on the REAL Nokia SR Linux node entirely through HERD's own API,
  execution service, and driver sandbox (drivers/srl_l2), not by calling the driver
  directly. It reuses the seeded `nos-lab-dut-1`/`nos-lab-dut-2`/`nos-lab-srl`
  devices and cabling (`scripts/seed_nos_lab.sh`), wires the two DUTs together on a
  topology canvas with no switch node (cabling's pathfinder resolves it through the
  real switch's `ethernet-1/1`/`ethernet-1/2` ports), activates a reservation over
  it, and saves the fork to drive the connection-driven L2 reconcile (ADR 0009).
  It reads the VLAN id HERD allocated from the reservation's own
  `GET /reservations/{id}/wiring-status` surface instead of assuming a number,
  independently verifies on the real device (a separate `docker exec nos-test-srl
  sr_cli` session, never the driver's own session) that the `mac-vrf`
  network-instance exists AND both subinterfaces are bound into it, cancels the
  reservation, and independently verifies both the port bindings and the VLAN
  definition are gone, cross-checking HERD's own ledger (RELEASED) against the
  device at each step. `delete_vlan` runs in its own driver session shortly after
  the membership ledger flips RELEASED, so the teardown-side device check polls
  rather than asserting once. Same gating and cleanup discipline as
  `test_frr_l3_via_stack_live.py`; see docs/NOS_LAB.md.
- Shipped phase 3a of the emulated-gear test tier (ADR 0010): the checked-in
  NOS test lab can now be wired into a running HERD stack so HERD drives the
  real devices through its OWN execution service and driver sandbox, not by
  calling a driver directly. New Makefile targets `nos-attach`/`nos-detach`
  connect/disconnect the lab containers (`nos-test-srl`, `nos-test-frr`) to
  the dev stack's Docker network, so the execution service reaches them by
  CONTAINER NAME over Docker DNS (container IPs are not stable across a
  recreate; container names are). `seed_devices_public.py` gained
  `seed_nos_lab` (gated by `SEED_NOS=1`, mirroring `seed_frr_demo`'s
  `SEED_FRR=1` shape): it registers the real `drivers/srl_l2` and
  `drivers/frr_l3` packages, one device template each, the two lab devices
  with `field_data.ip` set to the container name, two placeholder DUT
  devices, and cabling to the SR Linux node's `ethernet-1/1`/`ethernet-1/2`
  ports, laying the groundwork for a later phase to derive an L2 VLAN
  membership from recorded L1 hops (ADR 0009); a new `--nos-only` seed-script
  mode and `scripts/seed_nos_lab.sh` wrapper mirror the existing `--acl-only`/
  `seed_frr_demo.sh` pattern. `tests/nos_lab/test_frr_l3_via_stack_live.py`
  is the end-to-end proof: it drives a real static route onto the real FRR
  node entirely through HERD's API (a reservation whose fork carries L3
  routing intent, ADR 0009/0014), independently verifies the change via
  `docker exec ... vtysh`, removes it via reservation cancellation, and
  proves a device-rejected route lands as a FAILED execution run with the
  device's own error text, not a false success. `tests/unit/test_seed_nos_lab_driver.py`
  pins the new driver-zip-from-disk helper. Opt-in, needs both the lab
  (`make nos-up`) and a stack with the lab attached (`make up`,
  `make nos-attach`); not part of `make test`, `make master`, or
  `make everything`. See `docs/NOS_LAB.md`.
- Made "a driver must report a device rejection as a failure" a binding
  contract (issue #771): `docs/DRIVERS.md` gains a section beside "Dry-run
  support" laying out the rule, since both are binding requirements with a
  stated consequence, return `{"success": False}` on a device rejection
  rather than raising, put the offending line in `error` since that is what
  lands in a wiring assignment's `last_error` column, and never verify a
  driver's own work through its own read path. `drivers/frr_mgmt.configure()`
  is the fix: it classifies every `%` line in the config output, reports
  `{"success": False}` with the device's own offending line on any genuine
  rejection, and counts a batch as clean only when every `%` line found is a
  recognized benign marker, surfaced under `benign_warnings` instead of
  masked as a plain success. User-visible consequence: a config apply whose
  batch includes an idempotent removal (`% Refusing to remove a
  non-existent route`) now succeeds with `benign_warnings` rather than
  failing. New opt-in coverage in `tests/nos_lab/test_frr_mgmt_driver_live.py`
  drives the real driver against the FRR node and independently verifies the
  rejection case installs nothing.
- Added `drivers/frr_l3/`, the first real Layer 3 Switch driver package: SSH-to-vtysh over the same FRRouting node and netmiko `cisco_ios` transport as `drivers/frr_mgmt`, implementing `login`/`logout`/`configure_route`/`remove_route`/`status` per the Layer 3 Switch contract (docs/DRIVERS.md). `configure_route` sends `ip route <destination> <next_hop>`, or `ip route <destination> <interface>` when `next_hop` is `None` (an interface route); `remove_route` sends the same line prefixed with `no `. `supports_dry_run: true` is honored on every mutating method (no session opened, transcript still recorded). Verified live against the checked-in NOS test lab's FRR node (docs/NOS_LAB.md) that both `configure_route` and `remove_route` are idempotent under redelivery: FRR silently no-ops a duplicate route install, and a duplicate removal answers a benign CLI warning rather than an error, so this driver treats both as success, matching the redelivery guarantee the Layer 2 contract states explicitly for `create_vlan`. `tests/unit/test_frr_l3_driver.py` (stack-free, netmiko mocked) and `tests/nos_lab/test_frr_l3_driver_live.py` (opt-in, `HERD_TEST_NOS_REQUIRED=1`) cover it; see docs/DRIVERS.md's new "FRR reference driver" section and docs/NOS_LAB.md.
- Added phase 0 of the emulated-gear test tier (ADR 0010): a checked-in,
  license-free network-OS lab (`infra/nos-test/`) with two nodes, Nokia SR
  Linux (Layer 2, netmiko `nokia_srl`) and FRRouting over SSH-to-vtysh
  (Layer 3 / Cisco dialect, netmiko `cisco_ios`), modeled on
  `infra/ldap-test/`'s stateless, seed-on-boot shape. SR Linux needs a small
  checked-in CLI baseline (`infra/nos-test/srl/baseline.cli`) applied and
  saved on every boot to clear a factory-fresh device's `[FACTORY]`
  config-prompt tag, which otherwise breaks netmiko 4.7.0's
  `nokia_srl.check_config_mode` regex. New Makefile targets `nos-up`,
  `nos-down`, `nos-status`, `nos-logs`, `nos-reset` (opt-in only, not part of
  `make test`, `make master`, or `make everything`); `tests/unit/test_nos_lab_compose.py`
  pins the compose file's stateless shape and port non-collision with the
  dev/gate stacks; `tests/nos_lab/test_nos_lab_live.py` is an opt-in live
  suite (`HERD_TEST_NOS_REQUIRED=1`, mirroring the LDAP live-suite
  convention) that creates and independently verifies a VLAN on SR Linux
  and a static route on FRR. See `docs/NOS_LAB.md`.
- Added `drivers/srl_l2/`, HERD's first real (non-mock) Layer 2 Switch
  driver: Nokia SR Linux over SSH via netmiko's `nokia_srl` platform,
  modeled structurally on `drivers/frr_mgmt/` (HERD_-prefixed connection
  params, dry-run gating, `record_command`, `DriverError` on missing
  connection params) but mapping VLANs onto SR Linux's mac-vrf
  network-instance model instead of Cisco's flat `vlan <id>` object: a
  VLAN is a `network-instance vlan<id> type mac-vrf`, and a port joins it
  through a tagged-or-untagged bridged subinterface plus a binding. Every
  mutating command is issued as an absolute `set /` or `delete /` path, a
  load-bearing detail: SR Linux's candidate-mode CLI otherwise carries a
  "current context" across commands in the same session that an
  unqualified command can silently inherit. `create_vlan` and
  `delete_vlan` are idempotent by construction (SR Linux treats a
  redefinition or a delete-of-missing as a no-op commit), proven live, not
  merely asserted, in the new `tests/nos_lab/test_srl_l2_driver_live.py`.
  `tests/unit/test_srl_l2_driver.py` pins the exact command text for the
  tagged and untagged forms and the commit-per-mutating-op invariant with
  netmiko mocked. See `docs/DRIVERS.md` and `docs/NOS_LAB.md`.

#### AI orchestrator

- The reservation assistant can now consult documentation instead of answering
  from training data (issue #31, ADR 0015). Two read-only tools, `search_docs`
  and `read_doc`, search and page through named sources: the published HERD
  manual, which now ships inside the ai-orchestrator image, plus any corpus an
  operator mounts through `AI_DOCS_CORPUS_DIRS`, plus allowlisted https URLs
  when `AI_DOCS_WEB_ENABLED` is on (off by default). The tools are advertised
  only when at least one source is enabled and refused at the dispatch
  boundary otherwise; a web fetch must match an allowed prefix, resolve
  entirely to public addresses, survive re-validation at each of at most three
  redirects, declare a text content type, and stay under a byte cap. Results
  go through the same per-tool size cap and untrusted framing as every other
  tool result.

#### Wiring retry and execution

- The two wiring retry channels can no longer drive the same FAILED row at once
  (issue #817). The manual endpoint and the background tick both load
  hardware-retryable FAILED ledger rows and drive them through the same applies;
  issue #814 stopped a late failure write from corrupting the ledger, but
  nothing claimed a row before it was driven, so one connection could get two
  concurrent driver calls, from two execution processes even, since the tick
  runs in every replica. Each retried row is now claimed by a compare-and-swap
  on a new nullable `claimed_until` column immediately before that row's own
  driver call, and a row the other channel holds is skipped with no driver call
  and no ledger write. The claim is taken at drive time rather than at
  selection because one batch is driven sequentially behind a per-switch login,
  so the last row of a batch can reach its driver call minutes after the batch
  was selected. Its budget is derived from the driver-call timeout and the
  in-line attempt count rather than a new setting, every record path clears the
  stamp, and a stamp left behind by a process that died mid-drive expires by
  itself, so there is no reaper and no heartbeat. The retry outcome vocabulary
  gains a seventh value, `in_progress`, for a row the other channel was already
  driving: reporting it beats omitting it, since an absent row reads as one that
  was already fixed. The L1 and L2 FAILED-to-ACTIVE success flips also moved to
  the same SQL compare-and-swap L3 already used, so all three layers share one
  flip discipline.
- A wiring retry no longer resurrects rows that were released while it was
  running (issue #814). Both retry channels (the manual endpoint and the
  background tick) now record a driver failure against the ROW they loaded,
  under a compare-and-swap on (id, status FAILED), instead of upserting by
  (reservation, switch, port/pair). The upsert matched only non-RELEASED rows,
  so a retry whose rows were flipped ACTIVE by the manual channel and then
  released by a fork save during its driver call found nothing and INSERTED a
  fresh FAILED row, leaving zombie build-direction failures for wiring the
  reservation no longer intends (four L2 membership rows where two were
  correct). The stale write is now a logged no-op at all three layers; the
  fresh-build path keeps its upsert, since a build after a release is a
  legitimate re-add.

#### Purpose classification

- An admin can now classify one reservation's purpose on demand instead of waiting for
  the background sweep to reach it (issue #808). `POST
  /admin/purpose-review/{reservation_id}/classify` runs the sweep's own single-row
  classifier synchronously for exactly that reservation and returns its outcome (`ok`,
  `timeout`, `transient`, `failed`, or `forbidden`, all still 200; `feature_off`
  answers 503 instead, matching how other AI-gated endpoints respond when
  unconfigured); 404 for an unknown reservation, 409 `not_eligible` before the
  reservation reaches a terminal state, 409 `already_suggested` once a suggestion
  already exists. It deliberately ignores `purpose_classify_max_attempts`, so it
  doubles as a way to retry one exhausted row without running the global backfill, and
  the sweep's oldest-first fairness is unchanged. The single-row classifier moved from
  the expiration task to `purpose_service.classify_purpose_one` so the new router does
  not import from `tasks/`; the sweep reconciler now calls it from its new home. Fixes
  the reused-stack flake in `test_purpose_review_flow.py` (the test now triggers its
  own reservation directly instead of waiting on the sweep's backlog), and the
  now-unused `classify_sweep_first` pytest marker and its collection-reordering hook
  are removed.

#### Seeding, CI, and tooling

- Nightly now uploads the per-test e2e artifact directory on failure (PR #813): the
  workflow sets `HERD_E2E_ARTIFACT_DIR` into the workspace so the failure upload carries
  each failed test's screenshot, page HTML, console log, and traceback alongside the
  compose logs; `tests/unit/test_nightly_e2e_artifacts_wiring.py` pins the wiring.
- Moved the frontend test runner to vitest 5 and `@vitest/coverage-v8` 5 (PR #806,
  superseding Dependabot #796 and #800, which each failed alone on a peer conflict).
  jest-dom 7.0.1 augments vitest's `Assertion` with one type parameter and vitest 5's
  has two, so every matcher vanished from the type system (1220 `tsc` errors);
  `frontend/src/test/vitest-jest-dom.d.ts` is a temporary shim that re-augments the
  two-parameter shape until jest-dom ships a compatible release (upstream
  testing-library/jest-dom#738). Runtime was never affected: the same 1372 tests pass
  before and after.
- Moved seeding into a `seedtools/` package with a subcommand CLI and retired
  the root module and both shell wrappers (issue #791). `seed_devices_public.py`
  (2,637 lines), `scripts/seed_frr_demo.sh`, and `scripts/seed_nos_lab.sh` are
  gone; `python -m seedtools full | acl | frr | nos` replaces them, with
  `--full` on `frr` and `nos` layering a demo onto the whole population the way
  the wrappers did, and `make seed`, `make seed-frr`, and `make seed-nos` as
  the front doors. Credential resolution lives once in `seedtools/client.py`
  (SEED_*, then SUPERADMIN_* from the environment, then SUPERADMIN_* read from
  `.env`, then a generic placeholder), so the Makefile recipe and the two
  wrappers no longer each re-implement it, and the personal default address the
  root module carried is gone. Behavior is otherwise unchanged: the same
  section headers print, every subcommand is still re-runnable, and SEED_FRR=1
  / SEED_NOS=1 still layer the demos onto `full`. The four seed unit tests now
  import the package instead of loading a file by path.
- Fixed `pick_dut_template`'s last fallback tier in `seed_devices_public.py`
  (issue #781, follow-up to #775): it returned `items[0]` even when no
  template declared the seed fields, guaranteeing a create that fails
  inventory validation. It now returns `None` in that case, which the caller
  already handled by skipping ACL fixture seeding. Also hardened
  `template_field_keys` to skip fieldless entries and gave the
  `get_or_create_device` field-key assert a message.
- Fixed `make seed` failing with "Unknown fields: ip, login, password"
  when an integration-suite-seeded template (`int-seed-template-*`,
  Management-backed but declaring only `model`) sorts ahead of a template
  that declares the fields `get_or_create_device` sends (issue #775).
  `pick_dut_template` now only considers templates that declare every key
  the seed will send, preferring a Management-backed one among those, then
  any usable one, then the previous fallback; the required key set lives in
  one constant, `SEED_DEVICE_FIELD_KEYS`, which `get_or_create_device`
  asserts against so the two cannot drift apart.
  `tests/unit/test_seed_pick_dut_template.py` covers the exact ordering
  from the bug.
- Fixed the stack-lifecycle Makefile targets so a `make everything` gate cannot be
  blocked by a leftover e2e Selenium container: every compose `down` (`down`, `clean`,
  `clean-data`, `gate-clean`, `_clean-images`, `_master-stack-down`) now enables the
  `e2e` profile, since a profile-gated service that is not enabled is neither active
  nor an orphan and survived a plain `down` holding host port 4444. The e2e recreate
  now uses `--wait` against a new Grid healthcheck on the Selenium service, so the
  first Playwright page load no longer races the new container's network setup
  (`net::ERR_NETWORK_CHANGED`, the recurring first-test login timeout). Pinned by
  `tests/unit/test_makefile_stack_lifecycle.py`.

#### Visibility and security

- Internal refactor, no behavior change: `GET /connections` (issue #719) now
  resolves its non-admin visibility filter through the shared
  `resolve_caller_visibility` helper (issue #763) instead of an inline copy of
  the admin-check/fetch/503 sequence, the same helper the topology validate
  and pathfind routes already use. Status codes, detail text, the admin
  bypass, and the empty-visible-set short circuit are unchanged; the only
  observable difference is the warning logged on an unanswerable inventory
  lookup, which now carries the shared event key `caller_visibility_unavailable`
  instead of the route-local `connections_visibility_unavailable`, matching
  the other two consumers.
- Device visibility now gates the topology validate route and both pathfind
  routes (issue #763, hardening). For a non-admin caller, a canvas node naming
  a device outside their device-group visibility is reported as
  `missing_device` and is excluded from the reachability and Layer 3 passes, so
  `POST /topologies/{id}/validate` no longer answers interface and subnet
  questions about gear the caller cannot see; a pathfind pair naming such a
  device is refused with the same `404 Device not found` an unknown id gets
  (the batch route reports it per pair through a new `error` field), and a
  transit hop through one comes back redacted: `PathHop.device_id` is now
  nullable, with a new `hidden` flag and no port names. Hop counts,
  reachability and the topology editor are unaffected, admins are not filtered
  at all, and an unanswerable visibility lookup fails closed with a 503. The
  open `GET /topologies/{id}` stays open by decision, recorded in
  `docs/ROLES.md`.

## [0.4.0] - 2026-09-05

- Shipped lab purpose classification end to end (ADR 0013, issue #646, phases 1
  to 3): a closed taxonomy per reservation, an AI-suggested category the owner
  or an admin confirms on the new Administration > Purpose Review page, a
  background reconciler that classifies reservations as they end, and device-
  and user-level purpose reporting that counts transit gear on a reservation's
  paths, with CSV downloads for every purpose section.
- Ran a full multi-pass code review of the codebase (the 2026-09-04 review
  sweep) and closed all 21 resulting issues (#701 to #721). The headline fixes:
  fork saves and activation snapshots now refuse a canvas endpoint device
  outside the reservation's membership (cross-tenant provisioning was reachable
  before), port claims across forks are serialized with transaction-scoped
  advisory locks at both fork write paths, scheduled config-apply jobs re-check
  the creator's authority at fire time, and three read surfaces are gated (group
  member emails are admin-only, device config-version reads follow device
  visibility, the connections list is filtered to the caller's visible devices).
- Made the transactional outbox wake on write (issue #682) through Postgres
  NOTIFY/LISTEN, so a staged event publishes within milliseconds instead of
  waiting for the relay tick; hardened the listener for reconnects, TLS and
  pooler DSNs, and saturated batches (#707). NATS pull consumers now fetch one
  message per pull, which fixed the first-run notification flake at its source
  (#648).
- Moved the purpose-classification reconciler onto its own background task with
  a transient outcome class, so a slow orchestrator no longer delays the
  expiration sweep and a rate limit no longer burns a row's attempts (#702,
  #706).
- Added AI-proposed network elements (#632): the topology generator can place
  the four element types and the commit path keeps them.
- Closed the fork restore-versus-save race with a row lock (#626), indexed the
  ACTIVE fork listing and preloaded the wiring ledger (#710), and stopped device
  lists from truncating at 500 rows (#703).
- Hardening batch from the sweep: Traefik's dashboard bound to loopback,
  superadmin deactivation refused, user-profile preference validation, bounded
  upload buffering that rejects an over-cap request before it is fully read,
  provider error text kept out of responses, and a fail-closed AI commit path
  for network elements.

### Delivery detail

#### Purpose classification and reporting

- Reliability fix: the purpose-classification reconciler now runs on its own
  background task instead of riding the reservations expiration sweep (issue
  #702, 2026-09-04 review sweep finding). The reconciler awaits one
  orchestrator LLM call per eligible row, serially, up to
  `purpose_classify_batch_size` rows (default 20) each bounded by
  `purpose_classify_timeout_seconds` (default 30s); a single tick could
  therefore run for minutes, delaying every other reconciler that shared the
  loop (scheduled activation, auto-completion, both provisioning-timeout
  backstops, fork archiving, wiring heal, and pending prune) behind a slow or
  hung orchestrator. `services/reservations/app/main.py` now starts a second
  task, `purpose_classify_loop`, at its own interval
  (`PURPOSE_CLASSIFY_INTERVAL_SECONDS`, default 60s), cancelled alongside the
  expiration task at shutdown; the reconciler's body stays sequential (no
  concurrent LLM calls). `expiration_loop` no longer calls the reconciler.
- Bug fix: the purpose-classification reconciler gains a `transient` outcome
  class and stops misreading an internal-token failure as the feature being
  off (issue #706, 2026-09-04 review sweep finding). Previously every
  non-403/404 failure, a 429 rate limit, a 502/503/504, a timeout, or a
  transport error, incremented a row's `purpose_classify_attempts` exactly
  like a genuine per-row rejection; a quota 429 lasts until UTC midnight
  (hundreds of sweep ticks), so a rate-limited owner's rows deterministically
  capped out, invisible in aggregate metrics. A new `transient` outcome for
  those cases ends the tick without touching any row's attempts, the same
  treatment `feature_off` already had. Separately, `POST
  /internal/classify-purpose` can 403 for two unrelated reasons
  (`AI_PURPOSE_CLASSIFICATION_ENABLED` off, or an internal-token mismatch),
  and the reconciler read either as "the feature is off", silently stalling
  the backlog after a token rotation while logging the wrong cause. The
  orchestrator's flag-off refusal now carries a structured detail,
  `{"error": "purpose_classification_disabled", "message": "Purpose
  classification is disabled"}`; the reconciler treats a 403 as feature-off
  only when that marker is present (falling back to the exact legacy
  plain-string detail for a pre-fix orchestrator image during a rolling
  upgrade), and logs any other 403 at WARNING as a likely internal-token
  problem instead. See the ADR 0013 amendment for the full outcome taxonomy.
- Added Download CSV buttons for the four purpose reporting sections (issue
  #696): `purpose`, `user_purpose`, `device_purpose`, and `purpose_suggested`
  join `UtilizationCsvSection` on the frontend, and `ReportingPage` gets a
  button on the purpose bar chart (confirmed data), a second button next to
  it for the AI-suggested bucket (shown only once `by_purpose_suggested` is
  in the report payload), and one on each purpose mix table (By User, By
  Device), all calling the existing `downloadUtilizationCsv` with the
  page's current window. The server has served these sections since #646
  phases 1-2; only the frontend wiring was missing.
- Added lab purpose classification, phase 3 (issue #646): device-level
  utilization reporting now inherits transit gear from cabling's fork
  wiring. Cabling gains `POST /internal/forks/devices/batch`
  (X-Internal-Token, 1 to 500 reservation ids) returning, per reservation
  with a fork (ACTIVE or ARCHIVED), the sorted distinct device ids across
  its `fork_connections` rows. Reservations' utilization report calls it in
  chunks of 500 and folds each reservation's fork-connected devices minus
  its own reserved `device_ids` into `by_device` and `by_device_purpose`: a
  transit device inherits the reservation's confirmed category (or
  `unclassified`) exactly as a reserved device does, and a device both
  reserved and on a path counts once, as reserved. `reservation_count`/
  `hours` and `reservations`/`device_hours` are now INCLUSIVE of transit
  gear; both buckets gain `transit_reservations` and a transit-hours field
  (`transit_hours` on `DeviceBucket`, `transit_device_hours` on
  `DevicePurposeBucket`) reporting the transit share alone. A new
  `include_transit` query parameter (default true) on both
  `GET /reports/utilization` and its CSV twin skips the cabling call
  entirely when false, reproducing phase-1 semantics; `UtilizationReport`
  gains `transit_included` echoing the effective value. Cabling being
  unreachable, or returning a non-200, fails CLOSED with 503
  `{"error": "transit_gear_unavailable"}` on both routes rather than
  silently reporting zero transit devices. The `device`/`device_purpose`
  CSV sections gain trailing `transit_reservations`/transit-hours columns.
  The reporting page's by-device Purpose Mix table gains a Transit column
  (hours plus a percentage for a mixed row, a small tag for a transit-only
  row) and a helper line naming which semantics are in effect.
- Added lab purpose classification, phase 2 (issue #646): the AI orchestrator
  gains `POST /api/ai/classify-purpose/preview` (user JWT, creation pass) and
  `POST /api/ai/internal/classify-purpose` (X-Internal-Token, end-of-
  reservation pass), both behind `AI_PURPOSE_CLASSIFICATION_ENABLED`
  (default off, enforced at the route boundary like recipe authoring). Each
  makes one forced `classify_purpose` tool call against a caller-supplied
  category list, assembling a prompt from purpose text, topology device/
  template/wiring signals, dynamic template names, config-apply job names
  and counts, fork version count, duration/status, and, when
  `AI_PURPOSE_INCLUDE_TRANSCRIPTS` is set (default on), the reservation
  assistant's own transcripts; a signal-fetch failure is logged and dropped
  rather than failing the request, reflected in the response's
  `signals_used`. `GET /api/ai/status` gains `purpose_classification`.
  Inventory gains one small additive internal endpoint,
  `GET /devices/{device_id}/apply-jobs/internal`, to feed the config-apply
  signal without a user JWT. See `docs/AI_PURPOSE_CLASSIFICATION.md`.
- Added lab purpose classification, phase 1 (issue #646): reservations gain an
  optional `purpose_category` validated against a configured taxonomy
  (`PURPOSE_CATEGORIES`), settable by the owner or an admin in any status
  including terminal via `PATCH /{id}/purpose-category`, listed via
  `GET /purpose-categories`, and carried additively on every
  `reservation.*` lifecycle event and the `/api/v1` facade. The utilization
  report and its CSV export gain `by_purpose`, `by_user_purpose`, and
  `by_device_purpose` breakdowns, with an explicit `unclassified` bucket for
  reservations with no category. Device-level classification counts reserved
  devices only; transit-gear inheritance is deferred to phase 3.
- Added lab purpose classification, phase 2 reservations-side pieces (issue
  #646, ADR 0013 points 8-11): a reservation transitioning into COMPLETED,
  CANCELLED, or FAILED now stamps `purpose_classify_requested_at`, the sole
  trigger for a new expiration-sweep reconciler that calls the AI
  orchestrator's internal classify-purpose endpoint in bounded batches
  (`PURPOSE_CLASSIFY_BATCH_SIZE`, capped at `PURPOSE_CLASSIFY_MAX_ATTEMPTS`
  retries per row) and stores the result verbatim as `purpose_suggestion`. New
  admin endpoints: `GET /admin/purpose-review` (paginated, filterable by
  suggested category) lists undismissed suggestions still worth a look,
  `POST /admin/purpose-review/{id}/accept` writes a suggestion (or a chosen
  override) into the confirmed `purpose_category`, `POST
  /admin/purpose-review/{id}/dismiss` drops a suggestion from the queue
  without touching the confirmed value, and `POST /admin/purpose/backfill`
  marks every terminal reservation with no suggestion yet as eligible
  (idempotent). The utilization report gains `by_purpose_suggested` (and a
  matching CSV section) for reservations with a suggestion but no confirmed
  category; `unclassified` now means neither a category nor a suggestion.
  Suggestions are internal review state, not exposed on the `/api/v1` facade.

#### Reservations and provisioning

- Pinned `PURPOSE_CLASSIFY_INTERVAL_SECONDS` to 5 in the dev/test compose
  override beside `EXPIRATION_INTERVAL_SECONDS` (release-gate catch): the
  purpose-review integration test, which only runs where an AI provider is
  configured, waits under the 30-second suite cap for the classifier loop
  that issue #702 moved onto its own 60-second default interval.
- Fixed reservations' cancel/release inventory cleanup treating a 404 from
  inventory as a hard failure (issue #716). Since #682 execution's terminal
  teardown starts roughly a millisecond after a cancel or release commits,
  and for a dynamic-only booking it deletes the materialized device; if that
  landed first, `_update_device_statuses` raised on the resulting 404,
  triggering retries and an ERROR log for a device that was correctly gone,
  and `_fetch_devices_best_effort` turned that same 404 into "assume
  exclusive", so the retry loop re-POSTed a status update against a device
  reservations had just been told did not exist. A 404 on the AVAILABLE
  (release) direction of `_update_device_statuses` now counts as success
  with no retry and no ERROR log; the RESERVED (booking) direction is
  unchanged and still raises, since inventory not recognizing a device being
  booked is a real failure. `_fetch_devices_best_effort` now surfaces a 404
  as the distinct `_DeviceGoneFromInventory` rather than a generic
  exception, and `_release_exclusive_devices_best_effort` drops that id from
  the release set with an INFO log instead of assuming it is exclusive and
  re-releasing it.
- Fixed the purpose-classify reconciler treating a per-call timeout as
  provider-wide evidence (issue #706 follow-up, release-gate catch, proven
  live on the dev stack): a timeout ended the whole sweep tick without
  bumping the row's attempt count, so one reservation whose classification
  call ran past `purpose_classify_timeout_seconds` blocked every older-queued
  row behind it forever, since the reconciler orders oldest-requested-first.
  Observed: the same reservation timed out on seven consecutive ticks with
  `purpose_classify_attempts` still 0 while 45 eligible rows waited behind
  it. A timeout now counts as its own outcome, bumps that row's own attempt
  count, and lets the tick continue to the next row, since a timeout is
  evidence about that row's call, not the whole provider; any other
  transport error (a connection error, for example) is unchanged and still
  ends the tick.

#### Cabling and forks

- Performance fix: the expiration sweep's per-tick ACTIVE fork reconcile no
  longer scans or reads without an index (issue #710, 2026-09-04 review sweep
  finding, medium and low severity). `reservation_fork` carried indexes only
  on `reservation_id`; nothing covered `status` or `created_at`, yet fork rows
  are never deleted by design (archived forks are the as-built record the
  transit report reads), so `GET /internal/forks`'s unconditional `COUNT(*)`
  plus filtered `ORDER BY created_at OFFSET`, drained fully every tick (60s in
  prod, 5s in dev), ran two unindexed passes over a table that grows forever.
  Cabling migration 0010 adds a partial index,
  `ix_reservation_fork_active_created_at` on `(created_at, id) WHERE status =
  'ACTIVE'`, mirrored in `ReservationFork.__table_args__` so the SQLite
  unit-test schema matches; no retention added, `total` stays in the listing
  response. Reservations' `_fetch_active_forks` now also stops paging on a
  page shorter than the requested limit, not only on reaching `total`, which
  is cheaper and does not depend on `total` staying consistent across pages
  against a live set. Separately, an ACTIVE fork whose reservation is unknown
  to reservations used to log a WARNING and re-warn every tick forever with no
  path to resolution; it now warns once per reservation id per process,
  pruned against each tick's known-fork-id set the same way the fork-backstop
  give-up counter is pruned. Finally, the wiring-staging heal's ledger read
  (one `db.get(ForkWiringLedger, id)` per ACTIVE fork per tick) is now a
  single column-only preload of `(reservation_id, last_staged_fork_version)`
  in chunks of 500, with the fresh per-row `db.get` kept only for forks that
  actually need healing so the read-to-write window for those rows is
  unchanged.
- Fixed a race where a fork restore committing between a save's read and its
  commit could have its restore-to-draft marker silently dropped by the
  save's stale in-memory copy: every fork-mutating route (restore, the loose
  canvas PUT, save, and prune-devices) now loads the fork row under
  `SELECT ... FOR UPDATE`, held through its own commit or rollback, so the
  writers serialize instead of racing (issue #626).

#### Outbox and events

- Fixed outbox LISTEN task hardening (2026-09-04 review sweep, four confirmed
  findings in `herd_common/outbox.py`'s `_listen_for_wakeups` and
  `run_outbox_relay`): (1) the lost-connection branch (the registered
  termination callback firing) now sleeps `retry_seconds` before
  reconnecting, exactly like the connect/registration-failure branch, so a
  pooler, `idle_session_timeout`, or reaper that terminates the idle LISTEN
  session cannot turn into a tight reconnect loop; every iteration also
  closes the connection it opened (if any) in a `finally`, guarded by a 5s
  `asyncio.wait_for`, before the next one is opened, so a flapping session
  cannot leak a backend per cycle, and `wake` is set only after a
  registration actually succeeds (a failed `add_listener` no longer falsely
  claims a catch-up). (2) The listener now connects with kwargs from
  `engine.dialect.create_connect_args(engine.url)` instead of a rendered DSN
  string: the SQLAlchemy URL's query-string spellings (`ssl`,
  `prepared_statement_cache_size`) are not the spellings asyncpg's own DSN
  parser accepts, so a `?ssl=require` or `?prepared_statement_cache_size=0`
  deployment previously lost wake-on-write silently (a WARNING every tick,
  no health signal); this was proven live against the gate Postgres before
  the fix. (3) `run_outbox_relay` now loops immediately, without waiting any
  part of a tick, when a drain comes back exactly `OUTBOX_BATCH_SIZE` rows:
  Postgres collapses one committing transaction into a single NOTIFY
  regardless of how many outbox rows it staged, so a commit that staged more
  than one batch's worth (the expiration sweep's per-expired-reservation
  `reservation.completed` events are a real example) previously got one
  wake and then waited a full tick for the remainder. (4) Added direct test
  coverage for the termination-then-backoff path and the registration-failure
  path, neither of which any existing test exercised. Every existing outbox
  behavior (clear-before-drain, wake honored only on a healthy tick, backoff
  math, cancellation propagation, `wake_on_write=false` as the tick-only
  escape hatch, listener cancelled in the relay's `finally`) is unchanged and
  still pinned by the existing tests.
- Changed the transactional outbox relay to wake on write instead of only
  polling a fixed tick (issue #682): `enqueue_event` now issues
  `SELECT pg_notify(channel, '')` on the committing session (a no-op on
  SQLite), on a per-service-schema channel named `herd_outbox_<schema>`
  (`herd_outbox_reservations`, `herd_outbox_execution`); `run_outbox_relay`
  (started by both the reservations and execution services, the only two
  outbox producers) starts a supervised Postgres LISTEN task and drains
  immediately on a notification instead of waiting out the rest of the
  tick, live-measured at low single-digit milliseconds from commit to
  publish. The tick remains the fallback cadence, unchanged outage
  backoff: a missed notification, a not-yet-reconnected listener, or any
  non-Postgres dialect still drains on the next tick exactly as before.
  New `OUTBOX_WAKE_ON_WRITE` setting (default true, both services) is the
  ops escape hatch back to tick-only behavior; execution also gains the
  three existing outbox tick/batch/retention overrides reservations
  already had, closing the gap where execution exposed none of them.
- Fixed the notifications, execution, and integration NATS pull consumers to
  fetch one message per pull instead of a batch of 10, so nats-py can no
  longer hold an already-delivered message for up to the fetch timeout while
  waiting for the rest of a batch that never fills (issue #648).

#### AI orchestrator

- Fixed the AI commit path silently dropping an approved network-element
  attachment when inventory's ports fetch failed (issue #717):
  `_fetch_device_ports` now raises `CommitError(503)` on a transport failure
  or a 5xx from inventory instead of treating it the same as a genuinely
  portless device (a 404 is still "no ports"). The fetch already runs before
  the topology is created, so the commit fails closed with nothing to roll
  back rather than silently omitting the user's approved attachment.
- Stopped four ai-orchestrator routes from returning raw provider or
  internal exception text in their HTTP `detail` (issue #713). Topology
  generation (`POST /generate`) now answers a provider `AIError` with the
  fixed `AI returned no usable response` and any other exception with
  `AI call failed`; purpose classification answers `AI classification
  failed`; the admin-only template identity suggestion answers
  `AI suggestion failed` and recipe drafting `AI recipe drafting failed`.
  Each site logs the real exception with its traceback server-side
  (`logger.exception`) so nothing is lost for operators, matching the rule
  the reservation assistant already applied.
- Indexed `assistant_conversations` on `(reservation_id, created_at)` in
  ai-orchestrator (issue #712, migration 0005) and bounded the transcript
  read that purpose classification uses. The read is now two steps: the
  reservation's conversation ids ordered by `created_at`, then only the
  newest 200 message rows across those conversations (ordered by
  conversation `created_at` and `position`, never a bare `position`, which
  resets per conversation), selecting just `(role, content_blocks,
  position)` and reversed in Python before the existing role filter and
  12000-character trim. The old `(user_id, reservation_id)` composite index
  is kept for now; dropping it is a follow-up.
- Hardened the purpose-classification request path in ai-orchestrator
  (issue #709). Both `POST /classify-purpose/preview` and
  `POST /internal/classify-purpose` now bound their bodies: `purpose` at
  2000 characters, `device_ids` at 200, `dynamic_requests` at 50 (the caps
  reservations' own `ReservationCreate` already enforces) and `categories`
  at 64, each rejected with 422. The provider-configured (503) and daily
  quota (429) gates now run BEFORE the signal gather in both handlers, so an
  unconfigured provider or an over-quota caller can no longer drive the
  inventory and cabling fan-out. `dynamic_requests` are deduped by template
  id before fetching (counts for a repeated template are summed, matching
  reservations' N-entries-means-N-instances semantic), and the internal
  pass's per-device inventory reads (device detail and config-apply job
  summaries) plus the per-template reads run concurrently under a
  semaphore of 8 instead of strictly one after another; results keep
  request order and the per-item drop-on-failure contract is unchanged.
- Added AI-proposed network elements (issue #632): the topology generator's
  `propose_topology` tool gains an optional `elements` array (role,
  `element_type` from the ADR 0012 vocabulary, label, and an allowlisted
  `attrs` object), and the model may attach a device to an element with one
  edge from the device role to the element role; an element-to-element edge
  or a role reused across a device and an element is rejected by the
  existing validate-and-repair loop. The committer, not the model, picks
  the device-side attachment port on commit, in natural port-name order,
  skipping ports already claimed by another attachment of the same device
  in the proposal. `GenerateResponse`/`CommitRequest` gain the matching
  additive `elements` field; a device-only proposal is unaffected. See
  `docs/AI_GENERATE.md`.

#### Security

- Security: `GET /api/cabling/connections` no longer hands every authenticated
  user the whole fleet's cabling graph (issue #719, surfaced by the 2026-09-04
  review sweep while verifying the fork-membership finding, #701; the
  fleet-wide dump was the reconnaissance step that made that exploit
  turnkey). A non-admin caller is now filtered in SQL to connections where at
  least one endpoint device is visible to them, resolved by forwarding the
  caller's own JWT to inventory's `GET /device-groups/visible-devices`
  self-service lookup; `total` and pagination reflect the filtered set, not a
  post-filtered page, and an empty visible set short-circuits to an empty page
  with no query at all. Admin and superadmin callers stay unfiltered and never
  trigger the inventory lookup. Fails closed for non-admins: an unreachable or
  non-2xx inventory answer returns 503 with nothing, rather than falling back
  to an unfiltered list. `docs/ROLES.md` documents the filter and the admin
  passthrough.
- Security: inventory's device config-version reads (`GET
  /devices/{id}/config-versions`, `.../config-versions/diff`, `.../config-
  versions/{version_id}`) now enforce the same non-admin group-visibility
  gate as `GET /devices/{id}` and `GET /devices/{id}/ports` (issue #718, a
  2026-09-04 review sweep decision item). Previously the three reads
  required only authentication, so any authenticated user could read
  another group's config history (IPs, hostnames, VLAN maps, routes, author
  attribution, and any free-form driver config) for a device outside their
  own visibility, even though the sibling device and port reads already
  404 a non-visible device. A non-admin caller outside the device's groups
  now gets 404 with the identical detail as the device read, so the
  endpoint cannot be used to tell "hidden" from "absent"; admins remain
  unfiltered. The write endpoints (create, restore, apply, schedule) are
  unchanged, since they already run the `manage`-or-active-reservation
  guard. `_resolve_visible_device_ids` moved out of `app/routers/devices.py`
  into `app/services/device_visibility.py` so device_configs.py can share it
  without a router-to-router import; ports.py now imports it from the same
  place. Documented in `docs/ROLES.md`.
- Security: `GET /api/auth/groups/{group_id}` is now admin/superadmin only
  (issue #711, 2026-09-04 review sweep finding). The route returned every
  member's email to any authenticated user, and since open self-registration
  under `AUTH_METHOD=local` puts a fresh account in "Not Grouped" and
  `GET /groups?limit=500` lists every group, one registration plus one
  detail call per group let a caller enumerate emails wholesale, which is
  exactly what `/api/auth/register`'s generic 409 exists to prevent. `GET
  /groups`, `GET /groups/user/{id}`, and `POST /groups/users/groups` are
  unchanged: they carry no member payload and gating them would break ACL
  checks and device-group visibility for plain users. `docs/ROLES.md` is
  corrected to stop claiming a user can read group member details.
- Security fix: the cross-reservation port-claim check (ADR 0006 Decision 4)
  is now an actual invariant instead of a save-boundary guard (2026-09-04
  review sweep finding, issue #721). Two gaps: `save_fork`'s claim check was a
  plain SELECT against other ACTIVE forks' committed rows, and the engine
  runs at Postgres default READ COMMITTED, so two concurrent saves on two
  different forks each ran that SELECT against the other's still-uncommitted
  insert, textbook write skew, with `uq_fork_connections_endpoints` scoped
  per-fork so nothing failed at commit either; the version-allocation retry
  loop named as the mitigation only ever serializes a same-fork version
  collision, which the claim check excludes by construction, so it never
  closed this window. Separately, `fork_service.create_fork`'s activation
  snapshot never ran the claim check at all, so two reservations forking the
  same topology, or two topologies sharing a trunk, could both materialize
  the same physical hops with zero concurrency involved. `save_fork` and
  `create_fork` now both call the new `fork_save_service.lock_port_claims`
  immediately before the claim check: a transaction-scoped Postgres advisory
  lock (`herd_common.advisory_lock.xact_lock`) over the sorted, canonical
  `forkport:{device_id}:{port}` keys of every claimed endpoint, keyed via
  `advisory_key_from_string` exactly like reservations'
  `_acquire_device_locks`. The second writer blocks until the first's
  transaction commits or rolls back, so its own claim check then reads the
  winner's committed rows and 409s correctly; the lock self-gates to a no-op
  on SQLite. Reservations' `ForkMembershipRefused` is generalized from the
  endpoint-membership 409 shape alone to any 409 cabling's fork-create route
  returns, so a port-claim conflict at activation gets the same definitive,
  not-retried treatment (WARNING logged, reservation stays ACTIVE unwired,
  expiration-sweep backstop skips it) that a foreign-device refusal already
  got. Documented in the ADR 0006 amendment; proved live against Postgres in
  `services/cabling/tests/test_fork_port_claim_race_live_pg.py`
  (`_gate-pg-live-tests`), confirmed to fail on main without the lock.
- Security fix: fork saves and activation snapshots now check the canvas's
  endpoint devices against the reservation's membership (2026-09-04 review
  sweep finding, security/HIGH). Since ADR 0009 phase 7 made the fork the sole
  driver of provisioning, nothing compared a fork canvas's endpoint device ids
  to the reservation's device set, so a user could name a foreign device as a
  canvas node and drive real provisioning onto it. Cabling's `ForkCreate` and
  the fork-save request now carry a required `member_device_ids`; `save_fork`
  and `create_fork`'s activation snapshot both refuse (409
  `fork_device_not_member`, naming the offending device ids) any canvas node
  outside that set, checking endpoint devices only, never a resolved path's
  transit devices or network element nodes. Reservations forwards its own
  device set (materialized dynamic instances included) on every fork
  create/save call and fails closed if that set cannot be read. Admins are not
  exempt: PATCH-add is the way to bring a device into a reservation. A 409 on
  activation is treated as definitive, not retried, and the expiration sweep's
  missing-fork backstop stops re-attempting a reservation once refused. An
  existing fork whose canvas already carries a non-member endpoint will 409 on
  its next save; the detail names the offending devices so the owner can
  PATCH-add or remove them.
  Phase 2 (issue #701) moves the same check to the front door: cabling's
  `POST /topologies/{id}/validate/internal` gains an additive `device_ids`
  field (the canvas's device nodes), riding reservations' existing single call
  to that endpoint at create/update time. Creating a reservation with a
  `topology_id` whose canvas names a device outside `device_ids` now fails
  fast with `422` (`topology_device_not_member`, naming the offending device
  ids) before any row is written, and a PATCH that changes `device_ids` on a
  topology-bound reservation re-runs the same check (`400`, same detail
  shape), instead of the mismatch only surfacing later as a fork 409 on
  activation.
- Security: scheduled device-config apply jobs re-check the creator's
  authority at fire time instead of relying solely on the schedule-time
  gate (issue #704). A job with no `reservation_id` fired straight into
  execution's internal-token path with no further check, and a caller-
  supplied `reservation_id` was never validated as active, owned, or
  containing the device; nothing revoked a queued job when its creator's
  ACL grant was removed or their reservation ended. Adds two internal-token
  routes (auth's `GET /internal/users/{id}/groups`, acl's
  `POST /internal/check`) and `herd_common.acl.user_has_manage_internal` /
  `user_has_manage_or_owns_active_reservation_internal` so the same
  schedule-time authorization (explicit `manage` grant, or reservation-
  owner of an active reservation containing the device) can be re-evaluated
  with only the job creator's user_id, no user JWT. `fire_job` now runs this
  check unconditionally before dispatching to execution and resolves
  `skipped` with `creator no longer authorized for this device` on failure.
  `schedule_apply_job` also now validates an optional `reservation_id`
  up front (422 if it is not an active reservation the caller owns that
  contains the device) and bounds `scheduled_for` with a new
  `apply_job_max_horizon_days` setting (default 30 days, 422 beyond it).
  Documented in `docs/ROLES.md` and `docs/ENV_VARS.md`.
- Fixed the base compose file publishing the unauthenticated Traefik
  dashboard and API (`api.insecure: true`) on all interfaces, so `make prod`
  exposed `/api/rawdata` (every router rule, backend container address,
  middleware chain) to anyone who could reach port 8080 (issue #708).
  `docker-compose.yml` now binds it `127.0.0.1:8080:8080`; the dev override
  deliberately adds no second binding, since compose merges port lists and
  nothing in dev, CI, or QA reaches the dashboard off-host. `FRESH_SETUP.md`
  describes the SSH tunnel for remote viewing, and a repo-root unit test
  (`tests/unit/test_compose_ports.py`) yaml-parses both compose files and
  pins the loopback-only publish.
- Security: `/api/ai/generate` no longer buffers a whole multipart upload
  into memory before its file-count and size caps run (issue #705):
  `_read_uploads` rejects a too-many-files request before reading a single
  byte, then reads each remaining part in 64 KiB chunks with a running
  per-file and aggregate total, aborting with 400 the instant either cap is
  crossed instead of after the whole part is buffered. `UploadFile.size` is
  treated as an untrusted hint only, never load-bearing for the check.
  `extract_files`'s own caps are unchanged and still re-run as defense in
  depth on the now-bounded upload.
- Fixed `PATCH /preferences` in the user-profile service validating only the
  incoming dict, never the merged result, so repeated patches could grow one
  JSONB row past every cap (issue #714). The service now validates each
  merged dict (the 200-key and 64 KB caps on `saved_filters` and `extras`,
  the key cap and page-size range on `page_sizes`) before assigning anything
  and answers 422 with a `merged <field> ...` detail; a rejected PATCH
  leaves the row exactly as it was. `PUT` and `DELETE` remain the shrink
  paths.

#### Inventory and auth

- Changed inventory's `apply_scheduler` to record a config-apply run whose
  execution response carries no `status` as `failed` instead of `success`
  (issue #720); unreachable today, since the contract requires `status`.
- Fixed an admin being able to deactivate the superadmin through
  `POST /users/{id}/deactivate` (issue #715). The endpoint now refuses a
  superadmin target with 400 `Cannot deactivate the superadmin`, mirroring
  the role-change carve-out; `POST /users/{id}/activate` is deliberately
  unchanged, since it is the recovery direction. A deactivated superadmin
  could not log in or refresh, was not re-seeded at startup, and the LDAP
  sync never reactivates a local user.

#### Frontend

- Fixed device lists silently truncating at the inventory service's 500-row
  page cap (issue #703). `fetchDevices` fetched page 0 of 500 and dropped
  `total`, so with more than 500 devices the reporting page's device index
  missed the oldest ones and attributed their hours to the "Unknown"
  template bucket (and the client-built by-template CSV exported that),
  while the device-group and topology-template pickers, the equipment
  browser, and the calendar showed partial lists. `ReportingPage` now
  resolves exactly the devices the report mentions through the batch
  endpoint via the new chunked `fetchDevicesByIds` and `useDevicesByIds`
  (hoisted from `hydrateCanvasNodes`), and the other four consumers use the
  new `fetchAllDevices`/`useAllDevices` page walker (server-side filters
  honored; `fetchAllDeviceNames` is a map over it). The server cap is
  unchanged; the page-0-of-500 idiom in the other API modules is left for a
  follow-up.

#### Developer platform, CI, and tests

- Added a `classify_sweep_first` pytest marker (issue #706 follow-up), read
  by a new `pytest_collection_modifyitems` hook in
  `tests/integration/conftest.py` that moves any item carrying it to the
  front of collection. The purpose-classify sweep classifies its backlog
  oldest-first and serially at model speed, so `test_purpose_review_flow.py`'s
  AI-gated test now runs before the rest of `tests/integration/` has a
  chance to queue its own cancelled/completed reservations ahead of it and
  push it past its poll budget on ordering alone, not a reconciler defect.

## [0.3.0] - 2026-08-30

- Shipped LDAP directory group sync end to end (ADR 0011): a directory group
  client and mapping store, a fail-closed reconciler with an asymmetric skip
  taxonomy, a deactivation/reactivation sweep, an interval sync loop, and an
  admin UI at `/admin/ldap-sync`, plus a checked-in LDAP test gate and live
  Postgres/LDAP coverage wired into `make master` and `make everything`.
- Added fork version preview, diff, and restore to the reservation topology
  editor: a read-only ghosted preview of any saved version, a client-side diff
  against another version or the current draft, and restore-to-draft that
  stages a version's canvas for the next Commit rather than reconciling
  directly.
- Introduced network element objects (ADR 0012, issue #22): a persistent,
  non-device canvas node that many device ports can attach to for a shared
  VLAN segment, subnet, external cloud, or patch-panel trunk, with backend
  validation and fork-save handling, a frontend palette and attach dialog, and
  a live Playwright suite; provisioning is deferred to a later anchored-VLAN
  phase.
- Added a seeded end-to-end test pass (issue #629) so a device-gated Playwright
  test can no longer silently skip behind a green gate: `make test-e2e-seeded`
  runs after the gate stack is seeded, in both `make everything` and nightly,
  and fails the run on any unexpected skip.
- Hardened JetStream for production: `make prod` now persists stream data
  across container recreates via a mounted volume, with a shared add-or-update
  stream helper and a configurable retention cap; dev and gate stacks keep
  starting every stream empty by design.
- Migrated the AI orchestrator to the anthropic 1.x SDK and made service
  images lock-faithful: every backend Dockerfile now installs from the
  workspace `uv.lock` export instead of resolving dependencies fresh, with a
  new CI check that diffs an image's installed packages against the lock.
- Consolidated recurring backend patterns into `herd_common`: pagination,
  database setup, CORS, internal-service calls with TTL caching, and more,
  removing duplicated code across most of the 12 services with no behavior
  change.
- Shipped a multi-port wiring dialog and a matching admin multi-connect
  dialog, both backed by a new bulk connection-create endpoint, and fixed
  same-device ("loopback") 1:1 pairing in the admin dialog.
- Closed two reconcile edge cases left from the ADR 0009 epic: record-time L2
  supersession settles a stale cross-reservation row on frozen wiring instead
  of leaking its VLAN allocation, and stale-intent revalidation widens
  release-direction reconcile against failed-but-still-intended rows without
  weakening build-direction rebuild.

### Delivery detail

#### AI orchestrator

- Migrated to anthropic SDK 1.x (issue #592, PR #608): `anthropic_provider.py` now
  imports `httpx2` (aliased as `httpx`, kept local to that file) so its exception
  catch matches the `TransportError` type the 1.x SDK actually raises, and gets its
  own `_build_anthropic_http_client` returning `anthropic.DefaultAsyncHttpxClient`
  (ca_cert wins, verification fails closed) instead of the shared
  `openai_provider._build_http_client`, which stays on plain httpx for the
  unaffected OpenAI-compatible path. `pyproject.toml` moved `anthropic>=0.39.0,<1`
  to `>=1,<2`; `uv.lock` regenerated (anthropic 0.96.0 to 1.0.0, plus the new
  httpx2/httpcore2/truststore transitive entries). This is the fix for the
  construction-time `TypeError` PR #591 had worked around.
- `GET /api/ai/status` reports a degraded state when the configured provider
  fails to CONSTRUCT rather than claiming `enabled: true` from the static
  settings check alone (issue #606, split out of #592; the 2026-08-24 gate case
  was an anthropic 1.0.0 rejecting an httpx 0.x client that the settings check
  could not see). The status handler now attempts a cheap, no-network provider
  construction, through the same `_build_provider` path `get_ai_client` uses,
  after the settings check passes, and reports `enabled: false`, `degraded:
  true`, `reason: <exception class name>` on failure; the reason is the
  exception CLASS NAME only, never the message, which can carry a base URL or
  key material. The construction attempt is cached for 30 seconds (module-
  level, monotonic clock) so the unauthenticated endpoint cannot be used to
  hammer provider construction. Additive response shape: `degraded` and
  `reason` are new fields, `enabled` semantics are unchanged for the
  already-covered unconfigured and successfully-configured cases.

#### Auth and LDAP

- ADR 0011, LDAP directory group sync (issue #38, CLOSED), delivered across six phases:
  - Phase 1 (PR #507): directory group client in auth's `ldap_service.py`
    (`fetch_group`, `resolve_member`, `resolve_members` batched over one shared
    connection, `user_present_by_email`), under the contract that `None` and
    `skip_reason` are answers the directory gave while `LdapUnavailableError` means
    it could not be asked; the sync functions refuse anonymous binds via
    `require_service_account`.
  - Phase 2 (PR #510): mapping store, `ldap_group_mappings` (migration 0006,
    canonical-DN key, `UNIQUE herd_group_id` so one directory group maps to one HERD
    group), admin CRUD at `/admin/ldap-sync/mappings` with a 422-vs-503 validation
    split and accept-with-warning on a memberless entry; the warning is derived from
    `member_dns` emptiness rather than an attribute-presence flag, because ldap3
    back-fills missing attributes so presence itself is unobservable.
  - Phase 3 (PR #512): reconciler `ldap_sync_service.run_sync`, with a fail-closed
    asymmetric taxonomy (a transport error skips the whole group; a directory answer
    such as not_found, missing_email, or missing_username skips only what was
    answered; per-op apply failures are isolated); group-membership removals are
    suppressed whenever any member skipped as missing_email or missing_username
    (not_found never suppresses), so an incomplete read never silently deletes
    memberships it could not fully verify; `is_active=False` users are invisible to
    membership sync in both directions; `ldap_sync_runs` audit rows (migration 0007,
    committed running-first); sync-now (`POST /admin/ldap-sync/run` 202, `GET runs`
    and `runs/{run_id}`) serialized by an asyncio lock plus a Postgres session-scoped
    advisory lock, committed post-acquire so it never idles in a transaction.
  - Phase 4 (PR #514): deactivation/reactivation sweep, with paged enumeration
    answering directory presence (any failure or truncated page raises rather than
    partially applying); disabled-filter enumeration overrides group-presence credit;
    a strict-both-terms circuit breaker aborts the sweep but still applies any
    reactivations already computed; reactivation is gated on `deactivated_by_sync`
    provenance (migration 0008), and the manual `/users/{id}/activate` and
    `/deactivate` endpoints always clear that provenance; every sweep flip is a
    guarded compare-and-swap update recorded only after commit, so a concurrent
    admin write always wins.
  - Phase 5 (PR #518): interval loop
    (`services/auth/app/tasks/ldap_sync_loop.py`), gated on `auth_method=ldap` AND
    `ldap_group_sync_enabled` (default false); the first tick sleeps a full interval,
    and a sub-60s interval clamps with a warning rather than failing boot;
    `ldap_sync_runs` retention (`ldap_sync_runs_retention_days`, default 90) rides
    the same loop, pruning at most once per 24h and advancing only on success, never
    pruning a running row (retention is loop-only, so a sync-now-only deployment
    accumulates rows by design); closed the config-bootstrap schema debt for all 11
    LDAP keys and the docker-compose env-passthrough gap.
  - Phase 6 (PR #526): admin UI at `/admin/ldap-sync` (mappings CRUD with the
    memberless-DN warning banner, run history with status badges and 2s polling that
    stops on a terminal or 30-minute-stale running row, sync-now with 409
    discrimination on pinned detail strings) plus `GET /admin/ldap-sync/status`,
    admin-gated but deliberately ungated on `auth_method` so it can report sync is
    off. `errorDetail` lives in a single copy at `frontend/src/lib/errors.ts`.
  - Open follow-up: issue #511 (further auth/LDAP consolidation).
- Advisory-lock consolidation and LDAP reconciler scaling (issue #513, PR #529):
  `herd_common` gained a shared `advisory_lock` module with both the
  transaction-scoped blocking `xact_lock` idiom (reservations' device-lock
  acquisition) and the session-scoped try-lock `session_try_lock`/`session_unlock`
  idiom (auth's LDAP sync slot), each self-gating to a no-op on non-Postgres so
  SQLite unit tests need no branch.
- Checked-in LDAP test-gate phase (PR #504): `make ldap-up` boots a stateless,
  checked-in `infra/ldap-test/` directory seeded with the ADR 0011 group fixtures,
  replacing the old external `HERD_LDAP_DIR`/home-directory setup; `make master` and
  `make everything` gained a `_gate-ldap-tests` phase that runs the live-LDAP auth
  suite as a hard requirement.
- Stale-run reaper for `ldap_sync_runs` (issue #528, PR #546): a hard process death
  mid-run (OOM kill, container crash, power loss) never reaches the run's finalize
  step, leaving the row stuck at `running` forever, since the retention prune
  deliberately never touches a `running` row. `reap_stale_running_runs` flips a
  `running` row past `LDAP_SYNC_RUN_STALE_SECONDS` (default `7200`) to `failed`
  with `error="run did not finalize (process died mid-run)"`, via a compare-and-swap
  `UPDATE ... WHERE status = 'running'` so a run that finalizes between the cutoff
  computation and the update keeps its real outcome. It runs at the start of every
  sync run, inside the run-serialization slot (never before it, so a call about to
  raise `SyncBusyError` can never reach a live run's row), covering both the
  interval loop and sync-now. The threshold is read through the new
  `effective_ldap_sync_run_stale_seconds()`, which clamps to the larger of 60
  seconds and twice the effective sync interval (with a warning, never a boot
  failure), so a tuning value at or below the interval cannot fail a run that is
  merely slow. The admin UI's 30-minute stale-row display (PR #526) is an
  independent, shorter client-side hint; this reaper is what corrects the audit
  record itself.
- LDAP integration suite and Postgres-live sync coverage wired into the gates and
  nightly (issue #572): `tests/integration/test_ldap_auth.py` was gated on
  `HERD_INTEGRATION_LDAP=1`, a variable no Makefile target, CI workflow, or compose
  file ever set, so it (and the sync-admin surface) self-skipped in `make master`,
  `make everything`, PR CI, and nightly alike. PR #582 first closed the
  real-Postgres half of the gap: `services/auth/tests/test_ldap_sync_service_live_pg.py`
  and `services/common/tests/test_advisory_lock_live_pg.py` exercise `_SyncSlot`'s
  cross-replica advisory-lock branch and the underlying `session_try_lock`/
  `session_unlock`/`xact_lock` SQL against a real server for the first time (every
  prior test ran on SQLite, which no-ops that whole code path). This delivery closes
  the rest: a new Makefile phase, `_gate-ldap-stack-tests`, runs after `test-e2e` in
  `master`, `everything`, and the nightly workflow, connects the checked-in
  `infra/ldap-test` server onto the ephemeral stack's compose network, recreates
  ONLY the stack's auth service (`--no-deps --force-recreate`) in LDAP mode, runs
  `test_ldap_auth.py` plus the new `tests/integration/test_ldap_sync_admin.py`
  (mapping create, sync-now, run polling, group-membership reconcile against a real
  directory, and a concurrent-sync-now race proving the loser gets the in-process
  busy 409), then always restores auth to local mode before any later phase (seeding,
  load tests) runs. `test_ldap_sync_admin.py` works around the resulting
  chicken-and-egg problem (in LDAP mode `authenticate_user` consults ONLY the
  directory, so the stack's seeded local superadmin cannot log in and there is no API
  path to an admin token) by promoting a JIT-provisioned directory user directly in
  Postgres via `docker compose exec postgres psql`, then re-logging in. A sibling
  `_gate-pg-live-tests` phase runs the two PR #582 files hard-required
  (`HERD_TEST_PG_REQUIRED=1`) against the gate stack's own Postgres, since they need
  no LDAP mode. Both new targets are deliberately free of `$(MAKE)` references in
  their own recipes (unlike the pre-existing `_gate-ldap-tests`), since a recipe line
  containing one executes for real even under `make -n`, and these mix in real
  docker network/compose mutation.
  Two follow-up fixes landed the same day from a live-gate rerun. First, a
  `COMPOSE_PROJECT_NAME` leak: `LDAP_COMPOSE` had no `-p`, and that env var (which
  `_gate-ldap-stack-tests` inherits from the gate targets) outranks a compose file's
  own `name:` in project resolution, so every bare `$(LDAP_COMPOSE)` call, including
  `down -v --remove-orphans`, silently targeted the GATE project instead of
  `herd-ldap-test`; a `down --remove-orphans` there reads every real gate container as
  an orphan of a project whose compose file defines one service, and removes them
  all, turning a routine LDAP-test-server teardown into a full gate-stack teardown.
  Fixed by pinning `LDAP_COMPOSE` with `-p herd-ldap-test` (the one project-name
  source no environment variable can override), dropping `--remove-orphans` from
  `_gate-ldap-stack-tests`' own `down` calls as a second layer, and adding two
  pre-flight guards: the target compose project must already have a running `auth`
  container, and its `config.json` must not have been promoted above the environment
  (no `config.bootstrapped` marker) by a prior config-UI save, since either would
  otherwise fail confusingly deep into the phase instead of failing fast with a clear
  message. Second, a seeded-stack collision: `tests/integration/test_ldap_auth.py`
  and `test_ldap_sync_admin.py` defaulted to the `user1..user25`/`herd-eng` fixtures,
  which collide by username with a `make seed`-seeded stack's local `user1..user1000`
  rows (JIT-provisioning refuses with `username_collision`), and separately are
  invisible to `ldap_sync_service`'s reconciler, which only ever touches LDAP-sourced
  users, so a mapped `herd-eng` group's membership assertion would silently lose its
  seeded members too. `infra/ldap-test/ldif/70-seed-integration.ldif` adds dedicated
  `ldapit-admin` and `ldapit-eng1..3` identities (uids that can never match the seed
  script's `user[0-9]+`/`admin[0-9]+` patterns) plus their own `cn=herd-it-eng` group,
  which both integration test files now use instead; the compose healthcheck (probes
  the last entry of the last bootstrap LDIF file to prove the seed is complete) and
  `test_ldap_service_live.py`'s `_seed_is_current` stale-seed guard were both
  retargeted/extended to the new file so an older checkout is still caught.

#### Reconcile and provisioning

- Record-time L2 supersession (issue #479, PR #492): `record_l2_membership_active`
  parks another reservation's stale ACTIVE row on the same (switch, port) as FAILED
  intended-RELEASED, if and only if that reservation's wiring is frozen (frozen
  equals condemned: the freeze commits before any teardown driver call and has one
  production call site). The frozen gate is load-bearing, not an optimization: an
  unfrozen cross-reservation ACTIVE row can be the port's rightful live holder and is
  never touched. This is deliberately not the L1 flip-to-RELEASED path, since
  `add_to_vlan` displaces nothing; settlement there rides the release-direction
  retry channels instead, which then release a superseded row's orphaned fabric
  VLAN allocation (previously only reachable via the apply tail, so a dead
  reservation's VLAN number could strand forever).
- Stale-intent settlement and revalidation (issue #491, PR #493): all three full
  reconciles' RELEASES diff now runs against (ACTIVE union FAILED intended-ACTIVE)
  rows while the BUILDS diff stays ACTIVE-only. The asymmetry is load-bearing:
  widening builds would permanently exempt a failed-but-still-intended pair from
  rebuild. Stale rows are direction-flipped by park helpers that never downgrade a
  concurrently-ACTIVE row, and ride the existing remove/release path; L2
  nil/dangling-allocation rows flip RELEASED with no driver call. Both retry
  channels now revalidate BUILD-direction rows against cabling's current intended
  set before driving; intent gone parks the row release-direction; a fetch failure
  fails closed (nothing driven, reported `still_failed`, not a 503). Pinned-reason
  zombie rows (unresolvable or not-a-simple-chain) stay excluded from the widening;
  their recovery remains a fork re-save.
- `cancel_reservation` and `release_reservation` adopt
  `_release_exclusive_devices_best_effort` (issue #599 part 1, PR #604) instead of
  reimplementing it inline, closing three gaps: a per-device warning log on a
  fetch failure during the release loop (a new optional `context_label` names the
  caller's operation, defaulting to "provision failure" for the two pre-existing
  callers so their behavior is unchanged); an optional `user_id` on the
  retry-exhausted error log; and the retry-exhausted message text parameterized
  by `context_label` so cancel and release keep their existing wording exactly.

#### Cabling and inventory

- Network element objects, phase 1 of 3, cabling backend only (ADR 0012, refs
  issue #22; frontend and docs land in later phases). A network element is a
  non-device canvas node (`networkElementNode`) many device ports can attach
  to with a many-to-one edge, modeling a shared VLAN segment, subnet, external
  cloud, or patch-panel trunk without a full mesh of point-to-point links or a
  new registry table (canvas-native and topology-local by decision).
  `node_to_element_map` (`fork_save_service.py`, beside `node_to_device_map`)
  maps React Flow node ids to element ids for nodes of type
  `networkElementNode`, keyed off `data.element.id` with a fall back to the
  node id. `classify_element_edge` (`fork_save_service.py`, beside
  `node_to_element_map`) is the one shared, pure edge-classification helper
  built on top of it: given one edge plus both maps it returns `"attachment"`
  (a device-to-element edge with a non-empty device-side port name),
  `"element_to_element"`, `"element_edge_no_port"` (a missing or empty
  device-side port name), or `None` (not an element edge, or an element
  endpoint paired with an unresolvable other side). `_run_topology_validation`
  (`services/cabling/app/routes/topologies.py`) and `resolve_canvas_wiring`
  both call this one classifier and act on its result identically. In
  `_run_topology_validation`, checked before the existing BFS pass:
  `"attachment"` is VALID with no BFS and never enters the pathfind batch,
  since an element is not a physical thing the cabling graph could contain a
  path to; `"element_to_element"` reports the new reason `element_to_element`;
  `"element_edge_no_port"` reports the new reason `element_edge_no_port`; `None`
  falls through to the existing `missing_device` handling, unchanged.
  `InvalidEdge`'s docstring (`schemas/topology.py`) now enumerates all four
  reasons; `reason` stays a plain `str`, no schema enum change. In
  `resolve_canvas_wiring`, only `"attachment"` is counted in the returned
  `element_attachments_skipped`: an element edge the validator would reject
  (`"element_to_element"` or `"element_edge_no_port"`) falls through to the
  same silent skip a genuinely broken non-element edge takes, uncounted, so
  the count only ever reflects edges the validator would actually accept.
  None of the three element classifications contribute a `WireSpec`. The
  result threads through a new `CanvasWiringResolution` return type, and the
  count from there through `ForkSaveResult` to the new additive
  `ForkSaveResponse.element_attachments_skipped:
  int = 0` field (`schemas/fork.py`), returned by `POST
  /internal/forks/{reservation_id}/save`; fork-on-activation snapshotting
  (`fork_service.py`'s `_snapshot_connections`) uses the same resolver and
  ignores the count. `tests/contract/snapshots/cabling.json` is regenerated
  (additive: one new `ForkSaveResponse` property, `required` unchanged) and
  `services/cabling/tests/` gains unit coverage for every classification rule
  (both endpoint orderings), the resolver's skip-and-count behavior on a
  mixed canvas, the response defaults, and (follow-on) that the resolver
  does NOT count an `element_to_element` or `element_edge_no_port` edge while
  still counting a genuine `attachment`, pinning the exact validator-agreement
  claim above.
- Bulk connection creation (PR #537): `POST /connections/bulk` (admin-only) creates
  up to 200 connections per call (the cap mirrors inventory's
  `BulkPortCreate.instances`), returning a per-row created/rejected
  `ConnectionBulkReport` so one bad pair does not sink the batch. It shares
  `_extract_bearer_token` and the per-row validation with the single-create path,
  and differs from it in one deliberate way: the device-group boundary check fails
  closed, so an unverifiable device (inventory unreachable) aborts the whole batch
  with 503 and creates nothing, rather than the single path's fail-open, mirroring
  the fail-closed fork-fetch rule from the reconcile epic.

#### Security

- Secrets reverse guard (issue #456, PR #486): `delete_secret` consults inventory's
  by-secret internal reverse lookup (`GET /hypervisors/by-secret/{id}/internal`) and
  refuses with 409 `secret_in_use` (hypervisor ids and names) while any hypervisor
  references the secret, 503 fail-closed when inventory is unreachable, no force
  flag. Secrets' `inventory_guard` module docstring records a claims-registry
  migration path if a second consumer of secrets ever appears.

#### Frontend

- Inventory page Next-click revert (PR #662, nightly run 33300868733): the
  search-debounce effect armed a 300ms `setSkip(0)` timer on every mount, so a
  Next click inside that window was silently reverted to page 1. The effect now
  returns early when the input already matches the applied search; a
  fake-timer vitest pins the race and the seeded e2e pass exercises it live.

- Multi-port wiring dialog (issue #517, PR #530): drawing a line between two device
  nodes on the topology canvas opens `WiringDialog.tsx` (port columns via
  react-window, drag or click-to-connect, per-line L1/L2/L3, "Connect 1:1 in order"
  over the visible filtered ports); the old single-pair modal survives as
  `QuickConnectPopover.tsx` behind a toolbar toggle. Confirming emits one enriched
  edge per line through a shared `addEnrichedEdges` append path. Many same-pair
  edges render as one `BundledEdge` with a count badge, a render-only projection
  over N distinct stored edges (member selection, per-member delete, and
  read-gating all fan out to the underlying edges). Issue #531 (both halves now
  closed, see below) originally noted that cabling's fork-save resolver collapsed N
  same-pair edges to one path on every provisioning path; the dialog showed a
  provisioning notice when N > 1 until the ports half shipped.
- Admin multi-connect dialog (PR #538): `MultiConnectDialog.tsx` brings the same
  staged-lines interaction to the admin Connections page, committing through
  `POST /connections/bulk`, and is the default create surface on
  `ConnectionsPage.tsx` (old single-pair modal behind a Multi/Single toggle). It
  reuses the shared port-selection primitives with `WiringDialog.tsx` but
  deliberately duplicates the dialog shell (drag-versus-click arbitration, SVG line
  geometry, staged-line CRUD) rather than abstracting it, since `WiringDialog` had
  just stabilized after several review rounds; tracked as issue #539 with an
  explicit note not to do that refactor until a third consumer or a double bug fix
  forces the issue. Partial-success handling in `bulkStaging.ts` keeps a rejected
  line staged with the server's reason, and keeps a line with no matching response
  row rather than assuming it was created.
- Dynamic-resources UX completion (issues #472, #473, PRs #487, #488, closing the
  #445 report): a collapsible dynamic-templates section in the Equipment Browser,
  draggable ephemeral canvas placeholders with an editable instance count that
  expand to the pinned per-template wire shape at reserve time, reserve-from-topology
  prefill of the dynamic block, dynamic-aware Copy on the Templates page, and an
  explicit orphaned-secret indicator on the Hypervisors page.
- Dead-code removal (issue #489, PR #490): the unused
  `components/topology-editor/TopologyEditor.tsx` was deleted;
  `pages/TopologyEditorPage.tsx` is and remains the live editor.
- `AdminGuard` route wrapper (issue #527, PR #547; issue #548, PR #550): the eleven
  separate per-page admin guards under `pages/admin/` plus `ReportingPage`'s own
  guard are replaced by one exported `AdminGuard`, at the time in `App.tsx`
  (moved to `components/guards.tsx` by issue #551, see below), wrapping a
  pathless parent route: all 14 admin-gated routes (`/reporting` plus the 13 `/admin/*`
  routes) now redirect a non-admin to `/topology` through the single component.
  Behavior is unchanged; this is a structural consolidation, not a feature or
  permission change.
- Issue #531 closed, both halves (PR #545 ports, this PR layer): the ports half made
  cabling's `resolve_canvas_wiring` resolve each canvas edge against its own
  `data.source_port_name`/`data.target_port_name` instead of only the device pair, so
  N same-pair lines from `WiringDialog.tsx` provision as N distinct wires; edges with
  no port names keep the old device-pair behavior. The layer half was decided as
  canvas-annotation-only rather than implemented: ADR 0009 option C already commits
  execution to deriving L2 VLAN membership and L3 route adjacency from the resolved
  L1 path hops, and `_fetch_fork_intended_wires` filters fork rows to `layer == "L1"`,
  so carrying the dialog's per-line layer into `WireSpec.layer` would drop those rows
  from every reconcile; no backend or provisioning code changed. `WiringDialog.tsx`
  gained a hover tooltip on both layer controls and a short note above the confirm
  button stating that the layer is recorded on the canvas and provisioning derives
  L2/L3 from the resolved path; `TOPOLOGY_EDITOR.md`, the manual, `FEATURES.md`, and
  `PLANNED_FEATURES.md` were corrected to stop describing it as a pending gap.
- Route table extracted for testability (issue #551, PR #560): `AuthGuard`, `GuestGuard`, and
  `AdminGuard` moved out of `App.tsx` into `components/guards.tsx`, and the
  `<Routes>` children moved into an exported `appRouteElements` constant in the new
  `routes.tsx`, leaving `App.tsx` as just `BrowserRouter` plus `Toaster` plus
  `ErrorBoundary` plus `<Routes>{appRouteElements}</Routes>`. A new
  `test/routes.test.tsx` runs `createRoutesFromElements` over `appRouteElements`
  with no rendering involved, walks the resulting tree via a subtree search (so a
  future wrapper around a guard does not false-red the test), and pins: the exact
  set of 14 paths (`/reporting` plus the 13 `/admin/*` routes) that carry an
  `AdminGuard` ancestor; that every one of those paths also sits under `AuthGuard`;
  that the route table has no duplicate paths; and that the `AdminGuard` group
  still renders an `Outlet` for its children. This closes the gap a PR #550
  reviewer found by mutation testing: before this change, moving a route out of
  the `AdminGuard` group left the full vitest suite green. `AdminGuard.test.tsx`
  still pins the guard's own redirect/render behavior; this test only pins route
  membership and structure.
- Consolidated admin-role check (issue #561, PR #607): the inline
  `role === "admin" || role === "superadmin"` predicate, copied across ten call
  sites, is replaced by one `isAdminRole` helper in `frontend/src/lib/roles.ts`.
  Behavior is unchanged; this is a structural consolidation.
- Loopback 1:1 pairing fix (issue #585, PR #603): `MultiConnectDialog`'s "Connect
  1:1 in order" button paired `freeSource[i]` with `freeTarget[i]` positionally,
  so picking the same device on both sides made every index a self-pair
  (`freeSource[i].id === freeTarget[i].id`) and the button staged nothing. A
  same-device pick now intersects the two independently filtered free-port lists
  by port id, in source-column order, and pairs adjacently: `(p1, p2), (p3, p4),
  ...`, with an odd leftover left unpaired and a port visible in only one
  column's filter excluded entirely. Different-device pairing is unchanged. A
  same-device pick with fewer than two pairable ports now reports "Need at least
  two free ports to pair", distinct from the different-device wording.
- Inventory page-size selector (issue #599 part 2, PR #605): `Pagination` gains
  optional `pageSizeOptions`/`onPageSizeChange` props that render a labelled
  Rows-per-page select and keep the bar visible even when the result set fits on
  one page; other `Pagination` consumers are unaffected. `InventoryPage` wires
  the selector (25/50/100/200, within the inventory list endpoint's `le=500` cap)
  to `preferencesStore`, so the chosen page size persists across sessions;
  `preferencesStore` drops the unused `getSavedFilter`.
- Fork version preview, diff, and restore (issue #622, ADR 0006 addendum): the
  fork history panel's read-only version list gains per-version Preview (a
  read-only render of that snapshot on the canvas, ghosted the same way the
  parent-topology preview is, with a "Previewing version N" banner and Exit
  control; editing, the wiring dialog, and Commit all lock while it is up), Diff
  (against another version or the current draft; `lib/forkDiff.ts` is the pure
  client-side set-difference, keying an edge on (source, target,
  source_port_name, target_port_name) rather than its own id so a redrawn
  identical wire never reads as churn; added/removed devices and wires list in
  the panel and added/removed edges color-highlight on the canvas), and Restore,
  rendered ACTIVE-only mirroring the Wiring tab's Retry button. Restore is
  restore-TO-DRAFT, never restore-and-reconcile, and appends no fork_versions
  row itself: it copies the version's canvas onto the fork's draft
  (`ReservationFork.draft_restored_from_id` tracks the pending, unsaved
  restore, surfaced as an amber "Draft restored from version N (unsaved)"
  chip), and nothing is wired until you run Commit to reservation, which is
  what appends the version carrying the `restored_from_id` marker. New API:
  `useForkVersion`/`useRestoreForkVersion` in `api/reservations.ts`, both
  proxying the reservations-service endpoints under
  `/reservations/{id}/fork/versions/{version_id}`. The preview/diff/restore
  canvas state lives in one hook, `hooks/useForkVersionPreview.ts`, so
  `TopologyEditorPage.tsx` only wires its result to the ReactFlow props and
  `ForkHistoryPanel.tsx` rather than growing further inline. Closes the epic's
  last `Partial` entry in `PLANNED_FEATURES.md`.
- Network element objects, frontend (issue #22, ADR 0012 phase 2; backend
  validation and fork-save handling land as a separate phase 1): a new canvas
  node kind, `networkElementNode`, models a non-device reachability hub (a
  shared VLAN segment, a subnet, an external cloud, or a patch-panel trunk)
  that many device ports can attach to without a device-to-device mesh.
  `NetworkElementNodeData` (`types/topology.types.ts`) carries a
  client-minted UUID element id, `element_type` (the closed four-value
  vocabulary), an editable `label`, and free-form `attrs`. Unlike a dynamic
  placeholder, an element node is the OPPOSITE of ephemeral: it PERSISTS into
  `canvas_data`, so `persistableCanvas` in `TopologyEditorPage.tsx` keeps it
  (and its edges) while still stripping placeholders, the one deliberate
  asymmetry in the six `isDynamicPlaceholder`-adjacent call sites the new
  `isNetworkElement` predicate needed its own decision at. The Equipment
  Browser gains an unconditional (not fetched, so never absent) "Network
  elements" collapsible section with four drag cards using the
  `application/herd-network-element` MIME; dropping one onto the canvas mints
  a fresh element id and multiple elements of the same type are allowed,
  unlike the one-placeholder-per-template rule. Drawing a line from a device
  to an element opens a new `ElementAttachDialog.tsx` (a single device-side
  `PortColumn` plus a static element target card, not `WiringDialog`, whose
  props assume a device on both sides) with multi-select: Confirm creates one
  attachment edge per selected port in a single `addEnrichedEdges` call, each
  carrying `source_port_name` and no target port, since the element side has
  no ports. `topologyStore.ts`'s `addEnrichedEdge`/`addEnrichedEdges` now
  normalize direction so the device always lands as the edge's `source` and
  the element as `target`, regardless of which side a connection was drawn
  from. Element-to-element connections are refused in both
  `isValidConnection` and `handleConnect` with the toast "Network elements
  cannot be linked to each other". N attachments to one element bundle into
  one `BundledEdge` for free: `groupEdgesForRender`'s pair key is node ids,
  not device ids, so no code change was needed there. `NetworkElementNode.tsx`
  renders dashed neutral gray with a per-type icon and an inline
  double-click-to-rename label, deliberately distinct from
  `DynamicPlaceholderNode.tsx`'s dashed purple so the two ephemeral-looking
  node kinds are never confused, since only one of them survives a save. The
  minimap colors an element node neutral gray. No provisioning of any kind in
  this phase; the frontend renders whatever `InvalidEdge.reason` string the
  backend validator returns and does not otherwise depend on the backend
  phase.

#### Developer platform and CI

- CI flake fix (PR #532): a test handed fakes out in a fixed order while
  `asyncio.gather` interleaved two tasks nondeterministically; fixed by making the
  test order-independent. It had bitten two unrelated PRs (a frontend-only
  dependabot bump and the wiring-dialog PR #530) before the cause was understood.
- Frontend build fix (PR #536): `tsc` was emitting `vite.config.js` into the
  frontend source tree; the build config no longer does that.
- E2E regression and fix (PR #541): PR #538 made the multi-connect dialog the
  default admin Connections create surface, which silently broke a Playwright test
  that assumed the old single-pair modal (e2e is not part of per-PR CI, so this only
  surfaced in the nightly run); the fix flips that test to Single mode before
  proceeding, and is the worked example behind the rule that changing a default UI
  surface requires grepping `tests/e2e/` for every test that opens it.
- Bulk-connection e2e coverage (issue #540, PR #542): Playwright coverage for the
  bulk connection create flow.
- Dead-code cleanup from a QA sweep (PR #600): removed backend and frontend code
  superseded by ADR 0009's L3/L2 reconcile ledgers and otherwise unreferenced,
  each item independently verified before removal. Backend: execution's
  `route_service.py` lost `assign_routes`, `get_pinned_routes`, `release_routes`,
  `release_routes_for_device`, and `is_route_active` (superseded by
  `record_route_active`/`record_route_failed`/`release_route_membership`, which
  `nats_consumer.py` actually calls); `vlan_service.py` lost `release_vlan` and
  `get_vlan_assignments` (superseded by `l2_membership_service.release_l2_membership`
  and the last-free `delete_vlan` allocation-coupling path); auth's unused
  `AccessTokenResponse` schema, user-profile's unused `auth_service_url` setting
  (it validates JWTs locally and never calls auth over HTTP), config's unused
  `GROUPS` constant, and cabling's dead `require_admin` re-export in
  `routes/templates.py` were all removed; a copy-paste logging bug in acl's
  grant-create log line (`body.resource_type` passed twice instead of a distinct
  4th field) was fixed and pinned with a regression test; a dead `bearer_token`
  parameter was dropped from cabling's `find_blocking_reservations`. Frontend:
  deleted the never-imported `DeviceDetailModal` component (device detail renders
  as a full page via `DevicePage.tsx`, not a modal) and its test, the zero-caller
  `useMultiDeviceConnections` hook, the unreferenced `AssistantRequest` type in
  `types/ai.types.ts`, and the unused `class-variance-authority` dependency.
  Duplication-reduction follow-ups from the same sweep are tracked separately in
  issues #595 to #599 and were deliberately left out of this PR's scope.
- Duplication-reduction batch from the #600 sweep's follow-ups, four independent
  extractions into `herd_common`:
  - Shared count-then-page pagination (issue #597, PR #612): `herd_common/pagination.py`'s
    `paginate(db, stmt, *, skip, limit)` runs a `func.count()` over the caller's
    statement (ORDER BY dropped for the count only) plus offset/limit on the
    caller's own statement, with ordering left entirely to the caller. Adopted by
    six list endpoints (acl `list_grants`; auth `get_all_users`,
    `get_all_groups`, `list_mappings`, `list_sync_runs`; notifications
    `list_for_user`); no query parameter or response shape changed at any of
    them. Notifications keeps its existing `offset` parameter and `le=200` cap by
    maintainer decision, and keeps its total decoupled from the `unread_only`
    filter as a separate statement, both pre-existing, tested behaviors.
  - Six third-copy extractions in one batch (issue #595, PR #614):
    `herd_common/database.py`'s `make_database(database_url)` factory, adopted by
    all 11 DB-backed services' `app/database.py`; `herd_common/cors.py`'s
    `add_cors_middleware(app, cors_origins)`, replacing the identical six-line
    `CORSMiddleware` block duplicated across 11 of 12 services (the config
    service's bootstrap-UI `allow_origins=["*"]` exception is untouched);
    `herd_common/auth.py`'s `caller_id(payload)`, adopted by notifications and
    user-profile; execution's `services/_uuid_utils.py` `as_uuid()`, replacing
    three duplicate copies; inventory's `services/manage_guard.py` for
    `_is_admin`/`_user_can_manage_device` (kept inventory-local since they bind
    this service's own settings); and cabling's `routes/forks.py` `_to_delta`
    helper, replacing the same seven-field `WireSpec` mapping written out three
    times. Pure extraction, zero behavior change.
  - Cabling UUID serializer collapse (issue #596, PR #610):
    `services/cabling/app/schemas/_types.py` defines `UUIDStr`,
    `OptionalUUIDStr`, and `UUIDStrList` `Annotated` aliases, replacing 27
    hand-written `@field_serializer` methods across six files. Verified
    offline that the OpenAPI contract is byte-for-byte unchanged.
  - Cached internal-service client (issue #598, PR #615):
    `herd_common/internal_client.py`'s `call_service(base_url, method, path, *,
    auth, ...)` is the client-side transport for one HERD service calling a
    sibling, selecting `InternalTokenAuth` or `ForwardedAuth`; and
    `herd_common/ttl_cache.py`'s `TTLCache`/`SingletonTTLCache` preserve the
    check-lock-re-check-fetch-store sequence three notifications clients had
    hand-rolled, with an injectable clock. Migrated: reservations'
    `_cabling_fork_call`/`_execution_wiring_call`; notifications'
    `ContactClient`/`PreferencesClient`/`AdminListClient`; inventory's
    `_fetch_user_group_ids`/`_fetch_user_group_names`; and `herd_common/acl.py`'s
    `user_has_grant`/`_owns_active_reservation`. Timeouts, error-mapping, and
    fail-closed semantics are unchanged at every call site.
- Lock-faithful service images (issue #593): every service Dockerfile installs
  third-party dependencies from the workspace `uv.lock` instead of resolving
  fresh at build time. Each Dockerfile now runs
  `uv export --frozen --package <svc> --no-hashes --no-emit-workspace
  --no-default-groups -o requirements.txt` and installs that file verbatim,
  then registers `services/common` and the service package themselves with
  `--no-deps` editable installs (no dependency resolution happens on that
  second pass). The build context is unchanged (repo root for every backend
  service); each Dockerfile now also copies the root `pyproject.toml` and
  `uv.lock` into a dedicated `/lock` layer, ordered before the service source
  so the dependency layer still caches across code-only edits. This closes
  the gap that let a freshly built image resolve `anthropic` 1.0.0 while
  `uv.lock` pinned 0.96.0 (the incident this issue tracks): a Dependabot PR
  that bumps a `pyproject.toml` specifier without regenerating the lock now
  fails both `uv lock --check` and every affected image build. A new CI step
  in the `backend` job builds the ai-orchestrator image and diffs its
  installed packages against the same `uv export --frozen` output via
  `scripts/check_image_matches_lock.py`, tolerating only the two editable
  workspace packages and pip's own bootstrap package.
- JetStream durability for `make prod`, with retention caps (issue #620): base
  `docker-compose.yml`'s `nats` service now mounts a `nats-data` volume at
  `/data` and runs `-js -sd /data -m 8222`, so stream contents survive a
  container recreate; `docker-compose.override.yml` replaces `command` with an
  ephemeral store dir, so `make up` and the gate stack keep starting every
  stream empty (test isolation; a volume there would let stale events survive
  a wiped Postgres and NAK into the DLQ). `herd_common/jetstream.py`'s
  `ensure_stream(js, *, name, subjects, max_age_seconds)` is a shared
  add-or-update helper: it tries `add_stream` first and falls back to
  `update_stream` only on the JetStream "stream name already in use with a
  different configuration" error (`err_code` 10058), since plain `add_stream`
  against an existing stream with a different config raises instead of
  returning it, which would otherwise break boot on an upgraded-in-place
  stack. Adopted by the three stream-owning lifespans (reservations'
  `HERD_RESERVATIONS`, execution's `HERD_HEALTH` and `HERD_DLQ`) with a new
  `NATS_STREAM_MAX_AGE_SECONDS` setting (default 7 days, 0 disables the cap)
  on both services. The integration-test NATS helpers
  (`tests/integration/_nats_helpers.py`, `test_health_alerting_flow.py`,
  `test_dlq_and_idempotency.py`, `test_failed_teardown.py`) stopped
  re-declaring streams they don't own via `add_stream` and instead confirm
  existence with `stream_info`, so they no longer race a configured `max_age`
  into an in-use error. `docs/OPERATIONS.md` documents the ephemeral-vs-durable
  split and `docs/ENV_VARS.md` documents the new knob.
- Reused-stack dedup fix for raw-published integration events (issue #611):
  `test_health_alerting_flow.py`'s `_bad_news_event` and `_recovery_event` now
  stamp a fresh payload `event_id`, and `_publish_health_event` sets the
  matching `Nats-Msg-Id` header, mirroring what the outbox producer always
  does. The dev/test NATS container has no volume, so a container recreate
  resets stream sequences to 1 while Postgres still holds notification rows
  keyed under the same `<stream>:<sequence>` dedupe key from an earlier run;
  the next run's inserts then collided on the unique constraint and were
  silently dropped as redeliveries. Stamping `event_id` gives every run its
  own dedupe key regardless of stream sequence.
- Shared auth test harness and generic `Paginated[T]` base (issue #511):
  six auth test files (`test_api_tokens.py`, `test_internal.py`,
  `test_groups.py`, `test_ldap_sync.py`, `test_auth.py`,
  `test_routers_direct.py`) each carried a byte-identical in-memory SQLite
  engine, sessionmaker, `get_db` override, and `setup_db` fixture, plus
  drifted mock-user builder helpers. New `services/auth/tests/_harness.py`
  holds the importable engine/sessionmaker/`mock_user` builder; `conftest.py`
  gains the shared autouse `setup_db` fixture and a `make_client` factory.
  Files that build a private engine by design (the LDAP-sync
  service/reaper/loop suites, the `*_unit.py` files, `test_auth_ldap.py`,
  `test_ldap_service*.py`) are untouched. Separately, `PaginatedUserResponse`,
  `PaginatedGroupResponse`, `PaginatedMappingResponse`, and
  `PaginatedSyncRunResponse` now subclass a new generic
  `app/schemas/pagination.py::Paginated[T]` instead of repeating the same four
  fields (`items`, `total`, `skip`, `limit`); subclassing rather than using the
  bare generic as a `response_model` keeps the OpenAPI component names and
  field order unchanged, so `tests/contract/snapshots/auth.json` needed no
  edit. `Paginated` is deliberately auth-local; promotion to `herd_common`
  waits for a second service wanting the same shape. Full auth suite: 442
  passed, 43 skipped.
- Shared reservations test harness (issue #628, PR #630, the same split as
  #511): six reservations test files (`test_fork_endpoints.py`,
  `test_fork_version_endpoints.py`, `test_wiring_proxy_endpoints.py`,
  `test_rbac_denial.py`, `test_coverage_gaps.py`, `test_reservations.py`) each
  carried a byte-identical in-memory SQLite engine, sessionmaker, `get_db`
  override, and bearer-scheme override. New
  `services/reservations/tests/_harness.py` holds the importable
  `TEST_DATABASE_URL`/`engine`/`TestSessionLocal`/`override_get_db`/
  `override_bearer`; `conftest.py` gains the shared autouse `setup_db` fixture
  and a `make_client(payload)` factory. Each file keeps its own small,
  differently-shaped client helper; only the copied block moved. Eight files
  that bind their session to `app.database`'s own engine at import time
  (`test_expiration.py`, `test_expiry_reminder.py`, `test_dynamic_requests.py`,
  `test_fork_archive_reconcile.py`, `test_fork_backstop_giveup.py`,
  `test_pending_fork_prune.py`, `test_wiring_changed_staging.py`, and
  `test_reservation_service_unit.py`, which patches
  `app.tasks.expiration.AsyncSessionLocal` directly) are untouched by design,
  mirroring auth's LDAP-sync exception; `test_fleet_report.py` mixes a
  fixture-scoped engine with a route-engine block and was left unmigrated
  rather than half-migrated. Full reservations suite: 519 passed, unchanged
  before and after.
- Seeded e2e phase, so a silently-skipping e2e test cannot hide behind a green
  gate (issue #629): the `everything` recipe's existing `test-e2e` pass runs
  before the gate stack is seeded, so every test gated on an available device
  (the fork Playwright tests' `_pw_create_reserved_topology`, `pw_two_devices_with_ports`,
  `transient_reservation`, ...) always skips there by design, and nothing in the
  gate or in nightly.yml re-ran that suite once the stack was actually seeded. New
  Make target `test-e2e-seeded` runs the identical `test-e2e` body (factored into a
  shared `_test-e2e-run` helper so the two cannot drift) with
  `HERD_E2E_REQUIRE_NO_SKIP=1`; `tests/e2e/conftest.py` gates a `pytest_sessionfinish`
  hook on that env var, collects every skipped test (including setup-phase skips
  from a fixture's own `pytest.skip()`) via `pytest_runtest_logreport`, and fails
  the run if any remain, printing each skipped node id and its reason. `everything`
  now runs `test-e2e-seeded` right after seeding the gate stack and before the
  load-test tail, so `EVERYTHING_LOAD=0`/`everything-noload` still gets the seeded
  e2e pass and only the load test itself is skipped; `master` is unchanged (no
  seed phase to run the seeded pass against). `nightly.yml` runs the same target
  right after its "Seed stack for load test" step. The unseeded `test-e2e` pass
  is kept as-is: it exercises the empty-stack UI paths deliberately, this is
  additive. `format_skip_block`, the pure formatting function behind the report
  block, is pinned directly in `tests/unit/test_e2e_seed_gate.py`.
  Follow-up from review: a new `seeded_skip_ok(reason)` marker (registered in
  `pyproject.toml`'s `markers` list, since `filterwarnings = ["error"]` would
  otherwise turn the unregistered-marker warning into a collection error)
  exempts a skip that is expected even on a seeded stack: applied to both
  `test_ldap_login.py` tests (the gate stack runs `AUTH_METHOD=local`; LDAP-mode
  coverage lives in the integration phase, not e2e), and to the two
  deliberately-manual placeholders, `test_ai_feature_gate.py::test_use_ai_button_toggles_with_key_change`
  and `test_config_playwright.py::test_config_save_and_restart_gated`. An exempt
  skip is never counted toward the failing total but is still printed, via the
  new `format_exempt_block` (also pinned in `tests/unit/test_e2e_seed_gate.py`),
  under its own `exempt (seeded_skip_ok):` heading so the log keeps showing it.
  Separately, `test_add_device_ui.py::test_create_device_via_form` had a real
  race on a seeded stack: it read the template `<select>`'s options before the
  templates query had populated them, so it skipped ("no device templates
  seeded") even when the stack actually had 28 templates seeded; it now waits
  (bounded, falling through to the existing skip on a genuine timeout) for at
  least one value-bearing option before checking. The sibling
  `test_template_select_has_placeholder` was checked for the same race and does
  not have it: its placeholder `<option>` is unconditional in
  `CreateDeviceForm.tsx`, rendered outside the templates map, so it needed no
  change. A second review pass added three more fixes: the `seeded_skip_ok`
  marker now also covers the five AI-gated tests (`test_ai_generate_dialog.py`'s
  three `ai_topology`-dependent tests, `test_ai_chat_multi_turn.py`'s chat test,
  and `test_tier2_playwright.py::test_assistant_stream_token_by_token`), since
  neither the gate nor `nightly.yml` configures an AI provider and that skip is
  an environmental gate no seed can satisfy; the new `WebDriverWait` in
  `test_add_device_ui.py::test_create_device_via_form` now ignores
  `StaleElementReferenceException`/`NoSuchElementException` while polling the
  template `<select>`, since a mid-poll re-render could raise one and was not
  caught by the existing `TimeoutException` handler; and the sessionfinish log
  no longer prints the `HERD_E2E_REQUIRE_NO_SKIP=1: 0 test(s) skipped` header
  when every skip is exempt, via the new pure `format_sessionfinish_report`
  helper (also pinned in `tests/unit/test_e2e_seed_gate.py`).

#### Documentation

- ADR 0012, network element objects (issue #22, design only, no code):
  `docs/design/0012-network-element-objects.md` records three decisions taken
  2026-08-29. Storage is canvas-native and topology-local, a new
  `networkElementNode` kind inside `topologies.canvas_data` (and so inside fork
  canvases and version snapshots for free) with no tables and no migration,
  rejecting the registry-plus-attachments shape the issue body proposed because
  reservation-time edits would mutate global rows, forks would need their own
  element story, and element delete would have to sweep every topology's
  canvas. Provisioning is out for v1: an element is a reachability hub only,
  attachments are declarative `layerEdge` rows the validator accepts without a
  BFS, and cabling's fork-save resolver skips them explicitly with a new
  additive `element_attachments_skipped` count, so the invariant is that an
  element edge never becomes a hop and ADR 0009's derivations need no element
  rule. The anchored-VLAN variant is recorded as phase 2 with its hook named
  (synthetic device-to-anchor-switch hops at fork save, since a chain endpoint
  never receives a driver call). AI generation of elements is deferred to a
  follow-up issue. `PLANNED_FEATURES.md` links the ADR from the network element
  objects bullet, which stays `Planned`.
- Network element objects, phase 3 of 3, docs plus live Playwright e2e (ADR
  0012, refs issue #22; closes out the epic phase 1, PR #634, and phase 2 both
  merged first). `TOPOLOGY_EDITOR.md` gains a "Network elements" section
  beside "Dynamic placeholders" covering the palette, the attach dialog, and
  the persist-versus-strip contrast between the two dashed node kinds (gray
  elements persist, purple placeholders never do); `USER_GUIDE.md`'s topology
  editor summary and the published manual
  (`docs/manual/user-topology.html`, a new "Network elements" section, and
  `docs/manual/user-reservations.html`'s equipment-browser note that
  elements never add to a reservation's device count) get the same coverage;
  `BULK_IMPORT_EXPORT.md` documents the CSV-does-not-carry-attachments
  limitation next to the existing isolated-node caveat; `FEATURES.md` gains
  the shipped capability and `PLANNED_FEATURES.md`'s bullet flips from
  `Planned` to `Shipped` with the three-phase delivery summary. This entry's
  ADR doc also gets one amendment: the "Canvas shape" call-site list for
  `isDynamicPlaceholder` missed a seventh site discovered during phase 2
  review, `handleAIProposal`'s device-id set, which phase 2 fixed with a
  positive `isDeviceNode` predicate (`frontend/src/lib/canvasNodes.ts`) and
  the extracted `collectCanvasDeviceIds` helper rather than a negated
  placeholder/element pair, since a negated pair silently stops being
  exhaustive the moment a future node kind is added.
  `tests/e2e/test_network_elements_playwright.py` (new) covers the two
  acceptance paths the ADR's "Testing" e2e level names: dropping a
  `vlan_segment` element from the Equipment Browser (a native DragEvent
  dispatch, since the card is a plain draggable div rather than a React Flow
  node) and attaching two ports through `ElementAttachDialog` via a real
  device-node-handle-to-element-node-handle drag (the same mouse
  move/down/move/move/up technique `test_wiring_dialog_playwright.py` uses
  for device-to-device wiring, since `NetworkElementNode` exposes exactly one
  target handle), then saving, reloading, and reading back through `GET
  /cabling/topologies/{id}` that the element node and both attachment edges
  persisted with `source_port_name` set and through `POST
  .../validate` that the canvas reports `valid: true`; and a reservation
  created against an element-carrying topology reaching `ACTIVE`, with the
  fork's `GET /reservations/{id}/fork` canvas carrying the element node and
  its `POST .../fork/save` response reporting `element_attachments_skipped:
  1` with zero released/built rows, confirmed by a `wiring-status` read-back
  showing no wiring at all for the attachment. Run live, twice, against a
  Playwright-driven Vite dev server proxied at the seeded gate stack (issue
  #629's device-availability seeding trap applies here too: run explicitly
  against a seeded stack, since both `make everything` and `nightly.yml` run
  e2e before `make seed`), plus every other `*_playwright.py` file that opens
  the canvas or the Equipment Browser
  (`test_wiring_dialog_playwright.py`, `test_connections_bulk_playwright.py`,
  `test_fork_live_edit.py -k _pw`, `test_tier2_playwright.py`), all green.

#### Test coverage

- Coverage batch (2026-08-30, PRs #650 through #660): backend workspace line
  coverage stood at 94.76% before this batch (secrets 80.8%, integration
  82.3%, the two lowest services); frontend stood at 73.75% lines with seven
  pages at 0%. After the batch: backend 97.07% lines with every service at 94% or above (secrets 98.3%, integration 97.3%), frontend 89.7% lines with 1,210 tests (was 879), and no file added since v0.2.0 below 85%.
  - Backend: secrets routers to 100% (PR #650); acl's grants router plus a
    root-level unit test for `scripts/check_image_matches_lock.py` (PR #651);
    integration's `nats_consumer` and `webhooks` to 100% (PR #653); cabling's
    forks routes and `fork_save_service` to 100% (PR #654); auth's
    `ldap_sync`, admin, and tokens routers to 100% (PR #656); execution's
    `nats_consumer` 90% to 95%, `wiring_retry_service` and
    `l1_assignment_service` to 100% (PR #659).
  - Frontend: admin group, device-group, users, and add-device pages 0% to
    95-100% (PR #652); the topology editor page 60% to 96%, `AppLayout` to
    100%, plus the reporting and reservations pages, alongside a new vitest
    coverage config that switches to include-based reporting so an untested
    file surfaces at 0% instead of being omitted from the report (PR #658);
    topology and template pages plus `FieldRow`, a 32% lane average to 96%
    (PR #660); the inventory, connections, drivers, recipes, and grants pages
    (PR #655).
  - Most of the backend router gaps were not missing behavior but a
    coverage.py artifact: post-await lines under-attributed behind httpx's
    `ASGITransport`, closed with direct handler-call tests in the existing
    `test_routers_direct.py` convention rather than new HTTP-layer tests.
    A minority were genuine gaps and got real behavior tests instead:
    cabling's prune paths, execution's race branches, and `AppLayout`'s
    dropdown-close behavior.

## [0.2.0] - 2026-08-03

- Completed the connection-driven reconcile epic (ADR 0009): initial provisioning, fork saves, and terminal teardown all flow through one full-reconcile pipeline over the three wiring ledgers, retiring the legacy device-set resolvers; device-set changes wire and release only through fork saves.
- Layered wiring-status surface: every wiring row is tagged l1/l2/l3 (L2 rows carry the resolved fabric VLAN, L3 rows a route count), the reservation Wiring tab renders all three layers with six-outcome retry counting, and the external `/api/v1` facade gains a read-only wiring-status passthrough.
- HERD-owned VLAN definition lifecycle: `create_vlan` on a fabric allocation's first built membership over a transit-inclusive switch scope, `delete_vlan` on last-free with a reuse-race supersession guard; driver `create_vlan` is required to be idempotent.
- Reliability hardening: unreadable fork intent defers convergence instead of tearing down live wiring; terminal teardown freezes wiring first, with commit-time frozen re-checks in all three ledgers; device removal releases wiring from the saved intended set, never the draft canvas, with a durable retry marker; NATS consumers wait for schema readiness during upgrades; startup never runs create_all on a migration-managed schema.
- Security: bulk topology import updates enforce the creator-or-admin gate per row, and visible-device lookups are self-or-admin.
- Frontend: ACL grants management UI, and dynamic-template authoring with hypervisor registration.
- Developer platform: Playwright effect-assertion e2e suite, the validation-gate stack isolated in its own compose project, and a shared HerdBaseSettings config base class.

### Delivery detail

#### Reconcile and provisioning

- ADR 0009 phases 6 through 8, completing the epic (issue #416, CLOSED): phase 6
  (PR #443) moved terminal-transition release onto the three ledgers' shared apply
  path (the teardown pass is deliberately not frozen-gated); phase 7 (PR #447)
  unified initial provisioning onto the connection-driven full-reconcile
  (activation creates the fork, then stages a delta-less `wiring_changed` event
  atomically with the ledger advance, since staging inside the ACTIVE-flip
  transaction races the outbox relay against the fork POST) and retired the
  device-set resolvers, the three `_execute_*` paths, and the event-action maps
  (net -3718 lines), with an expiration-sweep backstop for a reservation whose fork
  creation never landed; phase 8 (PR #449) layered the wiring-status surface by
  l1/l2/l3, extended the Wiring tab to all three layers, and added the read-only
  wiring-status passthrough on the external `/api/v1` facade (relayed verbatim as a
  deliberate exception to the facade's v1-freeze rule; manual retry stays internal
  only). The phase 7 review's hardening nits (issue #448, PR #453) and a
  terminal-teardown warn-and-skip for a missing `reservation_id` (issue #455,
  PR #458) landed as immediate follow-ups.
- Sweep batch (issues #459 through #463, plus the #442 decision; PRs #474, #476,
  #477, #478, #480): HERD-owned VLAN definition lifecycle, coupling `create_vlan` to
  the fabric allocation in the shared allocation-transition step so the fork-save
  reconcile, terminal teardown, and both retry channels share one implementation
  (fires per (allocation, switch) still pending over a transit-inclusive scope,
  since an undefined VLAN on a transit switch forwards nothing; a create failure
  parks that switch's dependent membership builds as FAILED intended-ACTIVE rows);
  `delete_vlan` runs on last-free per switch, is supersession-skipped when the
  number was re-allocated, and a delete failure is log-and-continue; fail-closed
  fork fetch, `_fetch_fork_intended_wires` raising on any non-genuine-404 non-200 so
  unreadable intent defers convergence instead of full-reconciling against an empty
  set and tearing down live wiring (the sibling `_fetch_*` helpers deliberately keep
  the non-200-means-absent idiom, since their misread degrades one connection, not
  the whole desired set); freeze-first terminal teardown, landing and committing the
  freeze before any ledger snapshot or teardown driver call, with all three ledgers
  re-checking frozen at record time; PATCH-remove reworked to prune the fork's
  saved intended set by edge incidence (through-hops serving a remaining saved edge
  survive, far hops of a pruned edge release) instead of round-tripping the draft
  canvas through a save, with a durable `pending_fork_prune_device_ids` retry marker
  (migration 0014); a NATS consumer schema gate
  (`herd_common.consumer_schema_gate.start_consumer_when_schema_ready`) that defers
  behind a background poll on the migration-managed path with model tables missing,
  instead of draining events into the DLQ during an upgrade window.

#### Security

- Authz tightening: bulk topology import updates enforce the creator-or-admin gate
  per row (issue #464, PR #475), and inventory's
  `GET /device-groups/visible-devices` is self-or-admin (issue #465, PR #474).

#### Frontend

- ACL grants management UI (issue #397, PR #450).
- Dynamic-template authoring and hypervisor registration (issue #398, PR #454):
  `HypervisorsPage.tsx` plus the dynamic fields in `TemplateEditorPage.tsx`, giving
  every backend feature of the dynamic-resources epic a frontend path.

#### Developer platform and CI

- Migration lifecycle fix (issue #419, PR #452):
  `herd_common.schema_init.create_all_and_stamp` now skips `metadata.create_all`
  entirely once a schema carries an `alembic_version` stamp, closing the
  create-all-vs-migration collision hazard at the source; a per-migration
  `has_table` guard is no longer required for new migrations.
- HerdBaseSettings (issue #384, PR #451): all 11 service Settings models now extend
  a shared `herd_common.base_settings.HerdBaseSettings` base, removing duplicated
  per-service settings boilerplate.
- Gate-project isolation (PR #446): the master/everything ephemeral validation
  stack runs in its own compose project, so gate runs no longer destroy the dev
  stack's volumes or seed data.
- Effect-assertion e2e suite (issue #388, PR #444): 19 tests asserting the
  backend-observable effect via API read-back after each UI action, not just UI
  acknowledgment, and restoring mutated state to baseline; also replaced the flaky
  Selenium `test_inventory_expanded` nightly case (issue #335) with a Playwright
  port.

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

### Delivery detail

#### Reconcile and provisioning

- Editable reservation topology forks (ADR 0006, issue #25 P3a, PRs #349, #353,
  #359 through #361): cabling owns `reservation_fork`/`fork_connections`/
  `fork_versions` (keyed by a bare `reservation_id`, no cross-schema FK) behind an
  idempotent internal fork-create call; reservations forwards the user-facing fork
  surface and archives forks from all terminal transitions; live-edit mode edits the
  fork instead of the parent topology, with debounced draft autosave, a
  released/built toast, a port-conflict dialog, and a read-only as-built render for
  ended reservations.
- Connection-driven L1 reconcile (ADR 0007, issue #345 P3b, PRs #363 through #365,
  #367, #368, #371, #372): fork saves stage a `reservation.wiring_changed` event
  anchored to a `fork_wiring_ledger` row advanced atomically with the outbox
  enqueue; execution applies events in fork-version order via
  `reservation_wiring_state.last_applied_fork_version`, chain-walking hops grouped
  by a new nullable `edge_key` column (migration 0008, the canvas edge id,
  deliberately kept outside connection identity) rather than pairing positionally; a
  Wiring tab surfaces per-connection status with manual retry for parked rows,
  backed by a batch-capped auto-retry channel.
- Driver-result-keyed success (issue #370, PR #387): the shared
  `driver_result_failed` helper judges success from the driver's own result payload
  rather than the sandbox transport flag (a healthy transport only means the child
  process did not crash), and every `run_driver_action` call site (health polls,
  device-check, manual/internal execute, configure, retry) now gates through it.
- Never-downgrade-an-ACTIVE-row discipline (issue #412, PR #414):
  `record_l1_failed` never overwrites an ACTIVE assignment row with a failure, since
  a failure recorded against a pair a concurrent writer has since proven connected
  is stale by definition.
- ADR 0009 phases 1 through 5 (issue #416, in delivery): phases 1-2 (PR #418) gated
  every L2/L3 driver call site through `driver_result_failed` and corrected the
  legacy device-set L1 resolver to pair per switch (exactly two reserved-adjacent
  ports pair unambiguously; any other count fails safe with a warning, since the
  device-set graph carries no edge intent); phase 3 (PR #424) added
  intended-direction wiring ledgers (migrations 0016 through 0018) with
  direction-scoped retry and freeze (terminal reservations may retry
  release-direction rows; the manual-retry proxy 409s only PENDING/
  PENDING_PROVISION; the UI Retry button is ACTIVE-only), the six-valued retry
  outcome vocabulary (reconnected/released/superseded/still_failed/
  not_retryable/frozen), and a cross-reservation supersession guard for no-driver-
  call releases; phase 4 (PR #437) made L2 VLAN membership fork-driven on
  `wiring_changed`, always a full reconcile derived from recorded hops with trunk
  hops excluded, split from the unchanged per-fabric VLAN-number allocation (first
  built membership allocates, last released frees); phase 5 (PR #438) made L3 route
  adjacency fork-driven the same way, with release adjacency-aware (routes stay
  while any intended hop lands on the switch) and the issue #20 pin lifecycle left
  untouched; L3 supersession is deliberately absent, since pins are per-reservation
  and non-exclusive.
- L3 driver contract (issue #20, PR #251): Layer 3 Switch promoted to a fully
  invoked connection type.
- Dynamic hypervisor-backed resources (ADR 0004, issue #32, PRs #264, #271, #272,
  #281): a hypervisor registry and dynamic template type, recipe drivers
  (`create_instance`/`destroy_instance`) run through the execution sandbox, an
  idempotent `dynamic_instances` ledger keyed by a stable per-request `request_id`,
  gated reservation activation via a `provision-result` callback, and a
  compare-and-swap expiration-sweep timeout backstop that moves a stuck
  PENDING_PROVISION reservation to FAILED without racing a late callback. The same
  backstop reverts a stranded physical-only reservation back to PENDING instead of
  failing it (issue #318, PR #350), since `reservation.created` stages atomically
  with ACTIVE, so a stranded row provably triggered no provisioning.
- Fleet-scale health polling (issue #24, PR #306): the health scheduler claims at
  most `HEALTH_POLL_BATCH_SIZE` due rows per tick and runs at most
  `HEALTH_POLL_MAX_CONCURRENCY` polls at once; devices carry a persisted in_use/idle
  poll tier flipped by reservation lifecycle events; `EXECUTION_POLLER_ONLY` runs a
  replica as poller-only with no API routers.
- Transactional outbox (issue #21, PR #237): the event producer writes an outbox
  row in the same DB transaction as the state change, and a background relay
  publishes it with a `Nats-Msg-Id` header for broker-side dedup plus a payload
  `event_id` for consumer-side dedup, so no request-path code publishes a
  state-change event to NATS directly.
- Off-loop driver execution (issue #317, PR #320): the execution consumer runs
  driver calls via `asyncio.to_thread` with a per-message `in_progress` ack
  heartbeat, so a slow provisioning call does not trigger ack-timeout redelivery.
- Driver-published config schemas (issue #23): a driver's optional
  `config_schema()` classmethod is extracted in the sandbox and cached per-SHA256;
  the `configure` boundary validates against the published schema first and falls
  back to inventory's connection-type registry only on a `PublishedSchemaError`, so
  a driver can accept a config vocabulary the registry's `additionalProperties:
  false` schema would otherwise reject.

#### Cabling and inventory

- Bulk import name-match semantics (issue #336, PR #348): topology import matches
  existing topologies by name and updates them in place rather than duplicating; a
  topology update that would rewire wiring held by another user's active
  reservation rejects per-row.
- Batch read endpoints (issues #249, #250, PR #302): inventory's
  `POST /devices/batch` and cabling's `POST /pathfind/batch` replace per-id GET
  fan-out from the topology editor.
- Cross-service referential guards (2026-07-19): inventory's admin device DELETE
  refuses with 409 `device_in_use` while a non-terminal reservation holds the
  device, via a fail-closed reverse lookup into reservations (issue #391, PR #407);
  cabling's `POST /connections` 422s when a referenced device does not exist, since
  inventory's `GET /device-groups/device/{id}` 404s for a missing device while
  still returning 200 `[]` for a real-but-ungrouped one, with the guard's fail-open
  path reserved for transport/5xx outages (issue #392, PR #406).
- Config-version restore active-reservation guard (issue #337, PR #352).

#### Security

- Constant-time internal-token verification (issue #311, PR #351):
  `internal_auth.internal_token_matches` replaces `==` comparisons for
  `X-Internal-Token` checks.

#### Developer platform and CI

- Playwright e2e adoption (PR #400, 2026-07-19): new e2e tests use Playwright's
  Python sync API via `pw_browser`/`pw_page` fixtures, running host-side against
  `E2E_HOST_BASE_URL` rather than the Selenium container; the Selenium suite is
  kept and ported opportunistically.
- Config precedence (PR #374, 2026-07-18): every service resolves settings through
  `herd_common.config_loader.herd_settings_sources`, with a `config.json` saved
  through the config UI outranking container env vars, and a first-boot
  auto-bootstrap file (marked with a sibling `config.bootstrapped` file) ranking
  below env until the first real UI save.
- Save-and-restart scoped to the current compose project (issue #373, PR #383): the
  config UI's restart action self-inspects for `com.docker.compose.project` and
  fails closed, so other compose stacks on the host are never bounced.
