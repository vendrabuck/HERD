# Environment Variables Reference

Every environment variable HERD reads, where it's consumed, and what it does. Values saved through the config UI take precedence over `.env`; a `config.json` that exists only from first-run auto-bootstrap does not (details below).

## Precedence (highest to lowest)

1. `config.json` once saved through the config UI, at `/data/herd-config/config.json` (the directory is overridable via `HERD_CONFIG_DATA_DIR`)
2. `os.environ` (shell / `.env` / docker compose environment block)
3. Auto-bootstrapped `config.json` (marked by a sibling `config.bootstrapped` file; a copy of the environment serving as fallback)
4. In-code defaults (usually dev-friendly values)

Which rung the file occupies depends on how it came to exist:

- A `config.json` created solely by the first-run auto-bootstrap (below) is a copy of the environment, not an operator decision. The config service writes a `config.bootstrapped` marker beside it and the file ranks below the environment, so a stack driven purely by `.env` behaves exactly as it always has: edit `.env`, recreate containers, done.
- The first real save through the config UI deletes the marker. From then on the file outranks the environment for every key it carries, which is what makes Save and Restart actually take effect: container env vars are fixed at container creation, so with env-first ordering a config-UI save could never change a running deployment.

Guard rails, in both modes:

- An empty string saved in `config.json` means "unset": the key falls through to the environment, so clearing an optional field in the UI hands it back to `.env`. Required fields cannot be cleared in the UI (the save is rejected); to hand a required key back to the environment, edit or delete `config.json` on the `herd-config` volume (deleting it re-runs the auto-bootstrap on the next config-service start).
- A `DATABASE_URL` set in the environment always outranks the URL derived from the file's `POSTGRES_*` values; the derived form hardcodes the in-compose `postgres:5432` host and exists only as a fallback.
- Keys outside the config schema (the dev/test knobs, `SECRETS_KEK`, and so on) never appear in `config.json` and always resolve from the environment.

Upgrading from a version without the marker: an existing `config.json` with no `config.bootstrapped` beside it is treated as UI-saved (file-first). To hand control back to the environment, create an empty `config.bootstrapped` file next to `config.json` on the volume.

## First-run auto-bootstrap

On the very first `make up`, the config service writes `/data/herd-config/config.json` automatically from the process environment when every required variable below is present and non-empty. That file is what gates the login page, so a complete `.env` now unlocks login without visiting the config UI. If any required var is missing the config service logs a warning listing them, skips the bootstrap, and the login page keeps directing you to the wrench icon.

The config page's own login password is set by `CONFIG_ADMIN_PASSWORD`. When you set it (in `.env` or the environment), that is the config-UI password and the config write surface is unlocked immediately. When it is unset, the config service generates a random one-time password on first boot and logs it once at WARNING (read it from `make logs config` or the container logs); the write and apply endpoints stay locked with HTTP 403 until you log in and change the password. There is no longer a hardcoded default password. The related `CONFIG_SESSION_SECRET` pins the config session-token signing key across replicas; when unset, a random per-process key is used.

## Config editor populates from env

The wrench-icon config editor shows every schema field that is present in the config container's environment even when `config.json` is missing the key; secrets sourced from env are still rendered as `********`. If a field is set both in `config.json` and in the environment, the file value wins in the editor (it represents an explicit save), and the services resolve it the same way at runtime, so what the editor shows is what the stack runs with.

For the config service to see these env vars, `docker-compose.yml` maps them into the container via the `environment:` block (see the `POSTGRES_*`, `AUTH_*`, `SUPERADMIN_*`, `INTERNAL_API_TOKEN`, `CORS_ORIGINS`, `NATS_URL`, `AI_API_KEY`, `LOG_LEVEL` passthroughs).

## Required

These must be set before the stack will run. The config service first-run flow will force-prompt for any that aren't already set via `.env`.

| Variable | Example / format | Used by | Purpose |
|---|---|---|---|
| `AUTH_SECRET_KEY` | 64-char hex string (`openssl rand -hex 32`) | auth, inventory, reservations, cabling, acl, execution, ai-orchestrator | HMAC secret used to sign and verify JWTs. MUST match across all services. Changing it invalidates every existing token. |
| `INTERNAL_API_TOKEN` | 64-char hex string | reservations, inventory, execution, cabling, user-profile, notifications, integration, secrets, ai-orchestrator | Shared secret for service-to-service calls that use the `X-Internal-Token` header. Must match across all services that speak to each other. ai-orchestrator uses it only to call execution's internal validate-package endpoint for recipe drafting. |
| `SECRETS_KEK` | base64-encoded 32 bytes (`python3 -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"`) | secrets | Key-encryption key for the credential store. No default: the secrets service refuses to boot without a valid value (the dev/test compose override supplies a dev-only key; production must set it). Losing it makes stored secrets unrecoverable. |
| `SECRETS_KEK_PREVIOUS` | same format, normally unset | secrets | Set only during a KEK rotation window: at boot, stored keys that fail under `SECRETS_KEK` are unwrapped with this and re-wrapped under the new KEK. Unset it once a boot has completed with both present. |
| `POSTGRES_USER` | `herd` | postgres, all backend services | Database owner. |
| `POSTGRES_PASSWORD` | strong password | postgres, all backend services | Database password. |
| `POSTGRES_DB` | `herd` | postgres, all backend services | Database name. |

## Superadmin seed (first-run only)

Read once at startup to create the seeded superadmin. Ignored on subsequent restarts; changing them post-seed does nothing.

| Variable | Purpose |
|---|---|
| `SUPERADMIN_EMAIL` | Email for the seeded superadmin. |
| `SUPERADMIN_USERNAME` | Username for the seeded superadmin. |
| `SUPERADMIN_PASSWORD` | Password (min 8 chars) for the seeded superadmin. |

## Auth

| Variable | Default | Purpose |
|---|---|---|
| `AUTH_ALGORITHM` | `HS256` | JWT signing algorithm. Any jose-supported HS* or RS* algorithm. |
| `AUTH_ACCESS_TOKEN_EXPIRE_MINUTES` | `30` | Access token lifetime. |
| `AUTH_REFRESH_TOKEN_EXPIRE_DAYS` | `7` | Refresh token lifetime. |
| `AUTH_METHOD` | `local` | Authentication backend. `local` uses bcrypt-hashed passwords in the HERD database; `ldap` binds against the directory server configured below. Only one is active at a time. |

## LDAP / Active Directory

Only consulted when `AUTH_METHOD=ldap`. When enabled, `/register` returns 409 and
user accounts are provisioned on first successful LDAP bind (no password hash
is stored locally). HERD role and group membership remain managed inside HERD;
Directory group mapping and sync (ADR 0011,
`docs/design/0011-ldap-group-sync.md`, issue #38) is fully delivered, all 6
phases: admin-managed mappings, an on-demand reconcile
(`POST /api/auth/admin/ldap-sync/run`), the opt-in deactivation sweep, a
background interval loop (`LDAP_GROUP_SYNC_ENABLED`) that reconciles on
`LDAP_SYNC_INTERVAL_SECONDS`, and the admin UI (`/admin/ldap-sync`) for
mapping CRUD, sync-now, and run history. The `LDAP_GROUP_*` and
`LDAP_SYNC_*`/`LDAP_DISABLED_FILTER` keys below belong to this feature. Retention (`LDAP_SYNC_RUNS_RETENTION_DAYS`) is
enforced ONLY by the interval loop, never by manual sync-now: the FIRST due
tick after a process starts prunes unconditionally (so a rolling restart
does not wait out a full day with nothing pruned), then at most once per 24
hours after that, checked at each tick boundary (an interval configured
above 24h therefore prunes once per tick, not once per day). A deployment
that runs only manual sync-now and never enables the loop accumulates
`ldap_sync_runs` rows indefinitely; that is a deliberate consequence of
keeping pruning out of the manual path, not an oversight.

| Variable | Default | Purpose |
|---|---|---|
| `LDAP_SERVER_URL` | (empty) | Directory URL, e.g. `ldaps://ad.example.com:636` or `ldap://ad.example.com:389`. |
| `LDAP_BIND_DN` | (empty) | Service-account DN used to search the directory. Leave blank for anonymous search. |
| `LDAP_BIND_PASSWORD` | (empty) | Service-account password. |
| `LDAP_USER_BASE_DN` | (empty) | Search base, e.g. `OU=Users,DC=example,DC=com`. |
| `LDAP_USER_FILTER` | `(sAMAccountName={username})` | Search filter. `{username}` is substituted with the escaped login input. |
| `LDAP_EMAIL_ATTRIBUTE` | `mail` | Directory attribute providing the user's email. Users without this attribute cannot log in. |
| `LDAP_USERNAME_ATTRIBUTE` | `sAMAccountName` | Directory attribute used as the HERD username. |
| `LDAP_GROUP_MEMBER_ATTRIBUTE` | `member` | Group entry attribute holding member DNs (Active Directory and `groupOfNames` use `member`; `posixGroup` `memberUid` is out of scope). Consulted by the ADR 0011 group sync. |
| `LDAP_GROUP_NAME_ATTRIBUTE` | `cn` | Group entry attribute cached as a mapping's display name. Consulted by the ADR 0011 group sync. |
| `LDAP_GROUP_SYNC_ENABLED` | `false` | Start the background loop that reconciles directory groups on an interval (ADR 0011 phase 5). Dark by default: admin-managed mappings and on-demand sync-now work regardless of this setting. Also requires `AUTH_METHOD=ldap`. |
| `LDAP_SYNC_INTERVAL_SECONDS` | `3600` | Seconds between interval-triggered reconcile runs. The first tick sleeps one full interval before syncing (no boot-time sync burst on a rolling restart). Values below 60 are clamped to a 60-second floor at loop startup (with a warning) rather than rejected, so a bad tuning value never blocks auth from booting. `GET /admin/ldap-sync/status` and the admin UI report the clamped, effective value, not this raw setting. |
| `LDAP_SYNC_RUNS_RETENTION_DAYS` | `90` | Days to keep `ldap_sync_runs` audit rows. Pruned only by the interval loop (never by manual sync-now): the first due tick after startup prunes unconditionally, then at most once per 24 hours after that, checked at tick boundaries. A `running` row is never pruned regardless of age. |
| `LDAP_SYNC_DEACTIVATION_ENABLED` | `false` | Opt-in for the deactivation/reactivation sweep. Independent of group mirroring: enabling mappings alone never deactivates anyone. |
| `LDAP_DISABLED_FILTER` | (empty) | A complete LDAP filter expression (not a value) identifying disabled accounts, e.g. the AD `userAccountControl` lockout-bit filter `(userAccountControl:1.2.840.113556.1.4.803:=2)`. Empty means absence-only detection. |
| `LDAP_SYNC_DEACTIVATION_MAX_PERCENT` | `20` | Circuit-breaker percent term: the sweep aborts, deactivating no one, only when the proven-absent-or-disabled count STRICTLY exceeds this percent of swept users AND the count floor below. Reactivations still apply on abort. |
| `LDAP_SYNC_DEACTIVATION_MIN_COUNT` | `3` | Circuit-breaker count floor (strictly exceeded together with the percent term). Keeps small deployments functional: one leaver in a four-user shop is 25 percent but under the floor, so it deactivates. |
| `LDAP_USE_TLS` | `true` | Require TLS. `ldaps://` URLs negotiate TLS implicitly; plain `ldap://` URLs use STARTTLS when this is true. |
| `LDAP_TLS_VALIDATE` | `true` | Verify the directory server's TLS certificate. The bind transmits the service-account and every user's password, so leave this on: an unvalidated certificate lets an active network attacker MITM the connection and harvest credentials. Set `false` only for a lab directory behind a self-signed cert you cannot pin via `LDAP_CA_CERT`; this logs a startup warning. |
| `LDAP_CA_CERT` | (empty) | Path (inside the container) to a CA bundle to verify the directory server against, e.g. a pinned internal CA. Used when `LDAP_TLS_VALIDATE=true`; when empty the system trust store is used. |

Worked Active Directory example:

```
AUTH_METHOD=ldap
LDAP_SERVER_URL=ldaps://dc01.corp.example.com:636
LDAP_BIND_DN=CN=herd-svc,OU=ServiceAccounts,DC=corp,DC=example,DC=com
LDAP_BIND_PASSWORD=<service account password>
LDAP_USER_BASE_DN=OU=Users,DC=corp,DC=example,DC=com
LDAP_USER_FILTER=(sAMAccountName={username})
LDAP_USE_TLS=true
```

## Service URLs (internal Docker network)

Read by services that need to call other services. The defaults assume the compose service names.

| Variable | Default | Read by |
|---|---|---|
| `AUTH_SERVICE_URL` | `http://auth:8000` | inventory, acl (forward user JWT to resolve groups) |
| `INVENTORY_SERVICE_URL` | `http://inventory:8000` | reservations, execution, ai-orchestrator, cabling (device-group boundary check), secrets (delete-time hypervisor reference guard, issue #456) |
| `CABLING_SERVICE_URL` | `http://cabling:8000` | execution, ai-orchestrator, reservations (connectivity validation via `/validate/internal`) |
| `RESERVATIONS_SERVICE_URL` | `http://reservations:8000` | ai-orchestrator, inventory (apply scheduler checks reservation activity via `/internal/{id}`), execution (dynamic-resources provision-result callback, `/internal/{id}/provision-result`, ADR 0004) |
| `EXECUTION_SERVICE_URL` | `http://execution:8000` | ai-orchestrator, inventory (apply scheduler dispatches configure runs) |
| `ACL_SERVICE_URL` | `http://acl:8000` | inventory, execution (carve-out check for non-admin configure on managed devices) |
| `USER_PROFILE_SERVICE_URL` | `http://user-profile:8000` | notifications (read prefs via internal endpoint and proxy PUT/GET) |
| `SECRETS_SERVICE_URL` | `http://secrets:8000` | inventory (validate a hypervisor's secret reference at registration/update), execution (resolve a hypervisor's secret value for a dynamic-resources recipe run, ADR 0004) |

If you run a service on a different host or port, update the URL in `.env` or the config UI. Paths should NOT include `/api/<service>` prefix; that's Traefik's prefix, not the app's route.

## Web / CORS / TLS

| Variable | Default | Purpose |
|---|---|---|
| `CORS_ORIGINS` | `""` in code; `https://localhost` at the docker-compose level | Comma-separated origins allowed by every backend's CORS middleware. Each service's `app/config.py` defaults to an empty string; `docker-compose.yml` supplies `${CORS_ORIGINS:-https://localhost}` for every service, so a fresh `.env` (which also sets `CORS_ORIGINS=https://localhost`) gets `https://localhost` in practice. Add your real hostname or IP to enable cross-host access. |

TLS is handled by Traefik with certs in `infra/traefik/certs/`; there is no env var for cert paths. See [OPERATIONS.md](OPERATIONS.md#tls-certificate-rotation).

## NATS

| Variable | Default | Purpose |
|---|---|---|
| `NATS_URL` | `nats://nats:4222` | Connection string. Absence is non-fatal at startup; services log a warning and run without event-driven features. |

## Logging

| Variable | Default | Purpose |
|---|---|---|
| `LOG_LEVEL` | `INFO` | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. Per-service. |

## Config service

The config page login password is set by `CONFIG_ADMIN_PASSWORD`; there is no longer a
hardcoded default. Config service auth is separate from HERD JWT.

| Variable | Default | Purpose |
| --- | --- | --- |
| `CONFIG_ADMIN_PASSWORD` | random per deploy | The config-page login password (`services/config/app/config_store.py`). When set, that value is the password and the config write/apply surface is unlocked. When unset, a random one-time password is generated on first boot and logged once at WARNING (read it from the config container logs); the write and apply endpoints return 403 until you log in and change the password. Never a source-visible constant. |
| `CONFIG_SESSION_SECRET` | random per process | HMAC key that signs and verifies the short-lived config-session token issued after config login (`services/config/app/auth.py`). If unset, a random secret is generated at process start, so sessions do not survive a config-service restart. Set it to a strong shared value only when you run multiple config replicas and need a session to verify across them. It is never a source-visible constant. |

## AI orchestrator

The orchestrator supports two backends via `AI_PROVIDER`: `anthropic` (the AsyncAnthropic SDK, against either the hosted Anthropic API with `AI_API_KEY`, or a local Anthropic-compatible endpoint via `AI_BASE_URL` with no key) and `openai_compat` (the AsyncOpenAI SDK against any compatible chat-completions endpoint, including vLLM, Ollama, LM Studio, OpenAI proper, and Azure OpenAI). All three AI endpoints gate on `ai_is_configured()` and return 503 when the active provider is not configured: `anthropic` is configured when either `AI_API_KEY` or `AI_BASE_URL` is set; `openai_compat` needs `AI_BASE_URL`.

| Variable | Default | Purpose |
|---|---|---|
| `AI_PROVIDER` | `anthropic` | Backend selector: `anthropic` or `openai_compat`. |
| `AI_BASE_URL` | (empty) | Endpoint URL for a non-hosted backend. For `openai_compat`, include the `/v1` suffix, e.g. `http://vllm:8000/v1`. For `anthropic` pointed at a local Anthropic-compatible endpoint (e.g. a vLLM serving `/v1/messages`), use the server ROOT with no `/v1` suffix (the Anthropic SDK appends `/v1/messages` itself). Leave blank for the hosted Anthropic API. |
| `AI_API_KEY` | (empty) | API key for the hosted Anthropic API. Leave blank for a local server (vLLM, Ollama, LM Studio) that ignores auth: the orchestrator sends an `EMPTY` placeholder when this is blank, for both `openai_compat` and `anthropic` pointed at a local `AI_BASE_URL`. |
| `AI_MODEL` | `claude-sonnet-4-6` | Model identifier passed to the provider. Format is provider-specific: `claude-*` for `anthropic`; provider-and-deployment-specific for `openai_compat` (e.g. `Qwen/Qwen3-35B-Instruct` on vLLM, `gpt-4o-mini` on OpenAI proper). |
| `AI_MAX_TOKENS` | `4096` | Per-call token cap. |
| `AI_DAILY_TOKEN_QUOTA` | `0` | Per-user daily budget of AI tokens (input + output) across all AI features (topology generation, the reservation assistant, and template-identity suggestions). `0` (default) disables enforcement and writes no usage rows, so behavior is unchanged until an operator opts in. When positive, a caller whose accumulated tokens for the current UTC day already meet or exceed this value is rejected with HTTP 429 and a `{limit, used, remaining, reset_at}` body, without calling the provider; the boundary call that crosses the limit is allowed and the next one is blocked. Counts reset implicitly on the UTC day boundary. Provider-reported usage is used when present, with a chars/4 estimate as a fallback. `GET /api/ai/quota` returns the caller's current usage. |
| `AI_TLS_VERIFY` | `true` | Verify the TLS certificate of `AI_BASE_URL`. Set `false` only for an endpoint behind a self-signed certificate (e.g. an on-prem vLLM server); the connection otherwise fails certificate verification before auth. Applies whenever `AI_BASE_URL` is set, including `anthropic` pointed at a local Anthropic-compatible endpoint. Prefer `AI_CA_CERT` over this for a known on-prem endpoint. |
| `AI_CA_CERT` | (empty) | Path (inside the container) to a CA bundle to verify `AI_BASE_URL` against, e.g. a pinned self-signed on-prem certificate. Takes precedence over `AI_TLS_VERIFY`: verification stays on and fails closed, which is preferable to disabling verification. Mount the cert into the orchestrator container (see `docker-compose.yml`) and set this to its in-container path. Applies whenever `AI_BASE_URL` is set. |
| `UPLOAD_MAX_FILE_BYTES` | `5242880` (5 MB) | Per-file cap for AI reference uploads. |
| `UPLOAD_MAX_FILES` | `5` | Max files per AI request. |
| `UPLOAD_MAX_EXTRACTED_CHARS` | `80000` | Aggregate text extracted from all files; per-file `truncated` flag appears in the response. |
| `ASSISTANT_MAX_TOOL_ITERATIONS` | `8` | Reservation assistant tool-use loop cap. On hit, one final call without tools forces a graceful answer. |
| `ASSISTANT_TOOL_RESULT_CHAR_CAP` | `8000` | Per-tool-result truncation ceiling. Larger payloads are clipped with a `[truncated: N chars omitted]` marker before reaching the model. |
| `ASSISTANT_OVERALL_DEADLINE_S` | `90.0` | Hard deadline for the reservation assistant route. 504 above this. |
| `ASSISTANT_PER_CALL_TIMEOUT_S` | `20.0` | Per-call timeout inside the assistant loop. 502 above this. |
| `AI_WRITE_TOOLS_ENABLED` | `false` | Expose the iter-3 write tools (propose_config_change, schedule_config_apply) to the reservation assistant. The tools always default to dry_run=true and route through the existing inventory schedule endpoint with full ACL gating; even with the flag on, no real apply runs without a user confirming the dry-run transcript via the UI. |
| `AI_RECIPE_AUTHORING_ENABLED` | `false` | Expose the admin-only recipe-drafting endpoints (`/api/ai/recipes/draft`, `/refine`, and the draft GET; ADR 0005, issue #28). Default off because the feature asks an LLM to draft code that will run against lab infrastructure after admin approval; enforcement is at the route boundary (403 with a pinned detail), not just absence from docs. Drafts are validated through execution's sandboxed validate-package endpoint and are never uploaded by the AI; upload stays the admin's explicit action. `GET /api/ai/status` reports the flag as `recipe_authoring` for conditional UI. |
| `AI_RECIPE_MAX_ATTEMPTS` | `3` | Bounded auto-repair for recipe drafting: total model attempts (initial draft plus repair rounds fed the validator's report) one draft or refine request may spend. Every attempt's tokens are metered against `AI_DAILY_TOKEN_QUOTA`. |
| `ASSISTANT_CONVERSATION_TTL_HOURS` | `24` | Multi-turn assistant conversations idle past this are deleted by the hourly sweeper, including the persisted question text in `assistant_messages.content_blocks` (issue #338). Reopening a reservation modal within TTL resumes the prior thread via sessionStorage. Must be a positive integer; `0` or a negative value is rejected at startup rather than treated as "expire immediately". |
| `ASSISTANT_MAX_TURNS` | `40` | Hard cap on total messages (user + assistant + tool_result) per conversation. When exceeded, the oldest user+assistant pair drops; the position-0 seed message is pinned. |
| `ASSISTANT_HISTORY_TOKEN_BUDGET` | `60000` | Approximate input-token budget per conversation (chars/4 estimate, no tokenizer dependency). When exceeded, eviction runs to bring history back under the budget. |
| `ASSISTANT_SWEEPER_INTERVAL_SECONDS` | `3600` | Background sweeper interval. Each cycle deletes conversations older than the TTL. |

### Database (multi-turn assistant)

The orchestrator gained its first DB-backed feature in the multi-turn chat work; it persists conversations + messages in the `ai_orchestrator` schema in the shared Postgres. The schema is created by `infra/postgres/init.sql` on a fresh install and managed by Alembic at `services/ai-orchestrator/migrations/`. Run `make migrate-ai-orchestrator` (or `make migrate`) to apply pending revisions.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///:memory:` | Async Postgres URL, e.g. `postgresql+asyncpg://herd:...@postgres:5432/herd`. The in-code default is an in-memory SQLite URL (used by the service's own unit tests); docker-compose always supplies a real Postgres URL for the running stack. |
| `DB_SCHEMA` | `ai_orchestrator` | Per-service schema for the orchestrator's tables. |

### Switching to a local LLM via vLLM

vLLM exposes an OpenAI-compatible server at `http://<host>:8000/v1` by default. To point HERD at it, set:

```env
AI_PROVIDER=openai_compat
AI_BASE_URL=http://vllm:8000/v1
AI_API_KEY=EMPTY
AI_MODEL=Qwen/Qwen3-35B-Instruct
```

Tool-use support (which both `/generate` and the reservation assistant require) is OFF in vLLM by default. Launch the server with:

```bash
--enable-auto-tool-choice --tool-call-parser hermes
```

The exact parser name depends on the model family and your vLLM version (Hermes-style works for Qwen3; some vLLM releases ship a model-specific `qwen3` parser; consult your vLLM docs). Without these flags the model emits tool-call-shaped text and vLLM returns it as a plain string, the orchestrator sees `stop_reason="end_turn"` with no tool block, and the loop exits with whatever free-form output the model produced.

If the vLLM server terminates TLS with a self-signed certificate (common for an on-prem deployment reached over HTTPS), you have two options. Preferred: pin the certificate. Fetch it (`openssl s_client -connect <host>:8000 </dev/null 2>/dev/null | openssl x509 > server-ca.pem`), mount it into the orchestrator container, and set `AI_CA_CERT` to its in-container path; verification stays on and fails closed. The cert's SAN must cover the host in `AI_BASE_URL` (IP or DNS name) or verification fails on a hostname mismatch. Quicker but less safe: set `AI_TLS_VERIFY=false` to skip verification entirely. Without one of these the orchestrator's HTTP client rejects the certificate and the call fails with a connection error before the request is sent. Leave both at their defaults for endpoints with a CA-signed certificate or plain-HTTP local servers.

### Switching to Ollama, LM Studio, OpenAI, or Azure OpenAI

Same shape, different `AI_BASE_URL`:

- **Ollama**: `http://ollama:11434/v1` (Ollama added an OpenAI-compatible surface in 0.1.30+).
- **LM Studio**: `http://lm-studio:1234/v1`.
- **OpenAI proper**: omit `AI_BASE_URL` (the SDK defaults to `https://api.openai.com/v1`); set `AI_API_KEY=sk-...`.
- **Azure OpenAI**: use the deployment-specific URL and set `AI_API_KEY` to your Azure key.

## Cabling service

| Variable | Default | Purpose |
|---|---|---|
| `ENFORCE_DEVICE_GROUP_BOUNDARIES` | `true` | When true, `POST /api/cabling/connections` rejects (422) a connection whose two devices belong to device groups that share none, keeping lab boundaries hard rather than advisory. Shared infrastructure (an L1/L2 switch bridging labs) is allowed by adding that switch to every lab group it serves. Enforced at create time only; existing connections are never re-validated. A device in no group is always cable-able. Set `false` to allow cross-lab cabling (e.g. a federated deployment). |

## Execution service

| Variable | Default | Purpose |
|---|---|---|
| `EXECUTION_TIMEOUT_SECONDS` | `30` | Subprocess timeout for a driver method call (non-status). |
| `STATUS_CHECK_TIMEOUT_SECONDS` | `10` | Shorter timeout for the `status` method. |
| `RECIPE_TIMEOUT_SECONDS` | `300` | Wall-clock subprocess timeout for a `Hypervisor`-connection-type recipe's `login`/`create_instance`/`destroy_instance`/`logout` calls, run by the NATS consumer's dynamic-resources create and teardown flows (ADR 0004, issue #32), not `POST /execution/execute`. Longer than `EXECUTION_TIMEOUT_SECONDS` because a hypervisor create or destroy can take minutes; `DRIVER_RLIMIT_CPU_SECONDS` still applies unchanged, since waiting on a remote API is not CPU time. |
  | `VALIDATE_PACKAGE_MAX_BYTES` | `10485760` | Size cap on the decoded archive accepted by the internal `POST /internal/validate-package` endpoint (ADR 0005, issue #28), which validates unapproved AI-drafted recipe packages without loading them as drivers. Matches inventory's `DRIVER_MAX_SIZE_BYTES` default. |
| `VALIDATE_DRY_RUN_TIMEOUT_SECONDS` | `10` | Per-method wall-clock timeout for the validate-package endpoint's sandboxed dry-run lifecycle. Dry-run methods simulate and return without wire I/O, so they get a status-check-style timeout, not `RECIPE_TIMEOUT_SECONDS`. |
| `DRIVER_CACHE_PATH` | `/data/driver-cache` | Local driver cache path. Volume-backed. |
| `DRIVER_RLIMIT_AS_BYTES` | `268435456` | POSIX `RLIMIT_AS` (address space) for the driver subprocess, in bytes; 256 MB default. `0` disables. Raise or disable for numpy/pandas/BLAS drivers, which reserve large virtual address space. |
| `DRIVER_RLIMIT_CPU_SECONDS` | `60` | POSIX `RLIMIT_CPU` for the driver subprocess, in seconds. `0` disables. |
| `DRIVER_RLIMIT_NOFILE` | `256` | POSIX `RLIMIT_NOFILE` (open files) for the driver subprocess. `0` disables. |
| `DRIVER_RLIMIT_NPROC` | `1024` | POSIX `RLIMIT_NPROC` (processes, per service uid) for the driver subprocess. `0` disables. This is a per-UID ceiling that counts every thread the service user already holds container-wide, so SSH/threaded drivers (netmiko, paramiko) need the headroom; 1024 still guards against a runaway fork bomb. |
| `ALLOW_DRIVER_PIP_INSTALL` | `false` | When `true`, a driver package's `requirements.txt` is `pip install`ed at execution time. Off by default: a runtime install pulls network code as the service user. When off, vendor deps into the package's `_deps/`. |
| `EXECUTION_POLLER_ONLY` | `false` | When `true`, this replica skips mounting the HTTP API routers at startup and runs only the background machinery (health scheduler, NATS consumer, outbox relay) plus the bare `/health` liveness route (issue #24). Lets the same image run a horizontally scaled poller fleet next to API replicas; pair the API replicas with `HEALTH_POLL_SCHEDULER_ENABLED=false` for a clean split. Concurrent pollers against one schema are safe: due rows are claimed with `SELECT ... FOR UPDATE SKIP LOCKED` plus a conditional update, so two replicas never poll the same device. |

The execution service also hosts the periodic health-poll scheduler (ROADMAP #13). Each polled device runs the existing driver `login`, `status`, `logout` sequence on its configured cadence; outcomes drop into `device_health_status` and history persists in `execution_runs`.

| Variable | Default | Purpose |
|---|---|---|
| `HEALTH_POLL_SCHEDULER_ENABLED` | `true` | Enable the in-process asyncio scheduler. Set to `false` for read-only replicas or debugging. |
| `HEALTH_POLL_SCHEDULER_TICK_SECONDS` | `30` | Tick cadence. Each tick scans `device_health_status` for due rows and fires up to `HEALTH_POLL_BATCH_SIZE`. Lowering this finds due rows faster but increases DB load. |
| `HEALTH_POLL_BATCH_SIZE` | `10` | Maximum due rows one tick claims (issue #24). Rows past the batch stay due and are picked up by later ticks, or by another poller replica. The default matches the pre-#24 hardcoded limit of 10. |
| `HEALTH_POLL_MAX_CONCURRENCY` | `1` | Maximum scheduler polls in flight at once within a replica (issue #24). Each poll runs a driver subprocess, so this bounds the scheduler's live subprocesses (manual executions and provisioning are not covered by this bound); polls past the limit wait and are reported as `polls_deferred` in the per-tick `health_tick` log. Polls execute on asyncio's shared default thread pool (`min(32, cpus + 4)` workers), so values above that pool size cannot run in parallel and queue while already claimed; the scheduler logs a startup warning when the setting exceeds the pool. Keep it at or below the pool size and scale poller replicas for more throughput. The default of 1 matches the pre-#24 strictly sequential firing. |
| `HEALTH_POLL_IN_USE_INTERVAL_SECONDS` | `0` | Poll interval for devices in the in-use tier, entered when a consumed `reservation.created`/`reservation.updated` event names the device and left on `completed`/`cancelled`/`failed` (issue #24). Moving to in-use also pulls the device's `next_poll_at` earlier (never later) so the faster cadence starts promptly; a poll already in flight during the transition reschedules once more on its old cadence before converging. `0` (default) disables the override: the registry-resolved interval (device or template `poll_interval_seconds`) applies, exactly the pre-tier behavior. |
| `HEALTH_POLL_IDLE_INTERVAL_SECONDS` | `0` | Poll interval for devices in the idle tier (not under an active reservation). `0` (default) disables the override so the registry-resolved interval applies. The tier itself is persisted on `device_health_status.poll_tier`, so it survives a service restart even though the lifecycle events do not replay. |
| `HEALTH_POLL_REGISTRY_REFRESH_SECONDS` | `300` | How often the scheduler re-fetches the device list from inventory's `/devices/health-config` endpoint. New devices or interval changes take effect within roughly this window. |
| `HEALTH_POLL_MAX_CONSECUTIVE_FAILURES` | `3` | Failures past this threshold trigger exponential backoff with jitter, so an UNREACHABLE device does not flood `execution_runs`. |
| `HEALTH_POLL_BACKOFF_CAP_SECONDS` | `3600` | Upper bound on backoff between polls. |
| `HEALTH_POLL_MINIMUM_INTERVAL_SECONDS` | `30` | Currently unused: the 30-second floor on `poll_interval_seconds` is the hardcoded `MIN_POLL_INTERVAL_SECONDS` in inventory's `app/schemas/device.py`, so changing this variable has no effect. |
| `HEALTH_POLL_NOTIFY_ENABLED` | `true` | Publish a `device.health_transition` NATS event when a device crosses the failure threshold (bad_news) or recovers. Set to `false` to silence alerts without rolling back the publisher code. |
| `TEMPLATE_CACHE_TTL_SECONDS` | `300` | How long the health-poll path caches a fetched template (keyed by template_id) before re-fetching from inventory (issue #316). A lab sharing a handful of templates across many devices otherwise re-fetches the same template on every poll; a hit within this window skips the inventory call. Scoped to the health scheduler only, not the on-demand executions router or the NATS consumer's own cache, so a just-edited template is still immediately visible there. A failed fetch is never cached. |

The execution service also runs the per-connection wiring auto-retry channel (ADR 0007 Decision 6, issue #345 P3b). A hardware apply failure during a fork-save reconcile lands a `FAILED` `l1_connection_assignments` row rather than rolling back the durable save; a background sweep reattempts the hardware-retryable ones (a driver or login failure), with the pinned unresolvable and not-a-simple-chain rows left for a fork re-save. The channel is batch-capped per tick exactly like the health scheduler, and mirrors its run-mode posture: enabled by default so a poller-only replica runs it, set `false` on API replicas to keep the work on the poller fleet.

| Variable | Default | Purpose |
|---|---|---|
| `WIRING_RETRY_ENABLED` | `true` | Enable the in-process background wiring auto-retry channel. Set to `false` on API replicas (paired with `EXECUTION_POLLER_ONLY=false`) so the sweep runs only on the poller fleet, the same split `HEALTH_POLL_SCHEDULER_ENABLED` provides for the health scheduler. Disabling it leaves FAILED rows for manual retry only (the owner-gated `POST /reservations/{id}/wiring/retry` proxy). |
| `WIRING_RETRY_INTERVAL_SECONDS` | `60` | Seconds between auto-retry ticks. A tick that raises backs off exponentially up to a cap, then resets on the next healthy tick, exactly like the health scheduler loop. |
| `WIRING_RETRY_BATCH_SIZE` | `20` | Maximum FAILED rows one tick reattempts (issue #345). Rows past the batch stay `FAILED` and are swept on later ticks. Bounds the driver subprocesses one tick can spawn, the same role `HEALTH_POLL_BATCH_SIZE` plays for polls. |
| `WIRING_RETRY_MAX_ATTEMPTS` | `10` | Cumulative driver-attempt cap for the auto-retry channel. `attempts` accumulates every driver call ever spent on a connection (the in-line apply plus each reattempt), so a row whose `attempts` reaches this cap is no longer auto-swept and is parked `FAILED` for manual retry only. The manual retry proxy ignores this cap by design, since it is the fallback for a parked row. |

## Inventory service (optional storage overrides)

Driver packages are stored locally by default. To use MinIO (or any S3-compatible store):

| Variable | Default | Purpose |
|---|---|---|
| `DRIVER_STORAGE_PATH` | `/data/drivers` | Local filesystem path for driver packages. Ignored when MinIO is configured. |
| `DRIVER_MAX_SIZE_BYTES` | `10485760` | Maximum accepted size (in bytes) of an uploaded driver package. Default is 10 MB. |
| `MINIO_ENDPOINT` | (empty) | Set to enable MinIO; endpoint like `minio:9000`. |
| `MINIO_ACCESS_KEY` | - | Required when MinIO is configured. |
| `MINIO_SECRET_KEY` | - | Required when MinIO is configured. |
| `MINIO_BUCKET` | `herd-drivers` | Bucket name. Must exist. |
| `MINIO_USE_SSL` | `false` | TLS for MinIO connection. |

Inventory also hosts the apply-job scheduler that runs scheduled device-config apply jobs:

| Variable | Default | Purpose |
|---|---|---|
| `APPLY_SCHEDULER_ENABLED` | `true` | Enable the in-process asyncio scheduler that polls for due apply jobs and dispatches them. Set to `false` to run inventory without the scheduler (useful for read-only replicas or debugging). |
| `APPLY_SCHEDULER_INTERVAL_SECONDS` | `30` | Polling interval. Lower values find due jobs faster but increase DB load. Reservation-bound jobs only fire while the reservation is active; the scheduler skips (does not catch up) past-due windows. |

Test-only fault-injection seam (issue #214):

| Variable | Default | Purpose |
|---|---|---|
| `HERD_FAULT_INJECTION` | unset | Test-only. When truthy (`1`/`true`/`yes`/`on`), the internal `POST /devices/{id}/status` endpoint returns 503 for any device whose name carries the `__herd_fault_status__` sentinel, letting integration tests drive the provisioning FAILED + device-revert path. Double-gated (env set AND the sentinel name), so it is inert for normal devices. Set only in `docker-compose.override.yml` (dev/test), which `make prod` excludes, so production never enables it. |

## Notifications service

| Variable | Default | Purpose |
|---|---|---|
| `PREFERENCES_CACHE_TTL_SECONDS` | `30` | In-process TTL for cached per-user notification preferences fetched from user-profile. |
| `AUTH_SERVICE_URL` | `http://auth:8000` | Base URL for auth's `/internal/admins` endpoint, used by the health-transition recipient resolver. |
| `RESERVATIONS_SERVICE_URL` | `http://reservations:8000` | Base URL for reservations' `/internal/active-users` endpoint, used to find users with an active reservation on a device that's transitioning. |
| `HEALTH_NOTIFY_ADMIN_CACHE_TTL_SECONDS` | `60` | In-process TTL for the cached list of admin user-ids used to fan out `device.health_transition` events. Admin list rarely changes so a longer TTL is fine. |

### Outbound channels (ROADMAP #40)

Transport config for the email, chat, and webhook dispatchers. All are instance-level: per-user opt-in is the channel toggle in `/settings`, not per-user credentials. A channel is "configured" only when its required settings are present; an unconfigured channel a user has opted into is a logged no-op, never an error. Outbound channels default off in preferences, so none of these need to be set for the in-app channel to keep working.

| Variable | Default | Purpose |
|---|---|---|
| `SMTP_HOST` | empty | SMTP server host. Email channel is configured only when `SMTP_HOST` and `EMAIL_FROM` are both set. |
| `SMTP_PORT` | `587` | SMTP server port. |
| `SMTP_USERNAME` | empty | SMTP auth username. When empty, no login is attempted (open relay or IP-allowlisted server). |
| `SMTP_PASSWORD` | empty | SMTP auth password. Use a secrets mechanism, not a plaintext `.env`, in production. |
| `SMTP_USE_TLS` | `true` | Issue STARTTLS before sending. |
| `SMTP_TIMEOUT_SECONDS` | `10` | Socket timeout for the SMTP send. |
| `EMAIL_FROM` | empty | From-address on outbound email. Required (with `SMTP_HOST`) to enable the email channel. |
| `CHAT_WEBHOOK_URL` | empty | Slack-style incoming-webhook URL. The chat message is POSTed as `{"text": ...}`, which Slack, Mattermost, and Rocket.Chat all accept. Setting this enables the chat channel. |
| `CHAT_TIMEOUT_SECONDS` | `10` | HTTP timeout for the chat POST. |
| `OUTBOUND_WEBHOOK_URL` | empty | Destination for the outbound webhook. Webhook channel is configured only when this and `WEBHOOK_SIGNING_SECRET` are both set. |
| `WEBHOOK_SIGNING_SECRET` | empty | HMAC-SHA256 key. The notification body is signed and sent as `X-HERD-Signature: sha256=<hex>`; the receiver recomputes the HMAC over the received bytes to authenticate the payload. An unsigned outbound webhook is never sent. |
| `WEBHOOK_TIMEOUT_SECONDS` | `10` | HTTP timeout for the webhook POST. |

`AUTH_SERVICE_URL` (above) also backs the email and chat dispatchers' recipient lookup via auth's `/internal/users/{id}/contact` endpoint, so `INTERNAL_API_TOKEN` must match on auth and notifications for outbound email and chat to resolve an address.

The notifications service runs two durable NATS consumers: one on `herd.reservations.*` (DLQ `herd.reservations.dlq.notifications`) and one on `herd.health.*` (DLQ `herd.health.dlq.notifications`). Distinct durables so a stuck health-event subscriber cannot block reservation events and vice versa. Absence of NATS is non-fatal at startup; the REST API still works and the consumers reconnect when NATS returns.

## Reservations service

| Variable | Default | Purpose |
|---|---|---|
| `EXPIRATION_INTERVAL_SECONDS` | `60` | How often the expiration loop wakes up to activate `PENDING` reservations, complete `ACTIVE` ones whose windows closed, and emit upcoming-expiry reminders. |
| `EXPIRY_REMINDER_LEAD_SECONDS` | `3600` | Lead window before `end_time` in which the expiration task publishes a `reservation.expiring_soon` event onto `HERD_RESERVATIONS` (ROADMAP #40). An ACTIVE reservation whose `end_time` is within this many seconds of now, and still in the future, gets exactly one reminder, deduped via `expiry_reminder_sent_at`. `0` disables the reminder. |
| `RESERVATION_START_GRACE_SECONDS` | `300` | On create, a `start_time` earlier than now minus this grace is rejected (422), so a user cannot book a window that already passed. The grace tolerates clock skew and "start now". It also sets the scheduled-vs-immediate boundary: a `start_time` more than this grace in the future is created `PENDING` and provisioned by the expiration task at start_time, while a booking within the grace is provisioned immediately. The expiration loop activates `PENDING` reservations whose start has ticked past. |
| `RESERVATION_MAX_DURATION_SECONDS` | `2592000` | On create, a window longer than this (default 30 days) is rejected (422), guarding against runaway or typo'd bookings. `0` disables the cap. |
| `PROVISION_TIMEOUT_SECONDS` | `900` | Provisioning backstop deadline for both stranded-reservation sweeps in the expiration task. Dynamic branch (ADR 0004, issue #32): a `PENDING_PROVISION` reservation carrying `dynamic_requests` whose row has not been updated in this many seconds is failed (`reservation.failed` is staged), so a lost provision-result callback from the execution service can never strand a reservation. Physical-only branch (issue #318): a physical-only `PENDING_PROVISION` reservation stranded past the same deadline (for example by a process restart in the inventory-flip window) is reverted to `PENDING` via compare-and-swap so a later cycle re-runs the flip and activation; nothing is torn down because no `reservation.created` was emitted yet. `0` disables both backstops. |
| `OUTBOX_RELAY_TICK_SECONDS` | `5.0` | Transactional outbox relay (issue #21) poll cadence in seconds: how often the relay drains unpublished `outbox` rows to JetStream. A NATS outage backs this off exponentially and a healthy tick resets it. |
| `OUTBOX_BATCH_SIZE` | `100` | Maximum outbox rows the relay publishes per tick. Each row is claimed with `FOR UPDATE SKIP LOCKED` and published with a `Nats-Msg-Id` header for publisher-side dedup. |
| `OUTBOX_RETENTION_SECONDS` | `604800` | How long published outbox rows are retained before the relay prunes them; default 7 days. |
| `CALENDAR_MAX_SPAN_DAYS` | `366` | `GET /calendar` has no `LIMIT` and no pagination, so a window (`range_end - range_start`) wider than this is rejected (422) rather than silently loading and holding an unbounded result set in memory (issue #315). `0` disables the cap. |
| `UTILIZATION_MAX_SPAN_DAYS` | `366` | `GET /reports/utilization` and `/reports/utilization.csv` have no `LIMIT` and no pagination, so a window (`end - start`) wider than this is rejected (422) rather than silently loading and holding an unbounded result set in memory (issue #389, the deferred sibling of #315). `0` disables the cap. |

The execution service runs the same outbox relay for the `device.health_transition` event, but it uses the `herd_common.outbox.run_outbox_relay` defaults (5s tick, 100 batch, 7-day retention) and exposes no environment overrides today.

## Integration service

The integration service (issue #33) exposes the versioned `/api/v1` external reservation
facade and fans reservation lifecycle events out to admin-registered outbound webhooks,
consumed via its own NATS durable consumer.

| Variable | Default | Purpose |
|---|---|---|
| `DB_SCHEMA` | `integration` | Per-service schema for the integration service's tables. |
| `WEBHOOK_DELIVERY_TIMEOUT_SECONDS` | `10.0` | HTTP timeout for a single outbound webhook delivery attempt. |
| `WEBHOOK_DELIVERY_ATTEMPTS` | `4` | Maximum delivery attempts (including the first) before a webhook delivery is recorded as dead-lettered. |
| `WEBHOOK_TEST_SINK_ENABLED` | `false` | When true, exposes a test-only sink endpoint used to assert webhook deliveries in integration tests. Leave off outside test environments. |

## Frontend (Vite build-time)

These are baked into the bundle at build time (Vite reads `VITE_*` env vars during `npm run build`). To flip a flag you rebuild the frontend image (`docker compose up -d --build frontend`).

| Variable | Default | Purpose |
|---|---|---|
| `VITE_AI_CHAT_ENABLED` | `false` | Render the multi-turn chat UI for the reservation assistant. When `false` the legacy single-shot UI renders instead and each request is independent. Flip to `true` per environment after smoke-testing the round-trip. |

## Database URLs (auto-computed)

Each service computes its own SQLAlchemy URL from `POSTGRES_*` vars. You do not normally set `DATABASE_URL` directly; it's derived. If you need to point a service at a different DB, override with `DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/db`.

## Adding a new env var

1. Add the field to the service's `app/config.py` Settings class (Pydantic).
2. Add it to `config_schema.py` in the config service so it appears on the config page.
3. Add it here with name, default, and purpose.
4. Add an example to `.env.example`.

Secrets (API keys, passwords) should use Pydantic's `SecretStr` in `app/config.py` and be marked as password-type in `config_schema.py` so the config UI masks the input and redacts in GET responses.
