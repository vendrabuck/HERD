# Changelog

## [Unreleased]

- Fixed: a reservation notification's end time is the UTC instant it names (#1564; INTEG-ROUTE-1 updated, INTEG-ROUTE-7 added). The "Reservation confirmed", "Reservation updated: ends", and "Reservation expiring soon" texts printed the wall clock of whatever offset the event's `end_time` carried and labeled it UTC, so `2026-11-25T10:00:00-08:00` read `2026-11-25 10:00 UTC`; it now reads `2026-11-25 18:00 UTC`. A value with no offset is taken as UTC, never as the host's zone. A value whose instant has no UTC equivalent (before year 1 or after 9999 once converted) is written unchanged, like any other string that does not parse. A non-empty non-string `end_time` (a number, a list, an object) now reads `unknown end`; it used to raise in the consumer, so the message was retried until it reached the dead-letter subject and the owner was never notified.
- Fixed: a purpose classify answer of 200 is stored only when it is a usable suggestion, and every other 200 counts one attempt on its row and lets the sweep go on to the next row (#1543, and the classify part of #1577; RES-PURPOSE-6, RES-PURPOSE-7, RES-PURPOSE-8, RES-PURPOSE-13, and section 10's classify row updated; the #1543 open defect removed, and section 13 of `reservations.md` and of `operations-and-observability.md` now list #1577's other sites, with its known-gap note on RES-CREATE-9, RES-DYN-1, RES-DYN-10, RES-HOLD-5, RES-HOLD-9, RES-FORK-10, RES-FORK-18, OPS-REPORT-11, OPS-REPORT-12, and OPS-REPORT-14). Usable means the body parses to a JSON object whose `top_category` is a string (the owner's decision of 2026-11-25), nested no deeper than 32 levels, holding no number that is not finite, and holding no string, dict keys included, with a NUL or an unpaired surrogate: nothing is stored that the store, Postgres's `json` operators, or the response models refuse later. Before, a list, a string, or a number was stored verbatim, after which Classify now, the review list, accept, dismiss (after its write committed), and the owner's `GET /{id}` answered 500; an object without a string `top_category` was stored, and accept with a null body then cleared the row's category; a JSON `null` stored nothing, counted no attempt, and was sent again on every tick with no cap. A body nested deeper than the JSON decoder's recursion limit raised `RecursionError`, which is not a `ValueError`, so it escaped the classifier: Classify now answered 500, and the sweep ended its tick on that row with no attempt counted, so the oldest such row was retried first on every tick and the rows behind it were never reached; a 403 with such a body did the same. On Python 3.12 the JSON encoder's limit is within a level or two of the decoder's (about 9,996), and the store runs on a deeper stack: a body 9,988 or 9,993 levels deep parsed and then failed on store (measured on SQLite), and a `NaN` or `Infinity` is refused by Postgres on store, each with the same stall. A NUL or an unpaired surrogate in any string or key (which a model's `rationale` can carry) was stored, after which on Postgres the filtered review list failed for every admin, and the unfiltered one too once the row had a category, and on every database the response models refused the surrogate. Every such 200 is now `failed`: one attempt, nothing stored, and one WARNING with the fixed action `purpose_classify_bad_body`, a fixed `reason` (`not_json`, `too_deep`, `wrong_shape`, `bad_text`), and the top-level type's name, never body or exception text; a 403 whose body does not parse is `forbidden`. The catch sits in `classify_purpose_one`, the one place that knows a failure is the row's own, not around the sweep's call. Migration 0019 resets every stored suggestion this rule refuses (judged by a copy of the rule frozen in the migration), stored text Python cannot parse (an integer over 4,300 digits, or nesting past the decoder's limit), and a suggestion time or dismissal left with no suggestion: all three become SQL NULL, the stamp and the attempt count are kept, and the sweep classifies the row again. It judges each stored value in Python, since every Postgres operator that looks inside a `json` value refuses text such as `"\u0000"`, and it refuses offline (`--sql`) mode. Tests: `services/reservations/tests/_purpose_classify_answers.py` gains 43 answers (a 200 of every JSON type that is not an object, an object without a string `top_category` in each way, bodies 200,000 levels deep at 200 and 403, an object at and one past the 32-level bound, four non-finite numbers, a NUL, a lone high surrogate, and a lone low surrogate each in a value, a nested value, a key, a nested key, and `top_category`, and a surrogate pair, a noncharacter, and a control character that are stored), which the three table tests run; new tests run three rows through the attempt cap for every unusable 200, pin the log line's fields, read the row back through the review list, accept, dismiss, `GET /{id}`, and `GET /` after Classify now and after migration 0019, and run the migration on SQLite and, in the new pg-live suite `test_purpose_suggestion_reset_live_pg.py` (wired into `_gate-pg-live-tests`), on the real `json` column.
- Tests: the live-Postgres suites no longer default to the dev stack's database; a `*_live_pg.py` suite connects only to the database `HERD_TEST_PG_DSN` names (#1401, the owner's decision of 2026-11-25; no product change). Before this change all twenty suites fell back to `postgresql+asyncpg://herd:herd@127.0.0.1:${POSTGRES_PORT:-5433}/herd`, the Postgres `make up` publishes, so a bare `make test`, `make coverage`, or `pytest` in a service directory wrote rows into the working database whenever that stack was up. With the variable unset the suites are skipped with a reason that says how to run them and connect to nothing; with `HERD_TEST_PG_REQUIRED` set and no DSN every test errors. A named database that does not answer is skipped as before, now with a reason that names it (driver, host, port, and database only, never a user, password, or query string), and errors when `HERD_TEST_PG_REQUIRED` is set; a strict mode that makes it an error either way was considered and is the owner's to turn on. The answer of the one `SELECT 1` probe (10 s connect timeout) is cached per DSN per process. The rule is one helper, `herd_common.pg_live_suite.pg_live_target`, replacing twenty per-file copies, with its own tests in `services/common/tests/test_pg_live_suite.py`. The gate is unchanged: `_gate-pg-live-tests` already passed the DSN with `HERD_TEST_PG_REQUIRED=1` on every command. A new repo-root guard, `tests/unit/test_pg_live_dsn_source.py`, fails when a live suite does not take its database from the helper; when a suite or the helper holds outside a docstring a Postgres URL whose host is not the placeholder `HOST`, a loopback address, or the port 5433; when a Python file outside its exemption list (the helper, the helper's tests, and the gate wiring guard) reads or writes either variable in the process environment, through an entry call's arguments (keywords and `**` included), an `update`, `__ior__`, or `patch.dict` call, an `|=`, a subscript, or a replaced `environ`, with the name spelled in literals or held in a name the file binds; and when the Makefile outside the gate rule, `.env.example`, or a `pyproject.toml` names `HERD_TEST_PG_DSN`. `docs/specs/TESTING.md` (pg-live suites) and `docs/specs/DEVELOPMENT.md` state the rule.
- Fixed: the main navigation no longer shows Reporting to a non-admin (#1402, owner decision: hide the link). Reporting sits behind `AdminGuard` and both report routes are admin only, so a plain user who clicked it was sent to `/topology` with no message. The main navigation is now declared as `MAIN_NAV` beside `ADMIN_NAV` in `frontend/src/lib/adminNav.ts`, each entry with an optional `requires`, and `AppLayout` renders it through `visibleMainNav`, which filters on `isAdminRole`, the predicate `AdminGuard` applies: an admin and the superadmin see Reporting as the last main link; a plain user, any other or missing role, and an account still loading or that failed to load get no Reporting element at all. `src/test/routes.test.tsx` now fails when any header link requires admin and its route is not AdminGuard-guarded, or the reverse, so a future admin page linked from the main navigation cannot repeat this; no other header link had the mismatch. Opening `/reporting` directly as a non-admin still redirects to `/topology` with no message, unchanged. This is a consistency fix in the UI, not an access control change: the backend refused a non-admin before and still does (OPS-REPORT-1). New rule IAM-UI-17; `docs/specs/CONVENTIONS.md` and the manual's quickstart and Reporting pages updated. Tests: `frontend/src/test/routes.test.tsx` (the requirement-matches-guard drift guard over every `MAIN_NAV` and `ADMIN_NAV` entry, and both filters over user, admin, superadmin, unknown, and malformed roles), `frontend/src/test/components/AppLayout.test.tsx` (the main navigation by role, loading, a failed account load, Tab order with and without Reporting, and the current-page mark), and the e2e `tests/e2e/test_register_and_roles.py` (`test_non_admin_has_no_administration_menu` now asserts no Reporting link once the account has loaded) and `tests/e2e/test_reporting_page.py` (`test_admin_main_navigation_links_to_reporting`), neither run on this host.
- Changed: a first single sign-on login whose email holds a character outside ASCII, in the local part or the domain, is refused and answers `/login?sso_error=failed`, creating nothing (#1400 in part, owner option (a); IAM-OIDC-9 and IAM-OIDC-10 updated, ADR 0016 D4 amended). The refusal is logged as `email_invalid`, except for a control character (a C0 or C1 control, U+0080 to U+009F being outside ASCII) or a lone surrogate, which the callback already drops so the claim counts as absent and the refusal is `email_missing`, as before this change. Before this change a visible look-alike of an existing account's email (a Cyrillic or Greek letter, a dotless i, a fullwidth form, a combining mark) was provisioned as a second account. The check runs in `_first_login_refusal` on the email claim exactly as it would be stored, with no Unicode normalization before it (a fullwidth letter that NFKC would fold to ASCII is refused, not folded), and before the name locks and the collision check, so the answer is the same whether or not the imitated account exists and no relink candidate is kept. It also refuses internationalized mailboxes; a domain in its ASCII `xn--` form is accepted and stored as sent. A returning login never reads the email, so an account provisioned with a non-ASCII email before this change keeps signing in. Limits by the choice of option (a): an all-ASCII look-alike and a punycode `xn--` domain are accepted. Still open (the identity specification's Open defects; any rule for the local and LDAP paths is the owner's decision): an ASCII email imitating an account that already holds a non-ASCII one, a local or LDAP account or an `oidc` account from before this change, is provisioned as a second account; for one, local registration stores a punycode domain decoded to Unicode, so a first single sign-on login sending the same mailbox in its `xn--` form does not collide. Tests: `services/auth/tests/test_oidc_provisioning.py` (22 non-ASCII classes with and without the imitated account, the reason order, ASCII acceptance with plus tags, mixed case, and punycode, a non-ASCII display name, the returning and pre-existing account paths; `test_visible_unicode_lookalikes_are_distinct_emails_today` is replaced) and `services/auth/tests/test_oidc_routes.py` (the same over the callback, seven C0, C1, and surrogate cases pinned to `email_missing`, and the log-hygiene check also matching the JSON-escaped form of each value). `docs/OPERATIONS.md` gains the `email_invalid` troubleshooting entry, with a query for accounts that hold a non-ASCII email.
- Fixed: a reservations background task that dies is logged when it dies, and can no longer stop shutdown (#1549; OPS-OUTBOX-11 and OPS-OUTBOX-13 updated). The expiration loop, the purpose-classify loop, and the outbox relay had no done callback, so a task that ended with an exception was silent until shutdown, and at shutdown its exception, re-raised at its await, skipped the cancel of the tasks after it and the NATS close. The new shared helper `herd_common.background_tasks.log_task_crash` logs such a task at ERROR the moment it ends (`action` `background_task_crashed`, the task's name, the exception class name only, never its text), and reservations adopts it for all three tasks. Shutdown now cancels all three before it awaits any, then awaits each through the second new helper, `await_stopped_task`, which returns, never raises, what a task ended with: an `Exception`, or a `BaseException` subclass that asyncio does not treat as a process-exit signal, is logged (`background_task_failed_at_shutdown`, class name only), is not raised, and the rest of shutdown, the NATS close included, still runs. A `KeyboardInterrupt` or `SystemExit` in a task is different and unchanged: asyncio raises it out of the event loop as the task dies, and `asyncio.run` then cancels the lifespan at its `yield`, so none of these shutdown steps run in that case. A `GeneratorExit` thrown into the shutdown while it awaits a task propagates at once; and a lifespan cancelled while it awaits a task finishes the steps and then ends cancelled (it used to swallow its own cancellation). In `herd_common.outbox`, the wake-up listener builds its connect arguments inside a `try`: a failure is logged once at ERROR (`outbox_listener_setup_failed`, class name only) and the listener returns, so the relay drains on its tick; the listener task carries the same crash callback, the relay's shutdown awaits it through `await_stopped_task`, and the relay, when cancelled, always ends with `CancelledError`, never a dead listener's exception. This affects execution's relay too. Execution's three `_surface_crash` callbacks, the consumer schema gate's, and inventory's `_log_scheduler_crash` are unchanged; moving them to the helper is a later cleanup.
- Fixed: the provision-result callback compares device ids as UUIDs, not as spellings (#1552; RES-DYN-7 and the errors table updated). `ProvisionResultRequest.device_ids` is now `list[uuid.UUID]`. A success callback that named one new id in two spellings (case, braces, `urn:uuid:`, 32 hex digits), or a held id in another spelling, answered 500 and left the row in `PENDING_PROVISION` until the timeout backstop failed it; each id now attaches once and the row activates. A `device_ids` entry that is not a UUID pydantic accepts (spellings only the stdlib `uuid.UUID` accepts, such as `uuid:<id>`, included), on a success or a failure callback, is refused with 422 before anything runs and changes nothing on the row; the provisioning timeout backstop owns it (owner decision). Execution sends canonical ids and an empty list on failure, so its callbacks are unchanged, and the OpenAPI contract snapshot is unchanged (the field is still an array).
- Fixed: the utilization report answers 503 `{"error": "transit_gear_unavailable"}` when cabling's transit answer is a 200 of the wrong shape (#1553; OPS-REPORT-7 and OPS-REPORT-8 updated). A body that does not parse (not JSON, not UTF-8, or nested deeper than the decoder's recursion limit, which raised a `RecursionError` and answered 500), or one that is not an object holding a `devices` object (a list, a string, a boolean, a number, null, an object without `devices`, `devices` that is not an object), now fails the report closed like a non-200 on both routes; before, most of these answered 500, a body that did not parse answered 422 with the decoder's message, and `{}`, `null`, or `{"devices": null}` reported no transit gear at all. Inside a well-formed map a device list that is not a list, or a device id that is not a string, is skipped and the report is still produced (owner decision); the report logs one WARNING per report (`transit_gear_entries_skipped`, two counts, no body text). The leniency PR #1547 pinned (a non-UUID key or device id string, a null list) is unchanged and logs nothing.
- Fixed: the pending fork-prune marker matches ids as UUIDs, not spellings (#1554; RES-FORK-17 and RES-PATCH-11 updated). A stored entry in another spelling of an id (any spelling the stdlib `uuid.UUID` accepts: case, braces, a `urn:uuid:`, `urn:`, or `uuid:` prefix, 32 hex digits, dashes in any place) was pruned every tick and never cleared, holding one of the 20 batch slots; the clear now removes every entry whose text parses to a pruned id, so the row leaves the batch after its first converged prune, and it rewrites only a non-empty list, leaving any other stored value for the heal. A device-set PATCH over a stored marker that is not a list of UUIDs answered 500 for `5`, `true`, or a list holding an object, and split a string or an object into junk entries; the union now keeps only a non-empty list's entries that parse as UUIDs, in canonical form and each id once, treats any other stored value as empty, answers 200, and logs a discard at ERROR after the edit commits (`pending_prune_corrupt_marker_discarded`, the stored type's name, and for a list the count of dropped entries, never the stored text; owner decision); an edit that loses its status guard logs no discard.
- Fixed: a JSON import that the parser refuses for its shape answers 422 `Invalid JSON: the document is nested too deeply or holds a number too long to read` on `POST /devices/import`, `POST /templates/import`, and `POST /topologies/import`, and nothing is written (#1511; INV-BULK-4, INV-BULK-22, and TOPO-BULK-7 updated, new INV-BULK-24). A document nested deeper than the parser's C recursion budget (just under 10,000 levels on the service image's Python 3.12, a 20 KB file) raised `RecursionError`, and an integer literal longer than 4,300 digits raised a `ValueError` that is not a `JSONDecodeError`; both escaped `_parse_json` and `parse_json_topologies` as 500s, and cabling's import takes any signed-in user. A syntax error keeps its `Invalid JSON: <decoder message>` detail. A device or template row whose `field_data` or `sections` is JSON text refused the same way (a CSV cell, or a JSON string in a JSON import) is rejected with the fixed reason `a JSON cell is nested too deeply or holds a number too long to read` instead of Python's exception text.
- Tooling: `make lint`, `make format`, and CI's ruff steps now cover all of `tests/` and `drivers/`, not only `tests/ai_eval/` and `tests/unit/` (#1557, the owner's decision of 2026-11-25). `ruff check .` and `ruff format --check .` from the repository root are clean over the whole repository. Bringing them in changed 26 Python files: the formatter alone rewrote 25, which also rewrapped three of the four lines over 100 columns the issue named, and the fourth line, a comment in a file the formatter left alone, was wrapped by hand. One Python code block in `docs/EXTERNAL_API.md` was put in the formatter's style (`[len("sha256=") :]`). No behavior changed: the AST of every touched Python file is identical, comments aside.
- Fixed: when a sandboxed driver call ends, for any reason, no process the driver started that stayed in its process group is left running (#1407; CFG-SBX-5 and CFG-SBX-8 updated). Before this change the child ran under `subprocess.run`, which on a timeout killed only the child itself: a helper the driver had started (an `ssh` client, an `expect` script, a vendor CLI) kept running, adopted by the container's init, still holding its device session and possibly still configuring the device after the call was recorded as timed out; and a helper that still held the child's stdout or stderr after a successful exit kept the call waiting until the timeout, so the success was recorded as `Execution timed out after <N>s`. The child, and the optional `pip install` child, now start in a session of their own, and however the call ends (an exit with any status, death by a signal, the timeout, or an exception in the service between the start and the reap) the service sends SIGKILL to the child's whole process group, never its own, before it reaps the child, which keeps the group id from naming a reused pid; the kill at the child's exit lets a call end when the child does. The exit is seen by a short-lived thread blocked in `os.waitid` with `WNOWAIT`, since the python-build-standalone interpreters uv installs have no `os.pidfd_open`. An honest driver's result, error mapping, and timing are unchanged; output that is not valid in the service's locale encoding still raises `UnicodeDecodeError` out of `execute_driver_method`, as it did under `subprocess.run(text=True)`, now after the group kill and the reap. The design assumes nothing else in the service reaps the child (no code in execution or `herd_common` waits for any child but its own or installs a SIGCHLD handler). Not covered: a helper that leaves the group (it calls setsid or setpgid, as a self-daemonizing process does) survives the call, and if it holds the child's stdout or stderr open the call still waits for it until the timeout; and a second exception raised while the service joins the exit watcher or waits for the killed child leaves that child unreaped and its two output pipes and the exit pipe's read end open. Tests in `services/execution/tests/test_driver_sandbox.py` run real children whose driver starts a grandchild, for an exit 0, an exit 0 with the grandchild holding the pipes, a raised exception, death by SIGKILL, and a timeout of a driver that ignores SIGTERM, and assert by pid and start time that the grandchild is gone and that the child was reaped, with every group kill sent to the child's group before the reap; others cover an exception in the parent after the spawn (child exited and child running), a cancelled awaiting task, the escaping grandchild (it survives, recorded as the limit), the new session, the refusal to signal the service's own group, the child's own kill when the group kill does nothing, the exit watcher joined before the call returns, the exact command and environment Popen receives, the deadline handed to the read, flat descriptor and thread counts over every ending, decoding (non-ASCII text, both newline forms, and an invalid byte) compared with what `subprocess.run(text=True)` makes of the same bytes, and the pip child's byte mode and timeout; every test in the file is bounded at 60 seconds or less (`pytest.mark.timeout`), so a dropped kill fails it instead of hanging. Existing tests that stubbed `subprocess.run` now stub `_run_child`.
- Tests and specification: `PATCH /{id}` is pinned to the order its checks run in today, and RES-PATCH-1, RES-PATCH-2, and RES-PATCH-7 now state it (#1551; no product change). When the visibility lookup answers, the non-admin visibility pre-check runs in the router before `update_reservation` looks the row up, so a non-admin naming an invisible device gets 403 whatever the reservation's owner or status, including an id that does not exist; the answer depends only on the body and the caller's visibility, so it reveals nothing about a reservation the caller cannot read. The owner's 404 comes after it and the status 400 after that, so a non-owner gets the same 404 as for an unknown id whatever the row's status. The pre-check never runs for an admin or a superadmin, or for a body whose `device_ids` is absent or null. The section 9 rows for the PATCH status 400, the 403, and the 404 say the same. In `services/reservations/tests/test_reservations.py`, one test runs 78 cases: three roles, a `device_ids` body naming only a visible device or also an invisible one, and a target that is an unknown id or a row owned by the caller or by another user in each of the six statuses. It asserts the status, the detail, that the visibility lookup was awaited once with the caller's id and token for a non-admin and never for an admin, and that a refusal reaches no device fetch and leaves the stored device set unchanged. It fails with the pre-check moved after `update_reservation` (all 13 invisible-body non-admin cases, and the 11 visible-body non-admin refusals, which then skip the lookup) and with the pre-check run for every role (all 52 admin and superadmin cases). The existing test of a PATCH without `device_ids` gains a body that sends `device_ids` as null with a purpose: 200, the purpose written, the device set unchanged, and no lookup, for every role and both editable statuses; it fails for both non-admin cases when the pre-check is keyed on the field being sent instead of on its value. The PATCH tests' visibility lookup stand-in is now autospecced, so a call passing the caller's id and token by keyword still matches the assertion.
- Docs: `docs/specs/CONVENTIONS.md` names the fork GET that forwards the caller's bearer (#1558). The sentence said "the fork GET ONLY"; it now says `GET /{id}/fork` and its two cabling reads, and lists the calls that send the internal token alone (the lazy-create POST, the version GET, the canvas PUT, save, restore, prune, archive). The owner confirmed on 2026-11-25 that the version GET stays without a bearer. No code changed.
- Docs: `docs/ENV_VARS.md` no longer says `docker-compose.yml` leaves ai-orchestrator's `DB_SCHEMA` to the in-code default (#1550). Compose sets `DB_SCHEMA: ai_orchestrator` for it, as for the other ten database-backed services; the sentence was wrong when it was added. No code or setting changed.

- Fixed: `PATCH /{id}` judges and stores a new `end_time` as its UTC instant, whatever offset it is written in (RES-PATCH-3, RES-PATCH-4, RES-PATCH-13); a value without an offset is UTC, and `reservation.updated` carries the end in UTC. A value with no UTC equivalent answers 422 (`end_time must fall between 0001-01-01T00:00:00Z and 9999-12-31T23:59:59.999999Z once converted to UTC`), never a 500. The conflict check for an added device uses the current UTC instant whatever the process time zone (RES-PATCH-9). New unit tests cover nine offset forms, from -23:59 to +23:59, naive included, at each boundary of the window; a new Postgres-live suite, `services/reservations/tests/test_reservation_patch_end_time_live_pg.py`, runs in `_gate-pg-live-tests`.

- Tests: no test reads the clock at import (#1561, no product change). The `backend` check was red on main with `assert 422 == 201` on creates in `services/reservations/tests/test_reservations.py`: the module computed `NOW` at import and booked at `START = NOW.isoformat()`, the create schema refuses a start more than 300 s (`reservation_start_grace_seconds`) before now, and the CI runner now takes about 365 s for the suite, so the tests that ran last were refused. A scratch plugin that moves the clock back while pytest collects found 158 tests in six reservations files that fail when collection is 400 s old and 200 in twelve files at 3 hours 5 minutes; no other service's suite, and not the repo-root `tests/unit`, fails a test under it that it passes without it. The 27 reservations files and `services/auth/tests/test_federated_identity_routes.py` that read the clock at import now bind their constants in an autouse `_clock` fixture when each test starts, and all of them pass under both shifts. `tests/unit/test_no_import_time_clock_in_tests.py` (AST) fails on a clock read that runs at import in any test file under `services/*/tests/` or `tests/`, and `docs/specs/TESTING.md` gains the rule.

- Tests and specification: five rule sentences close the utilization report defects #1547 left passing, and the tests held back for want of them now pin each (#1526, OPS-REPORT-2, OPS-REPORT-7, OPS-REPORT-10; no product change). OPS-REPORT-10 now states the fleet's order (`utilization_pct` rounded to two decimals, highest first, then `name` in code point order, full ties in inventory's order), when the device fetch stops (a page shorter than 500, or the page that brings the devices held to at least a `total` that is a JSON integer, a number written without a fraction or exponent, with a boolean read as 1 or 0; any other `total` is ignored, and an exact multiple of 500 reported in `total` takes no extra request), and the cap (at most 200 pages; a full 200th page short of a JSON integer `total` nulls the fleet as if inventory could not answer). OPS-REPORT-2 now states what the report query loads (rows that end after the window's start and start before its end, in a status the user, device, or fleet section counts; a zero-length row inside the window is loaded and skipped), and OPS-REPORT-7 what a malformed 200 answer from cabling counts (an entry whose reservation id is not a UUID string is ignored, a device id string that is not a UUID is ignored without costing the entry's other ids, and a null device list adds nothing). Eight test functions, 26 cases, are new. In `services/reservations/tests/test_fleet_report.py`, `_build_fleet_section` lists seven devices inventory gives in the wrong order as `B`, `a`, `d10`, `d9`, `zz` (all 25 percent), then 12.5, then idle, and lists `bc` (50.01 percent) first and `ba` before `bb` although `bb` is 50.0042 percent before rounding, with three idle devices all named `twin` kept in inventory's order, which is neither their ascending nor their descending id order; a fleet of 500 or 1,000 devices reported in `total` is read in one or two requests although the next would answer 503; with 1,000 devices in inventory, a `total` of 300, 500, `true`, or `false` ends the fetch after the first page, 501 after the second, and the string `"500"` or the number `500.0` only at the empty third page; after 199 full pages, a full 200th page with no `total`, a `total` of 100,001, or the string `"100000"` returns None, and one that reaches a `total` of 100,000, or a 200th page of 499 devices, returns every device, in exactly 200 requests each time; and through both routes an inventory that always answers a full page with no `total` gives a JSON report with `fleet` null and a fleet CSV answering 503 `Inventory service is unreachable`, after 200 requests (a 201st raises, so a fetch with no stop fails fast). In `services/reservations/tests/test_reporting_edges.py`, an ORM `load` listener shows the query loading rows inside the window, of zero length inside it, across either bound, and across the whole window, and not loading a row ending exactly at the start, starting exactly at the end, or far outside on either side; with one row per status in the window, it loads only `COMPLETED` for a `COMPLETED` list with or without a fleet status list when no inventory list is given, `ACTIVE` and `COMPLETED` once an inventory list is given with that fleet list, `ACTIVE` and `CANCELLED` for a `CANCELLED` list with an `ACTIVE` fleet list, the user section's statuses when the fleet list is left out, and every status when the fleet list is empty or no list is given. Each of the five defects #1547 left passing for want of a rule now fails one of these tests: the fleet sort dropped, the load query's window bounds dropped (both, the lower, the upper), and the fetch's `total` check made strict; so do ties ordered by name descending, a case-blind, length-first, or natural name order, the sort on unrounded hours or on the percentage rounded to one or three decimals, full ties broken by device id in either direction, either load bound made inclusive, the status `IN` clause dropped, the fleet statuses added to the query with no fleet section, the query loading only the fleet statuses, the all-status branch for an empty fleet list dropped or its `or` made `and`, the `total` check made an equality, reading a string or a fractional `total`, or ignoring a boolean one, the 100,000 stop moved one page either way or removed, returning the devices held at the cap, and the cap checked before `total`. The cabling call's `except httpx.HTTPError` narrowed to `httpx.TransportError` still passes the whole suite: it differs only for an httpx error that is not a transport error (a `DecodingError` on a 200 body, for one), and OPS-REPORT-8 names only a transport error or a non-200.
- Tests: the reservations calendar is pinned to its half-open window and to a visibility filter judged per row and for the caller, and `PATCH /{id}`, `PATCH /{id}/purpose-category`, and the provision-result callback are pinned to the rows and branches their rules name (#1533, RES-CAL-1, RES-CAL-3, RES-PATCH-7, RES-PATCH-8, RES-PURPOSE-2, RES-DYN-7; no product change). The nine one-line defects the 2026-11-24 mutation run listed in the issue fail these tests now: the calendar query's start bound dropped or made inclusive, its visibility NOT EXISTS uncorrelated from the outer reservation, and its `ORDER BY` dropped; the strict visibility lookup's non-200 refusal removed; the PATCH pre-check's `and` swapped for `or`; the PATCH's `TopologyDeviceNotMember` 400 removed; `set_purpose_category` without its `WHERE id`; and the callback's dedupe set no longer growing. In `services/reservations/tests/test_reservation_service_unit.py`, one test seeds twelve rows around a fixed UTC window (ending exactly at `range_start`, starting exactly at `range_end`, one microsecond inside each bound, straddling each bound, inside, containing the window, entirely before, entirely after), owned by two users, with every status among the rows that overlap, and asserts exactly the overlapping ids, order aside, with no status filter and with every status named; one keeps DEVICE_A and DEVICE_B visible and asserts that exactly the rows whose devices are all visible come back, a row with several devices and one invisible one and a row on the invisible device alone both left out without hiding the others (a reservation with no devices is left out of the test, since no decision covers it); one inserts rows latest start first and asserts ascending `start_time` under five filter shapes, of which on SQLite only the two with a status filter discriminate (Postgres plan order is not exercised). In `services/reservations/tests/test_reservations.py`, the calendar's visibility lookup answers per user through a stand-in `httpx.AsyncClient`, and for the `user` role the test asserts the rows the caller's answer allows and the one GET's URL, `user_id`, and bearer, with no internal token; for `admin` and `superadmin` it asserts every row and no call. `_CALENDAR_LOOKUP_FAILURES` gains a read timeout and ten non-200 answers (201, 204, 302, 304, 400, 404, 429, 500, 502, 503) whose body has the accepted shape, run by both its fail-closed test and its admin test. Over the three roles and both editable statuses, a PATCH of only `end_time` or only `purpose` answers 200 and never runs the visibility pre-check, and a `device_ids` PATCH runs it once for a non-admin with the caller's id and token (200 only when every requested device is visible; 403 with the pinned wording, no device fetch, and the device set unchanged when the added device, the already-held device, or every device is not visible) and never for an admin or superadmin (200). On both editable statuses, a refusal from the membership check (`TopologyDeviceNotMember`) answers 400 `{"error": "topology_device_not_member", "device_ids": [...]}` with the device set unchanged. In `services/reservations/tests/test_purpose_category.py`, the new write tests seed a second reservation of the same owner and status and run for every status, for the owner, an admin, and a superadmin, setting and clearing: the named row changes as RES-PURPOSE-2 and RES-PURPOSE-3 say and every column of the second row is unchanged; a third user's 403 writes neither row in any status; and an unknown id answers 404 for each caller with one or two rows present and writes none. In `services/reservations/tests/test_dynamic_requests.py`, a success callback whose `device_ids` repeat a new id and the already-booked device stores each id once and puts each once in the `reservation.created` payload and in the fork's member list. No rule states the calendar's `start_time` order, so the order test is not cited in a Pinned by list.

- Tests: the utilization report's status defaults, transit read, handling of transit answers that are not UUIDs, fleet paging, and zero-overlap skip are pinned, with every cabling and inventory request the new tests make answered by respx under the real httpx client (#1526, OPS-REPORT-2, OPS-REPORT-3, OPS-REPORT-7, OPS-REPORT-8, OPS-REPORT-9, OPS-REPORT-10; no product change). The issue lists fifteen one-line defects that passed the whole reservations unit suite. Nine now fail a new test: the CSV route's `status` default inverted for the user, device, and purpose sections or for the fleet section (its tests seeded only `COMPLETED` rows and never passed `status`); `_fetch_transit_devices` dropping every transit device from a 200 answer, four ways; `_cabling_fork_devices_batch` returning `None` or swallowing a transport error (no earlier unit test ran it); and `fetch_fleet_devices` stopping only when a short page and inventory's `total` agree. Five waited on rule sentences, and fail the tests the entry above lands with those sentences; one no longer applies: the day split's `hours > 0` check, which the #1523 fix removed when it rewrote `_split_hours_per_day`. The new `services/reservations/tests/test_utilization_report_routes.py` runs both report routes with respx answering inventory, auth, execution, and cabling under httpx (the database and caller overrides stay, and the status tests keep the directory's stub of `_fetch_transit_devices`). With one row in the window per status, each with its own owner, device, and power-of-two hours, both routes count only the `COMPLETED` row in the user, device, and purpose sections and the `ACTIVE` and `COMPLETED` rows in the fleet section when no `status` is given, and exactly the given statuses in each of those sections for each single status, a pair, and every status; on the JSON route the totals and the topology type, day, and group sections agree. The transit lookup sends one POST to cabling's batch path with the internal token, no `Authorization` header, a 10 second timeout, and only the counted rows' ids (an `ACTIVE` and a `CANCELLED` row in the window and a row after it are left out, though cabling lists a fork for each); fork devices a row did not reserve count as transit for the row they are listed under, and a counted row with no fork in the answer has none. A 200 answer in which one counted row's list holds strings that are not UUIDs before, between, and after its two switches, a key that is not a UUID comes before a second counted row's entry, and a third counted row's list is null counts exactly the valid switches of the first two rows and nothing from that key; no wrongly typed value (a number or a boolean as an id, a number as the list, `devices` as a list, a list as the body) is tried, since no rule says what one becomes. A 201, 204, 302, 400, 404, 499, 500, 503, or 599 from cabling, or a `ConnectError`, `ReadTimeout`, or `RemoteProtocolError`, is 503 `{"error": "transit_gear_unavailable"}` on both routes, a `ConnectError` is that 503 on each of the seven CSV sections, and `include_transit=false` answers 200 with no cabling request while cabling is down. In `test_transit_gear_rollup.py`, 501 rows are read in one chunk of 500 and one of 1 and the transit devices of both chunks are counted, and a 201, 404, or 500 or one of the three transport errors from either chunk raises `TransitGearUnavailable`; the order of the chunk requests, and whether the other chunk is asked once one has failed, are not pinned. In `test_fleet_report.py`, the inventory fetch lists every device in inventory's order for fleets of 0, 1, 499, 500, 501, 1,000, and 1,001 devices and for 700 devices with a `total` of 1,200, absent, a string, or null, each request a GET of `/devices` with `skip` in steps of 500, `limit=500`, the caller's token, a 10 second timeout, and no `X-Internal-Token` header; an inventory that answers a full page to every request and reports no total gets exactly 200 requests. The 10 second timeouts of the cabling and inventory requests and the absence of `X-Internal-Token` on the inventory request are pinned as built: the spec's upstream table lists the timeouts, and no OPS-REPORT rule states any of the three. In `test_reporting_edges.py`, a row ending at the window's start, starting at its end, of zero length inside it, or far outside it on either side shows in none of the totals or the user, device, topology type, day, and purpose sections beside a counted row. OPS-REPORT-9 now lists the four existing 503 route tests in `test_reservations.py` and two new ones, and is no longer under "Rules with no test"; OPS-REPORT-3 names `get_utilization_report_csv` as an enforcer. Each of the nine defects fails a new test, as do the fleet default narrowed or widened, either route's given `status` ignored by the legacy or the fleet sections, the query loading only the legacy statuses, the cabling status check moved to 300 or 400, the transit path's `except` clauses narrowed (the httpx one to `ConnectError` or `TimeoutException`, the `RuntimeError` one to `ValueError`), the batch call's method, path, body key, or timeout changed or the caller's bearer added, the transit map reset per chunk or read from the first chunk only, a malformed device id or reservation key ending its list or map (`break`) or raising instead of being skipped, a null device list not read as empty, `include_transit` ignored on either route, the 503 mapping removed or changed on either route, the fleet page size, step, short-page check, token, or timeout changed, the 100,000 stop moved by one page or removed, and the zero-overlap skip narrowed to negative hours. These one-line defects still pass the whole reservations suite, each because no rule states the behavior it changes or because no route can reach the difference: the httpx `except` on the cabling call narrowed to `httpx.TransportError` (no rule says what an httpx error outside that class becomes); the fleet section's own status check dropped (the routes always pass a legacy status set inside the fleet set, so the query already decides it); and the cabling status check changed from `!= 200` to `> 200` (it differs only on a final 1xx answer, which HTTP never delivers).

- Tests: the reservations service is tested as it is deployed: the `PURPOSE_CATEGORIES` comma string, the schema its tables sit in, its startup and shutdown, and its logging, access log, and CORS wiring (#1527, no product change; the Pinned by lists of RES-PURPOSE-1, OPS-LOG-1, OPS-LOG-7, OPS-LOG-8, and OPS-LOG-9 updated, and OPS-LOG-9 leaves the rules with no test). The 2026-11-24 mutation run found these one-line defects passing the reservations unit suite, which runs with the list default, `DB_SCHEMA=''`, and loop fakes that return at once: the comma string's empty-item filter inverted (the compose default parsed to an empty list) or the validator returning None; `outbox` or `fork_wiring_ledger` declared without its schema under the deployed `DB_SCHEMA`; any one of the three shutdown cancels removed; and `setup_logging`, the CORS middleware, or the request logger removed (the last two were already caught by the middleware list in `tests/unit/test_request_body_limit_wiring.py`). `services/reservations/tests/test_service_wiring_as_deployed.py` builds `Settings` from the string compose passes and checks the list a category must be in, and imports `app.main` in a subprocess with `LOG_LEVEL` and `CORS_ORIGINS` (the compose default origin plus one more) in its environment, after uvicorn's own logging setup, to check each request's one `herd.access` JSON line, what each level lets through, and a CORS preflight from a listed and an unlisted origin. In `test_main_task_lifecycle.py` a new shutdown test holds each of the three tasks in turn mid-cancellation and bounds shutdown by event-loop iterations, not a clock, so a task left uncancelled or unawaited fails at once; a new startup test checks that `create_all_and_stamp` is awaited once with the service's engine, metadata, and `DB_SCHEMA` setting; the real expiration and purpose-classify loops are shown to end when cancelled in a step or in their sleep, and a loop that survives the cancel ends at its next tick, where a fake step called again raises, so that test fails at once instead of waiting on a running loop; the four earlier lifespan tests' loop fakes now return at once, so a missing cancel no longer stalls them; and the comment that said the existing `wait_for` bound catches a shutdown hang is corrected (it cannot: the lifespan swallows the cancellation `wait_for` sends). A new repo-root guard, `tests/unit/test_schema_placement_and_logging_wiring.py`, imports each service's `app.main` with `DB_SCHEMA` as `docker-compose.yml` passes it and fails when a model table, a foreign key target, or a named native enum type is outside that schema, or the schema is not one `infra/postgres/init.sql` creates; when a service other than config does not install the JSON log handler at `LOG_LEVEL` at import; and when config uses that formatter, adds a handler to the root logger or to one of its own `app` loggers, or would emit an INFO record from one of them. A service with no database is named in its `WITHOUT_DATABASE` table and a database service with no table yet in `NO_TABLES_YET`, each with the reason; every failure names the service and what to do, and what the guard cannot see is listed in its module docstring.

- Tests: purpose classification is pinned to the row each step names and to the outcome of each class of orchestrator answer but one (#1525; RES-PURPOSE-5, RES-PURPOSE-6, RES-PURPOSE-7, RES-PURPOSE-8, RES-PURPOSE-10, RES-PURPOSE-11, RES-PURPOSE-13, RES-PURPOSE-14, and the new RES-PURPOSE-15, which states the review list's order; no product change). Eleven one-line defects from the 2026-11-24 mutation run passed the whole reservations unit suite: the sweep's "no suggestion yet" filter dropped (the one suggested row seeded was skipped by the classifier's own early return as well); accept and dismiss loading every reservation instead of the named one (every accept and dismiss test seeded at most one row); the `topology_id` condition of the classify payload inverted, so a row without a topology sent `"None"` (no test seeded a topology or read the key); an unparseable 200 counting no attempt or returning None, a transport error returning None, the already-resolved early return with `or` turned to `and` or returning None, and the 403 body helper returning None for a body that is not JSON (no test sent those answers, or reached that return, and read the outcome); and the review list's `ORDER BY` dropped (its rows were inserted in the order it produces). The new `services/reservations/tests/_purpose_classify_answers.py` lists, with its outcome word, one or more answers of each class it covers: a 200 whose body is a JSON object (a suggestion); four unparseable 200 bodies (not JSON, empty, HTML, invalid UTF-8); the 403 marker and its legacy string; seven 403 answers without the marker (a string detail, a dict detail, the marker outside `detail`, a list, HTML, empty, invalid UTF-8); 404 with JSON and with HTML; 429, 502, 503, and 504; every other `http.HTTPStatus` member, each with a well-formed suggestion body; every timeout class httpx defines; and every other transport error class httpx defines plus one of the test's own. It leaves out a 200 whose body parses as JSON but is not an object (a list, a string, a number, `null`), which the product mishandles (#1543, filed separately; RES-PURPOSE-8 and RES-PURPOSE-13 now carry a known-gap note and section 13 lists it under Open defects). The legacy string, the marker outside `detail`, and the list are read as the code reads them; RES-PURPOSE-6 does not say where the marker sits. Three tests run over the whole table. In `test_purpose_classify_reconcile.py`, `test_classify_one_maps_each_answer_to_its_outcome` asserts the word, one call for the named row, and every mapped column of that row against its value before (only the suggestion and its time, or the attempt count by one, may change, and `updated_at` only then) and of a second eligible row; `test_reconcile_ends_or_continues_the_tick_by_answer` gives the older of two eligible rows the answer and the newer a suggestion, and an answer that ends the tick leaves one call made and both rows unchanged, while any other is applied to the older row and the newer row is classified in the same tick. In `test_purpose_classify_trigger.py`, `test_trigger_answers_the_outcome_word_for_every_answer` expects 503 for feature-off and otherwise 200 with exactly the id, the word, and the stored suggestion, the attempt count moved by one where an attempt counts, and a second eligible row unchanged. Also new in `test_purpose_classify_reconcile.py`: `test_reconcile_batch_is_filled_only_by_eligible_rows` puts two rows that each fail one eligibility condition (a suggestion, the attempt cap, or no stamp) ahead of one eligible row in a batch of two; `test_reconcile_sends_each_rows_own_payload` compares the whole call for a row with a topology and one without: the base URL, method, path, timeout, and internal token that section 10's dependency table names, bound by `call_service`'s own signature, and the body as built (device ids as a set, since the model keeps no order); `classify_purpose_one` on a row that already has a suggestion, and on an unknown id, is "ok" with no call and nothing changed; and an exception from the call that is not an httpx error still returns an outcome word. `test_reconcile_transport_error_never_raises_and_is_transient` is now `test_reconcile_transport_error_ends_tick_without_bump`, with two rows, and asserts the word. In `test_purpose_review.py`, accept and dismiss run beside a second suggested row with the named row in each insertion position and compare every mapped column of both rows with its value before, other than the columns the step writes and `updated_at` on the named row; an unknown id is tried beside two suggested rows and a row without a suggestion beside one; the suggestion fixture's first distribution entry is no longer its `top_category`, so accept reading the distribution shows; `test_every_purpose_review_route_refuses_a_non_admin` sends a non-admin to every purpose review route, read from the app with `services/reservations/tests/_route_table.py` (every route whose first path segment is `admin` and whose second is `purpose-review` or `purpose`; a probe on a small app shows the shapes and mounts the read leaves out, and another test checks the read finds the five routes section 5 lists), and expects 403 with nothing written and no orchestrator call; and `test_review_list_is_newest_suggestion_first_then_by_id` inserts five rows out of order, two of them with the same time inserted larger id first, and pages through them by two. Each of the eleven defects fails at least one new test, as do 32 of 34 more tried by hand, among them the sweep's other two filters, three changes to the tick-ending set, each `except` in the classifier narrowed, the transient set, any 2xx read as a success, the payload's other fields, accept and dismiss reading the first row, and the review order's direction and tie-break (the other two, the sweep's batch bound one up and one down, fail only the existing `test_reconcile_respects_batch_size`), and so do five changes to the classify call (timeout, token, path, base URL, method), accept reading the first distribution entry, and the admin check removed from each of the five routes; four rewrites that keep the behavior (the sweep's filter as `== None` and as an `IN` subquery, accept and dismiss by primary key) still pass. The review order is proven on SQLite only.

- Tests: the reservations service's fork and wiring routes are pinned to their gate and upstream rules with the transport helpers running unmocked (#1532, RES-FORK-4, RES-FORK-5, RES-FORK-10, RES-FORK-18; no product change). These one-line defects passed the whole reservations unit suite: the lazy fork create's parent topology condition inverted (a reservation with a topology sent null, one without sent the string `"None"`), its error check inverted, moved past 400, or removed; the admin fallback load without its `WHERE id` (no admin test of these routes seeded a second reservation); the owner 404 removed from the canvas PUT and the save; `_execution_wiring_call` returning `None` or swallowing a transport error; and a cabling or execution 400 relayed as a 200 (no test sent a 400). Every earlier router test that reached the upstream call of these routes patched `_cabling_fork_call`, `_execution_wiring_call`, or `_lazy_create_reservation_fork`. The new tests patch nothing between the router and the network: respx answers under httpx (the dependency overrides for the database, the caller, and the bearer stay, as does the directory's autouse patch of `_fetch_transit_devices`, which these routes do not call). The new `services/reservations/tests/test_fork_and_wiring_routes.py` reads the routes from the app, every route whose first path segment is a path parameter and whose second is `fork`, `wiring`, or `wiring-status`, for each method it declares except HEAD and OPTIONS (other path shapes and mounted sub-applications are not read; the Limits are in its module docstring, and a probe app shows each selection limit). On each route, with the target in each status, a caller who is neither owner nor admin, who owns an `ACTIVE` reservation of their own, gets 404 `Reservation not found` and no request leaves the service. On each route asked about an `ACTIVE` reservation: the owner, an admin, and a superadmin, with another user's `PENDING` reservation inserted first, get the upstream's body, and the one upstream request goes to the expected cabling or execution path for the reservation named, carries the internal token, sends the expected body (the save's `member_device_ids` are the target's devices), and carries the caller's bearer as `Authorization` on the fork GET only and no `Authorization` header on the other six routes (`docs/specs/CONVENTIONS.md`); an upstream 400, 403, 409, 422, or 499 is relayed with its status and unwrapped detail; a 500, 502, 503, or 599 is 503, with cabling's JSON detail on a cabling route, while on a wiring route execution's detail text appears nowhere in the answer (RES-FORK-18 names no detail, and the replacement wording is not pinned); a cabling 5xx without a JSON body is 503 `Cabling service is unavailable` after one upstream request; and a `ConnectError`, `ReadTimeout`, or `RemoteProtocolError` is 503 with a detail starting `Failed to contact cabling service:` or `Failed to contact execution service:`. Those tests assume a route makes exactly one call, to cabling or execution, at the path its entry in the test's `UPSTREAM` table names: a route in the family missing from the table fails with a message naming it, a route that breaks an assumption goes in `OWN_TESTS` with the reason and keeps only the non-owner test, and an entry for a route the app no longer has fails with a message to remove it. In `services/reservations/tests/test_fork_endpoints.py`, the new lazy-create tests read the fork of an `ACTIVE` reservation that cabling answers 404 for. One of them runs as the owner and as an admin, with another owner's `ACTIVE` reservation with its own topology inserted first: the create posts the reservation's id, its parent topology (JSON null when it has none), a null parent version, its owner as `created_by`, and its devices, with no `Authorization` header while both fork reads carry the caller's bearer, and a 200 or 201 is followed by the re-read. The others run as the owner with one reservation: a create answered 400, 404, 409, 422, 499, 500, or 503 answers 503 with a detail starting `Cabling fork-create returned <status>:`; a `ConnectError`, `ReadTimeout`, or `RemoteProtocolError` on the create answers 503 with one starting `Failed to contact cabling service:`; and a fork miss in each status other than `ACTIVE` answers 404 `Fork not found` with no create. The bodies of the route-table helpers that #1529 wrote in `test_reservations.py` moved to `services/reservations/tests/_route_table.py`, shared by both files; `_internal_routes` and `_otherwise_valid_request` there now call them. Each of those defects fails a new test, as do the owner 404 removed from any of the seven routes, the gate moved after the upstream call on the version read, a status check moved ahead of the owner gate on the canvas PUT or the retry, the admin fallback offered to every role, narrowed to `admin`, or loading another row, the owner load without its user or its id filter, either transport `except` narrowed (cabling's to `NetworkError`, execution's to `ConnectError`), the 5xx boundaries moved, execution's detail relayed on a 5xx, the lazy create or the execution call sending the caller's bearer, and the lazy create made on a 200 or on a `PENDING` reservation. An `except` narrowed to `httpx.TransportError` still passes: no rule says what an httpx error outside `TransportError` becomes.

- Tests: the expiration sweep's restart backstop, its loop steps, and pending prune-marker entries that do not parse as a UUID gain tests that fail under the six surviving mutants the reservations mutation run listed and under the further surviving mutants its review found (#1531, no product change; RES-SWEEP-7, RES-SWEEP-10, and RES-FORK-17 updated). In `test_restart_backstop_release.py`, one test seeds a stranded physical-only row beside another reservation that carries a dynamic request (`ACTIVE`, `PENDING_PROVISION` inside the deadline, or stranded with its own request) and asserts both rows, so an inverted or uncorrelated NOT EXISTS fails; a second adds a dynamic row with a stale provision clock on the sweep's own session between the two backstops' SELECTs, standing in for a commit that lands there, and asserts the restart backstop leaves it in `PENDING_PROVISION` with no device write, so a deleted NOT EXISTS fails. In `test_coverage_gaps.py`, a guard reads the awaits in `expiration_loop`'s `while` body, an awaited call keyed by its dotted name built from the AST and each `async with` item and `async for` keyed as such, including those in the decorators, defaults, annotations, and class bases and keywords of a nested function or class, and requires exactly the four steps and `asyncio.sleep`, in any order; it does not see a call that is not awaited, an await outside the loop or in the body of a nested function, lambda, or class, a step passed un-awaited to an awaited wrapper, a rebound name, or whether a step is reachable or inside a `try`, and fixture cases pin both what it sees and those limits. A runtime test runs one tick with no step, each step, and every step raising a private exception, and asserts each step ran exactly once in any order, one error line per raising step carrying that exception, and one sleep; a step called a second time raises an exception the loop does not catch, so a loop without its sleep fails at once, and a 60-second timeout marker backs it and the purpose-classify loop test. The two tests that patched only the first two steps are gone. In `test_pending_fork_prune.py`, one row holds entries that do not parse as a UUID beside valid ones and another row holds valid ones; the test asserts the first tick clears the unparseable entries with no prune for them and keeps every valid entry, and both markers end SQL NULL once a prune converges.

- Tests: the reservations service's internal routes are pinned to their rules (#1529, RES-INTERNAL-1, RES-INTERNAL-3, RES-INTERNAL-4, RES-INTERNAL-5, RES-INTERNAL-8; no product change). These one-line defects passed the whole reservations unit suite: the `reservation_devices` EXISTS of `owns_active`, `active-users`, and `by-device` uncorrelated from the outer reservation (no earlier test seeded a reservation that passed the lookup's other filters while a different reservation held the queried device); `PENDING_PROVISION` dropped from `held-devices` (the one `PENDING_PROVISION` row's device was also on an `ACTIVE` row); the `ACTIVE` filter of `owns_active` widened to `PENDING_PROVISION`, `PENDING`, or `COMPLETED`, and that of `active-users` to `PENDING_PROVISION` or `PENDING`; the end of the window dropped in both (the only window tested as a negative had not started); and the token header made optional on `active-users`, `held-devices`, `by-topology`, and `by-device`, where a missing header then answered 403 instead of 422. In `services/reservations/tests/test_reservations.py`, the new correlation tests seed two reservations in window, `ACTIVE` unless the `by-device` test sets another status, one user's on one device and another user's on another, and query each device: `owns_active` for each user and device pair, `active-users` exactly the holder, and `by-device` exactly the holder's reservation in every status. The new status and window tests try, for `owns_active` and `active-users`, one user's reservation on the queried device in every status and in a current, an ended, and a not yet started window, beside another user's `ACTIVE` reservation in window on the same device; each bound is missed by a minute, never at the boundary instant. The new route tests seed nothing: every route with a path segment `internal`, read from the app for each method it declares except HEAD and OPTIONS (mounted sub-applications are not read; the Limits are in the tests' block comment), must answer 422 with one `missing` error at `["header", "x-internal-token"]` when the header is absent and 403 `Invalid internal token` to a wrong token. In `test_dynamic_gating.py`, `test_held_devices_lists_a_row_by_its_own_status_alone` gives the user's row, in each status, a device no other row holds, beside another user's row in the same status. `_insert_reservation_row` now takes `user_id` and `device_ids`. Each of those defects fails a new test, as do the inverted correlations in all three lookups, the start of the window dropped in `owns_active` and `active-users`, and the token check removed on any internal route.

- Tests: a cancel that loses its compare-and-swap is pinned against every winner status, not only `COMPLETED` (#1530, RES-CANCEL-4). `test_cancel_loses_to_a_concurrent_completion_is_a_noop` in `services/reservations/tests/test_reservation_status_cas.py` is now `test_cancel_loses_to_any_terminal_winner_is_a_noop`, run for each terminal winner (`COMPLETED`, `CANCELLED`, `FAILED`) from each live status: every column of the row stays as the winner wrote it, no second compare-and-swap runs, no event is staged, and neither inventory nor the fork archive is called. The new `test_cancel_loses_to_a_non_terminal_winner_retries_and_cancels` pins the complement: a winner that wrote a live status is retried from that status and cancelled with exactly one `herd.reservations.cancelled`. The test states the terminal set itself and takes every other enum member as live, so a status added later is tested as live, and one the product treats as terminal fails these tests until the test lists it too. `test_cancel_cas_exhaustion_warns_with_fixed_action_and_stages_nothing` now also pins the cancel at three compare-and-swap attempts in all; two and fifty passed the whole suite before. Two mutants of `_TERMINAL_STATUSES` from the 2026-11-24 mutation run (`CANCELLED` replaced by `FAILED`, `FAILED` replaced by `PENDING`) passed the whole unit suite before and fail these tests now.

- Docs: `docs/specs/TESTING.md` gains the rule "seed a row that must not match" under Test effectiveness rules. The 2026-11-24 mutation run on the reservations service (2,935 one-line defects) found that most surviving defects that mattered passed because a test seeded exactly one row, user, device, or reservation, so a query without its filter still gave the right answer (#1525, #1529, #1532, #1533). The rule: a test of anything that selects, filters, authorizes, or writes by a key seeds a matching row and a non-matching one and asserts on both, a negative test seeds the thing under a different owner or key, and the proof is that the test fails with the filter clause removed.

- Fixed: the utilization report's day section is split at UTC midnights and labeled with UTC dates whatever offset `start` and `end` carry, so its hours sum to `total_hours` (#1523, OPS-REPORT-4, OPS-REPORT-13). Since the first public release (v0.1.0) `_split_hours_per_day` read the local date fields of an offset-aware bound, and `GET /reports/utilization` and `GET /reports/utilization.csv` passed the bounds through as parsed: `start=2026-03-31T20:00-08:00` (04:00Z) with `end=2026-04-01T12:00Z` reported `total_hours` 8.0 and 12.0 hours on 2026-04-01, and a positive offset put hours under the wrong date. Both routes now convert the bounds to UTC (a bound without an offset is UTC, never the host's zone; a naive `start` with an offset `end` answered 500 before), the helper and `build_utilization_report` convert on their own too, the JSON echoes `window_start` and `window_end` in UTC, and the CSV file name carries the UTC dates. API callers that send an offset other than UTC were affected; the web app sends UTC (`toISOString()` in `frontend/src/pages/ReportingPage.tsx`) and was not. A bound whose instant has no UTC equivalent (an offset near year 1 or year 9999, such as `start=0001-01-01T00:00:00+05:00`) answers 422 `<name> must fall between 0001-01-01T00:00:00Z and 9999-12-31T23:59:59.999999Z once converted to UTC` (OPS-REPORT-1), never a 500, on both report routes and on the `GET /` list's four time bounds, which share the router's conversion (RES-LIST-8). The day split no longer overflows on a slice that starts on 9999-12-31, and it labels a day before year 1000 with four digits (`0001-01-01`, where Python 3.12's `strftime` wrote `1-01-01`).

- Fixed: the expiration sweep retries a failed fork prune again once more than 20 reservations have ever had their pending prune marker cleared (#1522, new RES-FORK-20; RES-FORK-17 updated). Since v0.2.0 (migration 0014, #462) the marker column `reservations.pending_fork_prune_device_ids` was plain `JSON`, so every clear stored the JSON literal `null`, not SQL NULL, and the pending-prune reconciler, which selects `IS NOT NULL` rows oldest `updated_at` first, 20 per tick, kept selecting every cleared row forever. Nothing in the sweep rewrites those rows, so they keep older `updated_at` values than a newly marked row (a later status, purpose, or category write does move a row's `updated_at`), and once 20 existed they filled every batch: a prune that failed on the device-set PATCH was never retried, leaving the removed device's fork wiring and port claims held (the case the marker exists to close), and each tick re-read the 20 dead rows. The column now stores SQL NULL when nothing is pending (`JSON(none_as_null=True)`); migration 0018 (`make migrate-reservations`) rewrites the stored empty forms, JSON `null` and an empty list, to SQL NULL, matching them with `json_typeof` and `json_array_length` so no row is converted to jsonb (a `json` value such as `["\u0000"]` that jsonb refuses would otherwise abort the upgrade); its downgrade is a no-op. The reconciler also heals, in the same tick and under the row lock, any marker it selects that holds nothing actionable (JSON `null` or an empty list written by a process still on the previous code, or a corrupt non-list value, logged at ERROR by type name), and leaves it if a concurrent device-set PATCH made it a real list meanwhile; a corrupt value such as `true` no longer raises `TypeError` and stops the rest of the pending-prune step every tick. The earlier tests read the marker back and compared it with `None`, which a JSON `null` also loads as; the new tests read `IS NULL` in the database after every path that empties the marker, run the reconciler behind more than a batch of emptied rows, heal every non-actionable shape in both statuses, pin the batch's size and order, and prove the column, the heal, and the migration on Postgres in a new live suite, `services/reservations/tests/test_pending_fork_prune_live_pg.py`, run by the gates' `_gate-pg-live-tests` phase.

- Tests: a repo-root guard refuses tracked generated files (#1520). The ignore file is untracked by decision, so a clone has no ignore rules, and the external PR #1503 committed 154 compiled `__pycache__` files that no check refused. `tests/unit/test_tracked_generated_files.py` fails when any path `git ls-files` lists has a component named exactly `__pycache__`, `.pytest_cache`, `.ruff_cache`, `.mypy_cache`, `node_modules`, `.venv`, `.env`, or `htmlcov` (a `.env` file and a virtualenv named `.env` both fail), has a component ending in `.egg-info`, or has a file name ending in `.pyc`, `.pyo`, or `.pyd`, named `.coverage` or `.coverage.<suffix>`, or named `.DS_Store`, at any depth. It matches whole components and exact names, case-sensitively (`.env.example`, `.env.local`, and `.coveragerc` pass), and does not refuse `dist` or `build`. It fails, never skips, when git is missing, the repository root is not the top of a git work tree, or the listing is empty, and its message lists the first 50 paths with the total and the remedy (`git rm -r --cached`, amend and `git push --force-with-lease` if already pushed, then `.git/info/exclude`). `docs/specs/DEVELOPMENT.md` now tells a contributor working from a clone to put such patterns in `.git/info/exclude`.
- Fixed: the import routes cap the file itself at `IMPORT_UPLOAD_BYTES` and refuse it one byte past, on inventory and cabling (#1417; new INV-BULK-23 and TOPO-BULK-20). Before this change a file could be up to `MAX_REQUEST_BODY_BYTES` larger than `IMPORT_UPLOAD_BYTES`, less the form overhead: the route's own `extends_body_limit` bound covers the whole multipart envelope. The per-file cap does not grow when an operator raises `MAX_REQUEST_BODY_BYTES`, and at most that much is copied out of the spool. Starlette has already received and spooled the upload by the time a route reads it, so this bounds what is copied into memory, not the upload itself, and a file up to 10 MiB is still decoded and parsed on the event loop. Step 2 of the issue (moving the parse off the event loop) is left out.

- Fixed: the TTL cache shares a failed in-flight fetch with the callers queued on it (#1397, #1513). Callers queued on a key during an outage now make one upstream attempt instead of N in a row, and one SSO discovery request serves every sign-in queued behind it. The fetch runs as its own task and every caller awaits it through `asyncio.shield`, so one caller's cancellation cannot reach the others, and a done callback reads a failed task's exception so asyncio does not log it as unretrieved. A fetch that raises now raises in every caller queued on it. A consequence worth stating: fetches for different keys now run concurrently where the old lock ran one at a time per cache, which is safe because the keys are independent upstream lookups.

- Tests: the herd_common body scan is held to its frozen budget by counts instead of a CPU-time ratio, so the budget comparison in 11 cost cases no longer reads a clock (#1512, the owner's option (a); OPS-VALID-8's Pinned by list now names the UTF-16 test). The escape-heavy, UTF-16, and no-marker cases in `services/common/tests/test_validation_errors.py` required the scan to cost under 1.5 times `_scan_cost_budget` in CPU time, but the two make the same C calls, so the ratio's true value is 1.00 and the required `backend` check read 1.548 and 1.524 on 2026-11-21 with no change to the scan. The growth bound (8 per fourfold step) is unchanged and still reads `process_time` in the 8 escape-heavy and UTF-16 cases; the memory bounds are unchanged. The cases now profile both sides with `call_profile`, new in `services/common/tests/_cost.py`: a `sys.monitoring` tool id of its own, no clock, no `sys.settrace` or `sys.setprofile`, so coverage (its C tracer before Python 3.14, its own tool id from 3.14) sees every event as before; it counts C calls, regex calls by pattern, Python starts, and Python steps in the profiled function's whole package, records by code object each Python function started outside it, raises instead of returning an empty profile, and leaves the garbage collector as it found it; `services/common/tests/test_cost_profile.py` pins it. `_within_budget` fails when the scan calls a C callable, or starts a Python function of `herd_common`, more times than the budget does, runs any regex pattern more times than the budget runs it (one the budget never runs counts as zero), starts a Python function outside `herd_common` beyond what the budget started outside its own home plus one `json.detect_encoding`, compared by code object and not by name (the budget itself may start only the UTF-16 codec's Python decode wrapper), takes more Python steps than the budget plus `_STEP_ALLOWANCE` (the measured difference: 1 on a UTF-8 body, 2 on the UTF-16 one), or takes more steps at 1 MB than at a quarter of it, or when a profile saw nothing; its message carries both sides' counts. The new `test_the_scans_patterns_are_the_budgets_copies` pins each of the scan's patterns to the budget's copy. `_BUDGET_BOUND` is gone. The counts were the same on 3.12.3, 3.12.12, 3.13.12, 3.14.0, and 3.14.3, plain and under coverage. Mutations of the product scan: extra passes, copies, regex passes, callbacks, and `find` loops fail by their calls, also from a helper moved to another module of the package; Python loops with no call in them (1,000 `in` scans, a counting loop, a loop that stops after 64 KiB) and one more line on the scan's path fail by their steps; a regex rewritten, or a new one, fails by its pattern, and one of the scan's own patterns run in place of another (the raw-byte search swapped for the high-escape search) fails by its count; work moved outside `herd_common` under a name the budget allows (the escape passes in another top-level module, a stand-in `detect_encoding`) and a callback from a C call into code outside it fail by their starts; the early exit removed fails every no-marker body; the helper moved unchanged within `herd_common` and a scan with one count dropped pass. Still not seen: operators added to a line the scan already runs, including one that drives work built before the call (a module-level `map` over 1,000 substrings tested with `in`, which runs 1,000 C searches of the body unseen, or a generator outside `herd_common` started before the call and resumed by `in` or a `for` loop, whose resumes are not counted); more work inside a call the budget makes as often with the same pattern (such as searching `body + body`); inputs none of the bodies exercises; work outside the one call; and the work inside `json.detect_encoding` and the UTF-16 codec's Python decode wrapper, outside both homes, of which only the starts are counted. `docs/specs/TESTING.md`'s cost test rule now says when to compare counts instead of time and what that does and does not catch, and adds a memory-bound neighbor on the SMT sibling that switches on and off to the runs required before a cost test merges.

- Fixed: one provision now configures at most 1,000 distinct static routes on a Layer 3 switch, per reservation pin, so an L3 drive and the retry claim it holds are bounded (#1493, the owner's limit; new TOPO-L3-12 and WIRE-L3-20; WIRE-CLAIM-3, WIRE-CLAIM-10, WIRE-L3-3, and TOPO-FORK-14 updated). Before, only the request body limit bounded the routes one switch could be given, on the order of 100,000 in one save, and since #1482 an L3 retry claim lasts as long as its whole drive, so one save could close a switch's pin to both retry channels for weeks and occupy the consumer. The limit bounds what a drive configures, not what a switch holds: a pin stored before the limit keeps its routes, a FAILED pin's record of possibly installed routes can grow past it, and inventory's config schema and config-apply jobs are not limited. The limit is one constant, `herd_common.l3_route_limit.MAX_ROUTES_PER_SWITCH` (1000), compared through one function by both services; it is not a setting, so cabling's check and execution's cannot disagree. Routes are counted distinct by the shared route identity, so a duplicate counts once, and merged over every canvas node naming the switch. Cabling refuses routing intent over the limit wherever it parses it: a fork save answers 422 `{"error": "l3_intent_malformed", "node_id", "message"}` with the message `switch <device id> has <count> distinct routes; a switch carries at most 1000` before any inventory call, the validate routes (and so reservation create and the topology editor's save) report it as `l3_malformed` with that message, and fork create at activation drops the switch's intent with a warning. Execution refuses, before any driver call, the route sets cabling cannot see: the drive gate checks intent first, and every provision (a reused pin, the switch's configured routes, a retry's stored set) is deduplicated and checked before its residue read, claim, switch read, and driver load; the switch is recorded FAILED with the new fixed reason `l3_too_many_routes` (on a frozen reservation the reconcile path records the teardown park instead, FAILED intended RELEASED with `TEARDOWN_PENDING_REMOVAL`, WIRE-LEDGER-17 and the #1501 entry). Only a retry pass claims a pin (the reconcile's own drives, deltas included, take none), so at the default timeouts a retry provision that removes nothing holds its claim at most 1,530 minutes (about 25.5 hours), and one that removes a residue of 1,000 routes and configures a disjoint 1,000 at most 3,057 minutes. Removals are never limited, since refusing one would leave routes installed; a known limit: the routes a FAILED pin records can grow past 1,000 over repeated failed provisions (each records its own set plus the removals that did not confirm), and a drive removing them is bounded only by their count. Execution now also drives each distinct route of a provision once: a switch configuration listing a route twice configured it twice and pinned both copies. Upgrade note: intent stored before this change with more than 1,000 routes on one switch is not deleted. A switch with such intent that is not already pinned ACTIVE with exactly those routes is recorded FAILED `l3_too_many_routes` with no driver call when execution next provisions it, keeping any routes an existing FAILED row records; every later save of that fork is refused with the 422 until the switch's routes are cut to 1,000 or fewer. A switch already pinned ACTIVE with exactly its intent is not touched (WIRE-L3-6, WIRE-L3-7): its routes stay installed and are removed in full at teardown. A reservation booked before the upgrade from a topology with more than 1,000 routes on one switch, and activated after it, gets its fork through the tolerant parse: the switch's routing intent is dropped with only a server WARNING (`l3_intent_dropped_at_activation`), the fork's canvas still shows the routes although none is installed, a switch that is adjacent through an ordinary hop is provisioned from its latest config version's routes instead (when it lists any), and every fork save answers the 422 until the routes are trimmed. A switch whose latest config version lists more than 1,000 routes is refused at provision as well. When the refused row records no routes (a switch never driven), trimming the config is enough: the next reconcile, a manual retry, or the background retry while the row is under `WIRING_RETRY_MAX_ATTEMPTS` drives it. When the row records routes (a reused pin), a retry re-drives those routes, so trimming the config does not help; new intent under the limit, the switch leaving the fork's wiring, or the teardown settles it. A plain topology save still does not parse routing intent, as before; validating or reserving such a topology reports the switch.

- Fixed: a reservation that ends while a wire is FAILED no longer keeps L1 or L2 wiring that no pass will remove, and a build failure recorded after the freeze no longer leaves a row that no pass drives (part of #1501, the owner's pieces 1 and 2; WIRE-TEARDOWN-2 and WIRE-TEARDOWN-6 rewritten, WIRE-CLAIM-1, WIRE-CLAIM-10, and WIRE-LEDGER-15 updated, new WIRE-LEDGER-17). The terminal teardown released ACTIVE L1 and L2 rows only, so a connect or join that did not confirm (a timed-out driver call may still have landed) stayed FAILED intended ACTIVE forever: the tick leaves a frozen build out and the manual retry reports it `frozen`. The teardown now parks each such row FAILED intended RELEASED, attempts 0 (`park_stale_l1_build` under the new `TEARDOWN_PENDING_DISCONNECT`, `park_stale_l2_build` under the new `TEARDOWN_PENDING_VLAN_REMOVAL`, as it already parked L3 pins since #1001) and removes it with the ACTIVE rows (a disconnect of something never connected is the idempotent no-op the #491 stale settlement relies on); an L2 row whose allocation is nil or gone is released with no driver call, a parked L2 row's allocation joins the emptied-allocation check, and three kinds of row are left as they are: a pinned-reason row (an unresolvable hop, or a switch whose driver package cannot load, #1199), a row whose key also has an ACTIVE row (L1 switch and pair; L2 switch, port, and allocation), and a row another retry pass holds a live claim on (the park runs inside `write_guard` with an empty claim ledger, so a claim taken after the read is respected too; the holder's own record settles it as before; the held row does not itself bring its allocation into the emptied-allocation check, though an ACTIVE row or a parked FAILED row of the reservation on the same allocation still does, and the VLAN is then deleted as before). A failed removal stays release-direction for the retry channels, and a redelivered teardown makes no driver call for a parked row. What an operator sees: when a reservation ends, a switch whose connect or join failed with a retryable reason now gets a driver call from the teardown, a `disconnect_ports` (L1) or a `remove_from_vlan` (L2) run for the failed pair or port, where before it was not contacted; `docs/DRIVERS.md` now states that such a removal of something never built must succeed. Separately, a key-based build failure (`record_l1_failed`, `record_l2_failed`, `record_route_failed` with no row id, and `record_route_reconcile_failed`) from a reconcile that passed its frozen gate before the freeze recorded intended ACTIVE after the teardown's read, or over a row the teardown had parked; on a frozen reservation it now records FAILED intended RELEASED, attempts 0 (L1 and L2 keep the failure's own reason; L3 records the teardown park's retryable `TEARDOWN_PENDING_REMOVAL`, so the tick drives it), decided inside the write's own UPDATE or INSERT (the new `wiring_due.frozen_build_failure_values`, over `reservation_frozen`, which now also takes a bound reservation id for an insert), for exactly the rows the teardown parks; the next tick removes it. A row RELEASED when the write reads its key is never rewritten (one released between that read and the UPDATE keeps status RELEASED but takes the write's reason and attempts 0, and on L3 also the write's routes, with its claim stamp cleared), an ACTIVE row is still immutable to a build failure, and release failures, writes on a reservation that is not frozen, and failure writes by row id are unchanged. Not closed, stated in the specification's section 13: a key-based write whose statement snapshot predates the freeze and that commits after the teardown's read; a frozen release driving the content its pass planned, including the teardown's own deprovision of a pin an intent-delta failure added a route to (#1481, and the ADR 0018 draft in #1491; pinned by known-limit tests); an L1 or L2 build held by a retry pass at the teardown, which a failed or dead holder leaves FAILED intended ACTIVE as before; and on L3 the teardown claiming nothing, as before (#1481). Tests: `services/execution/tests/test_frozen_wiring_settles.py` (unit and functional, SQLite), three Postgres-live tests in `test_wiring_retry_claim_race_live_pg.py` on a used ledger (the late write staged with events, the frozen read scoped to the row's reservation, and a freeze committed between each writer's read and its write), `tests/integration/test_ledger_teardown.py::test_cancel_while_every_build_is_failed_releases_every_row`, and `tests/integration/test_failed_teardown.py::test_l1_failed_event_disconnects_applied_pairs_and_failed_builds` (renamed from `test_l1_failed_event_disconnects_only_applied_pairs`, which pinned the old contract: the switch whose connect failed got no driver call).

- Fixed: a driver cache miss no longer blocks execution's event loop while the config-schema extraction runs, and the driver package download and every `_get_internal` read now end at a total deadline; other outgoing reads, such as the fabric lookup, still have only httpx's per-phase timeouts (part of #1498; CFG-LOAD-2, CFG-SCHEMA-11, CFG-LOAD-6, WIRE-GATE-3, WIRE-ORDER-11, and WIRE-DRIVER-7 updated). On a driver cache miss, `load_driver` ran the config-schema extraction (a synchronous sandbox `subprocess.run` of up to `STATUS_CHECK_TIMEOUT_SECONDS`, after a `pip install` of up to 60 seconds when `ALLOW_DRIVER_PIP_INSTALL` is on) on the event loop, so no other coroutine in the process ran meanwhile, the consumer's in-progress heartbeat included; it now runs on a worker thread, and `_SchemaExtraction` decides under one lock whether the coroutine or the worker removes the attempt's directory when a load is cancelled mid-extraction, so a late `_deps` write cannot leave a directory behind. The driver download and every `_get_internal` read (the event check, and the device, template, latest config, hypervisor, secret, and fork reads) had only httpx's per-phase timeouts, which a response dripping a byte under 30 or 10 seconds never trips; each now also runs under a total deadline, `DRIVER_DOWNLOAD_DEADLINE_SECONDS` (120) and `INTERNAL_READ_DEADLINE_SECONDS` (20), constants by decision. A deadline surfaces as the transient failure each path already had: a download stores `driver load failed: TimeoutError` with one attempt counted and stays retryable, and nothing reaches the cache; an internal read raises `TransientUpstreamError` (`<what>: transport error: TimeoutError`), so the consumer nacks and the manual retry route answers 503.

- Build: the frontend moves from @xyflow/react 12.12.1 to 13.0.2 (Dependabot #1506) with no intended change to the topology editor. v13 hands `onNodesChange` and `onEdgesChange` a `NodeChangeset` or `EdgeChangeset` instead of an array and no longer exports `applyNodeChanges` or `applyEdgeChanges`, so the canvas store applies a changeset with its own `applyTo`, and the page's bundle expansion (`handleEdgesChange`, TOPO-WIRE-7) walks the changeset and hands the store a new one, still expanding a select or remove on a bundle to every member, passing a replace on a bundle through, and stripping the render-only server reason from a replaced edge (TOPO-UIFORK-10). A changeset groups changes by element id and is applied per element, and the new one keeps each id's order. For the changes React Flow emits on this canvas (select and remove on edges; select, position, dimensions, and remove on nodes) the store's result is the one v12's array gave. v13's `applyTo` differs from v12's `applyNodeChanges` and `applyEdgeChanges` only when a changeset holds a replace or an add in certain orders, none of which this editor produces: a change after a replace on the same id (v12 dropped it, v13 applies it), a replace after a remove (v12 kept the element as replaced, v13 removes it), an add with an `index` (v12 inserted there, v13 appends), an add whose id already exists (v12 appended a duplicate, v13 keeps the existing element), and a change on an element added in the same changeset (v12 skipped it, v13 applies it). Every changed default that v13 still exposes as a prop is pinned to its v12 value on the canvas (`frontend/src/lib/reactFlowDefaults.ts`): `forceColorMode="light"` (v13 follows the system color scheme and dropped `colorMode`, whose v12 default was light), `elevateEdgesOnSelect={false}` (v12's default), and a fit-view padding of `4.545454545454546%` on the canvas and on its Controls' fit-view button, which gives the same pixels as v12's default of 0.1 legacy units for every viewport side from 1 to 10000 pixels (v13's default is 5%, and a number now means pixels). Three changes cannot be pinned and have no visible effect here: `nodeClickDistance` was removed (v12 default 0; v13 suppresses a node click only after a drag that moved nodes or ended outside the node, and the editor has no `onNodeClick` while the controls inside nodes are `nodrag`); `paneClickDistance` became `paneDragThreshold` (1 in both versions; in v13 it also has to be exceeded before a pan starts); and the canvas element now computes `color-scheme: light` where v12 left it `normal`, which resolves to the same light system colors because the page declares no color scheme. v13's stylesheet also moved into `@layer xyflow`, and `main.tsx` imported `index.css` after `App`, so the xyflow layer was declared before Tailwind's and Tailwind's preflight (`margin: 0`, `padding: 0`, `border: 0 solid`) outranked React Flow's panel margin and Controls button styles; `main.tsx` now imports `index.css` first, so xyflow's layer comes last and outranks Tailwind's layers the way the unlayered v12 stylesheet did. Tests: the store and page tests build real changesets with the library's constructors; new tests pin a mixed select, remove, and replace changeset on bundles, the order of several changes on one id through the page, the strip on replace, the pinned props on the canvas and Controls, the padding's pixel equivalence through v13's `getViewportForBounds`, a scan that every `<ReactFlow>` and `<Controls>` carries the pins, and the `index.css` import order. Not covered by any test that ran: real React Flow in a browser; the nightly e2e covers the canvas.

- Fixed: a wiring retry pass no longer claims and drives a row in a direction the row no longer intends, and no longer undoes another pass's park with a failure it records before its claim (#1496; WIRE-CLAIM-1 and WIRE-CLAIM-6 updated, WIRE-CLAIM-10 new, WIRE-CLAIM-8's read-back clarified). A pass plans each row's direction from rows it read earlier and claims the row immediately before that row's own driver call, and the claim matched on id, FAILED, and an unclaimed stamp only, so a build planned before another pass parked the row (intended RELEASED, the stale-build settlement of #491) still connected, added, or provisioned it and recorded it ACTIVE intended ACTIVE for a wire the owner had removed; and a failure the pass recorded by row id before its claim (a template read, driver load, or login failure, an L2 create_vlan failure, or an L3 switch read or gate refusal) set the parked row back to intended ACTIVE (on a frozen reservation that stranded an L3 pin the teardown had parked: its routes were never removed). `WiringRowClaims.claim` now takes the call site's direction (`intended=`, required: ACTIVE for `connect_ports`, `add_to_vlan`, and an L3 provision; RELEASED for `disconnect_ports`, `remove_from_vlan`, and an L3 deprovision) and its one UPDATE writes the stamp only when the direction is admitted (`direction_admits`: the row's `intended` equals it, or it is a release and the reservation is frozen, read inside the statement; a build never gets the frozen exception), returning the row's stamp so the two refusals are told apart from that statement alone. The row-id failure writers (`record_l1_failed`, `record_l2_failed`, `record_route_failed` given `row_id`) apply the same compare in their own UPDATE on a row the pass does not hold; a row the pass holds, the success and release flips, and the key-based (reconcile) writes are unchanged. A row refused for direction gets no value change (the claim's UPDATE still runs and writes the stamp back as it was; a refused failure write is rolled back), no execution run, and no driver call of its own (an L1 or L2 switch's shared login and logout still run; an L3 pin is claimed before its login, so nothing runs for it), joins the new `WiringRowClaims.wrong_direction` set rather than `lost`, and reads back by its status: the manual retry endpoint answers `still_failed` (status FAILED, with the row's attempts and reason) and the tick counts it in `still_failed`, not `in_progress`. A later retry drives it in the direction it then intends, except a FAILED intended-ACTIVE row on a frozen reservation, which no retry drives, as before (including one left so by a freeze that commits while a release claim waits on the row's lock: that claim reads not frozen and is refused). An admitted frozen release drives what the pass planned, so on L3 and L2 content a non-comparing writer added in the window (a newer route, a re-pointed allocation) is neither driven nor kept in the record, as before; this stale-content window is #1481's, pinned as known-limit tests. A row the pass already holds is answered from the direction it was claimed for. No API, schema, or frontend change. Not closed, both tracked by #1481, where the direction still matches: a retry that validated intent before a save and connects after the carried-delta apply passed over the row without parking it, and a retry planned from older content that drives it over a newer failed attempt.

- Security: the default request body limit `MAX_REQUEST_BODY_BYTES` is now 262144 (256 KiB), down from 4194304 (4 MiB), and the routes that legitimately take a large JSON body are marked to take 4 MiB more (#1484; OPS-VALID-9, OPS-VALID-10, and OPS-VALID-12 updated). Before, every route parsed up to 4 MiB before authentication, so any caller with no token could make any JSON route, login included, spend about 0.1 s of its event loop and 25 MB of memory per request; now that is bounded by 256 KiB on every route but the marked ones. The new constant `herd_common.validation_errors.LARGE_JSON_BODY_BYTES` (4194304) is the allowance, applied with the existing `extends_body_limit` mark, which adds to the setting: an operator who raises `MAX_REQUEST_BODY_BYTES` raises every marked route by as much. Marked: cabling's `PUT /topologies/{topology_id}`, `POST /templates`, `PUT /templates/{template_id}`, and `POST /connections/bulk` (200 items reach 544811 bytes of ASCII at the schema's bounds), reservations' `PUT /{reservation_id}/fork/canvas` and `POST /{reservation_id}/fork/save`, ai-orchestrator's `POST /commit`, and inventory's `POST /devices/{device_id}/config-versions`. The routes those are forwarded to take the allowance plus the 256 KiB envelope: cabling's internal fork canvas and save, and execution's `POST /execute` and `POST /execute/internal`. In the shape the topology editor saves, a canvas of 1,000 devices and 4,000 links is 2361613 bytes, and the canvas routes' limit at the defaults holds 1,887 devices with four links each. Measured to fit the default and left unmarked: cabling's `POST /pathfind/batch` (2,000 pairs, 236011 bytes as compact JSON) and user-profile's `PUT` and `PATCH /preferences` (139319 bytes at their caps); every other unmarked route whose schema bounds nothing (42, among them inventory's device and template create and update and the secrets create and update, besides the measured preferences `PUT`) stays at the default by decision, each named in the guard's new `UNBOUNDED_AT_THE_DEFAULT` table with why its body is small. ai-orchestrator's commit now measures the canvas it builds against cabling's limit for the canvas route, the setting plus the allowance, instead of the setting alone, which would have refused every canvas over 256 KiB. The wiring guard `tests/unit/test_request_body_limit_wiring.py` gains a `LARGE_JSON_ROUTES` table and now fails both ways, on a mark no table names and on a row whose route carries no mark, and on a mark whose bytes are not the sum of its rows; `schema_body_estimate` gives an upper bound on the compact body a standard encoder writes for every JSON route whose schema is bounded (6 bytes a character of a free string, 24 a number, canonical UUIDs and timestamps; a model that keeps extra keys is unbounded), and the guard fails on one over the default without a mark and on an unmarked route with no bound that `UNBOUNDED_AT_THE_DEFAULT` does not name; and each forward that takes the envelope names the route that admitted what it sends, and the receiver must add at least what that route adds plus the envelope. The default moves in `herd_common`, the config service's copy, the twelve `docker-compose.yml` lines, `.env.example`, and `docs/ENV_VARS.md`. The load test's `OversizedBodySender` sends one byte over the new default and no longer targets the commit, a new `LargeCanvasSaver` user saves a 1.3 MiB canvas of network element nodes beside it, the integration suite saves a canvas at the large limit and one byte over it and a 1 MiB fork canvas through reservations to cabling, and a new browser test saves a canvas of network element nodes over the default from the topology editor. Upgrade note: a client that sent a body over 256 KiB to a route that is not marked now gets 413 `Request body is larger than 262144 bytes`; raise `MAX_REQUEST_BODY_BYTES` on every service together if such a body is legitimate. An operator who set `MAX_REQUEST_BODY_BYTES=4194304` explicitly keeps 4 MiB on every route, gaining nothing, and the large-JSON routes then take 8 MiB and the routes they forward to 8.25 MiB; remove an explicit 4194304 to take the new default.

- Tests: the request body limit's wiring guard now fails on a route that streams its answer without a body parameter, unless it is listed by decision with a reason in its new `STREAMING_WITHOUT_BODY` table (#1483, the owner's option (a), no product change; OPS-VALID-9 updated). Such a route has its body read by the response's disconnect listener, and behind the request logger a body over the limit ends in a 500 (`RuntimeError: No response returned.`) or a stream cut short instead of the route's answer. `tests/unit/test_request_body_limit_wiring.py` finds a streaming route in every built app three ways: its `response_class` (declared on the route, its router, or the app), its endpoint's return annotation (also inside a union, `Optional`, or `Annotated`), and a call it returns in its own body, directly or through a local name assigned once, each a class with `StreamingResponse` or `EventSourceResponse` in its MRO, so a subclass counts. It does not see a stream built in another function, returned from a dependency, or sent through the ASGI `send`; the module docstring lists every spelling the scan follows and every one it does not. The fix round of the review taught it conditional expressions, `and` and `or`, walrus and tuple-unpacking bindings, `functools.partial`, lambda endpoints, decorators without `functools.wraps`, string annotations, and import aliases of `EventSourceResponse`. Fixture sources prove each detection both ways, and the one HERD streaming route, the reservation assistant, is seen and takes its body as a parameter, so nothing is listed. The `herd_common.validation_errors` module docstring no longer says a route that never reads its body answers as before without the streaming exception.

- Fixed: a wiring retry pass's drive claim on an L3 route pin is now sized for the pin's whole drive, so in the worst case the budget states (every attempt running to `EXECUTION_TIMEOUT_SECONDS`, and each driver action spending under the new one-second allowance outside its sandbox timers) a second pass no longer claims the pin and drives the same routes while the first is still driving them (#1482, the owner's option 1; WIRE-CLAIM-3 rewritten, WIRE-CLAIM-1 updated). The claim is taken once per switch, after the driver load and immediately before its login, and must cover the login, every route operation (residue removals and one call per route), and the logout, but it lasted the one-row budget of three driver actions (5 minutes at the defaults), so a pin with two or more route operations could outlive it. `_apply_l3_adjacency` now counts its drive sequence R before claiming and passes the new `wiring_claim.l3_pin_claim_budget(R)` as the new optional per-call `budget` of `WiringRowClaims.claim`: the worst case of R plus 2 actions, plus `L3_ACTION_UNTIMED_OVERHEAD_ALLOWANCE_MS` (1 second per action, an allowance for the execution-run writes, the worker-thread hop, the sandbox's file I/O, and the child's start and reaping, not a measured bound), rounded to the next whole minute, and never less than the three-action budget: at the defaults 5 minutes for R of 0 or 1, 7 for 2, 19 for 10, 75 for 47, 309 for 200. The claim outlives the drive as long as the drive's time outside its sandbox timers stays under R plus 2 seconds plus the rounding slack (at least 0.4 s at the default timeout, 0.2 s at any whole-second timeout); a larger overhead still lets a second pass in, and a test pins that known limit. `claim_budget` now takes the action count (default 3) and rounds to the next whole minute strictly after the worst case, summed in whole milliseconds, so a sum that is already a whole number of minutes no longer gives a claim that expires at the instant the drive ends; for three actions at any whole-second timeout the sum is never a whole number of minutes, so the L1 and L2 budget is unchanged and gets no allowance. Not counted by any budget, L1 and L2 included: with `ALLOW_DRIVER_PIP_INSTALL` on, the `pip install` the sandbox runs before the driver child (its own 60 s timeout) on every attempt while a package with a `requirements.txt` has no `_deps/` directory. There is no cap and no new setting: nothing bounds a pin's route count (until #1493, above, limited one provision to 1,000 routes per switch), so a claim lasts at most one minute more than the larger of the pin's worst case plus the allowance and the three-action worst case (a 50,000-route pin: 76,337 minutes, about 53 days), and a holder that dies mid-drive leaves its pin unclaimable that long. The claim still expires at its stamp and is still cleared by the holder's own record, and the reconcile's treatment of a held row is unchanged (#1481).

- Tests: the deep-value test of `holds_lone_surrogate` no longer fails on Python 3.14 (#1487, OPS-VALID-8). It took a list nested 10,000 deep to be too deep for `json.dumps`, which held on 3.12 and 3.13 (a fixed C recursion limit; 3.12 refuses 9,998 levels, 3.13 refuses 9,999) but not on 3.14, which checks the real stack, so its depth varies by build and stack size (3.14.3 serializes about 58,000 levels in 8 MiB). The product behaves the same on every interpreter: `holds_lone_surrogate` answers right at every depth through the serialization or its walk, and the body check never parses. `test_holds_lone_surrogate_on_a_value_too_deep_to_serialize` now finds the shallowest depth `json.dumps` refuses on the running interpreter, asserts twice that depth unserializable, and pins the walk's answer there; the new `test_holds_lone_surrogate_answers_at_every_depth_where_serializing_stops` pins the answer for every depth within three of that edge and from 9,996 to 10,000. The new `test_the_walk_finds_a_lone_surrogate_wherever_it_sits` closes an older gap, a walk pinned only on single-item lists: past that edge, over list, dict, and mixed spines, it puts the lone surrogate in a dict key, a dict value, a list's first and second item, and after a clean string, each with a clean twin that must answer False; six mutants of the walk that passed the whole common suite before (dicts skipped, keys dropped, values dropped, only the first or last list item pushed, the first string deciding) each fail it. All three run in a thread with an 8 MiB stack, so `ulimit -s` cannot move the depth. `docs/specs/TESTING.md` states the rule.

- Tests: the herd_common cost tests read CPU time through one helper and no longer fail on a busy runner (#1477, OPS-VALID-8). `services/common/tests/test_validation_errors.py::test_an_escape_heavy_body_costs_a_bounded_number_of_passes` timed the body scan with a wall clock and allowed 12 times `json.loads` of the same body; it read 18.5 on a shared CI runner for a PR that did not touch the code, and on the development host it failed 10 of 10 runs pinned to one core beside two CPU hogs, while its CPU-time ratio stayed under 9 in 50 runs pinned and 50 unpinned, so the code had not regressed. Every timing in `test_validation_errors.py` and `test_config_redaction_cost.py` now goes through `services/common/tests/_cost.py`: `growth_ratios` (the cost at n, 4n, and 16n) and `cpu_ratio` (the code against a reference on the same input), both in CPU time (`time.process_time`), timed in alternating rounds keeping each side's fastest call, and measured once more before a bound fails, so no ratio is single-shot. The escape-heavy test, renamed `test_an_escape_heavy_body_costs_linear_work_in_two_copies`, has three bounds: each fourfold step in size costs at most 8 times the one before (a quadratic scan fails), the scan of the 1 MB body costs under twice the worst ratio to one `bytes.lower` pass measured for its shape on the development host and on GitHub runners of two CPU models (Intel Xeon 6975P-C and AMD EPYC 9V74, run 44692177551), so a scan three times slower, or one whose cost follows the number of escapes, fails on all three, and one twice as slow passes on the development host; its memory bound is unchanged. The no-marker test's time bound is now per body, from the same measurements: 2 times a `bytes.lower` pass for ASCII and CJK text (was 4, which the full escape scan on those bodies, at 4.1 to 4.6, could pass) and 4 for a body of other escapes, which measured 2.0 on the Xeon. The other three timings keep their bounds, now in CPU time. `docs/specs/TESTING.md` states the rule for the next cost test, what CPU time does not protect against, and that the per-service `test_lone_surrogate_cost.py` files still use a wall clock.
- Tests: the herd_common body-scan cost bounds hold on every interpreter build measured (#1492, OPS-VALID-8). The #1477 constant-factor bounds in `services/common/tests/test_validation_errors.py` compared the scan with one `bytes.lower` pass, which costs the same on the distro and the python-build-standalone 3.12 builds, while the two-byte substring searches the scan is made of cost about 4.6 times as much on python-build-standalone (what uv installs) as on the Ubuntu distro 3.12.3; so `test_an_escape_heavy_body_costs_linear_work_in_two_copies[u0041_then_low]` and the ASCII and CJK bodies of `test_a_body_with_no_surrogate_marker_is_passed_after_two_searches` failed every run on python-build-standalone 3.12 while the code had not regressed. Both tests now compare the scan with `_scan_cost_budget`, a frozen budget: the C passes the scan was made of when #1492 measured it, with the same early exits and its patterns copied, not imported. It does not follow the scan; a cheaper scan leaves it alone, and it is raised only when a change to the scan measures over the bound, as a reviewed decision recorded with the new ratio. One bound, `_BUDGET_BOUND` (1.5), serves every body; the per-shape `_ESCAPE_PASSES_WORST` table and the per-body no-marker bounds are gone, and the growth and memory bounds are unchanged. New cost bodies take the scan's other paths: `korean_text` (a 0xED byte in most characters) and `lone_highs` in the escape-heavy set, and a UTF-16 body in the new `test_a_utf16_body_costs_its_decode_and_the_escape_passes` (growth, budget, and memory under three times the body). On the development host, unmutated, the ratio measured 0.913 to 1.108 in 30 runs in each of unpinned, pinned, pinned beside two CPU hogs, and under coverage on distro 3.12.3 and python-build-standalone 3.12.16 and 3.13.18, and 0.959 to 1.091 in 10 pinned runs on each of 3.12.12, 3.13.12 (deadsnakes), 3.14.0, and 3.14.3 (deadsnakes). Mutations of the product scan, 10 runs each on 3.12.3 and 3.12.16: the scan run twice or three times fails every body; a Python loop per 0xED byte fails `korean_text`, `findall` in place of `search` fails `lone_highs`, and a Python loop over the decoded UTF-16 text fails the UTF-16 body; the early exit removed fails every no-marker body, by memory; a Python step per `\u` escape fails the bodies whose escapes are mostly `\u` but not `backslash_pairs_then_low` or `four_backslashes_and_text`, and a Python loop over every backslash fails every body but `korean_text` on 3.12.3 and only the bodies dense in backslashes on 3.12.16 (the code comment on `_BUDGET_BOUND` listed each; #1512, above, replaced that bound with call counts). The other three timings keep their references and bounds, and their docstrings give the new figures: the structural scan against `json.loads` reached 0.12 on python-build-standalone 3.13.18 (bound 0.2). `docs/specs/TESTING.md` now requires a budget made of the same primitive operations, says when it is raised, requires a bound measured on a distro and a python-build-standalone build before it lands, and says how to build the throwaway environment.

- Security: every service, config included, bounds the size of a request body and refuses a larger one with 413 before it is parsed, scanned for a lone surrogate, or authenticated (#1466, the owner's option (b); new OPS-VALID-9 to OPS-VALID-12; OPS-VALID-4 and OPS-VALID-8 updated). Nothing bounded a body before: the gateway and uvicorn have no limit, and FastAPI parses a whole JSON body before a route's dependencies run, so a 9.8 MB list sent with no token cost acl 58 MB and 0.23 s of its event loop before the 401. `herd_common.validation_errors.install_validation_errors` now takes a required `max_body_bytes` and installs `LimitRequestBody`, a pure ASGI middleware, just outside `RefuseLoneSurrogates` (config, which installs its copy without that check, gets it innermost). Every service passes the new `MAX_REQUEST_BODY_BYTES` setting, default 4194304 (4 MiB, room for a topology canvas of about 1,000 devices and 4,000 links; lowered to 262144 by #1484, above), written in decimal digits and at least 65536; the eleven `herd_common` services read it through `HerdBaseSettings` and refuse to start on any other value, config reads it the same way in the new `services/config/app/request_limits.py` and uses the default with a warning instead, and `docker-compose.yml` passes it to all twelve. At the first read of a body, a `Content-Length` over the limit is refused before a byte is read; otherwise each message is counted as it is handed on, and the read that passes the limit is refused, so a chunked or under-declared body never reaches a route past it; a body of exactly the limit is accepted. The answer is `{"detail": [{"type": "body_too_large", "loc": ["body"], "msg": "Request body is larger than <limit> bytes"}]}` and one INFO log line names the limit and whether the body was declared or received over it. The connection stays open: uvicorn reads and discards the rest of the refused body, so a client still writing its body gets the 413 and can send its next request on the same connection (pinned over real sockets with both of uvicorn's HTTP implementations); the limit bounds memory and parsing, not bandwidth. A route that never reads its body, an unknown route, and a websocket or lifespan scope are unchanged; nothing is refused once the response has started. Known limit: a route that streams its answer without reading its body, given a body over the limit, does not answer as before; behind the request logger a declared body ends in a 500 (`RuntimeError: No response returned.`) and a chunked one cuts the stream short, with nothing of the body reaching the route. No HERD route streams without reading its body (the reservation assistant reads its JSON first), and a streaming route must read or drain its body first. A client using `Expect: 100-continue` gets the 413 without a `100 Continue` and must not reuse that connection. The cost is constant per message, with no copy of the body (measured 4.9 microseconds per small request, pinned to one core). Routes that take more mark their endpoint with the new ONE helper `extends_body_limit`. Upload routes pass the bound they already enforce and take the default plus that: inventory's driver upload and replace (`DRIVER_MAX_SIZE_BYTES`), ai-orchestrator's `POST /generate` (`UPLOAD_MAX_FILES` times `UPLOAD_MAX_FILE_BYTES`), execution's `POST /internal/validate-package` (the base64 length of `VALIDATE_PACKAGE_MAX_BYTES`), and the inventory and cabling bulk imports, which read their upload with no bound of their own (#1417, still open), so a fixed 10 MiB import file bound is now their only one. A body one service forwards to another is never refused there for what the sender adds: the six routes that receive a forwarded body (cabling's fork canvas and save, execution's `POST /execute` and `/execute/internal`, reservations' `POST /`, user-profile's `PATCH /preferences`) take a 256 KiB envelope; cabling's import resolves device names in batches of at most 32 KiB, the utilization report asks auth for user groups in batches of 500 (more than 500 users made it answer 422 and show everyone Ungrouped), and ai-orchestrator's commit measures the canvas it builds and answers 413 before writing anything when it is over the limit; and a downstream 413 reaches the user as a 4xx (notifications answered 503 for it before). The new guard `tests/unit/test_request_body_limit_wiring.py` builds every service's app and fails when its middleware list is not the declared one, the limit is not the service's setting, or a form route is unmarked, and it fails on any outbound call site that sends a body without a classification (keyed by function and receiver, so a second call in a classified function counts, and `httpx.Request`, `build_request`, and positional bodies to HERD's forwarders are seen); `tests/unit/test_validation_errors_wiring.py` now also requires the install's `max_body_bytes=settings.max_request_body_bytes` (config: `max_request_body_bytes()`). The load test gains `OversizedBodySender`, one user that sends bodies one byte over the limit to one route of every service and fails the run on any answer but the 413. Upgrade note: a client that sent a body over 4 MiB to a JSON route now gets 413 (since #1484, above, over 256 KiB to a route that is not marked); raise `MAX_REQUEST_BODY_BYTES` on every service together if a legitimate body is larger. The OpenAPI documents do not list the 413.

- Fixed: a wiring retry pass no longer rewrites, parks, releases, or frees a row another retry pass is driving through its failure writes and its settles (#1479, new WIRE-CLAIM-8 and WIRE-CLAIM-9, WIRE-CLAIM-2). Before, the failure write by row id of all three ledgers (`record_l1_failed`, `record_l2_failed`, `record_route_failed`) matched on the row id and status `FAILED` only, so a pass that loaded a row before another pass claimed it and then failed before its own claim (a switch, template, or driver load failure, an L1 or L2 login failure, an L2 VLAN define or allocation failure, an L3 gate refusal) rewrote `last_error`, added attempts, and cleared `claimed_until` under the holder's drive; a manual retry made then could report the row `not_retryable` instead of `in_progress`, or claim it and call the driver a second time. The settles a retry pass makes on rows it loaded but never claimed (the stale-build parks of all three layers, the L1 and L2 supersession releases, the L2 nil-allocation release, the L3 unapplied-pin release) checked nothing, so a pass with a stale copy could park a row another pass was driving and hand it to a third pass. And a holder's failure write trusted the pass's memory: a holder whose claim expired and was taken over still wrote its failure and freed the new holder's claim. Now those writes need the row to carry the writing pass's own claim stamp (`WiringRowClaims.held` records the exact stamp each claim wrote), or, for a row the pass does not hold, no live claim: the failure writes add the condition to their own `UPDATE` (no extra query), and the settles run inside `wiring_claim.write_guard`, which locks the row on the same condition first (one short select). A refused write changes nothing; a row still FAILED then reads `in_progress` in that pass's answer, while a row another writer already settled reads `reconnected` (ACTIVE) or `released` (RELEASED). The one wider write is the L3 holder's failure write after a switch whose route calls partly failed, which also lands when a writer that does not claim cleared its stamp, because only that write records routes those calls may have configured. Unchanged on purpose: a retry holder's success and release flips are unconditional as before, and the reconcile passes no claim ledger, so its writes are unchanged and can still change a row a retry pass holds (both #1481).
- Tests: the wiring retry claim race suite stages its races with explicit coordination instead of a fixed 0.4 s head start (#1473). A gated driver stub signals when the claim holder is inside its drive and blocks until the test releases it; the losing channel runs only after that signal and the holder is released only after the loser returned, and every wait is bounded at 30 s and fails the test when it expires. The suite's rows carry 1,000,000 attempts, far above the default retry cap of 10, so the retry tick of a stack running a cap at or below that value never selects them (`WIRING_RETRY_MAX_ATTEMPTS` is an unbounded setting; `test_seeded_rows_are_invisible_to_a_stack_tick` runs the real due selects at this process's unpatched cap, and proves nothing for a stack configured above 1,000,000) (the 2026-11-19 nightly failure, a seeded row reported `not_retryable`, matches the gate stack's tick picking up a seeded row, failing to find the suite's switch in inventory, and writing that failure over it, the defect fixed by #1479; this was read from the code and the log, not reproduced on a gate), and the suite's own tick runs with a raised cap; each of that tick's short due selects takes FOR UPDATE SKIP LOCKED on up to 10,000 FAILED ledger rows of the shared ledger, foreign ones included, for that select only. The stub also records when its gated action returns, so a stub that stops blocking fails every race test instead of passing on timing. Three tests guard #1479 with the same staging, two more pin that a holder's success is still recorded when a teardown or the reconcile cleared its claim during the drive, and others check on Postgres that a claim stamp round-trips exactly and that `write_guard` releases its row lock. The file runs in about 2 s instead of 11. `docs/specs/TESTING.md` gains the race-staging rule.

- Frontend: an author credit, `Designed and built by Lane Nelms`, sits under the sign-in card, the Register card, and the config page's sign-in and change-password cards, and in the About page's Frontend card. The name links to the author's LinkedIn profile in a new tab (`rel="noopener noreferrer"`, accessible name `Lane Nelms on LinkedIn (opens in a new tab)`) and follows every sign-in control in tab order. The wording and the address live in `frontend/src/lib/links.ts` (`AUTHOR_CREDIT_PREFIX`, `AUTHOR_NAME`, `AUTHOR_LINKEDIN_URL`) and render through one component, `AuthorCredit` (new rule OPS-CREDIT-1). Browser tests check on `/login`, `/register`, and the config sign-in card that the credit sits below its card with no sideways scroll. README.md has a new Author section. The manual still documents 0.7.0 and is left as it is; the 0.8.0 manual refresh picks the credit up.

- Fixed: the startup superadmin seed holds `SUPERADMIN_PASSWORD` to the registration password rules, so every value the seed accepts is one `POST /login` takes (#1450, IAM-BOOT-1, IAM-BOOT-3, IAM-HASH-3). Before, a value over 72 characters was seeded without complaint and then refused at local login and the single sign-on break-glass login with 422 `string_too_long`, and a value under 8 characters was seeded although `docs/ENV_VARS.md` promised a minimum of 8. The seed now judges the trimmed value with the constraints and field validators of `RegisterRequest`'s password field (`registration_refusal_code`): 8 to 72 characters, counted in characters as registration and login count them, not UTF-8 bytes, with no NUL character and valid UTF-8. A value that breaks a rule creates no account and logs one ERROR, `SUPERADMIN_PASSWORD breaks a registration password rule: <rule>. The superadmin account was not created; set a value that registration accepts and recreate auth.`, which names the rule and never the value, and startup continues. A NUL character or a lone surrogate used to stop auth at startup with a ValueError; it now takes the same path. The check runs only when an account would be created, so a stack that already has its superadmin logs nothing whatever the value. `docs/TROUBLESHOOTING.md` has a new entry, "No superadmin account after the first boot".
- Security: auth no longer checks or hashes passwords on its event loop, and limits password attempts per source address (IAM-HASH-7, new IAM-LOGIN-7). Every bcrypt computation of a login (local mode and the single sign-on break-glass login) or a registration now runs on a worker thread of auth's own pool, `AUTH_PASSWORD_HASH_THREADS` (default 0: half the CPUs auth may use, counting a readable cgroup CPU quota, between 1 and 4; a set value above those CPUs is lowered to them with a warning), never anyio's shared pool, so other auth routes keep answering while logins are checked; a request that waits 5 seconds for a free thread is answered 503 `Too many password checks in progress; try again shortly` with `Retry-After: 5`, never queued without bound, and a login waiting for a thread holds no database connection. Each source address (the address Traefik saw, the last `X-Forwarded-For` entry) may make `AUTH_LOGIN_MAX_ATTEMPTS` attempts at once (default 60), refilling over `AUTH_LOGIN_WINDOW_SECONDS` (default 60); successes count, and so does a registration without an admin's access token (`make seed` registers with one). Past the allowance the answer is 429 `Too many attempts; try again later` with `Retry-After`, the same for every account, checked for a login before any query, bcrypt, or directory call, and for a registration before any bcrypt (a registration carrying a bearer token that verifies first costs one account read, to judge the admin exemption); nothing is keyed by account, so no account can be locked out. Counts live in the auth process. The work of each login answer is unchanged (still one bcrypt). The three settings are wired through `docker-compose.yml` and documented in `docs/ENV_VARS.md`; `docs/OPERATIONS.md` and `docs/TROUBLESHOOTING.md` say what an operator and a user see. Tests: `services/auth/tests/test_login_limits.py`, `services/auth/tests/test_password_hashing_off_loop.py` (the event loop's heartbeat under concurrent logins, pinned to one core too), `tests/integration/test_login_limits_flow.py` (spends the test host's allowance, then waits one whole `AUTH_LOGIN_WINDOW_SECONDS` plus 3 s for it to refill, in a `finally`, under its own `pytest.mark.timeout`); the locust users wait out a 429 at sign-in, and the new `LoginLimitProbe` class floods the login from the load host and fails on any answer but 401 or 429. The OpenAPI document lists the 429 and 503 on `/login` and `/register`; its contract signature is unchanged.

- Fixed: the reservation assistant masks stored device configurations and run rows before they reach the model provider, so no credential value the mask recognizes is sent; the mask is a keyword heuristic, and a secret under a key name or in free text it does not recognize still is (#1464, AI-TOOL-5, AI-TOOL-6, AI-TOOL-8, new AI-TOOL-12, AI-TOOL-13, AI-WRITE-4, AI-IDENT-5). `get_device_current_config` returned the newest config version exactly as inventory stores it, so a `neighbor ... password X` line in an `frr_mgmt` configuration reached the provider and `assistant_messages.content_blocks`; `list_executions_for_reservation` did the same through each run's `output` (the driver's returned command list) and through the arguments of runs stored before execution masked them. Both, and `list_device_config_history`, now mask with `herd_common.config_redaction` (CFG-EXEC-8), mark the result with `redacted` and a fixed note telling the model `[redacted]` is not the stored text (the tool removes any marker key the stored record carried and writes its own), and fail closed: a configuration that is not an object or cannot be masked is withheld with its metadata only, and a history item or run row that cannot be masked is left out and counted in `withheld`. `list_device_config_history` now answers `{items, redacted}` instead of a bare list. A continued conversation masks its stored tool results again before resending them, so a conversation from before the upgrade does not resend an unmasked configuration; its earlier assistant replies are resent as written until it is swept. `propose_config_change` refuses a payload containing `[redacted]`, so a masked line copied from a read tool is never written as a credential. The template editor's Suggest with AI no longer sends a password field's default (the shared device credential) to the provider. See `docs/OPERATIONS.md`, Upgrade path; the limits are stated in the AI and device-configuration specifications.
- Changed: the credential mask for stored driver arguments (`herd_common.config_redaction`) is wider, and its cost is linear in the text's length (#1464, CFG-EXEC-8, CFG-SCHEMA-13). A value is masked under any key with a keyword part (`authentication-key`, `key_string`, `pre_shared_key`, `tacacsKey`, `wpa_passphrase`, `md5`, `psk`), not only under a key the log formatter's pattern names; a keyword with `:` or `=` attached masks its value (`Password: x`, `PASSWORD=x`, `key:x`); a string holding a JSON object, array, or string (up to 256 KiB) is masked as that value, and longer or cut JSON text is read through its escapes (`\npassword x`, `\"password\": \"x\"`), with an escaped line break ending the masked value; an XML element named by a keyword has its text masked (`<password>x</password>`, PAN-OS `<phash>`); Python's repr of a dict is masked (`{'password': 'x'}`); a URL's `user:password@` is masked through a pattern whose every quantifier is bounded (an unbounded one took 11 s on 64 KB of `a-a-a-...`); and `authentication`, `auth`, `phash` (PAN-OS), `psk`, `token`, and `bearer` are keywords, so FRR's `ip rip authentication string <secret>`, `ip nhrp authentication <secret>`, and `tcp-md5-auth <secret>` are masked (the mode word after `ip ospf authentication` is masked too, which fails safe). A refusal quotes any path part that is a credential keyword token (`at ['password']: ...`, `at bgp['auth_password']: ...`), so execution's second mask over the stored refusal leaves it whole. A run masked before this change whose configuration holds something the wider mask newly matches no longer matches its re-masked config version, so its retry is refused with the existing 409; start a new run. The heuristic's limits (an unrecognized key name, free text with no keyword, a secret on the line after its keyword, a separator the rule does not know) are stated in the module docstring and both specifications.
- Docs: the NOS lab's SR Linux node stays AppArmor-unconfined by decision (#1454). A trial on the ubuntu-26.04 runner applied `apparmor=docker-default`, the option that fixed the FRR node (#1441), and the SR Linux node never became healthy; `docs/NOS_LAB.md` records the result and what to do if a denial ever names an SR Linux process.
- Docs: the identity specification states that in LDAP mode the response time of a refused login can reveal whether the login name matches one directory entry (#1449, accepted and documented by the owner on 2026-11-18; IAM-LDAP-5 and Limits by decision). A name that matches exactly one entry costs a second connection and the user bind that a name matching none or several does not. It is not hidden: a dummy bind would add directory traffic to every mistyped login and can touch lockout counters, and a time floor would slow every login and still leak above the floor. Only the per-source attempt limit above (IAM-LOGIN-7) slows probing, and only per address; an operator can rate-limit `POST /api/auth/login` at a proxy. No code change.

- Fixed: in LDAP mode a login name that `LDAP_USER_FILTER` matches to more than one directory entry is refused instead of signing in as the first entry the directory returns (#1447, decided by the owner on 2026-11-18; IAM-LDAP-3, IAM-LDAP-5, IAM-LDAP-12, IAM-LDAP-14, new IAM-LDAP-16). `ldap_service._search_user` took `entries[0]`, so of two people whose entries matched one name the second could never sign in and which one won was the directory's ordering, and under a substring filter such as `(uid={username}*)` a prefix signed in as whichever entry came first, with that entry's password. The login search now asks for at most two entries; two returned, or one with `sizeLimitExceeded` (a directory whose own limit is one), is the ordinary 401 `Invalid credentials` whatever the password, with the status, body, and headers of an unknown name, no bind as any entry (no password is tried, no lockout counter touched), and one ERROR `ldap_user_ambiguous` naming the setting and the count, never a DN. An ambiguous name costs one search and no user bind, the same as an unknown name. Search result references are not entries and are not followed. Changed with it: one entry from a search that stopped at a time or administrative limit is now the 503 `Directory unavailable, credentials not checked` (the answer the same search with no entry already gave), not a sign-in, since an unfinished search does not prove the entry is the only match. Tests: `services/auth/tests/test_ldap_login_ambiguous.py` (a real ldap3 `MOCK_SYNC` directory with two entries for one name, through `_bind_user_sync` and `POST /login`, with connection and bind counts), the scripted result codes in `test_ldap_service.py`, and three live-directory tests with two temporary entries; `tests/_mock_directory.py` now counts binds and `tests/_fake_directory.py` records each search's arguments.

- Fixed: in LDAP mode a directory attribute value that is not text is no longer turned into account data (#1448, IAM-LDAP-5, IAM-LDAP-11, IAM-MAP-4, IAM-SYNC-4, IAM-SYNC-5, IAM-SWEEP-2, new IAM-LDAP-15 and IAM-SWEEP-10). ldap3 returns a value that is not UTF-8 as `bytes`, and login, member resolution, the group fetch, and the presence enumeration each called `str()` on it, so an email `b"\xffcarol@..."` was provisioned as the text `"b'\\xffcarol@...'"`. `ldap_service._text_value` is now the one conversion: a `str`, or `bytes` that decode as strict UTF-8, that is not empty once white space is stripped and holds no control character (a NUL, which Postgres cannot store, included), format character (a bidi override, embedding, or isolate, a zero width space, a byte order mark; the zero width non-joiner and joiner that Persian and Indic text needs are allowed between two letters or marks), line or paragraph separator, surrogate, or noncharacter, used exactly as given; an email must fit its 255-character column and a username its 100-character one. Anything else counts as absent, so login refuses the email with the 401 `Invalid credentials` and falls back to the login name for the username (when the login name cannot stand in either, a returning user still signs in and only a first login, which would provision an account, is refused), sync skips the member as `missing_email` or `missing_username`, a group name falls back to the DN, a member value that is not text is a new `not_text` member skip that, like the other two, holds back the group's removals for the pass and makes the run `partial` (before, it was silently dropped while the removals went ahead), and the enumeration leaves the value out. When the enumeration finds an entry with any email value that is not text (even beside a readable one), the deactivation sweep holds back the deactivations that entry leaves unproven: only the account whose username matches the entry's username, or, when that cannot be read either, every deactivation the disabled filter did not prove; disabled-filter deactivations, additions, and reactivations still apply. The run records `email_unreadable` with how many accounts were held and for how many passes in a row, and a pass that holds anything logs the ERROR `ldap_sync_deactivation_held`. A person who can write both their own email and username attributes can still hold back every unproven deactivation; that residual is stated in IAM-SWEEP-10. The first text value of a multi-valued attribute is used. Each read that left a value out logs one WARNING `ldap_attribute_not_text` naming the attribute, the DN, and the value classes, never a value. Login still reads the attributes only after the user bind, so the difference needs the user's password. IAM-LDAP-11 (the username fallback) now has tests. Upgrade note: accounts created before the fix from a binary value keep the repr text. One with a repr email can no longer sign in (login finds accounts by the directory's real email, and a new account for it collides on the username), and the deactivation sweep treats it as absent unless a directory entry still holds an unreadable email value with the same username; one with a repr username keeps it until a sync renames it (members of mapped groups, once the username attribute holds text) or an admin does. An Active Directory deployment that set `LDAP_USERNAME_ATTRIBUTE` to the binary `objectGUID` or `objectSid` has a repr username on every account; after the upgrade new logins take the typed login name, and sync skips every member as `missing_username`, which holds back every removal, until the setting names `sAMAccountName` (the default) or `userPrincipalName`. `docs/TROUBLESHOOTING.md` ("LDAP accounts with a bytes-repr email or username") has the PostgreSQL query that finds such rows and the remedy for each case.
- Fixed: in LDAP mode an `LDAP_USER_FILTER` that is not a usable template no longer makes every login answer 500 (#1446, IAM-LDAP-2, IAM-LDAP-12, new IAM-LDAP-14). The filter was built with `str.format`, so `(uid={user})` raised KeyError, `(uid={0})` IndexError, and a single brace ValueError, none of which the login path caught. `ldap_service.render_user_filter` now builds it strictly: `{username}` is the only placeholder (no attribute or index access, conversion, or format spec), `{{` and `}}` stay literal braces, and the template must hold `{username}`, since a filter without it found the same entry for every login name, so any name with that entry's password signed in as it. `user_filter_problem` then refuses a template over 4096 characters, one ldap3 cannot build into a search request (any exception counts, so a deeply nested template's RecursionError or an environment variable's lone surrogate no longer stops auth from booting), and, reading ldap3's parse tree for three probe login names, one where `{username}` could supply an attribute, an assertion, or a whole filter (`({username})` let the login name `uid=alice` sign in as alice), sits inside `(!...)`, or shares an `(|...)` with a branch that does not hold it (`(|(uid={username})(uid=user4))` signed any name in as user4); its verdict is memoized per template. A fourth probe with white space at both edges checks that edge white space in a login name cannot change the filter's shape; a login name that is white space alone is refused with the 401 before any directory call, since ldap3 strips assertion values and `(uid=*{username})` with a blank name became `(uid=*)` and signed in as the first entry. Substring, approximate, and ordering assertions on `{username}` stay allowed and are documented as discouraged. A defect makes every LDAP login answer the 503 `Directory unavailable, credentials not checked` before any directory call, the same for every login name and password, with one ERROR `ldap_user_filter_invalid` naming the setting, the defect, and the class, never the login name; in LDAP mode auth logs the same ERROR once at startup (`log_ldap_startup_problems`, called from the lifespan) and still boots, as it does for an incomplete configuration.
- Security: a refused device configuration is no longer quoted back, stored, or sent to the assistant's model provider (#1457, CFG-SCHEMA-4, CFG-EXEC-10, new CFG-SCHEMA-13). `herd_common.device_config` built its refusal from jsonschema's own message, which quotes the failing part of the config (`'<line>' is too long`, `[<the whole list>] is too long`), so an FRR vtysh line such as `neighbor 192.0.2.1 password <secret>` that broke the `drivers/frr_mgmt` schema's bounds reached inventory's and execution's 422, the execution run's stored `error` (beside the masked `input_params` of CFG-EXEC-8), the run list, and through `list_executions_for_reservation` the model provider; the AI commit's 422 quoted the proposed value the same way. A refusal now reads `device '<name>': config failed schema validation at <path>: <rule>`, built by the new `schema_error_text` from the error's path, keyword, and schema only (`at commands[0]: must be at most 512 characters long`, `at interfaces[0].zone: must be one of 'trust', 'untrust', 'dmz'`, `at the top level: does not allow the field 'bogus'`), for every draft 2020-12 keyword; field names are shown, masked after a credential keyword, cut to 64 characters, and quoted with `repr`; a driver-published schema's `enum`, `const`, and `pattern` text is not quoted (the registry's is), and a field name it defines in `required` or `dependentRequired` is shown only when it is a plain name (letters, digits, `_`, `-`, `.`, at most 64 characters) and is otherwise counted (`needs a required field`); the `ConfigValidationError` no longer chains the jsonschema error, so a logged traceback cannot print it. Execution also masks the refusal's reason with `redact_command_text` before storing it and answers the same text; the device-name prefix is kept apart on the error (`ConfigValidationError.prefix` and `.reason`) and never masked, so a device named `core key switch` no longer cuts its refusal to `device 'core key [redacted]`, and execution's text equals inventory's. Callers that matched jsonschema's wording break: a refusal no longer contains `is too long`, `is not of type`, `Additional properties are not allowed`, or the refused value, though it still contains `config failed schema validation`. Tests: `services/common/tests/test_device_config_refusal_text.py` puts a canary in the value for every validator keyword (a guard fails when a jsonschema upgrade adds one), the real FRR schema, the registry, nested combinators, deep paths, and the field-name rules; `services/execution/tests/test_config_refusal_text.py` sends canary configs through `POST /execute` and `POST /execute/internal` and checks the 422, the stored run, the run list, and every log line; inventory's create and restore and the AI commit gain the same checks; `test_execution_service_edges.py` now reads the FRR schema from the driver instead of a copy that had lost its bounds. Rows written before this change keep their text, but every run execution answers withholds it (new CFG-RUN-8, `withhold_legacy_refusal_text` in `ExecutionRunResponse`): the list, the detail, and the assistant's run-list tool show `<prefix>: config failed schema validation (details withheld; recorded before values were removed from these messages)`. Docs: `docs/DRIVERS.md` and CFG-SCHEMA-13 now say that the `format` keyword is not enforced (no format checker is wired). Upgrade note: the read-time mask does not cover a direct database read, a log line written before the upgrade, or a tool result already stored in `assistant_messages.content_blocks`; [OPERATIONS.md](docs/OPERATIONS.md), "Upgrade path", has the query that finds the rows, an optional rewrite, and those copies. On a dev stack execution and inventory mount `herd_common` and pick the change up on a restart; ai-orchestrator, which does not, needs a rebuilt image (`docker compose up -d --build ai-orchestrator`, or `make up`).
- Security: a `config.json` that holds an operator's settings and becomes unusable now stops every service but config at startup instead of being replaced by environment defaults (#1451 review, OPS-SET-4). Before, a truncated, empty, or unreadable file, or a directory at the path, was ignored with a WARNING whoever wrote it, so a stack whose operator had saved `AUTH_METHOD=ldap` (or `oidc`) on the config page, with no `AUTH_METHOD` in `.env`, came back in compose's default `local` mode, where `POST /register` is open, and an `AUTH_SECRET_KEY`, `INTERNAL_API_TOKEN`, or `SUPERADMIN_PASSWORD` rotated on the page reverted to the `.env` value. Whether a file is the operator's is read from the `config.bootstrapped` marker the config service writes beside its first-boot copy of the environment and deletes on the first config-page save: with the marker, an unusable file is still ignored with one WARNING (its values were the environment's); without it, the settings load raises `herd_common.config_loader.ConfigFileUnusable` naming the path, the reason, and the remedy (repair the file, remove it to use the environment, or save again on the config page). A missing file is unchanged (environment values, no log). The config service still ignores an unusable file and serves its page, so the operator can re-save from it. **Upgrade note:** a stack whose `config.json` has no marker (saved on the config page, written by hand, or kept from a version before the marker) and is unreadable or not a JSON object, which ran on `.env` values before, now stops at startup with that error; `docs/TROUBLESHOOTING.md` ("A service exits at startup naming config.json") has the three fixes. A fresh volume is unaffected: the first boot writes a valid copy with the marker, or no file when `.env` lacks a required key.
- Docs: the VRF test statements match CI (#1453). `docs/specs/TESTING.md` said GitHub runners lack the `vrf` module so the `nos-dialect` job skips the three VRF tests, and `docs/NOS_LAB.md`, `docs/SIMULATED_LAB_GUIDE.md`, the comment in `infra/nos-test/frr/start.sh`, and a docstring in `tests/nos_lab/test_frr_l3_driver_live.py` named the runner as the host without the modules. The runner image has the modules but does not load them; ci.yml's `nos-dialect` job and nightly.yml's feature-tier step run `sudo modprobe vrf dummy` first, and the VRF tests pass there (CI run 44206733684: the three dialect VRF tests passed, 28 passed and none skipped; nightly run 44206748116: the via-stack VRF test passed). Each now says so, and describes the one fallback exactly: a failed `modprobe` prints `vrf/dummy modules unavailable; VRF tests will skip`, the node boots without the fixture, and the VRF tests skip with the job green.
- Fixed: the config page's change-password form counts password length the way the config service does, in characters (code points), not UTF-16 units (#1452, OPS-CONFIG-8). An emoji outside the Basic Multilingual Plane is one character to the server and was two to the page, so the page refused 17 to 32 emoji the server accepts, its `maxLength={32}` cut a pasted value to 16 of them (both fields alike, so the shorter password was stored), and 4 to 7 emoji passed the page to a 422 the user saw only as `Failed to change password`. The bound now lives in `frontend/src/lib/configPassword.ts` (`codePointLength`, `configPasswordLengthOk`), both inputs drop `minLength` and `maxLength`, and a 422 from the route shows the server's own fixed sentence (`configChangePasswordErrorText` in `frontend/src/lib/errors.ts`); the 503, a validation list, and every other failure keep `Failed to change password`. The login and register pages check no password length, so they needed no change.
- Fixed: the form of `config.json` no longer crashes a service at import with an unhandled error (#1451, OPS-SET-4, OPS-SET-5); what an unusable file does instead is the entry above. A wrong-typed value inside a valid file still stops a service, by design, through that setting's validation (`{"LOG_LEVEL": 5}` is a `ValidationError` naming the field, as a bad environment value is). `herd_common.config_loader._load_json` caught only `JSONDecodeError` and `OSError`, so a file holding a byte that is not UTF-8, nesting deeper than the parser allows, or an integer over Python's 4300-digit limit raised at import in every service but config, valid JSON that is not an object (a list, a string, a number, a boolean, `null`) raised `AttributeError` in `HerdJsonConfigSource`, and a FIFO at the path blocked the import forever. The loader now checks the path with `stat` before any read (a directory, FIFO, socket, or device is refused without opening it, and the open is non-blocking), treats a dangling or looping link as unusable rather than missing, catches `ValueError`, `RecursionError`, and `OSError`, refuses a non-object, and reads the file as UTF-8 whatever the locale; each case is then an unusable file, judged by who wrote it (the entry above). A path where nothing exists is still a missing file with no log. The config service's own `config_store.load_config` gets the same path checks and the UTF-8 read, and its WARNING now names the exception's class instead of its text (a `UnicodeDecodeError`'s text quotes a byte of the file). `DATABASE_URL` derivation from `POSTGRES_*`: a numeric part is used as Python writes it, as a numeric password already was (`12345`; a float such as `1e3` becomes `1000.0`), so a numeric user or database, which raised `TypeError` at import, now derives a URL; a `true`, list, or object part, which was stringified into a wrong URL as a password and raised `TypeError` as a user or database, a `false` part, which was skipped silently as unset, and a lone surrogate, which raised `UnicodeEncodeError`, now derive no URL and log the keys; a `0` part, which was skipped silently as unset, now derives from `0`; a whitespace-only part, which built a URL such as `   :pw@postgres`, is now unset as OPS-SET-4 says. `MemoryError` on a huge file is not handled.
- Auth: a superadmin can recover single sign-on accounts through the API (#1399, decided by the owner on 2026-11-18; ADR 0016 D4 as built; new rules IAM-OIDC-29 to IAM-OIDC-39). Before this an `OIDC_ISSUER` change locked out every single sign-on user, and a person whose provider reissued their `sub` could never sign in again, because a sign-in with an unknown (issuer, subject) whose email an account holds is refused (IAM-OIDC-10) and the only way out was hand-written SQL. `POST /api/auth/admin/federated-identities/rewrite-issuer` (`{old_issuer, new_issuer, dry_run}`, `dry_run` required) moves every identity under exactly `old_issuer` to `new_issuer` in one UPDATE, keeping each subject and account; it answers `{dry_run, matched, conflicts, rewritten}`, 400 when the two issuers are equal, and 409 with the count, nothing changed, when any subject already has an identity under the new issuer. `PUT /api/auth/users/{id}/federated-identity` (`{issuer, subject}`) replaces one `oidc` account's identity with exactly that pair; 404 for an unknown account, 409 for a local or LDAP account and for a pair another account holds. The owner chose "unlink so the next login links afresh"; a bare unlink cannot do that without linking the next sign-in by its email, which ADR 0016 D4 forbids, so the route relinks to the subject the superadmin names, in one step. The routes are superadmin only and answer in every auth mode (in OIDC mode through the break-glass login or a superadmin API token), never change an account id, never read an email or a claim, take only issuers and subjects a login could present (the shared `identity_key_part_ok`), and write one audit line per call with the caller and counts but no issuer or subject; a database error answers a fixed 500 logged by class name. No UI page. From the adversarial review, three further owner decisions the same day: (A) a first login refused because its email belongs to an ordinary `oidc` account keeps the refused (issuer, subject) as a relink candidate against that account (migration 0011: `federated_relink_candidates`, 24-hour life, at most 5 per account and 1000 in all, oldest evicted, and `federated_relink_capture_totals`, a count of distinct pairs that never goes down), listed by the new superadmin route `GET /api/auth/users/{id}/federated-identity/candidates` with a fingerprint and never the subject; the relink also takes `{candidate_id}` in place of the pair, exactly one form, and discards the account's candidates; a candidate is only evidence that someone tried, so the runbook requires confirming with the person out of band (an agreed minute, exactly one new candidate) and forbids a relink while more than one is live; the capture never changes the refusal and a capture failure is logged by class name. (B) The rewrite's dry run and 409 name the colliding accounts as id pairs (`collisions`, at most 50, `collisions_truncated`). (C) Audit lines carry 16-hex HMAC-SHA256 fingerprints of the pairs and issuers under a key derived from `AUTH_SECRET_KEY`, never the values; `docs/OPERATIONS.md` shows how to compute one. Also fixed in review: a returning login's `last_login_at` write is now a compare-and-swap, so a sign-in in flight when a relink or rewrite commits is refused (`identity_changed`, IAM-OIDC-39); the runbook re-runs the dry run after the `OIDC_ISSUER` change (a first-time user signing in between the rewrite and the change is provisioned under the old issuer), uses `.env` rather than the config page for the OIDC keys, gives Dex's `sub` recipe correctly (a base64url protobuf message), and says what sessions survive a relink. Docs: the `OIDC_ISSUER` warning in `docs/ENV_VARS.md`, the runbook sections "Changing the OIDC issuer" and "A user's first single sign-on login is refused as a collision" in `docs/OPERATIONS.md`, `docs/ROLES.md`. Tests: unit, functional through the routes with real tokens and through the single sign-on routes against the fake provider, a new Postgres-live suite `services/auth/tests/test_federated_identity_live_pg.py` in `_gate-pg-live-tests`, three recovery stories in the live Dex suite, and three through the gateway in `tests/integration/test_oidc_auth.py`; the auth contract snapshot gains the three routes and eight schemas.
- Fixed: no HERD service answers a request-validation 422 with pydantic's `input` or `ctx` any more, and none answers 500 because a body that fails validation held a lone surrogate (#1455, new OPS-VALID-1 to OPS-VALID-5). Auth's handler from #1426 moved, unchanged in code, to `herd_common.validation_errors`, and every service's `app/main.py` now installs it: acl, ai-orchestrator, auth, cabling, execution, integration, inventory, notifications, reservations, secrets, and user-profile; the config service keeps its standalone copy, now pinned identical in code to the shared module by an AST comparison. Each error is exactly `{type, loc, msg}` and each service's OpenAPI `ValidationError` component lists only those keys. Before, FastAPI's default put the submitted value in `input`, for a missing field the whole body: a secrets create without `name` returned the plaintext `data`, a webhook registration its signing `secret`, a device create the values of its `field_data`, an execute request its `method_kwargs`; and a lone surrogate in a refused body (`{"name": "\ud800"}` on a secrets create) was a 500. `loc` still names the keys a caller sent, a key inside a secret's `data` included but never its value, and `msg` can still quote part of a value (a UUID's bad character, an inventory dropdown default, an unknown webhook event type); the module docstring lists these. A new repo-root guard, `tests/unit/test_validation_errors_wiring.py`, finds the services from the `services/` directory and fails when an app does not install the handler, when a FastAPI app is built outside `app/main.py`, or when a module registers its own validation handler or replaces an app's `openapi`. Each service gains `tests/test_validation_errors.py` (a missing field, a wrong type, and a lone surrogate through the real app; every body-bearing secrets route), and `tests/integration/test_secrets_flow.py` gains the secrets case through the gateway. Contract note for `/api/v1`: a facade 422 item no longer carries the optional `input` and `ctx` keys, removed within v1 as a security fix (neither was ever required; `type`, `loc`, and `msg` are unchanged); `docs/api/v1-openapi.json` and [EXTERNAL_API.md](docs/EXTERNAL_API.md) say so, and the facade's envelope test now pins the three keys. A reservations validation 422 the facade relays is the same list; a reservations 422 for a reason of its own is still a string `detail`, and some quoted the submitted value (`Unknown purpose_category '<value>'; allowed: <list>`, until #1459 in the next entry). The contract snapshots are unchanged: `ValidationError` is outside their signature. A detail a route builds itself (`HTTPException(detail=...)`) is not rendered by the handler. Also fixed: a custom validator's message that quotes a submitted string can no longer hold a lone surrogate. Pydantic-core cannot build an error whose message holds one; it raises UnicodeEncodeError before any handler runs, so `POST /templates` on inventory with a dropdown `default` (or option) of `"Z\ud800"` was a 500. Inventory's dropdown message now quotes each submitted value with `repr`, so the default reads as before and the options are now quoted (`Default 'x' is not in options: 'a', 'b'`, was `... options: a, b`); integration's unknown-event-type and user-profile's `page_sizes` messages already quoted with `repr` and now have surrogate tests; OPS-VALID-5 states the rule for validator authors. Upgrade note: acl, ai-orchestrator, user-profile, and notifications have no dev mount, so they pick up the handler only from a rebuilt image (`docker compose up -d --build <svc>`, or `make up`, which rebuilds); a `docker compose restart` keeps their old 422.
- Fixed: no HERD service but config accepts a request body holding a lone surrogate any more, so none stores, forwards, or echoes text that cannot be encoded as UTF-8 and then answers 500 (#1456, #1458, #1459, new OPS-VALID-6, OPS-VALID-7, and OPS-VALID-8). A plain `str` field, the keys and values of a `dict[str, str]`, and a value typed `Any` accepted a JSON escape such as `\ud800` with no pair, and the next step that had to encode it failed: a secret stored with one answered 500 on every reveal, the internal reveals execution and inventory use included, and a surrogate in a `data` key came back as U+FFFD; `POST /execute/internal` and reservations' create, purpose-category PATCH, and admin accept answered 500 when they echoed one in a 422; the `/api/v1` facade answered 500 because httpx could not forward one. `herd_common.validation_errors.install_validation_errors` now also installs `RefuseLoneSurrogates`, an ASGI middleware placed innermost. It looks only at a body FastAPI parses as JSON (`application/json` or an `application/*+json` subtype; any other body, or one with no content type, is passed through unread), scans its raw bytes without parsing them, and when the body holds a lone surrogate anywhere answers 422 with exactly one item, `{"type": "string_unicode", "loc": ["body"], "msg": ...}` (pydantic's own type and message), which never names the field: a constrained field that pydantic answered at the field before (an auth password) is now answered at `["body"]` too. A valid surrogate pair is accepted. For valid JSON the scan refuses exactly when any member, also one a later duplicate key replaces, holds a lone surrogate (fuzzed against `json.loads`, 2,600,000 bodies); a body that is not valid JSON but holds a surrogate escape gets this 422 instead of a JSON decoding error. The scan is linear in the body's length with at most two copies of it in memory; measured through an app behind a 401 dependency at 1 to 16 MB, pinned to one core, a structural body it refuses costs 0.01 to 0.10 times the same request without the check (which parses it), a `text/plain` body costs the same, and its worst case, a string made of escaped surrogate pairs, adds 7 to 8 ms per MB, up to 7.6 times the near-zero cost without it; these are observations, not a bound (OPS-VALID-8). It runs on the event loop like FastAPI's own parse, and only a bound on body size, which no service has yet (#1466), limits either. A route that reads its body refuses one before its dependencies run, as FastAPI's own 422 for malformed JSON does, so such a request to an authenticated route is now a 422 rather than a 401 or 403; an unknown route or a route that reads no body is unchanged. Multipart uploads pass through; the inventory and cabling importers check the JSON they parse from an uploaded file themselves and refuse one with a fixed 422 (INV-BULK-22, TOPO-BULK-19), and a CSV row whose JSON cell holds one is rejected on its own. The config service installs its copy without the check, by decision (its passwords keep the OPS-CONFIG-8 answers), and its settings save now refuses one with `{"errors": ["Settings must be valid UTF-8 text"]}` (OPS-CONFIG-31); the wiring guard allows that opt-out to config alone. Because the check runs when a route reads its body, before its dependencies, a surrogate body to an authenticated or role-gated route (the #1399 recovery routes included, IAM-OIDC-29) is answered 422 to any caller, as a body that is not JSON already was. A secret stored with one before this change fails closed: every reveal answers 409 `Stored secret data is not valid UTF-8 text; an admin must replace it with PUT /secrets/{secret_id}` until a PUT replaces its data, never the data altered (OPS-SECRET-19); inventory answers that 409 at hypervisor registration with a 422 instead of a 503, and execution treats it as permanent. Details no longer echo a submitted value outside a fixed set: an unknown `purpose_category` reads `Unknown purpose_category; allowed: <list>` (was `Unknown purpose_category '<value>'; ...`) on create, PATCH, accept, the list filter, and through the facade; the internal execute refusal reads `internal execute is restricted to action='configure'` (was `...; got '<action>'`); and an unknown health `last_status` filter reads `Unknown last_status; allowed: UNKNOWN, HEALTHY, DEGRADED, UNREACHABLE` (was `Invalid last_status: <value>`), each built by the new ONE helper `herd_common.error_details.unknown_value_detail` except the fixed internal-execute text. Also fixed: a cabling topology import file that is not UTF-8 answered 500; it is now inventory's 422 `Import file must be UTF-8 encoded; re-save it as UTF-8 and retry`. Upgrade note: acl, ai-orchestrator, user-profile, and notifications pick up the check only from a rebuilt image (`docker compose up -d --build <svc>`, or `make up`).
- Fixed: in LDAP mode `POST /login` no longer caps the password at 72 characters, the bound bcrypt needs, since in that mode the password goes to the directory and never to bcrypt (#1404, IAM-LOGIN-3, new IAM-LOGIN-6). The bound now follows the mode that judges the password, read when the request arrives: 72 characters in local mode and for the single sign-on break-glass login, exactly as before (the same 422 `string_too_long`, before bcrypt runs), and 1024 in LDAP mode, a ceiling on the request only (1024 characters are at most 4096 UTF-8 bytes, the most hashing takes), with the password handed to ldap3 unchanged; ldap3 prepares it with SASLprep before the bind (IAM-LDAP-13), so a NUL in LDAP mode is now the 401 before any directory call. The schema keeps a `max_length` of 1024, so a lone surrogate is still the 422 `string_unicode` (IAM-LOGIN-5) and a password over 1024 is told 1024 in every mode. The login page's 422 text names both bounds (`frontend/src/lib/login.ts`, `LOGIN_INPUT_REFUSED`). The LDAP test directory gains `uid=ldapit-longpass` with a 100-character password (`infra/ldap-test/ldif/70-seed-integration.ldif`, before the healthcheck's last entry), used by the live LDAP suite and `tests/integration/test_ldap_auth.py`; the live suite's stale-seed probe checks it too, so a directory seeded before it fails with the `make ldap-reset` remedy.
- Fixed: in LDAP mode a directory that cannot be asked no longer answers `POST /login` with 401 `Invalid credentials`, so users are no longer told during an outage that their password is wrong (#1403, IAM-LDAP-5, new IAM-LDAP-12). `ldap_service.bind_user` now raises `LdapUnavailableError` whenever the directory could not be asked (incomplete configuration, a malformed URL, a refused or timed-out connection, a TLS failure, a refused service-account bind, a search that raises, or that found no entry and ended with any result code but success, and a user bind whose connection fails, fails TLS, or gets no answer within 30 s), and the route answers 503 with the fixed detail `Directory unavailable, credentials not checked`; it is still fail closed, with no session, no account created, no local fallback, and no directory text in the body. Every answer the directory gives about the login itself stays the 401: an unknown login name, a user bind refused with any result code (wrong password, disabled or locked account, and busy or unavailable too, since only an existing user reaches that bind), and an entry without the email attribute. A password ldap3's SASLprep (RFC 4013) refuses client-side (a tab, a NUL, another control character, a private-use character, mixed right-to-left and left-to-right text) is the 401 before any directory call, for every username alike (IAM-LDAP-13; the review of this change found that, filed as an outage at the user bind, it answered existing users 503 and unknown ones 401). The login user bind now has the service connection's 30 s receive timeout, so a directory that accepts the connection and never answers ends in the 503 instead of holding a worker thread. The login page already shows `Could not reach HERD. Try again in a moment.` for a 5xx, so the frontend is unchanged. `docs/TROUBLESHOOTING.md` splits "LDAP login fails immediately" into the 503 and 401 causes.
- Fixed: the login page renders no sign-in method until `GET /api/auth/login-options` has answered (#1420, IAM-OIDC-24 and the new rule IAM-OIDC-28, ADR 0016 D10 as amended). While the request was pending the page showed today's page, the password form and the Register link, so an OIDC-mode page flashed a form the mode hides (focus could drop to the page body when it went, a click on Register reached a 409, and a typed password stayed in form state), and a request that never answered left that form up for good. The pending state now shows the card's header, the configuration link, and the unconfigured banner when it applies, with placeholder bars (the `Skeleton` component) in the form's place inside a `role="status"` region whose screen-reader text is `Loading sign-in options...`; the bars take exactly the local-mode form's height, so a local-mode answer does not move the card (measured in Chromium: 0 px), while an OIDC answer shortens it. The request is aborted after `LOGIN_OPTIONS_TIMEOUT_MS` (5 s), which counts as a failure and ends in D10's fallback, today's page. After a failure, and never for a confirmed answer, an alert above the unchanged form says `Sign-in options could not be loaded.` with a `Try again` button that asks again (the pending state, then the answer; each attempt under the same 5 s limit; repeated clicks send one request), so an OIDC-mode user whose answer was slow or who was offline at load is no longer stuck on a password form the server refuses. Text typed into the fallback survives a retry that fails again; a retry that confirms single sign-on keeps the email and drops the password. The request is sent even while the browser reports itself offline (`networkMode: "always"`) and is never refetched in place (focus, reconnect, or retry), because TanStack Query puts a query with no answer back to pending when it fetches again. A confirmed answer stays up when a later background refetch fails. A signed-in visitor is still sent on by `GuestGuard` before the page asks. Visible in every mode: the login form now appears one round trip after the page. E2E: `tests/e2e/test_login_pending_playwright.py` (new) holds the request in the browser (its hung-request case also checks the notice and that Try again recovers), `test_oidc_login_playwright.py` now proves the form and the Register link were never inserted before the button (a MutationObserver from the first byte, `pw_watch_login_page` in `tests/e2e/conftest.py`), and `test_register_and_roles.py` waits for the form after the registration redirect.
- Fixed: `GET /api/auth/admin/ldap-sync/status` answered 500 in OIDC mode (#37, found in the ADR 0016 phase 5b review): its response model kept its own `Literal["local", "ldap"]`. The mode now has one type, `AuthMethod` in `services/auth/app/config.py`, which `Settings`, that model, and `GET /login-options`'s model all use; the admin LDAP sync page's TypeScript type gains `oidc`, and the page keeps mapping create and sync-now disabled in that mode, as in local mode. The OpenAPI contract snapshot is unchanged (it records the field's type, `string`, not its values).
- Tests: an OIDC-mode stack phase (#37, ADR 0016 phase 5b). `make master`, `make everything`, and the nightly run `_gate-oidc-stack-tests` right after the LDAP-mode phase: it connects the Dex lab to the stack network as `oidc-test`, recreates only auth with `AUTH_METHOD=oidc`, the lab settings, break-glass off, and the redirect URI of the gateway address, runs the new suites, and restores local mode in a trap. `tests/integration/test_oidc_auth.py` signs in through the gateway and Dex's form (provisioning, reuse, the single-use handoff, the token accepted by another service, `/register` 409, the seeded superadmin's password login refused, the collision and unverified users refused); the locust class `OidcLoginUser` drives the chain for 30 s and requires every planted expired flow row pruned and one new row per sign-in (everything and nightly only); `tests/e2e/test_oidc_login_playwright.py` signs in through the browser with a dedicated `pw_oidc_browser` that maps `oidc-test` to the lab, logs out, and checks the cancelled and failed messages. Each suite skips unless its variable (`HERD_INTEGRATION_OIDC`, `HERD_LOAD_OIDC`, `HERD_E2E_OIDC`) is set, so local-mode runs never select it; nightly starts and stops the lab and collects its logs with the other diagnostics. Break-glass on is not tested against a running stack, by decision (`docs/GAPS.md`).
- Auth: `AUTH_METHOD=oidc` is accepted (#37, ADR 0016 phase 5b, decided by the owner on 2026-11-14), because the gates and nightly recreate auth in that mode for the stack tests and auth refused to start with a value it did not admit; the mode is not yet supported for operators (the config page's dropdown offers only `local` and `ldap`, though the config service does not check the value, so `oidc` reaches `config.json` from the environment or through a save; the handbook does not describe the mode until phase 6), and since an operator who sets `AUTH_METHOD=oidc` now reaches it before issues #1399 and #1420 are settled, both block the 0.8.0 release (both since settled, above).
- Tests: the OIDC test provider registers the HERD callback of the address the stack is browsed at (#37, ADR 0016 phase 5b; templating decided by the owner on 2026-11-14). `infra/oidc-test/dex.yaml` is now a gomplate template for the static client's redirect URIs, rendered by the Dex image's own entrypoint: `https://localhost/api/auth/oidc/callback` is always registered, and `https://<HERD_GATEWAY_BIND>/api/auth/oidc/callback` is added when that variable, trimmed, is a host other than `0.0.0.0` and `localhost` (an IPv4 dotted quad, a DNS name, or a bracketed IPv6 literal, and nothing else: a value holding whitespace, a newline, a quote, a slash, or a port registers localhost alone), the rule `make gateway-url` follows. The lab's compose file passes `HERD_GATEWAY_BIND` into the container, so no tracked file names a host address. The live suite and `make test-auth-oidc` are unchanged.
- Fixed: the NOS lab's FRR node saves its configuration on Ubuntu 26.04 hosts. `infra/nos-test/docker-compose.yml` now runs the node under `security_opt: [apparmor=docker-default]`, keeping `privileged: true`. A privileged container defaults to AppArmor `unconfined`, and an unconfined process that execs a binary is attached to any host profile whose path matches it; Ubuntu 26.04 (and GitHub's `ubuntu-latest` from 2026-11-16) loads an enforcing `staticd` profile for `/usr/lib/frr/staticd` that grants no write in `/etc/frr`, so every frr_mgmt `write memory` failed with `Can't open configuration file /etc/frr/staticd.conf.XXXXXX.` and the advisory `nos-dialect` job went red on two tests. `tests/unit/test_nos_lab_compose.py` pins the option; docs/NOS_LAB.md, docs/specs/TESTING.md, and docs/TROUBLESHOOTING.md describe the symptom and the check.
- Tests: `tests/unit/test_make_certs_wiring.py` reads the generated certificate's subject and issuer with `-nameopt RFC2253`, so its self-signed check no longer depends on openssl's default name format. GitHub's `ubuntu-latest` moved to the ubuntu-26.04 image (OpenSSL 3.5.5), which prints `subject=CN=HERD dev gateway` where OpenSSL 3.0 printed `subject=CN = HERD dev gateway`, and the old substring count found no match, so the `backend` job failed on an unchanged certificate. The test now requires the subject to be exactly `CN=HERD dev gateway` and the issuer to equal it.
- Chore: NOTICE names the copyright holder as `Copyright 2025-2026 Lane Nelms` in place of `HERD Contributors`; LICENSE is unchanged. The header's Help link is rewritten as its own component, `frontend/src/components/layout/HeaderHelpLink.tsx`, which `AppLayout` renders beside the notification bell. It still opens the published manual (`MANUAL_URL`, #960) in a new tab with `rel="noopener noreferrer"` and the accessible name Help. Its icon is lucide's `CircleHelp`, hidden from assistive technology and drawn at the bell's 20px size, and the link takes the padding, colors, and focus ring of the Logout button. `frontend/src/test/components/HeaderHelpLink.test.tsx` covers its name, address, target, rel, and hidden icon.
- Tests: an OIDC test provider and the live OIDC service suite (#37, ADR 0016 phase 5a). `infra/oidc-test/` runs Dex v2.46.1, pinned by digest, stateless, with a static `herd` client and four fixture users (two normal users, one whose email equals the seeded local `user1@herd.dev`, one with `email_verified` false); it enforces PKCE with S256 only and enables only the authorization-code grant. The issuer `http://oidc-test:5556/dex` is the same for a container on a Docker network (alias `oidc-test`) and for a host-side client that maps the name to the address published on `HERD_GATEWAY_BIND`. New targets `oidc-up`, `oidc-down`, `oidc-status`, and `test-auth-oidc` (hard-required: fails, never skips, when the provider is down), and a `_gate-oidc-tests` phase in `master` and `everything` right after the live-LDAP phase, which starts the provider only when it is not running and stops only what it started. `services/auth/tests/test_oidc_service_live.py` (8 tests) drives HERD's real OIDC code against Dex, each sign-in through Dex's login form over HTTP: discovery, JWKS key selection, a full sign-in through the routes that provisions and then reuses an `oidc` account bound to Dex's issuer and fixed `sub`, a second Dex user who gets a separate account, Dex refusing a wrong `code_verifier` (`token_refused`) with none of Dex's text in any emitted log line, HERD refusing a foreign nonce (`nonce_mismatch`), and the `email_taken` and `email_unverified` refusals. When the port answers with a discovery document that is not the lab's, every test fails with the cause and the remedy. Each test fails under at least one of thirteen mutations of the product code (no PKCE, no verifier, no nonce check, no nonce, the wrong username claim, the wrong subject, an issuer-only identity lookup, refused signing keys, an ignored `email_verified`, no collision check, Dex's error text logged in a message or an extra field). Like the live-LDAP suite it skips in `make test-auth` and ci.yml when no provider answers, and no workflow runs it yet (`docs/GAPS.md`; phase 5b adds nightly). Dependabot tracks `infra/oidc-test`. Measurements of Dex, Keycloak, and mock-oauth2-server are in `docs/specs/TESTING.md`; ADR 0016 D12 records them. No product code changes.
- Build: Dependabot no longer proposes an msw major for the frontend. The `npm` block of `.github/dependabot.yml` ignores `version-update:semver-major` for `msw`, which stays on 2.x by decision: msw 3 intercepts jsdom's XMLHttpRequest at the socket, so handlers can run out of send order, a handler's `request.credentials` can belong to an earlier request on the same connection, and a request still in flight when a test ends can be answered by the next test's handler. Five unit tests and the frontend image build failed on Dependabot #1432; the migration that makes them pass is parked on the closed PR #1434. Minor and patch updates of msw still arrive in the grouped pull request.
- Tests: the nightly Save and Restart step switches IPv6 off on the runner before it runs (#1326 follow-up). The step's first run (2026-11-14) failed although the restart itself worked: the restarter restarted all eleven services in about six seconds and answered 200, but the test's browser runs on the Docker host, each container restart removed and recreated a veth interface with an IPv6 link-local address, and Chromium aborted the in-flight `POST /api/config/apply` with `ERR_NETWORK_CHANGED`, so the Restart Result panel never rendered. With IPv6 off the veth interfaces carry no address and a restart changes none. `docs/specs/TESTING.md` ("Other e2e facts") records the trap. No product code changes.
- Changed: the auth service hashes passwords with bcrypt directly instead of the unmaintained passlib 1.7.4, and bcrypt moves from 4.0.1 to 5.0.0 across the workspace with its `<4.1` cap lifted (#1355; supersedes Dependabot #1393, whose suites failed in passlib's bcrypt self-test). `services/auth/app/services/passwords.py` keeps every decision passlib made, so every stored hash still signs in with exactly the passwords it accepted: a table of 802 (password, hash) decisions recorded by running passlib on the previous commit (`services/auth/tests/data/passlib_1_7_4_fixtures.json`, with its generator) is replayed by `services/auth/tests/test_password_hashing.py`. Only the first 72 UTF-8 bytes of a password reach bcrypt, as passlib cut silently and bcrypt 5 would otherwise refuse: a registration password of up to 72 characters can be longer than 72 bytes and is hashed and checked on its first 72 (the backward-compatible option; refusing over 72 bytes at registration is left to the owner). A password with a NUL character or over 4096 UTF-8 bytes is still refused, `$2a$`, `$2y$`, and `$2$` hashes still verify and `$2x$` still does not, new hashes are still `$2b$` at cost 12, and nothing rehashes on login. Two refusals change status: a login with a NUL in the password, and a login against a stored hash that is not a usable bcrypt hash, answer 401 `Invalid credentials` where passlib raised a 500; the second logs one WARNING without the hash. Every check, refusals included, costs exactly one bcrypt: at cost 12 against the dummy hash for a refusal, at the stored hash's own cost otherwise, which is 12 for every hash HERD wrote (IAM-LOGIN-2 is now pinned at the bcrypt layer). The config service cuts at 72 bytes explicitly too (`config_store._password_bytes`), keeps the 284 decisions its bcrypt 4.0.1 code made (`services/config/tests/test_config_passwords.py`), and answers 401 instead of a 500 for an auth file whose hash is missing or unreadable; its ERROR line about the auth file is logged only when the stored hash is unusable, never because of the password a client sent. The auth pytest configuration drops its ignore for passlib's `'crypt' is deprecated` warning. Specification: identity-and-access section 8.20 (IAM-HASH-1 to IAM-HASH-8), IAM-REG-2, IAM-REG-4, IAM-LOGIN-2; operations-and-observability OPS-CONFIG-6 and OPS-CONFIG-7.
- Changed: the dev, gate, and CI compose override no longer forces the config-page password. `docker-compose.override.yml` set `CONFIG_ADMIN_PASSWORD=admin123!` unconditionally, so a `CONFIG_ADMIN_PASSWORD` in `.env` had no effect on any stack but `make prod`. The override now reads `${CONFIG_ADMIN_PASSWORD:-admin123!}`: unset or empty keeps the documented default, a set value wins. The e2e fixture that seeds a fresh config (`tests/e2e/conftest.py`) logs in with the same variable, as `test_config_playwright.py` and `test_toast_placement_playwright.py` already did. Set the variable on any dev or demo stack that other machines can reach; an existing `herd-config` volume keeps its stored password until the auth file is removed or the password is changed in the page. `README.md` and `.env.example` now say what a blank value means on each kind of stack.
- Frontend: single sign-on in the browser, dark at the time (#37, ADR 0016 phase 4). The login page now asks auth's `GET /api/auth/login-options` which method is active and renders from the answer alone: a `Sign in with {provider_name}` button when `sso` is present (a top-level navigation to `/api/auth/oidc/login`), the password form when `password_login` is true (behind a collapsed `Administrator sign-in` toggle in OIDC mode, where its submit is a secondary button so the page keeps one primary action), and the Register link only when `registration_open` is true. Visible change in LDAP mode: the Register link is gone from the sign-in card, since `/register` answers 409 there; the page itself still opens by its address. A local-mode page is unchanged, markup included, and while the answer is pending or on any error (a backend without the route answers 404, a 5xx, no answer, a body that is not a valid answer) the page is exactly today's (D10); in OIDC mode that means the password form shows until the answer arrives, and the request has no timeout (#1420, which blocks phase 6). An `sso_error` query value shows one fixed message (`cancelled`, `unavailable`, or `failed` for every other value; the raw value is never shown) and is then removed with a replace. The new `/login/sso` page (under `GuestGuard`, outside the app layout) posts `{}` to `/api/auth/oidc/exchange` once per mount, StrictMode included, adopts the returned pair through the same function a password login uses, and lands on the returned `next` only when it is a safe in-app path (else `/topology`), recorded as a return path bound to the signed-in account's `sub`; any failure stores nothing and goes to `/login?sso_error=failed`, and an answer that arrives after the page unmounted (another tab signed in meanwhile) is dropped without a navigation, a token write, or a message. The refresh interceptor's skip list gains `/auth/login-options` and `/auth/oidc/exchange` and now matches each path's end instead of a substring; a recorded return path can no longer name `/login/sso`. Rules IAM-OIDC-24 to IAM-OIDC-27; manual quickstart and troubleshooting pages updated for the LDAP-mode Register link. No OIDC deployment could exist at the time, so nothing else a user saw changed (since phase 5b, above, auth accepts `AUTH_METHOD=oidc`).
- Auth: the single sign-on routes are present and, at the time, dark (#37, ADR 0016 phase 3). `services/auth/app/routers/oidc.py` adds `GET /oidc/login` (302 to the identity provider, setting the `herd_oidc_flow` binding cookie), `GET /oidc/callback` (always a 302: `/login/sso` with the one-time `herd_oidc_handoff` cookie on success, else `/login?sso_error=` `cancelled`, `unavailable`, or `failed` per the ADR's outcome table, failing the claimed flow row), and `POST /oidc/exchange` (the handoff cookie and an empty JSON body for the `POST /login` token body plus `next`, once; 401 `Single sign-on session expired; sign in again` otherwise, also for any unexpected exception after the body is validated). An unexpected exception in any of the three is logged by class name only, never with a traceback or its message. Outside `AUTH_METHOD=oidc`, which `AUTH_METHOD` refused at the time (since phase 5b, above, auth accepts `AUTH_METHOD=oidc`), each answers 404 `Single sign-on is not enabled` before reading any input, so no deployment could reach them then. Visible today: the new public `GET /login-options` answers in every mode, and its `registration_open` is false in LDAP mode, where `POST /register` already answers 409. In OIDC mode `POST /register` answers 409 and `POST /login` refuses every account unless `OIDC_BREAK_GLASS_LOCAL_SUPERADMIN` is true, which admits an active local superadmin only; every answer costs one bcrypt. A new `oidc` account's username follows the owner's option C: the username claim when it fits `^[A-Za-z0-9][A-Za-z0-9._@+-]{0,99}$`, else the email when it does, else the first login is refused as `username_invalid` (or `username_missing` when neither the claim nor the email was present, which the earlier `email_missing` refusal always preempts through the callback); provisioning also refuses any other username as `username_invalid`. `OIDC_REDIRECT_URI` is now invalid when its path holds a character outside letters, digits, `-._~/%`, since the path becomes both cookies' unquoted Path attribute and a `;` there would inject a cookie attribute. The lifespan logs the OIDC startup warnings. Tests: `services/auth/tests/test_oidc_routes.py` (every row of the callback outcome table, every exchange failure, both cookies' attributes, the D13 refusals over HTTP, the catch-alls, and log hygiene over every record), `test_login_options.py`, `test_oidc_break_glass.py`; the log-hygiene check of `test_oidc_service.py`'s `idp` fixture now reads the setup and call records, where before it read `caplog.records` in teardown, which holds only the teardown phase, so it checked nothing. ADR 0016 records option C with its refusal table, what the binding and handoff cookies do not defend against (a tossed `herd_oidc_flow` or `herd_oidc_handoff` from a sibling subdomain signs the victim in to the attacker's account), and the as-built details; the identity-and-access specification gains section 8.19 (IAM-OIDC-15 to IAM-OIDC-23); the auth contract snapshot gains the four routes and four schemas.
- Fixed: an inventory outage costs one read timeout per switch in an allocation release pass, not one per allocation (#1358). Since #1209 the release isolates a failed switch or template read per allocation and continues, but nothing remembered the failure, so each further allocation on an unreachable switch waited out its own 10 second read. Each call of `_release_orphaned_allocations` now reads through its own `_ReleasePassReads` view of the event's fetch context: a read that raised `TransientUpstreamError` is not requested again in that pass, and every later read of it raises the same class with the same arguments, chained to the first. That memory starts empty on every call and is never shared: a failure seen by an apply, or by another reservation in the same retry tick, is not consulted, and the next pass reads again, so a one-off blip never becomes a skipped `delete_vlan` that nothing re-drives. The shared `_FetchContext` still remembers no failed read; it now also caches answered template reads (`get_template`, used by `_run_vlan_definition_op`). Each affected allocation still gets its own `vlan_delete_upstream_failed` line and stays released with its switch listed (WIRE-VLAN-12).
- Security: the gateway-routed config service no longer holds the Docker socket and no longer runs as root (#1326, ADR 0017). Save and Restart now asks a new `restarter` container (`infra/restarter/`, standard library only), over `herd-ctl`, an internal network only the config service and the restarter join. The restarter is the one HERD container that holds the socket; its whole API is a parameterless `POST /restart` and `GET /health`, it has no gateway route and no published port, and it runs read-only with every capability dropped. It restarts exactly the services that read `config.json` (the eleven that mount `herd-config:/etc/herd:ro`, every replica of each, never a `docker compose run` one-off container) within its own compose project, which it takes from `COMPOSE_PROJECT_NAME`; Selenium is no longer restarted. The Restart Result names each service once. One deadline of 660 seconds bounds a whole restart however many containers match, and a container not reached by then is reported as `Failed to restart <service>: PassDeadlineExceeded`. A second restart while one runs answers `A restart is already in progress`. Errors keep the class-name-only wording, plus `Cannot reach the restart service: <Class>`, `The restart service answered HTTP <status>`, and `The restart service answered an unexpected body` from the config side. The config image runs as `appuser`. `tests/unit/test_restarter_compose_guard.py` fails when a service other than the restarter and Traefik mounts the socket or a directory above it, and `tests/unit/test_dockerfile_users.py` fails when a service image's final stage runs as root. Save and Restart's real path now runs as the last test step of the nightly workflow, which checks every restarted service is healthy again. Traefik still holds the socket for discovery; #1424 tracks narrowing it. Upgrade note: rebuild with `make up` or `make prod` (not `make restart`, which swaps no image), then, for a `herd-config` volume an earlier release created, run once `docker compose run --rm --no-deps --user root config chown -R appuser:appuser /data/herd-config` (add `-f docker-compose.yml` under `make prod`, or `-p <project>` for another project). Until then the config service logs the command once at startup, its status and schema still answer, and every save answers 503 `The config data directory is not writable; the config service log names the one-time fix`.
- Fixed: a VLAN allocation is released by exactly one writer (#1357). `_release_orphaned_allocations` set the row RELEASED with a plain ORM write after reading it ACTIVE, so a reservation's L2 apply tail and the retry channels' release of superseded rows could both release one allocation and both drive `delete_vlan`. The release now goes through `vlan_service.release_allocation_if_orphaned`, one UPDATE that matches only while the row is ACTIVE and no ACTIVE membership references it, so a membership whose join commits after the orphan check and before that UPDATE runs also keeps the allocation live. A join still uncommitted when it runs is not seen, because the join side does not check that the allocation is still ACTIVE; that residual is left open. A pass that loses logs `vlan_release_lost_race` at INFO, drives nothing for that allocation, and continues; the winner's deletes start from the row as re-read after the release, so a switch whose definition was committed before that re-read is undefined too. The insert in `find_or_assign_allocation` and this compare-and-swap are now the only status writes on an allocation (WIRE-VLAN-4). `services/execution/tests/test_vlan_release_cas_live_pg.py`, the seventeenth Postgres-live suite, races two real releases on a held row lock and a join against a release on a ledger with foreign rows.
- Fixed: the reservations startup warning and the NATS comments in reservations, execution, and `herd_common.outbox` no longer promise what the code does not do (#1337, RES-EVENT-4, new RES-EVENT-5). A broker unreachable at boot logged `NATS unavailable at <url>, events will be skipped`, but events are never skipped: they wait in the outbox. The warning now reads `NATS unavailable at <url>; events wait in the outbox until the service is restarted with the broker reachable`, and a connect that succeeds while ensuring the `HERD_RESERVATIONS` stream fails, which kept the connection but logged the same broker-down line, now logs `JetStream stream HERD_RESERVATIONS could not be created or updated at <url>; the connection is kept, so events publish if the stream exists, and while it is missing they wait in the outbox and the relay retries them every tick` (an update of an existing stream can fail too, and then publishes succeed at once); docs/OPERATIONS.md lists that line beside `NATS unavailable at ...`, with no restart needed. The comments that said a connection that was None at startup is picked up later now say that nothing connects after a failed connect (only execution's consumer start deferred by the #463 schema gate connects after boot), and execution's health scheduler and `_ensure_health_stream` docstrings no longer say transition events are dropped without NATS or that its unused `nc` argument publishes them. Only log text and comments changed: the one new branch chooses which warning to log.
- Security: the config password can no longer hold a NUL, and the empty password never signs in to the config page (#1429). bcrypt appends a NUL to the key and repeats it, so `POST /change-password` with eight NULs passed the 8-character minimum and then `POST /login` with `""` answered 200 with a session that could also save and restart. One character rule, `password_problem` in `services/config/app/config_store.py`, now decides which strings can be a config password at every entry point, and hashing itself refuses what it refuses: a NUL anywhere, a string that cannot be encoded as UTF-8, and the empty string. Change-password answers 422 `Password must not contain a NUL character`; login answers 401 for an empty password, or one holding a NUL, whatever the stored hash. Upgrade note: an operator whose stored config password holds a NUL (only a crafted API request could set one; a browser field cannot type it) can no longer sign in with it, and recovers by removing `config_auth.json` from the `herd-config` volume, which reseeds from `CONFIG_ADMIN_PASSWORD` or a generated, logged password. A hash stored from a password with NUL separators (`ab`, NUL, `ab`, NUL, `ab`) still matches the NUL-free `ab` until the password is changed. OPS-CONFIG-6, OPS-CONFIG-7, and OPS-CONFIG-8 say so.
- Fixed: `POST /change-password` on the config service no longer answers 500 for a new password holding a lone surrogate; it answers 422 `Password must be valid UTF-8 text` and the stored hash is unchanged (#1428). The route's length refusals are now the same shape, a string `detail` that never holds the submitted password (`Password must be at least 8 characters`, `Password must be at most 32 characters`, same wording as before); they were a validation list that echoed it, and a lone surrogate in that echo could not be rendered at all. `CONFIG_ADMIN_PASSWORD` gets the same rule: a value with a byte that is not UTF-8 seeds nothing, every login is a 401, `/status` reports not rotated, and each `/status` read and each login attempt logs the fixed ERROR `CONFIG_ADMIN_PASSWORD is not valid UTF-8 text, so it cannot be the config password; no auth file written, login refused`, with no arguments and never the value, until the variable is fixed (it was a 500 on `/status` and login). Every other 422 the config service answers is now rendered as exactly `{type, loc, msg}` by a config-local handler (`services/config/app/validation_errors.py`, a standalone copy of the auth service's #1426 handler), so no 422 carries pydantic's `input` or `ctx` and none holds a submitted password (a number or a list where the password belongs, a misspelled key, or a whole body were echoed), and a lone surrogate in `loc` or `msg` is replaced with U+FFFD so the body always encodes: `{"password": ["\ud800"]}` on login and `{"np": "abcdefg\ud800"}` on change-password were 500s. `msg` and `loc` can still quote part of other values or key names. OPS-CONFIG-5, OPS-CONFIG-8, and OPS-CONFIG-30 say so.
- Fixed: the config service's public `GET /status` no longer answers 500 when `config_auth.json` holds valid JSON that is not an object (a list, a string, a number, `null`, a boolean); it answers 200 with `password_changed` false, as it already did for unparseable JSON (#1427). `load_auth` now refuses such a file itself with `ConfigAuthError` and one ERROR naming the class only, so every reader fails closed the same way: login stays a 401, `PUT /settings` and `POST /apply` from a session issued before the file changed answer the rotation 403 instead of a 500, and `POST /change-password` from such a session answers 503 `The config password store is unusable; the config service log names the cause` and leaves the file as it was (an unparseable file was a 500 there too). `password_changed` now counts as rotated only when it is the JSON boolean `true`: a stored `"yes"` or `1` used to unlock the write surface without a rotation. Both files are also read failing closed on every way `json.load` and `open` can fail short of running out of memory: a byte that is not UTF-8, nesting deeper than the parser allows, and an integer over Python's digit limit were 500s on `/status`, login, save, and change-password for `config_auth.json`, and on the settings editor for `config.json`, which now gets the existing corrupt-file answer. OPS-CONFIG-4, OPS-CONFIG-7, OPS-CONFIG-9, and OPS-CONFIG-10 say so.
- Changed: inventory's auth user-group lookups have one home in the service layer, `services/inventory/app/services/auth_groups.py` (#1339). `fetch_user_group_ids` and `fetch_user_group_names` (with `AUTH_GROUP_PAGE_SIZE`) were private helpers of `app/routers/device_groups.py`, and `device_visibility`, which holds inventory's one non-admin visibility path, imported them from that router inside two function bodies to dodge an import cycle. Both now import the module and call through it, so a test patches `app.services.auth_groups.<name>` once for every caller; the function bodies, status codes, and messages are unchanged, and no module under any service's `app/services/` or `app/tasks/` imports from `app.routers`. Test patch targets, inventory.md (INV-VIS-2, INV-VIS-3, INV-GRP-11, INV-GRP-12), CONVENTIONS.md, and ARCHITECTURE.md name the new home.
- Fixed: a driver package upload, file replacement, download, or delete no longer stalls the inventory service (#1327, INV-DRV-19). Inventory runs one uvicorn worker, and `driver_service` called the blocking `services/inventory/app/storage.py` functions (a MinIO round trip, or a local file write, read, or unlink of up to `DRIVER_MAX_SIZE_BYTES`), hashed the upload, and opened its zip or tar on the event loop, so every other request on the replica, `/health` included, waited for it. Each storage call now runs through `asyncio.to_thread`, and the hash and the `driver_metadata.json` read run together in one worker call (`_inspect_package`); every bound and refusal wording is unchanged. `services/inventory/tests/test_driver_storage_offload.py` records whether each blocking call had a running event loop and proves `/health` answers while an upload is held in storage. Because a file replace that keeps its file name reuses the storage key and reads and writes now overlap in threads, a local-storage write (the default, `MINIO_ENDPOINT` empty) no longer truncates and rewrites the stored file in place, which let a concurrent download return a torn archive with a 200: `_write_local_atomically` writes a dot-prefixed `.tmp` file in the same directory and renames it onto the key, so a reader gets exactly one complete version and a failed write leaves the old object and no temporary file (INV-DRV-21, `services/inventory/tests/test_storage_atomic_write.py`). Three behaviors on the same path are now pinned by `services/inventory/tests/test_driver_storage_consistency.py`: a same-name replace keeps the archive it just stored (INV-DRV-12), and a storage failure commits no row on create and leaves the row on the old, still-stored archive on replace (INV-DRV-20).
- Build: Dependabot no longer proposes a Node major for the frontend's build image. The `docker` block of `.github/dependabot.yml` ignores `version-update:semver-major` for `node`, beside the existing Python, Postgres, and NATS ignores, because ci.yml's frontend job runs Node 22 only and a runtime major is moved by hand. `tests/unit/test_dependabot_runtime_majors.py` reads the `docker` and `docker-compose` blocks, finds the images each one tracks in its directories, and fails when a node, python, postgres, or nats image is tracked without a semver-major ignore, or is tracked by no block; `docs/specs/DEVELOPMENT.md` lists the Node major among the hand bumps.
- Docs: accuracy sweep after the 2026-11-11 to 11-13 merges. README.md counts over 11,000 backend unit tests, over 2,400 frontend tests, 273 integration tests, and 221 e2e tests (the current CI and nightly runs), and sixteen Postgres-live suites (the OIDC flow store's joined in #1363), calls the manual searchable (#1298), and links `NOTICE` and the Code of Conduct beside `LICENSE`; docs/specs/DEVELOPMENT.md also counts sixteen Postgres-live suites. PLANNED_FEATURES.md said the OIDC design is ADR 0016 (Proposed) with phases 1 and 2 merged dark and single sign-on not yet usable (updated in phase 5b, above); FEATURES.md mentions the manual search and says local accounts sign in by email. SECURITY.md counts five published advisories, two of them for issues fixed in v0.7.0. The manual's quickstart says a sign-in lands on the Topology page (or the page that sent you to sign in), not the Reservations page; the quickstart and troubleshooting pages no longer say the Register button is greyed out in LDAP mode (it opens the form, and the submit answers `Local registration is disabled; this deployment uses LDAP authentication.`), and the troubleshooting page's signed-out entry describes the shared session across tabs (#1316) and the return to the page after sign-in (#1333) instead of advising to clear local storage and claiming a key rotation forces every user to sign in again; the reservations page names the short id button as the keyboard way into the detail view (#1317); the search index is regenerated. docs/TROUBLESHOOTING.md no longer says the PDF reader has no memory bound (#1202), describes the shared refresh in its expired-token item, and its notifications-schema patch no longer names the `herd` role or grants to it (#1325); docs/OPERATIONS.md says a busy PDF reader answers 503, not 400; docs/USER_GUIDE.md names the **Sign in** button and the Email field's acceptance of a login name (#1335); docs/ADMIN_HANDBOOK.md's role table no longer lists changing your own password, which no route does; docs/GAPS.md no longer says `frontend/src/hooks/` does not exist or that `ErrorBoundary` has no suite of its own (#1344), and counts 71 e2e files and 221 tests.
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
- Fixed: auth refuses a stored bcrypt hash above cost 14 without running bcrypt on it (#1425). `verify_password` checked a well-formed hash at any cost up to 31, at that cost, on the event loop; each step doubles the work, so a cost-31 row (only a direct database write can make one; HERD writes cost 12) held the single-worker auth service for about 31 hours per login attempt, and anyone who knew the row's email could send one. A hash above `MAX_VERIFY_COST` (14, two steps above `BCRYPT_ROUNDS`) in `services/auth/app/services/passwords.py` is now unusable like any malformed hash (IAM-HASH-4): one check against the cost-12 dummy hash, the WARNING `Stored password hash is not a usable bcrypt hash`, and the same 401 `Invalid credentials` as a wrong password, on the local login and the OIDC break-glass login alike. A hash at cost 14 or below verifies at its own cost as before. This is a deliberate departure from passlib parity (IAM-HASH-1); no recorded passlib decision is above the ceiling. The config service's own password check is unchanged. Upgrade note: an account whose row holds a hash above cost 14 (possible only through a direct database write or an import) stops signing in, with that WARNING on each attempt; `docs/TROUBLESHOOTING.md` (One local account always gets `Invalid credentials` after an upgrade) gives the query that finds such rows and the reset.
- Fixed: auth answers 422, not 500, for a password holding a NUL character or a lone surrogate, and no auth 422 carries pydantic's `input` or `ctx`, so none echoes a submitted password (#1426). `POST /register` with a NUL reached `hash_password`, whose ValueError became a 500; a lone surrogate at `/register` or `/login` was refused by pydantic, but FastAPI's default 422 handler echoed the input back and Starlette could not encode it as UTF-8. The password rule now has one definition, `validate_password` in `services/auth/app/services/passwords.py` (no NUL, encodable as UTF-8, at most 4096 bytes), with fixed messages that never hold the password; `hash_password` applies it and `RegisterRequest` runs it as a field validator, so registration refuses exactly what hashing refuses: a NUL is the 422 `value_error` with `Value error, Password must not contain a NUL character`, a lone surrogate the 422 `string_unicode` (IAM-REG-2, IAM-HASH-3). `LoginRequest` is unchanged: a NUL at login stays the 401 `Invalid credentials`, a lone surrogate is now the 422 (IAM-LOGIN-5). Auth installs its own `RequestValidationError` handler (first in `services/auth/app/validation_errors.py`, since moved to `herd_common.validation_errors` by #1455, below): each error is rendered as exactly `{type, loc, msg}`, without pydantic's `input` (which held the password, and for a missing field the whole body) or `ctx`, and any lone surrogate in `loc` or `msg` becomes U+FFFD (IAM-HASH-9); auth's OpenAPI `ValidationError` component now lists those three keys only. `msg` is still the validator's text and can quote part of an email or UUID, and `loc` names the keys a caller sent; no password error does either. The other eleven services kept FastAPI's default handler until #1455, above. The superadmin seed failed startup on such a password at this change, with the fixed message; since #1450, above, it logs one ERROR and startup continues.
- Tests: three e2e corrections after the first seeded browser run of the 2026-11-11 frontend changes (203 passed, 9 failed, all in tests). `tests/e2e/test_purpose_classify_playwright.py` locates the Classify now button by its exact name: a reservation row's button is now named with the reservation's purpose (#1317), and the test's own purpose text, "e2e classify now ...", matched the loose locator. `tests/e2e/test_reservation_detail.py::test_reservation_detail_modal_opens` scopes its title wait to the open dialog: with a document title per route (#1317) the unscoped text match resolved first to the page's `<title>`, which is never visible. `tests/e2e/test_topology_editor_canvas_identity_playwright.py` (new with #1315, never run before) releases its held device batch call through the route handler instead of removing the handler first, which left the route already handled.
- Tests: two e2e assertions corrected after the first browser run of the session and login change (#1316, #1333). `tests/e2e/test_login.py::test_login_failure_shows_error` waited for the old toast text and now waits for "Invalid credentials"; `tests/e2e/test_multi_tab_session_playwright.py` no longer asserts that two access tokens differ, because tokens minted for one account within the same second are identical, and the refresh tokens already prove both rotations. In that run the two-tab test passed every step before that assertion: the second tab refreshed with the rotated token and both tabs stayed signed in.
- Fixed: a refresh in one browser tab no longer signs the user out of the other open tabs (#1316). Each tab read the stored tokens once, at load, so a second tab refreshed with the refresh token the first tab had already rotated, auth refused it (a used token has no grace window), and the second tab cleared the tokens every tab shares. On a 401 the client now decides on what storage holds at that moment: when storage already holds another access token of the same account than the one that was refused, the request is retried with it and no refresh is sent; a stored token of another account is never sent, and the tab is signed out instead. A refresh and a logout hold the Web Lock `herd-auth-refresh` where the browser offers Web Locks (secure contexts), so they run one at a time across tabs; a tab waits for the lock at most 20 s, then goes on without it. A refused refresh polls storage for up to 2 s for another tab's write before it ends the session, with or without the lock (a lock grant and another tab's write are not ordered across processes), so a revoked session signs out up to 2 s later; the shared tokens are cleared only when storage still holds the refused token. A refreshed pair is stored only when storage still holds the token that was sent, otherwise it is revoked: within one tab, and across tabs when Web Locks exist, a refresh in flight never undoes a logout, while without Web Locks a logout landing between that check and the write can still be overwritten. Logout waits for the tab's refresh in flight and revokes the refresh token storage holds inside the lock, not the one in the tab's memory. A refresh that fails without a 401 (no answer, a timeout, a 5xx) keeps the tokens, and the refresh request times out after 15 s. Each tab follows the others' `storage` events, so a login, refresh, or logout in one tab reaches the rest (a logout signs every tab out); a tab that processes another account's login late signs out first and then adopts it. A tab whose storage is blocked, or whose own write to it failed once, works from memory and never adopts a stored pair. Every sign-out, whatever its cause, empties the query cache and resets the preferences store (`frontend/src/queryClient.ts`), so the next account in the tab never sees the previous one's data. `docs/specs/identity-and-access.md` IAM-UI-1 to IAM-UI-4, IAM-UI-11, IAM-UI-12, IAM-UI-15, and IAM-UI-16 state the rules; `frontend/src/test/api/clientMultiTab.test.ts` stages the interleavings with two store and client instances over one storage, `frontend/src/test/sessionReset.test.tsx` walks each path into the signed-out state, and `tests/e2e/test_multi_tab_session_playwright.py` runs two tabs of one browser context against a real stack.
- Fixed: the login page names why a sign-in failed, and a sign-in returns to the page that needed it (#1333). Only a 401 says the credentials are wrong; no answer (including no answer within the new 15 s client timeout), a 404 from the gateway, and any 5xx say `Could not reach HERD. Try again in a moment.`; a 422 names the length limits, a 429 asks for a wait, any other status says the answer was unexpected, and no server text is shown. `AuthGuard` records the path, query, and hash a signed-out visitor asked for, and the account last signed in in the tab, including after a session ends mid-page; the login page and `GuestGuard` go there after sign-in only for the same account (or after a first visit), and only when it is a same-origin path starting with one `/` (no `//`, backslash, scheme, or control character, and not `/login` or `/register`); anything else goes to `/topology`. The inputs carry `autocomplete` `username` and `current-password`, and the icon-only configuration link has an accessible name. IAM-UI-5, IAM-UI-9, and IAM-UI-13 state the rules.
- Fixed: a directory user whose login name has no `@` can submit the login form (#1335). The identifier field was `type="email"`, so the browser refused a plain name in LDAP mode, where the identifier is the directory login name (IAM-LDAP-2). It is now a text field, still labeled `Email` with placeholder `you@example.com` (local accounts sign in by email), with `autocomplete="username"`, `inputmode="email"`, no client-side email check, and the helper line "Directory accounts: enter your directory login name." tied to it with `aria-describedby`; the backend validates the value in both modes, and the request body still sends it as `email`. A wrong password now reads "Invalid credentials. Check your email or login name and your password." The field keeps its `login-email` id, so no e2e selector changed. The page does not yet know the active mode (that waits for `GET /api/auth/login-options`, ADR 0016), so the Register link still shows in LDAP mode. IAM-UI-14 states the rule.
- Fixed: a request refused with FastAPI's default 422 no longer blanks the web app, and a page that fails while rendering no longer takes the header and navigation with it (#1314, #1344). 35 error handlers in 19 frontend files trusted the shape of `response.data.detail`: 25 through the `detail?: string` cast the issue named, and 10 through an untyped axios read, the assistant stream's `fetch` body, or a cast of the detail's own fields (the config save's `errors`, the fork save's `message`, the version restore's `reservations`). They handed a 422's list of objects to `toast.error` or to JSX, which threw, or interpolated it as `[object Object]`; the editor's Save as Template with a name over 100 characters was one such path. Every one now goes through `errorDetail` in `frontend/src/lib/errors.ts`, whose new core `detailText` cuts a plain string to 1000 characters and formats a validation list as `field: message` per entry: the location's non-blank parts joined with dots, without a leading `body`, `query`, `path`, `header`, or `cookie` source unless that source is the whole location (`body: Field required`); at most three entries named, then `(and N more)`; cut to 300 characters; the echoed `input` never shown. Any other non-string shape, an empty or blank string included, is the caller's fallback. `profileSaveErrorText` joins its labelled list through the same bounded helper, and it and `deleteDeviceErrorMessage` fall back for a blank string detail. Three surfaces that showed other error text now show fixed copy: a config version create (`Could not save the config version. Try again.`, never axios's `Request failed with status code 500`), the reporting page's error strip, and the assistant's error and status frames, whose fields are type-checked. A version restore refused with a string detail (a 403, a 404) shows that text, and a blocked 409 whose entries carry no id shows `Restore failed` instead of an unchanged dialog. A `no-restricted-syntax` block in `frontend/eslint.config.js` refuses, outside `lib/errors.ts` and the tests, a `detail` typed `string` inside an `as` cast, a `satisfies` clause, or a type argument, a dotted or computed `.data.detail` access, and destructuring `detail` from `.data`; `src/test/lint/errorDetailLintRule.test.ts` runs the real config over the refused forms, the allowed forms, and the documented misses. The Save as Template name input takes at most 100 characters. `AppLayout` wraps its routed outlet in `RouteErrorBoundary`, keyed on the pathname plus a retry count: a page that throws shows `This page could not be displayed` with Try again, Reload page, and Go to Topology (not on `/topology` itself) under the intact header, Try again remounts the route, and navigating to another route renders it normally. Behavior change: any pathname change, a route parameter alone included, now remounts the routed page, resetting its local state, focus, and scroll position; a query-string or hash change does not. The root boundary in `App.tsx` stays as the last resort, now also holds the `Toaster` (a toast that throws shows the last-resort page instead of unmounting the app), and offers Reload page instead of a Try again that re-rendered the same failure; no fallback shows the error's message any more, it goes to the console only. Three frontend tests that pinned the old fallback for a list detail now expect its text, two now pin the fallback with a list entry that has no `msg`, one with an object, and the root boundary test asserts the thrown message is absent. New tests: a shape table and the string and list bounds in `src/test/lib/errors.test.ts`; `TopologyEditorPage.ErrorText.test.tsx` and `GroupsPage.validationToast.test.tsx` feed a real 422 list through MSW into an inline error and a real Toaster, and pin the restore refusal text; `RouteErrorBoundary.test.tsx` covers containment, the fixed text, Try again, recovery by navigation, and which navigations remount; `App.test.tsx` makes a toast throw. Integration and load tests do not apply; no browser e2e was added.
- Fixed: the topology editor can no longer save one topology's canvas into another (#1315), and the fork commit summary reports the routes a save built and released (#1321). The canvas store is global and nothing cleared it, so opening topology B after A showed A's canvas under B's name until B's hydration returned, and indefinitely when B's fetch failed, with Save enabled the whole time; a Save there wrote A's wiring into B as B's newest version. The page now remounts its editor for every topology id and `reservationId`, so a version preview, restore target, AI proposal, or open dialog never carries over to another canvas; each editor clears the store in a layout effect when it mounts and when it unmounts, and the store clears itself when the session signs out (a token refresh clears nothing). Until the editor's own load lands a "Loading canvas..." status covers the canvas; Save, Reserve, Save as Template, Clear canvas, History, and Use AI are disabled, and drops, new connections, AI proposals, dragging, selecting, and the Delete key are refused, so an edit made during the load is never silently replaced. A request token guards the load's deferred writes and every later wholesale canvas replacement (version preview, exit, and restore, and the fork history's), so one that resolves after its editor unmounted, including a late fork hydration after leaving live edit, is dropped. A failed topology fetch outside live edit shows "Topology not found" (404) or "Could not load this topology" with a way back to the list, and no canvas. The commit toast adds a `Routes released N, built M` line from the save's `l3_routes_released` and `l3_routes_built` when either is above zero; a response without them reads as 0. `frontend/src/test/pages/TopologyEditorPage.CanvasIdentity.test.tsx` drives twenty sequences without resetting the store between steps (A then B cached and uncached, in place and through the list page; B's fetch failing with 503 and 404; late hydrations after navigating to C, to an empty topology, after unmount, and after leaving live edit; A, B, and back to A; a version preview on A followed by B, and one resolving after it; the same topology between plain mode and live edit; a drop during the load; a remount; logout then another user; a new empty topology; entering live edit; StrictMode's double mount), and `ForkSaveResultToast.test.tsx` covers L1-only, L2-only, routes-only, mixed, all-zero, and older responses. `tests/e2e/test_topology_editor_canvas_identity_playwright.py` holds the device batch call with `page.route`, opens A, moves to B in the app, and reads both topologies back after a Save. Integration and load tests do not apply.
- Fixed: list rows and the Administration menu work from the keyboard (#1317, WCAG 2.1.1 and 4.1.2). On Inventory, Templates, Topology, User Groups, and Device Groups the name cell is a link to the item, and on Reservations the short id is a button that opens the detail dialog, named from the id, the action, and the purpose cut to 60 characters (`11111111, open reservation: fw test`); the row click stays as a mouse convenience through `rowClick` (`frontend/src/lib/rowNavigation.ts`), which ignores clicks on the link, checkboxes, and row actions, so the link never navigates twice. The Administration menu is a disclosure button with `aria-expanded` and `aria-controls` that opens on click, Enter, or Space, lists ordinary links reached with Tab, and closes on Escape (focus returns to the button), an outside click, focus leaving it, or a route change; hover still opens it, a click while hover-opened keeps it open, and the mouse leaving a hover-opened panel that holds keyboard focus keeps it open instead of dropping focus to the page. Its eleven links come from one array, `ADMIN_NAV` in `frontend/src/lib/adminNav.ts`, and `frontend/src/test/routes.test.tsx` fails when that array and the AdminGuard route group disagree; which role sees which link is unchanged. The layout gains a skip link and a `<main>` landmark around the routed page, and each page under the layout sets `document.title` (`Inventory - HERD`) from `ROUTE_TITLES` in `frontend/src/lib/routeTitles.ts`, restoring the original title when the layout unmounts. Two keyboard gaps outside list navigation remain: the device rows in the reservation edit-devices dialog and the expandable device rows in the reservation inventory tab are still click-only.
- Fixed: every dialog is named by its own title (#1318). `Modal`, `ConfirmDialog`, and `BulkImportExport` hardcoded their title ids (and `ConfirmDialog` its description id), and closed dialogs stay mounted, so pages with several dialogs (the topology editor, the device config section, one `ConfirmDialog` per reservation row) had duplicate ids and each `aria-labelledby` resolved to the first title in the document; the ids now come from `useId()`. The Create Connection modal on the admin Connections page still mounts only while open, now for the reasons that remain (a fresh form per open, and no hidden copies of its device search inputs or Cancel button for a first-match e2e locator to land on); its comment no longer cites the shared id. The e2e tests that selected `#modal-title`, `#confirm-dialog-title`, or `#bulk-import-title` now assert the open dialog's accessible name with `to_have_accessible_name`. The unused `open_admin_menu` helper in `tests/e2e/conftest.py` is removed: nothing called it, and its wait also matched the always-visible Templates link.
- Auth: the single sign-on flow store and federated identities, present and dark at the time (#37, ADR 0016 phase 2). Migration 0010 adds the `oidc_login_flows` and `federated_identities` tables; `services/auth/app/services/oidc_flow_service.py` begins, claims, completes, fails, and exchanges a sign-in flow, each step one conditional update on the expected status, storing only SHA-256 hashes of the state, binding, and handoff secrets and sanitizing the return path; `resolve_federated_user` and `create_oidc_user` in `auth_service.py` provision `auth_source="oidc"` accounts keyed by (issuer, subject) and refuse, without linking, any login whose email or username another account holds, compared case-insensitively and, on Postgres, under advisory locks so the refusal also holds between concurrent first logins; it returns an account or nothing for any claim content, refusing a NUL, control, format, or unpaired surrogate character, surrounding whitespace, or a malformed email with a fixed reason. The flow store requires 43-character state, binding, and handoff secrets, and its prune skips locked rows and never fails a sign-in. No route called any of it and `AUTH_METHOD` accepted only `local` and `ldap` at the time, so nothing an operator could reach changed (since phase 5b, above, auth accepts `AUTH_METHOD=oidc`). Tests: unit suites for the flow store (every status crossed with every operation, live and expired), provisioning (every collision against local, LDAP, and OIDC accounts), and the migration (create_all and the migration build the same tables), plus `test_oidc_flow_service_live_pg.py` in the gates' Postgres-live phase for the races. `docs/specs/identity-and-access.md` gains rules IAM-OIDC-1 to IAM-OIDC-14.
- Manual: search across the published manual (#1298). Every page of `docs/manual/` has a search box in its header (`/` focuses it) whose results list the page, the section heading, and a snippet with the matched words marked, each linking to the section's anchor. Every query word is required, the last one as a prefix, case-insensitively, and a dotted version such as `0.3.0` is one word that ranks its own release page first; otherwise heading matches rank above body matches, and earlier release pages rank after current pages. Release pages are labeled as release notes. The index is prebuilt: `scripts/build_manual_search_index.py` (stdlib only, `make manual-index`) writes `docs/manual/assets/search-index.js`, 240,744 bytes for 23 pages and 189 sections, as a script assigning a global; no page loads it until the reader first focuses or types in the search box, when `manual.js` adds it as a script element, so search also works from `file://` and no request is made while a query runs. Results are built from text nodes, never parsed as markup, and the result count is announced through a live region. The UI mockups (`.shot-stage`) are not indexed. The 33 glossary entries without an id gained one, and the seven step headings of `admin-setup.html` that its sidebar links are sections of their own. The header nav now collapses behind the menu toggle at 980 px instead of 720 px: between those widths its links, the wordmark, and the search box do not fit on one row of the bar (on main the links already spilled out of the bar from 724 to 844 px). `tests/unit/test_manual_search_index.py` fails when the committed index is stale, when a page loads the index itself or does not load `manual.js`, when an index anchor is missing from its page or a sidebar target is not an index anchor, or when an id repeats on a page; `frontend/src/test/manual/manualSearch.test.ts` tests the matching and ranking function and the rendered results; `tests/e2e/test_manual_search_playwright.py` drives a real browser against a local static server, with no stack, including a header sweep from 721 to 1100 px. Integration and load tests do not apply: the manual is static pages with no service surface.
- Docs: accuracy sweep after v0.7.0. SECURITY.md names v0.7.0 as the latest release; README.md counts eight locust user classes (`DeviceConfigUser` joined in #1100); docs/ENV_VARS.md no longer says the bound-address certificate warning lasts until #1239 lands, and docs/GAPS.md aims its certificate check at the certificate `make certs` writes instead of a committed CA; docs/USER_GUIDE.md places the Inventory search and filters in the left filter panel (#957); docs/ARCHITECTURE.md, docs/DRIVERS.md, docs/AI_GENERATE.md, and docs/TROUBLESHOOTING.md describe the 1 MiB tar header bound as the header check #1279 made it, not a per-read bound; and `docs/specs/identity-and-access.md` section 13 records the three documents issue #1010 named as corrected instead of still disagreeing. Three older errors found on the way: docs/ADMIN_HANDBOOK.md no longer tells the superadmin to change a password HERD cannot change, docs/GAPS.md names the auth health route `/api/auth/health`, and `docs/specs/DEVELOPMENT.md` gives the gate stack's address as the gateway URL rather than localhost.
- Tests: `tests/unit/test_manual_release_pages.py` guards the manual's release pages against facts that expire when the next release ships. It fails when a page in `docs/manual/` does not carry exactly one release link in its header, pointing at the newest release page (by integer version) with the text `Release X.Y.Z`; when a release page's sidebar does not list every release page newest first with exactly one entry, its own, marked active; when a demoted release page's dek does not link the release immediately after it; or when a demoted release page contains one of four known expiring phrasings ("The current release is", "now describes", "now documents", "The version this manual documents"). It knows only those four phrasings and judges no other prose. `docs/specs/DEVELOPMENT.md` now states the release-page rule and the three edits a release makes to the old manual pages, and `docs/specs/TESTING.md` lists the guard.
- Tests: `test_wiring_changed_reconcile.py::test_stale_wiring_writer_does_not_clobber_concurrent_winner` no longer fails about once in sixty runs at its step-2 check that a writer is mid-drive (#1238). The cause was the background retry tick, not the product: when the tick fetched the switch before the test armed the slow fail knob, it held the FAILED row's claim with the unarmed, fast context, the test's one manual retry answered `in_progress`, and nothing drove the row again inside the 6 s poll. Step 2 now fires the manual retry again while no login run is in flight and the latest call answered `in_progress` for the row, at most once per 0.25 s poll and only within 10 s of the first call, each call restarting the 6 s login watch; every call is awaited, and the check still fails when no writer logs in, now listing each retry answer for the row.
- Tests: the e2e harness makes the next login timeout diagnosable (#1266, still open; these changes name the flake, they do not remove it). The failure-artifact hook in `tests/e2e/conftest.py` now also writes for a SETUP-phase failure, under a `setup-` prefixed directory: the five files for every page or driver created during that test's setup (`pw_page` and the Selenium fixtures register theirs), else for what the test's resolved fixtures hold (`pw_page`, the open pages of `pw_contexts`, or a Selenium driver), else traceback, an empty console log, and a meta.txt that says no page or driver was registered or resolved for the test; the registry is emptied after every phase. Every page.html has the value of each password input redacted, `ADMIN_PASSWORD` and the password the login helpers receive print as `'[redacted]'` in traceback arguments and locals, and the Selenium console log is now captured (the sessions ask for the browser log and the hook reads it with the getLog command, which `webdriver.Remote` needs; before, every Selenium console.log was empty). `pw_login` clicks inside `expect_response` on `POST /api/auth/login`, fails at once with the status and the password-redacted body on a non-200, and only then waits for /topology with `wait_until="commit"`; `_do_login` names a login form that never rendered, and a post-login timeout names the page's URL and the login response statuses the page received. The `logged_in_browser` and `admin_browser` fixtures register `driver.quit` as a finalizer before they log in (and `browser` before anything else can raise), so a login that raises no longer leaks one of the Selenium container's three sessions. `tests/unit/test_e2e_failure_artifacts.py` pins all of it with fakes, autospecced Selenium mocks, and one inner pytest run in a subprocess under `--tb=short` and `--tb=long -l`, no browser.
- Docs: accuracy sweep after the 2026-11-09 merges. docs/TROUBLESHOOTING.md and docs/ENV_VARS.md no longer say e2e failure artifacts are written only for a call-phase failure: they name the `setup-` directories, and TROUBLESHOOTING.md says page.html has password values redacted and that the Selenium console log is now read (it no longer calls it best-effort and possibly empty). docs/MANUAL_TESTING.md no longer implies the conftest's Selenium fixtures leak a session when a login raises. `docs/specs/DEVELOPMENT.md` says the release-page rule takes precedence over the older manual briefs' Part B. `docs/specs/TESTING.md` lists the `nos-dialect` job among the per-PR CI jobs and no longer calls the repo-root `tests/unit/` suite small or limits it to the seed script, the drivers, and the fetch-retry helper.
- License: `LICENSE` is now the verbatim Apache License 2.0 text. The previous file was headed Apache 2.0 but differed from the canonical text (section 4 carried MIT-style grant wording, section 8 said "exemplary" where the license says "consequential", and the appendix was missing), so GitHub reported the license as Other. The project has always been offered under Apache 2.0, as the README states; the copyright line moves to a new `NOTICE` file.
- Fixed: one VLAN allocation whose `delete_vlan` cannot read its switch or template from inventory no longer stops the release of the allocations after it. A `TransientUpstreamError` from that read escaped the orphaned-allocation release, so the allocation in hand stayed released with its `defined_switch_ids` untrimmed and every later orphaned allocation in the pass stayed `ACTIVE` with no memberships, at the tail of each reservation's L2 apply (the reconcile nacked, the terminal teardown stopped before L3) and in the L2 retry's release of superseded rows. The release now catches that error per allocation: the switches whose delete succeeded before it leave `defined_switch_ids`, the failing switch and the ones not yet tried stay listed, the pass goes on with the next allocation, and one line is logged at `ERROR` with action `vlan_delete_upstream_failed` (`error_class`, `vlan_assignment_id`, `reservation_id`, `vlan_id`, `switch_ids`; the class name only, never the error's text), and by owner decision the allocation is released rather than kept `ACTIVE` with the definition left on those switches never re-driven (keeping a switch in a known-good state is the L2 driver's job, docs/DRIVERS.md). Two retry results change with it: a manual retry of a superseded row in that situation now answers `superseded` where it answered 503, and when the failing read is at the tail of one reservation's L2 apply inside the retry tick, that reservation is no longer logged with `wiring_retry_reservation_upstream_failed` (its rows were already driven and `skipped_upstream` stays 0; the line logged is `vlan_delete_upstream_failed`). The retry tick's `wiring_retry_superseded_release_upstream_failed` catch stays as a backstop that no `TransientUpstreamError` reaches today. Any other exception still propagates (#1209).
- Auth: the first pieces of OpenID Connect single sign-on are present and dark at the time (ADR 0016 phase 1, #37); no route used them and `AUTH_METHOD` refused `oidc` then, so nothing an operator could reach changed (since phase 5b, above, auth accepts `AUTH_METHOD=oidc`). `herd_common.jwt_codec.decode_id_token` is a second named decode beside `decode_access_token`: one asymmetric algorithm from the nine in `ID_TOKEN_ALGORITHMS` (never `none`, never HS, so an HMAC token keyed by the client secret or the public key is refused before any decode), exact `iss`, `aud` membership, `exp`, `iat`, `iss`, `aud`, `sub`, and `nonce` required, and a 60 s leeway for ID tokens only (an owner decision; HERD's access tokens keep 10 s). `tests/unit/test_jwt_settings_wiring.py` admits it as a second `DIRECT_DECODE_ALLOWED` entry and admits the one header read that picks the provider's key as the first `UNVERIFIED_READ_ALLOWED` entry. The auth service gains `app/services/oidc_service.py` (discovery and JWKS cached an hour and never on failure, an unknown signing key refetching the JWKS at most once a minute, the PKCE S256 authorization URL, the `client_secret_basic` code exchange, and ID-token verification with the `azp`, nonce, subject, key-suitability, and RFC 9068 `typ` rules, an RSA provider key under 2048 bits refused, a 10 s overall deadline on each provider request, and only `OidcUnavailableError` or `OidcRejectedError` raised for anything the provider sends) and the twelve `OIDC_*` settings with their compose lines, `.env.example` lines, and a docs/ENV_VARS.md section; `OIDC_BREAK_GLASS_LOCAL_SUPERADMIN` is a field only until phase 3. `pyjwt[crypto]` is now auth's runtime dependency and common's dev dependency (`cryptography` was already locked). `herd_common.logging.setup_logging` now sets the `httpcore` logger to INFO in every service, so its DEBUG lines, which carry outbound response headers, stay out of the log even at `LOG_LEVEL=DEBUG`.

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
