# Decision: OpenID Connect Single Sign-On, Issue #37

Status: Proposed 2026-11-11. Scope set by the owner: this ADR first, then OIDC only
(authorization-code flow with PKCE, just-in-time users, config service, login UI),
built in the phases below; SAML 2.0 is deferred to a follow-up issue. Thirteen
decision points are listed under Decision, each marked DECIDED-BY-PRECEDENT (with the
precedent cited) or DECIDED BY THE OWNER. The five owner decisions (D1, D5, D7, D10,
D12) were put to the owner as open questions in the first draft and decided by the
owner on 2026-11-11, each as recommended; "Owner decisions" at the end lists them. The
status stays Proposed until phase 6 ships and this record is amended as built. No code
in this doc. Context verified against the HERD-public tree at `d27a9973` on
2026-11-11.

## Context

Issue #37 asks for a third authentication method that brokers login to an external
identity provider (IdP), provisions federated users just in time, keeps HERD the
authority for role and group membership, and leaves every downstream service
unchanged. The issue's audit comment (2026-07-31) adds two corrections: `api_token`
is already a third `auth_source` value, and the config service's bootstrap schema and
`GET /status` are missing work items.

Relevant existing fabric, verified:

- Mode switch: `auth_method: Literal["local", "ldap"] = "local"`
  (`services/auth/app/config.py:41`), one backend for every login.
  `authenticate_user` (`services/auth/app/services/auth_service.py:520-523`) routes
  every `POST /login` to `_authenticate_ldap` in LDAP mode, so a local account,
  the seeded superadmin included, cannot log in there (IAM-LDAP-1, IAM-BOOT-5 in
  `docs/specs/identity-and-access.md`). `POST /register` answers 409 in LDAP mode
  (`services/auth/app/routers/auth.py:34-38`, IAM-REG-1).
- Break-glass today: in LDAP mode no login yields a superadmin session
  (IAM-USER-9). The documented paths are a superadmin API token minted before the
  switch and exchanged at `POST /tokens/exchange`, a pre-switch refresh token, or
  switching `AUTH_METHOD` back to `local` in the config editor
  (`docs/ADMIN_HANDBOOK.md`, bootstrap gotcha). Assigning `superadmin` through the
  API is refused (IAM-USER-3).
- JIT precedent: `_authenticate_ldap` (`auth_service.py:440-517`) resolves the HERD
  row by the directory EMAIL, provisions through `create_ldap_user`
  (`auth_service.py:261-308`: `auth_source="ldap"`, `hashed_password=None`, role
  user, `full_name=username`), refuses an email owned by a non-LDAP account
  (`auth_service.py:480-489`, IAM-LDAP-9), and refuses a username collision without
  retry or rename (`auth_service.py:465-479`, IAM-LDAP-10). Login never updates a
  stored username or role (IAM-LDAP-8). The comment at `auth_service.py:444-454`
  records that login fails closed where sync repairs, by design.
- `auth_source`: `String(16)`, default and server default `local`, no CHECK
  constraint (`services/auth/app/models/user.py:48-50`); `email` is unique
  `String(255)` and `username` unique `String(100)` (`user.py:39-40`). The only
  values written to the column are `local` (`auth_service.py:241`) and `ldap`
  (`auth_service.py:288`). `api_token` is NOT a column value: it is an
  `auth_source` CLAIM on access tokens minted by an API-token exchange
  (`services/auth/app/services/api_token_service.py:101-108`). The new value must be
  distinct from all three regardless.
- Tokens: `create_tokens_for_user` (`auth_service.py:526-544`) mints the access JWT
  (`sub`, `username`, `email`, `role`, signed by `create_access_token` in
  `services/auth/app/utils/jwt.py:15-20`) and an opaque refresh token stored only as
  its SHA-256 hash. `rotate_refresh_token` (`auth_service.py:547-604`) consumes a
  refresh token with ONE guarded UPDATE that re-asserts not-revoked, not-expired,
  and user-active (`auth_service.py:584-600`); `revoke_refresh_token`
  (`auth_service.py:607-618`) is the logout. `UserResponse`
  (`services/auth/app/schemas/auth.py:65-78`) does not expose `auth_source`.
- One-decode rule: every access-token decode is
  `herd_common.jwt_codec.decode_access_token`
  (`services/common/herd_common/jwt_codec.py:33-41`, 10 s leeway
  `ACCESS_TOKEN_LEEWAY_SECONDS`). The repo-root guard
  `tests/unit/test_jwt_settings_wiring.py` refuses a PyJWT decode anywhere but
  `DECODE_HELPER` (line 182) or a `DIRECT_DECODE_ALLOWED` entry (lines 186-194, one
  today: the config service's `_verify_token`), refuses any `algorithms=` that is not
  a one-element list literal or any decode with signature verification off, and
  refuses `get_unverified_header` outside `UNVERIFIED_READ_ALLOWED` (line 197, empty
  by design). An ID-token verifier therefore REQUIRES a guard change, whatever library
  is chosen; a library that decodes without PyJWT (authlib's own JOSE code) would
  simply be invisible to the guard.
- Dependencies (`uv.lock`): `authlib` is absent. `pyjwt` 2.15.0 is locked with no
  extra requested by anyone, so RS256 verification is not possible in auth today:
  `cryptography` 50.0.0 is in the lock only through `herd-secrets`, `paramiko`, and
  `pdfminer-six`. `httpx` 0.28.1 is already a runtime dependency of auth
  (`services/auth/pyproject.toml`). `respx` 0.22.0 is locked (a reservations dev
  dependency). Auth tests run with `filterwarnings = ["error"]`
  (`services/auth/pyproject.toml`).
- Config service: the bootstrap schema offers `AUTH_METHOD` options `local` and
  `ldap` only (`services/config/app/config_schema.py:71-85`) and refuses keys outside
  `SCHEMA_KEYS` (`config_schema.py:547`). `GET /status`
  (`services/config/app/main.py:88-93`; the audit comment's 74-79 has drifted)
  returns only `configured` and `password_changed`; its consumers are
  `useConfigStatus` (`frontend/src/api/config.ts:39-49`, read by
  `frontend/src/pages/LoginPage.tsx:14-16`), the frontend tests
  `frontend/src/test/api/config.test.tsx`, `frontend/src/test/pages/LoginPage.test.tsx`,
  `frontend/src/test/pages/ConfigPage.test.tsx`, and the e2e helpers
  `tests/e2e/conftest.py` (`_ensure_config_seeded`),
  `tests/e2e/test_config_playwright.py`, `tests/e2e/test_toast_placement_playwright.py`.
  The config service is `herd_common`-free (CONVENTIONS, Config service), reads its
  own copy of the schema keys from its environment (`load_env_values`,
  `services/config/app/config_store.py:180-187`; `docker-compose.yml:18-21` passes
  `AUTH_METHOD` and the LDAP keys to it), and a UI save writes `config.json`, which
  outranks env from then on (OPS-SET-1; `docs/ENV_VARS.md` has the ladder).
- Frontend tokens: access and refresh tokens live in `localStorage`
  (`frontend/src/stores/authStore.ts:14-35`, IAM-UI-1) and travel as a bearer header
  added by the axios instance (`frontend/src/api/client.ts`), whose refresh
  interceptor skips only `/auth/refresh` and `/auth/login`. `useLogin`
  (`frontend/src/api/auth.ts:36-45`) stores a `TokenResponse` with `setTokens`. The
  login page always renders a password form and a Register link
  (`LoginPage.tsx:56-101`); it does not know the active method.
- Gateway: Traefik routes `PathPrefix(/api/auth)` to auth and strips the prefix
  (`docker-compose.yml:240-245`), so auth never sees its public path or host. Traefik
  runs with `--accessLog=true` (`docker-compose.yml:119`), whose default format
  records the request URI with its query string. HERD's own request log records the
  path only (`services/common/herd_common/logging.py`, `RequestLoggingMiddleware`).
  On this project's dev hosts the gateway binds `HERD_GATEWAY_BIND` (the wired NIC,
  `https://192.168.1.27`), and the dev certificate's SAN covers only localhost
  (issue #1239).
- Log hygiene: `JSONFormatter` redacts `extra=` keys matching `_REDACT_KEY_PATTERN`
  (`herd_common/logging.py:56-61`: password, secret, token, cookie, credential, and
  others; NOT code, state, nonce, or verifier). Upstream text goes to the log message,
  never to a response; error responses carry fixed details (CONVENTIONS, Auth and
  identity, the `MAPPING_DIRECTORY_UNAVAILABLE_DETAIL` rule; Driver packages, log
  extras).
- Shared helpers to reuse: `herd_common.ttl_cache.SingletonTTLCache` and `Uncached`
  (a failed fetch is never cached, issue #1075); `herd_common.advisory_lock` is not
  needed (no singleton work here).
- LDAP test lab precedent: `infra/ldap-test/docker-compose.yml` (pinned image,
  stateless, LDIF fixtures, health check probing the last seeded entry); Makefile
  targets `ldap-up`, `ldap-down`, `test-auth-ldap` (hard-required live suite,
  `Makefile:828-856`), `_gate-ldap-tests` (`Makefile:862-870`), and
  `_gate-ldap-stack-tests` (`Makefile:1180` onward: refuses when a UI-saved
  `config.json` would shadow its env overrides, recreates ONLY the gate auth service
  in LDAP mode, runs `tests/integration/test_ldap_auth.py` and
  `test_ldap_sync_admin.py`, then restores local mode). `nightly.yml` runs
  `make ldap-up` and the same stack phase (`.github/workflows/nightly.yml:93-124`).
  Per-PR CI runs only `backend`, `frontend`, the advisory `integration`, and the
  advisory `nos-dialect` jobs (`.github/workflows/ci.yml`).
- E2E vantage points: Selenium drives a browser inside the stack network at
  `E2E_BASE_URL` (default `https://traefik`, `tests/e2e/conftest.py:56`); Playwright
  runs on the host against `E2E_HOST_BASE_URL` (`conftest.py:60`, the `pw_browser`
  and `pw_page` fixtures at lines 802-895, `ignore_https_errors=True`). New e2e tests
  use Playwright (`docs/specs/TESTING.md`, End-to-end tests). The LDAP browser test
  `tests/e2e/test_ldap_login.py` is an exempt skip on every gate stack (its
  `seeded_skip_ok` reason says the LDAP-mode phase covers integration, not e2e).

## Decision

### Shape of the flow (summary)

All new HTTP routes live on the auth service; nothing downstream changes.

1. The login page asks auth `GET /login-options` (public) which method is active.
2. The "Sign in with" button (the provider's display name follows) navigates the
   browser to `GET /api/auth/oidc/login`. Auth creates a flow row (state, nonce, PKCE verifier, safe `next` path, a hash of
   a browser-binding secret), sets the binding secret as an HttpOnly cookie, and
   answers 302 to the IdP's authorization endpoint (`response_type=code`,
   `code_challenge_method=S256`, `response_mode=query`).
3. The IdP redirects to `GET /api/auth/oidc/callback?code=...&state=...`. Auth
   checks the binding cookie against the flow row, claims the row with one
   compare-and-swap, exchanges the code (with the verifier and the client secret),
   verifies the ID token, resolves or provisions the user, writes a one-time handoff
   secret's hash to the row, sets the handoff secret as a second HttpOnly cookie, and
   answers 302 to the SPA route `/login/sso`. No token and no secret ever appears in
   a URL HERD produces.
4. The SPA page at `/login/sso` calls `POST /api/auth/oidc/exchange` (same origin;
   the browser attaches the handoff cookie), which consumes the handoff with one
   compare-and-swap and answers the same `{access_token, refresh_token, token_type}`
   body `POST /login` answers, plus `next`. The SPA stores the tokens with the
   existing `setTokens` and navigates to `next`.

From step 4 on, the session is an ordinary HERD session: same JWT, same refresh
rotation, same logout.

### D1. Library: hand-rolled on httpx plus PyJWT, not authlib (DECIDED BY THE OWNER)

Decided by the owner on 2026-11-11, as recommended: hand-roll the relying-party flow
(discovery, authorization URL, code exchange, JWKS) on `httpx`, and verify the ID
token with PyJWT through a second named decode helper in `herd_common.jwt_codec`.
Tradeoff: about 300 lines of protocol code we own, against a new
dependency whose JOSE verification the repository's one-decode guard cannot see.

Options considered:

| Dimension | authlib | hand-rolled, httpx plus PyJWT |
|---|---|---|
| New dependency | yes (`authlib`, not in `uv.lock`) | no; `pyjwt[crypto]` on auth adds `cryptography`, already locked at 50.0.0 |
| Fit with `test_jwt_settings_wiring.py` | its own JOSE decode, invisible to the guard; the guard's one-decode invariant would hold only on paper | one more named decode site the guard sees and pins |
| Fit with `filterwarnings = ["error"]` | unknown deprecation surface across a large package | only code we own plus PyJWT, already under the setting |
| Protocol coverage | broad (many grant types we do not need) | exactly code flow with PKCE S256 for one confidential client |
| Error and log hygiene | library exceptions carry IdP text; every catch must map them | we choose fixed details and class-name-only logging at each step |

Implementation notes the phases must follow:

- `herd_common.jwt_codec` gains `decode_id_token(token, key, algorithm, *, issuer,
  audience)`: one `jwt.decode` call with `algorithms=[algorithm]` (a one-element
  list, as the guard demands), `issuer=`, `audience=`,
  `leeway=ID_TOKEN_LEEWAY_SECONDS` (60 s, see D8), and `options={"require": ["exp", "iat",
  "iss", "aud", "sub", "nonce"]}`. The guard gets one `DIRECT_DECODE_ALLOWED` entry
  for it (count 1, with a reason) and one `UNVERIFIED_READ_ALLOWED` entry for the one
  function that reads the header's `kid` and `alg` before key selection. Placing the
  helper in `jwt_codec.py` keeps every PyJWT decode in one reviewed module beside
  both leeway constants; the guard's `DECODE_HELPER` stays the access-token helper.
- Keys come from the JWKS through `jwt.PyJWKSet` (needs `cryptography`), never
  through `jwt.PyJWKClient`, which fetches with blocking `urllib` and has its own
  cache we would not control.
- `pyjwt[crypto]` becomes auth's runtime dependency and common's dev dependency (the
  common tests sign RS256 tokens); `uv lock` once.

### D2. Flow state lives in a database table, consumed by compare-and-swap (DECIDED-BY-PRECEDENT)

Precedent: refresh tokens are server-side, stored as SHA-256 hashes, and consumed
single-use by one guarded UPDATE (`rotate_refresh_token`,
`auth_service.py:584-600`); every ledger in the repository is a forward-only CAS
writer (CLAUDE.md constraints; CONVENTIONS, Reservations).

New table `oidc_login_flows` in the `auth` schema:

- `id` UUID PK
- `state_hash` Text unique (SHA-256 of the 32-byte random `state`, through the
  existing `hash_token`)
- `binding_hash` Text (SHA-256 of the 32-byte random browser-binding secret)
- `nonce` Text (random, compared verbatim to the ID token's `nonce`)
- `code_verifier` Text nullable (PKCE verifier, 43 to 128 characters; set to NULL
  when the callback claims the row)
- `next_path` Text (validated, see D11)
- `status` String: `pending`, `claimed`, `authenticated`, `exchanged`, `failed`
- `user_id` UUID nullable, FK `users.id` ON DELETE CASCADE (same schema)
- `handoff_hash` Text nullable unique
- `created_at`, `expires_at` (pending rows: 600 s), `handoff_expires_at` (60 s)

State machine, forward only, every edge one guarded UPDATE whose WHERE re-asserts the
expected status and the expiry:

| From | To | Writer | Guard |
|---|---|---|---|
| (none) | `pending` | `GET /oidc/login` | insert |
| `pending` | `claimed` | callback, BEFORE the code exchange | `state_hash` and `binding_hash` match, `status='pending'`, `expires_at > now` |
| `claimed` | `authenticated` | callback, after ID-token verification and user resolution | `status='claimed'`; sets `user_id`, `handoff_hash`, `handoff_expires_at` |
| `claimed` | `failed` | callback, any failure after the claim | `status='claimed'` |
| `authenticated` | `exchanged` | `POST /oidc/exchange` | `handoff_hash` match, `status='authenticated'`, `handoff_expires_at > now`; the user is re-checked active in the same statement (the correlated-subquery form `rotate_refresh_token` uses) |

Replay protection follows from the table: a replayed callback loses the
`pending` to `claimed` CAS (zero rows), so the IdP code is never redeemed twice by
HERD; a replayed ID token cannot match because its `nonce` belongs to an already
claimed row; a replayed handoff loses the `authenticated` to `exchanged` CAS.
Multi-replica safety: no state is held in process memory, so any replica can serve
any step.

Pruning: `GET /oidc/login` deletes, in its own transaction before the insert, rows
whose `expires_at` is more than one hour past (a bounded `DELETE ... WHERE id IN
(SELECT ... LIMIT 500)`), so the table stays bounded without a background loop.
Rejected alternatives: server memory (lost on restart, wrong replica after a load
balancer hop); a signed cookie holding state, nonce, and verifier (no begin-time
write, but it needs a second signing format keyed off `AUTH_SECRET_KEY` and still
needs a consumed-state table for replay, so it saves nothing at the callback).

### D3. Tokens reach the SPA through a one-time handoff cookie and a same-origin POST (DECIDED-BY-PRECEDENT)

Precedent: the SPA's whole session model is bearer tokens in `localStorage`
(IAM-UI-1, `authStore.ts`) obtained from a JSON `TokenResponse` (`useLogin`,
`api/auth.ts:36-45`); the refresh interceptor depends on it (IAM-UI-2). The OIDC path
therefore ends in the same JSON body, not in a different session model.

- Never in a URL: no token, handoff secret, or HERD-generated code appears in a
  query string or fragment, so nothing lands in Traefik's access log, browser
  history, or a Referer header.
- The callback sets `herd_oidc_handoff` (HttpOnly, Secure, SameSite=Strict,
  Max-Age 60, Path = the public prefix derived from `OIDC_REDIRECT_URI`, see D11) and
  answers 302 to `/login/sso`. SameSite=Strict is correct here: the exchange is a
  same-site fetch from the SPA page, which carries Strict cookies regardless of how
  the page was reached.
- `POST /oidc/exchange` takes an empty JSON body (a cross-origin form cannot send
  `application/json` without a CORS preflight, which auth's CORS policy refuses for
  foreign origins), consumes the handoff (D2), clears the cookie, and answers
  `OidcExchangeResponse`: `TokenResponse` plus `next`. Failure answers 401 with the
  fixed detail `Single sign-on session expired; sign in again`.
- Rejected: tokens in the redirect URL (query or fragment); moving HERD tokens into
  HttpOnly cookies (a rewrite of the SPA's auth model, the interceptor, and every
  service's bearer handling, out of scope).

### D4. Identity key is (issuer, sub); collisions refuse; new auth_source `oidc` (DECIDED-BY-PRECEDENT)

Precedent: LDAP never converts or takes over a non-LDAP account (IAM-LDAP-9), refuses
a username collision outright (IAM-LDAP-10), and never updates a stored identity at
login (IAM-LDAP-8).

- Key: the pair (`iss`, `sub`) from the verified ID token, stored in a new table
  `federated_identities` (`id` UUID PK; `user_id` UUID FK `users.id` ON DELETE
  CASCADE, unique; `issuer` Text; `subject` String(255); unique (`issuer`,
  `subject`); `created_at`; `last_login_at`). A separate table, not columns on
  `users`, so the SAML follow-up can add NameID-keyed rows without another `users`
  migration. Email is NOT the key: it is mutable at the IdP and IdPs disagree on
  whether they verify it.
- `auth_source` value: `oidc` (fits `String(16)`; distinct from `local`, `ldap`, and
  the `api_token` claim). No column migration is needed for the value itself
  (`user.py:48-50` has no CHECK constraint).
- First login (no federated row): require the configured email claim (default
  `email`; an absent email refuses, as an LDAP entry without email refuses,
  IAM-LDAP-5) and refuse when `email_verified` is present and false. If any `users`
  row has that email compared case-insensitively, refuse: the local or LDAP account is
  never linked or converted, and the federated user sees the generic failure. The
  username follows the owner's option C (an owner decision, amended into this record
  in phase 3): the configured claim (default `preferred_username`) when it matches the
  ASCII allowlist `^[A-Za-z0-9][A-Za-z0-9._@+-]{0,99}$` in full (1 to 100 characters,
  a letter or digit first), else the email when it matches, else the first login is
  refused; the match is a full match, so a trailing newline, which `$` alone would
  admit, does not pass, and no non-ASCII look-alike ever does. (The first draft took
  the claim when present, the email only when the claim was absent, and refused a
  claim longer than 100 characters; option C falls back to the email instead.) The
  refusal reason follows whether any candidate was present (supervisor decision in
  the phase 3 review), where present means a string, which `VerifiedIdToken` keeps
  only when it is non-empty and free of control characters and lone surrogates:

  | Username claim | Email | Username | First login |
  |---|---|---|---|
  | fits | present | the claim | proceeds to the next check |
  | absent, or present and not fitting | fits | the email | proceeds to the next check |
  | present, not fitting | present, not fitting | none | refused `username_invalid` |
  | absent | present, not fitting | none | refused `username_invalid` |
  | any | absent | the claim when it fits, else none | refused `email_missing` |

  The email checks run before the username checks, so an email they refuse (absent,
  unverified, malformed, too long) is refused with its own reason whatever the
  username; that is why the last row never reaches a username reason, and why
  `username_missing` (no candidate present at all) is never the reason through the
  callback and remains only for a direct caller of `resolve_federated_user`. The
  callback sets `FederatedLogin.username_unusable` whenever it found no username but a
  candidate was present: the third and fourth rows, and the last row with a claim
  that does not fit, where the email refusal comes first.
  A returning login never reads the username. If the chosen username is taken,
  refuse, no retry, no rename. Otherwise create the user (`auth_source="oidc"`,
  `hashed_password=None`, role `user`, `full_name` from the `name` claim truncated to
  200 characters or the username) and the federated row in ONE transaction, then
  auto-assign "Not Grouped" through `_auto_assign_not_grouped` like every new account
  (IAM-REG-5). The email refusal also covers another `oidc` account, the case of an
  IdP that reissued a subject; recovering such a user is an admin action outside v1.
- Returning login: the federated row decides; the stored email, username, role, and
  full name are never updated at login (IAM-LDAP-8 analog). `last_login_at` is
  written.
- A federated row whose user is not `auth_source="oidc"` cannot exist (both rows are
  created together); the resolver still refuses one defensively.
- Migration shape: one Alembic revision `0010_oidc_sso.py` under
  `services/auth/migrations/versions/` creating both tables, following the file
  layout of `0004_auth_source.py` and `0009_user_profile.py` (`_schema` from
  `DB_SCHEMA`, a full `downgrade`). No data migration. New tables only, so the
  startup-read rule of issue #1261 (IAM-BOOT-6) is unaffected.
- Linking an existing local or LDAP account to an IdP identity is out of scope for v1
  (see Out of scope).

### D5. `AUTH_METHOD=oidc` is an exclusive third mode; break-glass is an opt-in local superadmin login (DECIDED BY THE OWNER, break-glass part)

The exclusive mode itself is DECIDED-BY-PRECEDENT: `auth_method` is one global
switch (`config.py:38-41`, IAM-LDAP-1), and in OIDC mode `POST /login` refuses and
`POST /register` answers 409 (IAM-REG-1 analog). Side-by-side local plus OIDC is
rejected for v1: it doubles every refusal rule and the login page's states for no
issue-stated need.

Break-glass was decided by the owner on 2026-11-11, as recommended: a new setting
`OIDC_BREAK_GLASS_LOCAL_SUPERADMIN` (default `false`). When false, `POST /login` in
OIDC mode answers 401 `Invalid credentials` for every account, after running bcrypt
against the dummy hash (IAM-LOGIN-2), so a refusal takes the same time as a wrong
password. When true, `POST /login` in OIDC mode accepts exactly one class of account:
`auth_source="local"`, role `superadmin`, `is_active` true, correct password; every
other account (a local admin or user, an `oidc` or `ldap` account, an unknown email)
gets the same 401 `Invalid credentials` with the same timing equalization. Auth logs
one WARNING at startup whenever the setting is true in OIDC mode, so an operator who
leaves it on sees it. Tradeoff: it reopens a
password surface in an SSO deployment (default off keeps IAM-LDAP-1 semantics
exactly), but under OIDC the IdP is often a third-party service HERD's operators
cannot repair, and the LDAP-era break-glass (a superadmin API token minted in advance)
needs foresight that a fresh OIDC deployment cannot have: with the switch off, a fresh
OIDC install has no way to promote its first admin short of switching back to `local`.
The rejected alternative was to mirror LDAP exactly and document only the API-token
path. The API-token path and the pre-switch refresh token still work as in LDAP mode.

Refresh and API-token exchange keep ignoring the mode (IAM-USER-9), so existing
sessions and API tokens survive an IdP outage until they expire.

### D6. HERD stays the authority for role and groups; no claim mapping in v1 (DECIDED-BY-PRECEDENT)

Precedent: the issue's goals and out-of-scope list; ADR 0011, Blast radius ("Role
assignment never syncs; HERD stays the authority for role") and Out of scope ("SSO/OIDC
claims as a sync source"); IAM-SYNC-18. OIDC users are provisioned as `user` and
promoted inside HERD by a superadmin (IAM-USER-2). First-superadmin bootstrap stays
the env seed (IAM-BOOT-1); under OIDC the seeded account is local, so it can sign in
only through D5's break-glass switch or an API token. Optional claim-to-role mapping
and group claims are out.

### D7. Session lifetime is HERD's refresh rotation; logout is local only (DECIDED BY THE OWNER, on RP-initiated logout)

DECIDED-BY-PRECEDENT for the lifetime: the issue says to carry the federated session
into HERD's refresh rotation. HERD requests no IdP refresh token (no
`offline_access` scope), stores no IdP token, and never calls the IdP after the
callback. A HERD session lives `AUTH_ACCESS_TOKEN_EXPIRE_MINUTES` per access token
and up to `AUTH_REFRESH_TOKEN_EXPIRE_DAYS` (default 7) through rotation, exactly as a
local session. Deactivation: `is_active=False` refuses the callback (generic failure),
the exchange (D2's guarded UPDATE), and every refresh (existing guard). An account
disabled at the IdP keeps working in HERD until its refresh token expires or an admin
deactivates it in HERD; SCIM and back-channel logout, which would close that gap, are
out of scope per the issue.

RP-initiated logout through the IdP's `end_session_endpoint` was decided by the owner
on 2026-11-11, as recommended: not in v1, and it stays under Out of scope, consistent
with issue #37, which keeps IdP-driven logout out of the first iteration. HERD logout
stays local (`POST /logout` revokes the HERD refresh token, IAM-UI-4). Tradeoff: after a HERD logout, "Sign in with SSO" may
re-enter silently while the IdP session lives, which surprises a user on a shared
machine; doing it right needs the ID token kept as `id_token_hint` and a registered
post-logout redirect URI, and is a clean follow-up.

### D8. Discovery and JWKS: cached, rotation-aware, pinned algorithm, fail closed (DECIDED-BY-PRECEDENT)

Precedents: the single-algorithm decode (IAM-JWT-5), the 10 s leeway (IAM-JWT-6,
issue #1186), fail-closed login on an unreachable upstream (IAM-LDAP-5), the shared
TTL cache that never caches a failure (`herd_common.ttl_cache`, issue #1075), and the
in-repo HTTP client timeouts of 5 to 15 s (for example
`services/cabling/app/services/reservation_guard.py`, `services/acl/app/services/auth_client.py`).

- Discovery: `GET {OIDC_ISSUER}/.well-known/openid-configuration`; the document's
  `issuer` must equal `OIDC_ISSUER` byte for byte (no trailing-slash normalization),
  `response_types_supported` must contain `code`, and when
  `code_challenge_methods_supported` is present it must contain `S256` (absent is
  accepted: several IdPs support PKCE without advertising it). Every endpoint it names
  must be `https` unless `OIDC_REQUIRE_HTTPS=false`.
- Caching: discovery and JWKS each in a `SingletonTTLCache` with a fixed 3600 s TTL;
  a failed fetch returns `Uncached` and is retried on the next request. On an unknown
  `kid`, the JWKS is refetched at most once per 60 s (a fixed floor, so a token with a
  random `kid` cannot drive one IdP fetch per request), then the token is refused.
- Algorithm: `OIDC_ID_TOKEN_ALGORITHM`, a `Literal` of RS256, RS384, RS512, PS256,
  PS384, PS512, ES256, ES384, ES512 (default RS256), refused at Settings load like
  `AUTH_ALGORITHM` (IAM-JWT-4). Never `none`, never an HS algorithm (an HS ID token
  keyed by the client secret is not supported). The header's `alg` must equal it and
  the selected JWK's `kty` must fit it, before the decode.
- Clock skew: `ID_TOKEN_LEEWAY_SECONDS`, 60 s, in `herd_common.jwt_codec`, not a
  setting. Decided by the owner on 2026-11-11: 60 seconds for ID tokens, because the
  provider's clock is not HERD's and the nonce already prevents replay. HERD's own
  access tokens keep `ACCESS_TOKEN_LEEWAY_SECONDS` (10 s). (The first draft reused
  the 10 s constant.)
- HTTP: one `httpx.AsyncClient` per call with `httpx.Timeout(10.0, connect=5.0)`
  (5 s to connect, 10 s for each read or write) and an overall 10 s deadline on each
  request, body included, through `asyncio.timeout` (corrected in phase 1: httpx's
  timeouts bound each operation, not the request, so a server that drips bytes
  never trips them), `follow_redirects=False`, TLS verification on, `OIDC_CA_CERT` as the trust bundle
  when set (the `LDAP_CA_CERT` precedent). A test seam replaces the transport
  (`httpx.MockTransport`), the module-attribute seam pattern of
  `services/integration/app/services/destination.py` (`resolver`); no new test
  dependency.
- Fail closed: an unreachable or invalid discovery document, JWKS, or token endpoint
  refuses the login. `GET /oidc/login` and the callback are browser navigations, so
  they answer 302 to `/login?sso_error=unavailable` (never a JSON 503 the user would
  see raw); the cause is logged with the exception class in `extra` and the upstream
  text, truncated, in the message.

### D9. Client secret in env and the config schema; log hygiene by rule and by test (DECIDED-BY-PRECEDENT)

Precedent: `LDAP_BIND_PASSWORD`, auth's credential to its own upstream, is an env
setting and a `secret: True` schema key (`config_schema.py:111-119`). The secrets
service (ADR 0003) is rejected for this: auth would gain a login-time dependency on a
service that itself trusts auth's JWTs, and its reads are ACL-gated per resource,
designed for device credentials. A UI save writes the secret into `config.json` in
plaintext, exactly as it does `LDAP_BIND_PASSWORD` today; recorded as a consequence,
not changed here.

Log and response rules for every OIDC module:

- Never logged, in a message or `extra`: the client secret, the authorization code,
  `state`, `nonce`, the PKCE verifier, the ID token, the IdP access token, the binding
  or handoff secret. `_REDACT_KEY_PATTERN` does not match `code`, `state`, `nonce`, or
  `verifier`, so key-name redaction is not a safety net here; the flow id (UUID) is
  the correlation key.
- IdP error text (`error_description`, a token-endpoint error body) is logged in the
  message only, truncated to 200 characters; it never reaches a response or a URL.
  As built (phases 1 and 3) the rule is stricter: no provider text is logged at all.
  The callback ignores `error_description` and logs only whether `error` was
  `access_denied`; the protocol module logs the RFC 6749 error code only when it is a
  registered one.
  The browser sees one of three fixed `sso_error` codes: `cancelled` (IdP
  `error=access_denied`), `unavailable` (D8), `failed` (everything else: bad token,
  collision, inactive account, expired flow).
- Exceptions are logged by class name in `extra` (the `log_unusable_key` and
  class-name-only precedents).
- A caplog-based unit test drives every failure branch and asserts that none of the
  secret values above appears in any record (the shape of the #1252 profile-log rule).

### D10. Config service gets the schema; the active method comes from auth's `GET /login-options`, not config's `GET /status` (DECIDED BY THE OWNER, on the source of the active method)

DECIDED-BY-PRECEDENT for the bootstrap schema (ADR 0011 phase 5 added its keys;
the issue audit names the gap): `AUTH_METHOD` gains the option `oidc`, and a new
`OIDC` group carries every setting in the Settings section below, `OIDC_CLIENT_SECRET` as
`type: "password"`, `secret: True`. The config service's compose `environment:`
block gets the same keys so `bootstrap_from_env` can copy them; the compose wiring
guard does not cover the config service (CONVENTIONS, Config service), so this is
checked by hand and by a config-service test that every `OIDC_*` schema key has a
compose line.

Where the login page learns the active method was decided by the owner on
2026-11-11, as recommended: from a new public `GET /login-options` on AUTH, answering
from auth's live Settings (shape in the API surface section below); config's
`GET /status` stays unchanged. This departs, deliberately, from the wording of
issue #37 ("Surface the active method through the config-status path the frontend already
consults") and from its audit comment, which lists a `GET /status` change as a work
item; that work item is closed by this decision, not done. The reason is staleness.
Tradeoff: one more request on the login page, but it is the only answer
that cannot lie. The config service can compute only what the config ladder SAYS, not
what auth RUNS: a saved but unapplied `config.json` would report the new method early,
and `_gate-ldap-stack-tests` already recreates only auth with `AUTH_METHOD=ldap`
while config keeps `local`, so a config-sourced answer is wrong in our own gate today.
It also keeps the herd_common-free config service from growing a second copy of the
precedence ladder. `registration_open` lets the page hide Register in LDAP mode too,
where `/register` already answers 409 while `LoginPage.tsx:96-101` still shows the
link; that LDAP-mode change ships with phase 4 and is visible as soon as phases 3 and
4 are both merged, ahead of phase 6, by decision (it removes a link that can only
fail). The frontend treats any error from
`GET /login-options` as local mode (the password form, today's behavior): the backend
enforces the mode regardless, so this fallback widens nothing.

### D11. Redirect URI, `next`, CSRF, and the gateway address (DECIDED-BY-PRECEDENT)

Precedent: host-derived URLs are never trusted in HERD (Traefik strips the prefix and
auth sees no public host, `docker-compose.yml:240-245`); fail-closed parsing at
boundaries (CONVENTIONS throughout).

- `OIDC_REDIRECT_URI` is configured, never derived from `Host` or
  `X-Forwarded-Host`. It must be absolute, `https` (unless `OIDC_REQUIRE_HTTPS=false`),
  carry no query or fragment, and its path must end in `/oidc/callback`; the cookie
  Path for both cookies is that path minus `/callback` (normally `/api/auth/oidc`), so
  auth needs no separate public-prefix setting. Amended in phase 3: the path must also
  hold only RFC 3986 unreserved characters, `/`, and `%`, because it becomes the
  cookies' Path attribute, which Starlette writes unquoted, so a `;` there would end
  the attribute and inject another (a `Domain`, say); any other character makes the
  redirect URI invalid (`oidc_configuration_problems`).
- `next` (query parameter on `GET /oidc/login`) is stored in the flow row, never sent
  to the IdP. Accepted only when it is at most 512 characters, starts with exactly
  one `/` (not `//`, not `/\`), contains no backslash or control character, and
  `urllib.parse.urlsplit` gives an empty scheme and netloc; anything else becomes
  `/topology`, silently. The SPA navigates to it with the router, a second same-origin
  bound.
- CSRF and login CSRF on the callback: `state` must match a `pending` row AND the
  `herd_oidc_flow` cookie (HttpOnly, Secure, SameSite=Lax, Max-Age 600) must hash to
  that row's `binding_hash` (constant-time compare). Lax is required: the callback
  arrives as a top-level cross-site GET from the IdP, which carries Lax cookies but
  not Strict ones; `response_mode=query` keeps it a GET. An attacker who sends a
  victim a callback URL carrying the attacker's own code and state fails the binding
  check in the victim's browser.
- What the binding cookie does and does not defend against (added in phase 3). It is
  host-only (no `Domain` attribute), so no sibling host is sent it, and HttpOnly, so
  no script reads it. It defends against an attacker who cannot write a cookie that
  the victim's browser sends to HERD's host: the attacker's callback URL, opened in
  the victim's browser, arrives with no binding cookie or one that hashes to another
  row, and the claim fails. It does not defend against an attacker who can write
  such a cookie, for example a sibling subdomain under the same registrable domain
  setting `herd_oidc_flow` with `Domain=` the parent domain (cookie tossing), or a
  network attacker answering a plain-HTTP request to such a host where HSTS does not
  cover it. That attacker begins a flow, plants its binding secret in the victim's
  browser, and sends the victim the callback URL with the attacker's own code and
  state; the claim succeeds and the victim is signed in to the attacker's account
  (login CSRF). When the browser holds both the victim's own cookie and a tossed one,
  auth reads the last `herd_oidc_flow` in the `Cookie` header (Starlette's parsing),
  whose order the browser sets (longer Path first, then older first), so a tossed
  cookie can also make the victim's own sign-in fail. The handoff cookie has the same
  limit by the same mechanism, with no flow cookie and no callback in the victim's
  browser: the attacker signs in at the provider in their own browser, takes the
  `herd_oidc_handoff` secret from their own callback's `Set-Cookie` without
  exchanging it, tosses it into the victim's browser with a Path that covers
  `/oidc/exchange`, and sends the victim to `/login/sso` within the handoff's 60 s.
  That page's exchange is a same-origin request from HERD's own page, so the browser
  sends the tossed cookie whatever its SameSite, and the victim is signed in to the
  attacker's account: the same login-CSRF class, open on the same condition. Neither
  variant gives the attacker the victim's account or tokens: both HERD cookies are
  host-only and HttpOnly, so the victim's own binding and handoff secrets never reach
  a sibling host or a script, and the exchange answers tokens only to a JSON request,
  which a foreign origin, a sibling subdomain included, cannot send without the CORS
  preflight auth refuses unless `CORS_ORIGINS` names that origin. The `__Host-` cookie prefix would make the
  browser refuse a tossed cookie, but it requires `Path=/`, which the path scoping
  above rules out; a deployment that shares its registrable domain with hosts it does
  not trust should treat login CSRF as open.
- When the callback carries an `iss` parameter (RFC 9207), it must equal
  `OIDC_ISSUER`.
- Gateway address: the IdP registration and `OIDC_REDIRECT_URI` must use the address
  users browse to. On this project's dev hosts that is the `HERD_GATEWAY_BIND`
  address (`https://192.168.1.27/api/auth/oidc/callback`), never localhost; the
  certificate warning of issue #1239 applies to the callback like any other page.

### D12. Test IdP and the five QA levels (DECIDED BY THE OWNER, on the IdP)

Decided by the owner on 2026-11-11, as recommended: Dex (`ghcr.io/dexidp/dex`, pinned
by digest) as the containerized IdP, in a checked-in `infra/oidc-test/` lab that
mirrors `infra/ldap-test/`, with the measure-first step kept: phase 5a measures the
three candidates and proves Dex against the issuer-reachability mechanism and PKCE
and nonce enforcement before phase 5b builds on it. If Dex fails any of those, phase
5a stops and returns the choice to the owner with the measurements; it does not
switch IdPs on its own. Tradeoff:
Dex is a real OIDC provider (its PKCE, nonce, and signing behavior are the behavior we
must interoperate with) with a real login form for the browser test, at a fraction of
Keycloak's footprint; it is less configurable per test than a mock, which is fine
because every negative case runs at the unit and functional levels against locally
signed tokens.

| Candidate | For | Against |
|---|---|---|
| Keycloak | most realistic, every feature | heaviest image and slowest start of the three (to be measured in phase 5a before it is ruled out on numbers), realm-import JSON to maintain |
| Dex | real provider, small and fast to start (to be measured), static-password login form, YAML config | fixed users per config file; claim names to confirm (`preferred_username` versus `name`, settable through `OIDC_USERNAME_CLAIM`) |
| navikt/mock-oauth2-server | programmable claims per request, fast | a mock: whether it enforces PKCE was not verified for this ADR, and a lenient IdP hides exactly the defects the live tier exists to catch; acceptable fallback only if phase 5a confirms PKCE and nonce enforcement |

Issuer reachability is the hard constraint: the issuer URL must be identical for the
auth container (discovery, token, JWKS) and for the browser. Recommended mechanism,
proved in phase 5a: Dex joins the gate stack network with the alias `oidc-test` (the
`docker network connect` step `_gate-ldap-stack-tests` uses for `ldap-test`) and
publishes its port on `${HERD_GATEWAY_BIND}`; the issuer is
`http://oidc-test:5556/dex`; the host-side Playwright browser maps the name with the
Chromium switch `--host-resolver-rules=MAP oidc-test <gateway bind address>` in a
dedicated `pw_oidc_browser` fixture (the shared `pw_browser` stays untouched). The
lab runs plain HTTP, so the gate phase sets `OIDC_REQUIRE_HTTPS=false`, the
`LDAP_USE_TLS=false` precedent of the LDAP lab; auth logs a startup warning whenever
that setting is false in OIDC mode (the `LDAP_TLS_VALIDATE` precedent; clarified in
phase 1: outside OIDC mode the setting is inert, so no warning fires there).

Levels, matching the existing harnesses:

| Level | What | Where | Runs |
|---|---|---|---|
| 1 Unit | ID-token verification (every claim rule, every algorithm refusal, `kid` rotation and the 60 s refetch floor, `azp`), discovery validation, `next` sanitizer, flow-row state machine, provisioning and collision rules, settings validation, log hygiene; `decode_id_token` in common | `services/auth/tests/test_oidc_service.py`, `test_oidc_flow_service.py`, `test_oidc_provisioning.py`, `test_oidc_settings.py`; `services/common/tests/test_jwt_codec.py`; `tests/unit/test_jwt_settings_wiring.py`; frontend vitest for `LoginPage`, `OidcCompletePage`, `api/auth.ts` | per PR (`backend`, `frontend`) |
| 2 Functional | the full redirect chain through the real auth app (`GET /oidc/login`, callback, `POST /oidc/exchange`, then `GET /me` and `POST /refresh`) against an `httpx.MockTransport` IdP that signs with a test RSA key, plus every rejection over HTTP; a live suite against Dex; a Postgres-live race suite | `services/auth/tests/test_oidc_routes.py`; `services/auth/tests/test_oidc_service_live.py` (hard-required under `HERD_TEST_OIDC_REQUIRED=1`); `services/auth/tests/test_oidc_flow_service_live_pg.py` (two concurrent claims of one state, two concurrent exchanges of one handoff, exactly one winner each) | mock suite per PR; live suites in the gates and nightly |
| 3 Integration | gate auth recreated in OIDC mode against Dex: first login provisions `auth_source=oidc`, second login reuses the row, the minted JWT authorizes a call on another service with no new trust config (the issue's acceptance criterion), `/register` 409, `/login` refused, `GET /login-options` | `tests/integration/test_oidc_auth.py`, gated by `HERD_INTEGRATION_OIDC=1` | `_gate-oidc-stack-tests` in master, everything, nightly |
| 4 Stress and load | a locust `OidcLoginUser` that drives the HTTP redirect chain including Dex's form post, then refreshes; asserts zero 5xx and that the flow table stays bounded by pruning | `tests/load/locustfile.py`, a class enabled only with `HERD_LOAD_OIDC=1`, short fixed run inside the OIDC stack phase | everything and nightly (master runs no load phase) |
| 5 Browser e2e | Playwright: SSO button shown and password form hidden in OIDC mode, Dex login, landing on `/topology`, `GET /auth/me` read-back of the provisioned user, logout, a cancelled login showing the `cancelled` message; no new Selenium tests (TESTING.md: new e2e is Playwright; Selenium's in-network browser would also need the issuer mapped inside the Selenium container) | `tests/e2e/test_oidc_login_playwright.py`, an exempt skip (`seeded_skip_ok`) on local-mode stacks, run explicitly by the OIDC stack phase | `_gate-oidc-stack-tests` and nightly |

Not per PR: the live levels, exactly as LDAP's. Adding Dex to the advisory per-PR
`integration` job is a possible later step once the gate phase has a flake record.

### D13. OIDC security checklist (DECIDED by this design, item by item)

| Threat or check | Closed by |
|---|---|
| Authorization-code injection | PKCE S256 per flow (verifier never leaves auth) plus the browser-binding cookie on the callback (D11) |
| PKCE downgrade | HERD always sends `code_challenge_method=S256`; discovery advertising methods without S256 refuses (D8); the confidential-client secret is a second factor on the code exchange even against an IdP that ignores PKCE |
| Login CSRF and session swapping | `state` bound to a server row AND to the browser's HttpOnly binding cookie (D11) |
| State and handoff replay | single-use CAS transitions (D2) |
| ID-token replay and nonce binding | per-flow random `nonce`, required claim, compared to the claimed row; the row is consumed once (D2) |
| Token substitution (an ID token issued to another client) | `aud` must contain `OIDC_CLIENT_ID`; when `aud` has more than one value, `azp` is required; when `azp` is present it must equal `OIDC_CLIENT_ID` |
| Issuer validation | exact match of the configured issuer, the discovery `issuer`, the token `iss`, and the RFC 9207 `iss` parameter when present (D8, D11) |
| IdP mix-up | one configured IdP per deployment, plus the issuer checks above; multi-IdP is out of scope |
| Algorithm confusion, `alg: none`, HS with the client secret | one pinned asymmetric algorithm, the guard's one-element list, `kty` check (D8) |
| Signing-key rotation | `kid` miss triggers a rate-floored JWKS refetch (D8) |
| `at_hash` | not checked, by decision: HERD never uses the IdP access token (no userinfo call, nothing forwarded), so the hash would bind nothing HERD consumes; it is recorded so a later userinfo call adds the check with it |
| Expired or future tokens | `exp` and `iat` required, 60 s ID-token leeway (D8) |
| Open redirect | `next` stored server-side and sanitized; `OIDC_REDIRECT_URI` fixed (D11) |
| Host-header injection into redirects | no URL is derived from request headers (D11) |
| Token leakage through URLs and logs | handoff by cookie and POST (D3); log rules (D9) |
| Account takeover through email | email never links an existing account (D4) |
| Unauthenticated row growth at `/oidc/login` | rows pruned at insert (D2); there is no rate limiting on any login route today (identity-and-access spec, section 8.2, Out of scope), recorded as a known limit |
| Deactivated users | refused at the callback and at the exchange; refresh already refuses (D7) |

### Settings (all consulted only when `auth_method == "oidc"`)

| Variable | Default | Purpose |
|---|---|---|
| `OIDC_ISSUER` | empty | Issuer URL, exact string the IdP puts in `iss` |
| `OIDC_CLIENT_ID` | empty | Client id registered at the IdP; the required `aud` |
| `OIDC_CLIENT_SECRET` | empty | Confidential-client secret, sent with `client_secret_basic` |
| `OIDC_REDIRECT_URI` | empty | Public callback URL registered at the IdP (D11) |
| `OIDC_SCOPES` | `openid email profile` | Requested scopes; must contain `openid` |
| `OIDC_ID_TOKEN_ALGORITHM` | `RS256` | The one accepted ID-token algorithm (D8) |
| `OIDC_USERNAME_CLAIM` | `preferred_username` | Claim used as the HERD username at provisioning |
| `OIDC_EMAIL_CLAIM` | `email` | Claim used as the HERD email at provisioning |
| `OIDC_PROVIDER_NAME` | `single sign-on` | Button text, "Sign in with ..." |
| `OIDC_REQUIRE_HTTPS` | `true` | Refuse non-https issuer, endpoints, and redirect URI; false only for a lab, logs a warning |
| `OIDC_CA_CERT` | empty | CA bundle path inside the container for a private IdP CA |
| `OIDC_BREAK_GLASS_LOCAL_SUPERADMIN` | `false` | D5: when true, `POST /login` in OIDC mode admits local superadmin accounts only; logs a startup warning when true in OIDC mode |

Each one lands as a Settings field in `services/auth/app/config.py`, a
`${VAR:-default}` line in the auth service's compose `environment:` block with the
default equal to the Settings default (`tests/unit/test_compose_settings_wiring.py`),
a commented line in `.env.example` beside the LDAP block, and a row in a new
`docs/ENV_VARS.md` section. Incomplete configuration in OIDC mode does not stop auth
from booting (the LDAP precedent, IAM-LDAP-5, keeps the break-glass and refresh paths
alive); auth logs one startup warning naming the missing variables, and
`GET /oidc/login` answers 302 to `/login?sso_error=unavailable`. Fixed in code, not
settings: flow TTL 600 s, handoff TTL 60 s, discovery and JWKS TTL 3600 s, JWKS
refetch floor 60 s, HTTP timeouts 5 s connect and 10 s per read or write with a 10 s
overall deadline per request, prune batch 500,
`next` cap 512 characters.

### API surface (the contract phases 3 and 4 build against)

Paths are the auth service's own; through the gateway each is prefixed with
`/api/auth` (the browser and the SPA use the prefixed form). "Not in OIDC mode" means
`settings.auth_method != "oidc"`. Every fixed detail below is the exact string, used
verbatim in the code and asserted verbatim in tests. A 404 or 401 body is FastAPI's
`{"detail": "<string>"}`; a 422 is FastAPI's default validation body.

#### Response models (`services/auth/app/schemas/auth.py`)

| Model | Fields |
|---|---|
| `SsoOptions` | `provider_name: str` (the `OIDC_PROVIDER_NAME` value) |
| `LoginOptionsResponse` | `auth_method: Literal["local", "ldap", "oidc"]`, `registration_open: bool`, `password_login: bool`, `sso: Optional[SsoOptions]` (null outside OIDC mode) |
| `OidcExchangeRequest` | no fields, `model_config = {"extra": "forbid"}`; the body is exactly `{}` |
| `OidcExchangeResponse` | subclasses `TokenResponse` (`access_token: str`, `refresh_token: str`, `token_type: str = "bearer"`) and adds `next: str` |

`GET /login-options` answers, per mode:

| `auth_method` | `registration_open` | `password_login` | `sso` |
|---|---|---|---|
| `local` | `true` | `true` | `null` |
| `ldap` | `false` | `true` | `null` |
| `oidc` | `false` | the value of `OIDC_BREAK_GLASS_LOCAL_SUPERADMIN` | `{"provider_name": ...}`, also when OIDC settings are incomplete (the begin route then answers `unavailable`) |

#### Routes

| Method and path | Caller credential | Request | Success | Failures (status, fixed detail or Location) |
|---|---|---|---|---|
| `GET /login-options` | none | none | 200 `LoginOptionsResponse` (table above); `Cache-Control: no-store` | none; answers in every mode |
| `GET /oidc/login` | none | optional query `next` (D11; an invalid value becomes `/topology`, never an error) | 302, `Location` = the IdP authorization URL (see IdP-facing requests), sets `herd_oidc_flow`; `Cache-Control: no-store` | 404 `Single sign-on is not enabled` when not in OIDC mode; 302 `Location: /login?sso_error=unavailable` when OIDC settings are incomplete or discovery cannot be fetched or validated |
| `GET /oidc/callback` | `herd_oidc_flow` cookie | query `state`, and either `code` or `error`; optional `error_description` (logged only) and `iss`; other parameters ignored | 302 `Location: /login/sso`, sets `herd_oidc_handoff`, clears `herd_oidc_flow`; `Cache-Control: no-store` | 404 `Single sign-on is not enabled` when not in OIDC mode; otherwise always a 302 that clears `herd_oidc_flow`, `Location` per the callback outcome table below |
| `POST /oidc/exchange` | `herd_oidc_handoff` cookie | JSON body `{}` (`OidcExchangeRequest`) | 200 `OidcExchangeResponse`, clears `herd_oidc_handoff`; `Cache-Control: no-store` | 404 `Single sign-on is not enabled` when not in OIDC mode; 422 for a missing, non-JSON, or non-empty body (as built: also a body sent without a JSON Content-Type, by FastAPI's strict content type; a 422 consumes nothing and does not clear the cookie); 401 `Single sign-on session expired; sign in again` when the cookie is missing, unknown, already exchanged, expired, or its user is now inactive, and (as built) when any other exception is raised after the body is validated, logged by class name only (the cookie is cleared on every 401 too) |
| `POST /login` | none | `LoginRequest`, unchanged | local and LDAP modes unchanged; OIDC mode with `OIDC_BREAK_GLASS_LOCAL_SUPERADMIN=true` and an active local superadmin with the right password: 200 `TokenResponse` | OIDC mode, every other case: 401 `Invalid credentials` with `WWW-Authenticate: Bearer`, after the dummy-hash bcrypt (D5) |
| `POST /register` | none | `RegisterRequest`, unchanged | local mode unchanged | OIDC mode: 409 `Local registration is disabled; this deployment uses single sign-on.`, before the body is used (the IAM-REG-1 shape); LDAP mode unchanged |
| `POST /refresh`, `POST /logout`, `POST /tokens/exchange` | unchanged | unchanged | unchanged in every mode (IAM-USER-9) | unchanged |

Callback outcome (first matching row wins; every failure row also moves a claimed flow
row to `failed`):

| Condition | `Location` |
|---|---|
| `state` missing, no `pending` row for it, the row expired, `herd_oidc_flow` missing, or its hash differs from `binding_hash` | `/login?sso_error=failed` |
| `error=access_denied` | `/login?sso_error=cancelled` |
| any other `error` value, or neither `code` nor `error` present | `/login?sso_error=failed` |
| `iss` present and not equal to `OIDC_ISSUER` | `/login?sso_error=failed` |
| OIDC settings incomplete; discovery, JWKS, or token endpoint unreachable, timed out, non-JSON, or answering 5xx | `/login?sso_error=unavailable` |
| token endpoint answers 4xx (for example `invalid_grant`) or no `id_token` | `/login?sso_error=failed` |
| ID-token verification fails (any D8 or D13 rule) | `/login?sso_error=failed` |
| provisioning refused (D4: email missing or unverified, email or username taken, username too long) | `/login?sso_error=failed` |
| linked user inactive | `/login?sso_error=failed` |
| the flow row cannot be completed (`complete_flow` answers False) | `/login?sso_error=failed` |
| success | `/login/sso` |

As built (phase 3): any other exception raised while the callback runs, at any row,
answers `/login?sso_error=unavailable` and fails a claimed row; it is logged once by
exception class name only, with no traceback and no message. `GET /oidc/login` has the
same catch-all, and maps a `ValueError` from the flow store to `unavailable` too.
`POST /oidc/exchange` has one around everything after FastAPI validates the body
(consuming the handoff, reloading the account, minting the tokens): the exception is
logged the same way, so no database error's bound parameters reach a log, and the
answer is the exchange's 401, cookie cleared. Every answer the three routes' own code
produces carries `Cache-Control: no-store`; a 422 does not, nor would a 500 raised
outside the catch-alls (in FastAPI's dependency handling, or while building a response
after a catch-all's scope). Outside OIDC mode each route answers its 404 before
reading any input, so a malformed body gets the 404, not a 422.

#### Cookies

| Name | Value | Attributes | Set by | Cleared by |
|---|---|---|---|---|
| `herd_oidc_flow` | 32 random bytes, base64url | HttpOnly, Secure, SameSite=Lax, Max-Age 600, Path = the D11 cookie path | `GET /oidc/login` (a second begin in another tab replaces it, so the first tab's callback then fails as `failed`) | every callback response |
| `herd_oidc_handoff` | 32 random bytes, base64url | HttpOnly, Secure, SameSite=Strict, Max-Age 60, same Path | a successful callback | every 200 and 401 exchange response, the catch-all's 401 included (as built: the 404 outside OIDC mode and a 422 answer before the route's code runs, set no cookie, and so leave it) |

"Cleared" means a `Set-Cookie` with the same name and Path, an empty value, and
Max-Age 0. As built it is Starlette's `delete_cookie`: the value is written `""`, the
cookie's own SameSite, HttpOnly, and Secure attributes are repeated, and an `expires`
of the current time is added.

#### IdP-facing requests

- Authorization URL: the discovery document's `authorization_endpoint` with query
  `response_type=code`, `client_id`, `redirect_uri` (`OIDC_REDIRECT_URI`), `scope`
  (`OIDC_SCOPES`), `state`, `nonce`, `code_challenge` (base64url of the SHA-256 of the
  verifier, no padding), `code_challenge_method=S256`, `response_mode=query`.
  `state` and `nonce` are 32 random bytes, base64url; the verifier is 64 characters
  from the RFC 7636 unreserved set.
- Token request: `POST` to `token_endpoint`, form-encoded `grant_type=authorization_code`,
  `code`, `redirect_uri`, `code_verifier`, with HTTP Basic authentication of the
  form-urlencoded `OIDC_CLIENT_ID` and `OIDC_CLIENT_SECRET` (RFC 6749 section 2.3.1).
  Only `id_token` is read from the answer.

#### Frontend contract (phase 4)

- `LoginPage` calls `GET /api/auth/login-options` on mount. On any error it renders
  today's page unchanged (D10). With `sso` present it renders a primary button
  `Sign in with {provider_name}` that sets `window.location.href` to
  `/api/auth/oidc/login` (no `next` in v1; the backend defaults to `/topology`). The
  password form renders when `password_login` is true; in OIDC mode it sits behind a
  collapsed "Administrator sign-in" toggle. The Register link renders only when
  `registration_open` is true, which hides it in LDAP mode.
- `LoginPage` reads `sso_error` from the query string and shows one toast:
  `cancelled` gives `Sign-in was cancelled.`; `unavailable` gives
  `Single sign-on is unavailable right now. Try again later.`; `failed` and any other
  value give `Single sign-on failed. If this continues, contact an administrator.`
  Then it removes the parameter with a `replace` navigation.
- New route `/login/sso` (`frontend/src/pages/OidcCompletePage.tsx`, outside
  `AppLayout`, under `GuestGuard`) posts `{}` to `/api/auth/oidc/exchange` exactly
  once per mount, guarded by a ref so React StrictMode's double effect cannot spend
  the single-use handoff twice. On 200 it calls `setTokens`, invalidates
  `currentUser`, and navigates to `next` with `replace`; on any error it navigates to
  `/login?sso_error=failed` with `replace`.
- `frontend/src/api/client.ts` adds `/auth/oidc/exchange` to the paths the refresh
  interceptor skips, beside `/auth/refresh` and `/auth/login`.

As built (phase 4; the rules are IAM-OIDC-24 to IAM-OIDC-27 in
`docs/specs/identity-and-access.md`):

- Rendering is a pure function of three fields of the answer
  (`frontend/src/lib/sso.ts`, `loginPageView`): the button exactly when `sso` is an
  object, the form exactly when `password_login` is true (behind the toggle when `sso` is
  present too), Register exactly when `registration_open` is true. `auth_method` is not
  read. An answer is valid only as an object with boolean `password_login` and
  `registration_open`, an `sso` that is null, absent, or a non-array object, and at least
  one way to sign in; anything else is D10's fallback, today's page, whose markup the
  local-mode answer reproduces exactly (same input elements, so a form filled in while
  the answer was pending, or found by an e2e helper before it, survives it). A
  `provider_name` that is not a non-blank string becomes `single sign-on`, the backend's
  default.
- The pending state is today's page too: while the `GET /login-options` request is
  pending, the password form and the Register link render, and the request has no
  client timeout, so a request that never answers leaves today's page up. In OIDC mode
  the password form therefore shows until the answer arrives. Accepted for this phase
  and filed as issue #1420, which blocks phase 6.
- The toggle is a disclosure button (`aria-expanded`, `aria-controls`); behind it the
  form's submit is a secondary button (the design system's gray-100 variant), so the page
  keeps one primary action, the "Sign in with" button. Wherever the form is not behind
  the toggle (local and LDAP mode, and the fallback) the submit stays primary, so that
  markup is unchanged. The button navigates with `window.location.assign`, equivalent to
  setting `href`, through `frontend/src/lib/topLevelNavigation.ts` so tests can replace it.
- `sso_error`: `cancelled` is a neutral toast, the other two error toasts; one toast id
  and a ref keyed on the location keep it to one toast under StrictMode. The replace
  navigation removes that parameter alone and keeps the rest of the query, the hash, and
  the location state (the return path of IAM-UI-13).
- `/login/sso` does not call `navigate(next)` itself. React Router applies a navigation
  as a transition while the token store updates synchronously, so `setTokens` followed by
  `navigate(next)` let `GuestGuard` redirect on the old location (to `/topology`) first;
  a test through the real guards caught it. The page instead records `next` as its
  location's return path (`{from: next, sub}`, a replace, where `sub` is the subject of
  the exchanged access token, so `GuestGuard` honors the path for that account only;
  an access token whose subject cannot be read is a failure), adopts the tokens once
  that state has committed, and `GuestGuard` replaces the page with it. An answer that
  arrives after the page unmounted is dropped (no navigation, no adoption, no message):
  the review of this phase reproduced a race in which another tab's sign-in sent the
  page away and the late answer then replaced the other account's page with the flow's
  `next`. `next` passes `safeReturnPath` twice (when the answer is parsed, and in
  `GuestGuard`), which now also refuses `/login/sso`. The exchange is sent with
  `withCredentials` and the 15 s login timeout; a 200 without a non-empty token pair is a
  failure. A timeout can come after the server consumed the handoff; the user then sees
  the failed message and signs in again.
- The refresh skip list (`REFRESH_SKIP_PATHS`, `skipsRefresh`) matches the end of the
  request path rather than a substring, and lists `/auth/login-options` too: under the
  substring match `/auth/login` covered it by accident, so no entry could be tested on
  its own.

#### Internal interfaces between phases

These signatures let phases 1 and 2 be built in parallel and joined in phase 3. Every
function is `async`; secrets passed in are the raw values, and only their SHA-256
hashes (`hash_token`) are stored.

| Phase and module | Function and arguments | Returns |
|---|---|---|
| 1, `oidc_service.py` | `build_authorization_request()` | `AuthorizationRequest`, a frozen dataclass: `url`, `state`, `nonce`, `code_verifier` |
| 1, `oidc_service.py` | `redeem_code(code, code_verifier, nonce)` (fetch discovery and JWKS, exchange, verify) | `VerifiedIdToken`, a frozen dataclass: `issuer`, `subject`, `email` (optional), `email_verified` (optional bool), `username_claim` (optional), `name` (optional) |
| 2, `oidc_flow_service.py` | `start_flow(db, *, state, nonce, code_verifier, binding_secret, next_path)` | the new flow id (`uuid.UUID`) |
| 2, `oidc_flow_service.py` | `claim_flow(db, *, state, binding_secret)` | `ClaimedFlow` (`id`, `nonce`, `code_verifier`, `next_path`), or None when no guarded UPDATE matched |
| 2, `oidc_flow_service.py` | `complete_flow(db, flow_id, *, user_id, handoff_secret)`; `fail_flow(db, flow_id)` | bool, whether the guarded UPDATE matched |
| 2, `oidc_flow_service.py` | `exchange_handoff(db, *, handoff_secret)` | a (user id, `next_path`) pair, or None |
| 2, `auth_service.py` | `resolve_federated_user(db, login)`, where `login` is a `FederatedLogin` frozen dataclass (`issuer`, `subject`, `email`, `email_verified`, `username`, `full_name`) defined in phase 2, plus `username_unusable` (bool, default False) added in phase 3 | the `User`, or None for every D4 refusal and every inactive user, each logged with a fixed `reason` in `extra` |

Phase 1's errors are `OidcUnavailableError` (the callback maps it to `unavailable`)
and `OidcRejectedError` (mapped to `failed`); neither carries IdP text in its `str()`.
The modules live under `services/auth/app/services/`. Phase 3 maps `VerifiedIdToken`
to `FederatedLogin` (`federated_login_from_token` in `app/routers/oidc.py`; username by
option C, D4: the claim when it fits the allowlist, else the email when it does, else
None with `username_unusable` set when a candidate was present, which a first login
refuses as D4's table says; `full_name` is `name` truncated to 200 characters, or the
username).

## Consequences

- Easier: enterprise deployments get IdP-enforced MFA and password custody moves out
  of HERD for federated users; downstream services, the JWT, refresh, API tokens, and
  RBAC are unchanged.
- Harder: the auth image gains `cryptography`; the repository's one-decode guard gains
  its first auth-side entries, each needing an adversarial review in both directions;
  the gates and nightly gain an OIDC phase and a second test lab.
- Accepted limits: an IdP-side disable reaches HERD only at refresh-token expiry
  (default 7 days) or a HERD-side deactivation; HERD logout does not end the IdP
  session (D7); the client secret saved through the config UI sits in `config.json`
  in plaintext, as `LDAP_BIND_PASSWORD` does; no rate limiting on login routes, as
  today; a user whose IdP email changes keeps the old HERD email.
- To revisit: RP-initiated logout (D7), Dex in per-PR CI (D12), account linking (Out
  of scope), the secrets service as the client-secret store once it has a
  service-principal read path.

## Delivery phases

Every phase is one PR that leaves main green and the feature dark: `oidc` is not a
legal `AUTH_METHOD` value until phase 6, so no operator can reach the new code
before then; tests reach it by setting `settings.auth_method` with `monkeypatch`, the
way `services/auth/tests/test_auth_ldap.py` sets `ldap`. Every phase updates
`CHANGELOG.md` (Unreleased) and, where it adds behavior or a route, the
identity-and-access specification in the same PR with numbered rules (a new `IAM-OIDC`
topic). Sizes are rough: S under 300 changed lines, M 300 to 900, L over 900, tests
included.

### Phase 1: settings, discovery, JWKS, ID-token verification (M)

- `services/common/herd_common/jwt_codec.py`: `decode_id_token` (D1); module
  docstring updated.
- `tests/unit/test_jwt_settings_wiring.py`: one `DIRECT_DECODE_ALLOWED` entry, one
  `UNVERIFIED_READ_ALLOWED` entry, docstring updated to stay exactly true.
- `services/auth/app/services/oidc_service.py` (new): discovery fetch and validation,
  JWKS cache and key selection, authorization-URL builder (PKCE S256), code exchange,
  ID-token verification (`azp`, nonce, `email_verified`), the transport seam; the
  phase 1 signatures under "Internal interfaces between phases".
- `services/auth/app/config.py`: all twelve settings of the Settings section,
  `OIDC_BREAK_GLASS_LOCAL_SUPERADMIN` included (its field only; its behavior lands in
  phase 3), the algorithm `Literal`, and a startup-warning helper (missing settings,
  `OIDC_REQUIRE_HTTPS=false`, break-glass on); `auth_method` is NOT widened.
- `docker-compose.yml` (auth block), `.env.example`, `docs/ENV_VARS.md` (new OIDC
  section).
- `services/auth/pyproject.toml` (`pyjwt[crypto]`), `services/common/pyproject.toml`
  (dev `pyjwt[crypto]`), `uv.lock` (one `uv lock`; `uv lock --check` is a gate step).
- Tests: `services/auth/tests/test_oidc_service.py`, `test_oidc_settings.py` (every
  default, the compose pairing through the existing guard, the algorithm refusal at
  load, each startup warning), `services/common/tests/test_jwt_codec.py` additions
  (unit level; every claim and algorithm refusal, with a mutation proof for the guard
  change).
- Docs: `docs/specs/identity-and-access.md` (IAM-JWT-5 text names the second
  decode), `docs/specs/CONVENTIONS.md` (Auth and identity: the second decode helper and
  the OIDC log rules).

### Phase 2: flow store and federated identities (M)

- `services/auth/migrations/versions/0010_oidc_sso.py` (new): both tables (D2, D4).
- `services/auth/app/models/oidc_login_flow.py`, `federated_identity.py` (new);
  imported in `services/auth/app/main.py` beside the other models so `create_all`
  sees them.
- `services/auth/app/services/oidc_flow_service.py` (new): create with pruning,
  claim, authenticate, fail, exchange; every transition one guarded UPDATE; the phase
  2 signatures under "Internal interfaces between phases".
- `services/auth/app/services/auth_service.py`: `create_oidc_user` and
  `resolve_federated_user` with the D4 refusals, taking the `FederatedLogin`
  dataclass defined in this phase (so it does not import phase 1's module).
- Tests: `services/auth/tests/test_oidc_flow_service.py`,
  `test_oidc_provisioning.py` (unit, SQLite); `test_oidc_flow_service_live_pg.py`
  (Postgres-live races) added to the `_gate-pg-live-tests` recipe in the `Makefile`
  (`tests/unit/test_pg_live_gate_wiring.py` enforces it); a migration up and down test
  in the shape of `test_user_profile_migration.py`.
- Docs: the spec's Concepts and data and State model tables (flow row lifecycle).

### Phase 3: routes and mode wiring, still dark (L)

- `services/auth/app/routers/oidc.py` (new): `GET /oidc/login`, `GET /oidc/callback`,
  `POST /oidc/exchange`, with the callback outcome table and the cookie table of the
  API surface section; cookie helpers deriving Path from `OIDC_REDIRECT_URI`.
- `services/auth/app/routers/auth.py`: `GET /login-options`, answering in every mode
  per the per-mode table (so `registration_open` is `false` in LDAP mode from this
  phase on); `register` 409 in OIDC mode.
- `services/auth/app/schemas/auth.py`: `SsoOptions`, `LoginOptionsResponse`,
  `OidcExchangeRequest`, `OidcExchangeResponse`.
- `services/auth/app/services/auth_service.py`: `authenticate_user` in OIDC mode
  refuses every account, or with `OIDC_BREAK_GLASS_LOCAL_SUPERADMIN=true` admits only
  an active local superadmin (D5), running the dummy-hash bcrypt on every refusal.
- `services/auth/app/main.py`: include the router.
- `tests/contract/snapshots/auth.json`: regenerated with
  `HERD_UPDATE_OPENAPI_SNAPSHOTS=1 make test-contract` against a running stack. As
  built, regenerated without a stack: the contract test's own `_signature` applied to
  the auth app's `app.openapi()`; every entry the change did not add came out
  byte-equal to the committed snapshot, so the diff is the four routes and four
  schemas alone.
- Tests (functional, through the real auth app; MockTransport IdP):
  `services/auth/tests/test_oidc_routes.py`, the happy path to `GET /me` and
  `POST /refresh`, every row of the callback outcome table, every exchange failure,
  each cookie's attributes, and every refusal in D13 over HTTP;
  `services/auth/tests/test_login_options.py`, all three modes including
  `password_login` following the break-glass setting;
  `services/auth/tests/test_oidc_break_glass.py`: in OIDC mode with the setting off,
  a local superadmin with the right password gets 401; with it on, that superadmin gets
  200 while a local admin, a local user, an `oidc` account, an `ldap` account, an
  inactive local superadmin, and an unknown email each get 401 `Invalid credentials`;
  the dummy hash runs on every refusal (asserted by spying on `verify_password`); and
  the startup warning fires only when the setting is true in OIDC mode. `/register`
  409 in OIDC mode beside the LDAP-mode tests in `test_auth_ldap.py`.
- Docs: spec API surface, Errors, and Features sections (the break-glass rule numbered
  with its tests); `docs/ROLES.md` endpoint matrix (four public routes).

### Phase 4: frontend, still dark (M)

- `frontend/src/api/auth.ts` (`useLoginOptions`, `exchangeOidcHandoff`),
  `frontend/src/types/auth.types.ts`, `frontend/src/api/client.ts` (skip list).
- `frontend/src/pages/LoginPage.tsx`, per "Frontend contract (phase 4)": SSO button
  when `sso` is present, password form only when `password_login` (behind the
  "Administrator sign-in" toggle in OIDC mode), Register link only when
  `registration_open` (this hides it in LDAP mode; record it in the phase's CHANGELOG
  line as a visible LDAP-mode change), the three `sso_error` messages; the fallback on
  a `/login-options` error is today's page (D10).
- `frontend/src/pages/OidcCompletePage.tsx` (new) and `frontend/src/routes.tsx`
  (`/login/sso` under `GuestGuard`); `frontend/src/test/pages/pageScrollContainer.test.tsx`
  (`OUTSIDE_LAYOUT_PAGES`); `frontend/src/test/routes.test.tsx` if its literal lists
  change.
- Tests: vitest for both pages and the API hooks with msw handlers: each mode's
  rendering, the Register link absent in LDAP mode and present in local mode, the
  error fallback, each `sso_error` message and its removal from the URL, a single
  exchange POST under StrictMode, and both exchange outcomes; the existing
  `LoginPage.test.tsx` cases keep passing in local mode.
- Dark by construction: until phase 3 is deployed, `GET /login-options` answers 404
  and the page renders today's form; after phase 3, only the LDAP-mode Register link
  changes before phase 6.

### Phase 5a: the OIDC test lab and the live service suite (S to M)

- `infra/oidc-test/docker-compose.yml`, `infra/oidc-test/dex.yaml` (new): pinned
  image, stateless, a static client for HERD, fixture users (a normal user, one whose
  email equals a seeded local account's for the collision test).
- `Makefile`: `oidc-up`, `oidc-down`, `oidc-status`, `test-auth-oidc`
  (hard-required), `_gate-oidc-tests`, wired into `master` and `everything` beside
  `_gate-ldap-tests`.
- `services/auth/tests/test_oidc_service_live.py` (functional, live Dex: discovery,
  JWKS, a full code exchange driven over HTTP through Dex's login form).
- Measure first (D12, decided): before the lab files are final, record image size
  and cold-start time for Dex, Keycloak, and mock-oauth2-server, prove the
  issuer-reachability mechanism, and prove that Dex refuses a wrong `code_verifier`
  and echoes the `nonce`. If Dex fails any of those, stop and return the choice to the
  owner with the numbers.
- Docs: `docs/specs/TESTING.md` (an OIDC test directory section beside the LDAP one),
  `docs/specs/DEVELOPMENT.md` (targets).

### Phase 5b: stack integration, load, and browser e2e (M)

- `Makefile`: `_gate-oidc-stack-tests` (the `_gate-ldap-stack-tests` shape: refuse
  on a UI-saved `config.json`, connect `oidc-test` to the gate network, recreate only
  auth with `AUTH_METHOD=oidc` and the lab settings, run, restore local mode in a
  trap), wired into `master`, `everything`, and `.github/workflows/nightly.yml`
  (start and stop steps beside the LDAP ones, diagnostics before teardown).
- `tests/integration/test_oidc_auth.py` (including `GET /login-options` and, with
  break-glass off as the phase configures it, the seeded superadmin's `POST /login`
  refused), `tests/load/locustfile.py` (`OidcLoginUser`),
  `tests/e2e/test_oidc_login_playwright.py`, `tests/e2e/conftest.py`
  (`pw_oidc_browser`).
- Coverage gap, by decision: break-glass ON is exercised at the functional level
  (phase 3) only, not against a running stack, because a second recreate of the gate
  auth service costs a full restart cycle for one branch; recorded in `docs/GAPS.md`.
- Docs: `docs/specs/TESTING.md`, `docs/GAPS.md`.

### Phase 6: light up (M)

- `services/auth/app/config.py`: `auth_method: Literal["local", "ldap", "oidc"]`.
- `services/config/app/config_schema.py`: the `oidc` option and the `OIDC` group;
  the config service's compose block gets the `OIDC_*` lines;
  `services/config/tests/` asserts the option, the group, and the compose pairing.
- Docs: the spec's Purpose, Configuration, Test coverage map, and Known limits;
  `docs/ARCHITECTURE.md` (Auth and RBAC: the third method and `auth_source='oidc'`,
  the issue's acceptance criterion), `docs/ROLES.md` (an OIDC section beside the LDAP
  one), `docs/ADMIN_HANDBOOK.md` (IdP registration walk-through, break-glass,
  bootstrap), `docs/USER_GUIDE.md`, `FEATURES.md`, `PLANNED_FEATURES.md` (SSO moves
  to shipped, SAML stays planned), `README.md` if it lists auth methods, this ADR's
  status line to Accepted with as-built amendments.

### Parallel work

| Phase | Depends on | Can run beside |
|---|---|---|
| 1 | none | 2, 4 |
| 2 | none | 1, 4, 5a |
| 3 | 1, 2 | 4, 5a |
| 4 | this ADR's API table only | 1, 2, 3, 5a |
| 5a | 1 | 2, 3, 4 |
| 5b | 3, 4, 5a | none |
| 6 | 5b | none |

Phases 1, 2, and 4 touch disjoint source files (phase 1: `config.py`,
`oidc_service.py`, `jwt_codec.py`, the guard, compose, `.env.example`,
`ENV_VARS.md`, the lockfile; phase 2: models, migration, `oidc_flow_service.py`,
`auth_service.py`, the Makefile pg-live recipe; phase 4: `frontend/` only). The shared
hotspots are `CHANGELOG.md` and `docs/specs/identity-and-access.md`, both append-only
per phase; the second PR to land rebases them. Phase 5a also edits the `Makefile`
(new targets) while phase 2 edits its pg-live recipe, in different regions.

## Out of scope

- SAML 2.0 (follow-up issue, below).
- Directory or IdP group claims as a membership source; claim-to-role mapping (D6).
- IdP-initiated, back-channel, or front-channel logout; RP-initiated logout (D7,
  deferred by the owner on 2026-11-11).
- SCIM provisioning and deprovisioning.
- More than one IdP per deployment; local plus OIDC side by side (D5).
- Linking or converting an existing local or LDAP account to a federated identity.
- The userinfo endpoint, IdP refresh tokens, `offline_access`.
- Token-endpoint authentication other than `client_secret_basic` (`client_secret_post`,
  `private_key_jwt`); public clients.
- HERD session tokens in cookies instead of `localStorage` (D3).
- Exposing `auth_source` in `UserResponse` or the admin users page.
- Rate limiting of login routes.

## SAML follow-up

SAML 2.0 gets its own issue and ADR once OIDC is accepted as built. It reuses, from
this design: the exclusive-mode switch (a fourth `AUTH_METHOD` value or an
`oidc`/`saml` protocol setting, decided there), `federated_identities` keyed by
(entity id of the IdP, NameID) with the same collision refusals, the flow table for
`RelayState` and request-id binding (its `nonce` column holds the AuthnRequest id for
`InResponseTo`), the handoff cookie and `POST` exchange, `GET /login-options`, the
break-glass decision, and the test-lab pattern. What it adds: an ACS endpoint that
receives an HTTP-POST binding (so the binding cookie must be SameSite=None, a
difference from D11 that ADR must justify), XML signature validation (a library
choice: signxml or python3-saml, with an XML-hardening review), SP metadata
publication, and an IdP test container that speaks SAML.

## Owner decisions

The first draft put five questions to the owner. All five were decided by the owner on
2026-11-11, each as recommended; no owner question remains open. A sixth owner decision,
the username rule (option C), was made before phase 3 and is the last row below. The one condition
still pending is the phase 5a measurement, which can return D12 to the owner.

| Decision | Outcome | Lands in |
|---|---|---|
| D1 library | hand-rolled on httpx plus PyJWT, `decode_id_token` in `herd_common.jwt_codec` | phase 1 |
| D5 break-glass | exclusive `oidc` mode with `OIDC_BREAK_GLASS_LOCAL_SUPERADMIN`, default off | field in phase 1; behavior and tests in phase 3; stack check (off) in phase 5b; handbook in phase 6 |
| D7 RP-initiated logout | not in v1; stays in Out of scope | none |
| D10 active-method source | auth's public `GET /login-options`; config's `GET /status` unchanged, departing from the issue's wording because of staleness | endpoint in phase 3; login page and the LDAP-mode Register link in phase 4 |
| D12 test IdP | Dex, measured and proved first | phase 5a, then 5b |
| D4 username, option C (added in phase 3) | the username claim when it fits `^[A-Za-z0-9][A-Za-z0-9._@+-]{0,99}$`, else the email when it fits, else the first login is refused | phase 3 |
