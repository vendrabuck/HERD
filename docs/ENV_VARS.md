# Environment Variables Reference

Every environment variable HERD reads, where it's consumed, and what it does. `.env` values take precedence over the config service's `config.json`.

## Precedence (highest to lowest)

1. `os.environ` (shell / `.env` / docker compose environment block)
2. `config.json` written by the config service at `/data/herd-config/config.json` (the directory is overridable via `HERD_CONFIG_DATA_DIR`)
3. In-code defaults (usually dev-friendly values)

This means you can override a config-service setting temporarily via the shell, and the override wins until you remove it.

## First-run auto-bootstrap

On the very first `make up`, the config service writes `/data/herd-config/config.json` automatically from the process environment when every required variable below is present and non-empty. That file is what gates the login page, so a complete `.env` now unlocks login without visiting the config UI. If any required var is missing the config service logs a warning listing them, skips the bootstrap, and the login page keeps directing you to the wrench icon.

The admin password on the config page itself is unchanged: it stays `admin123!` on first visit regardless of bootstrap state, so the config UI keeps its default-password gate.

## Config editor populates from env

The wrench-icon config editor shows every schema field that is present in the config container's environment even when `config.json` is missing the key; secrets sourced from env are still rendered as `********`. If a field is set both in `config.json` and in the environment, the file value wins in the editor (it represents an explicit save). Runtime services continue to read `os.environ` directly, so the precedence ladder above still applies to the actual behavior of the stack.

For the config service to see these env vars, `docker-compose.yml` maps them into the container via the `environment:` block (see the `POSTGRES_*`, `AUTH_*`, `SUPERADMIN_*`, `INTERNAL_API_TOKEN`, `CORS_ORIGINS`, `NATS_URL`, `ANTHROPIC_API_KEY`, `LOG_LEVEL` passthroughs added in commit `1e9fd09`).

## Required

These must be set before the stack will run. The config service first-run flow will force-prompt for any that aren't already set via `.env`.

| Variable | Example / format | Used by | Purpose |
|---|---|---|---|
| `AUTH_SECRET_KEY` | 64-char hex string (`openssl rand -hex 32`) | auth, inventory, reservations, cabling, acl, execution, ai-orchestrator | HMAC secret used to sign and verify JWTs. MUST match across all services. Changing it invalidates every existing token. |
| `INTERNAL_API_TOKEN` | 64-char hex string | reservations, inventory, execution, cabling, user-profile, notifications | Shared secret for service-to-service calls that use the `X-Internal-Token` header. Must match across all services that speak to each other. |
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
LDAP groups are not mirrored automatically in v1.

| Variable | Default | Purpose |
|---|---|---|
| `LDAP_SERVER_URL` | (empty) | Directory URL, e.g. `ldaps://ad.example.com:636` or `ldap://ad.example.com:389`. |
| `LDAP_BIND_DN` | (empty) | Service-account DN used to search the directory. Leave blank for anonymous search. |
| `LDAP_BIND_PASSWORD` | (empty) | Service-account password. |
| `LDAP_USER_BASE_DN` | (empty) | Search base, e.g. `OU=Users,DC=example,DC=com`. |
| `LDAP_USER_FILTER` | `(sAMAccountName={username})` | Search filter. `{username}` is substituted with the escaped login input. |
| `LDAP_EMAIL_ATTRIBUTE` | `mail` | Directory attribute providing the user's email. Users without this attribute cannot log in. |
| `LDAP_USERNAME_ATTRIBUTE` | `sAMAccountName` | Directory attribute used as the HERD username. |
| `LDAP_USE_TLS` | `true` | Require TLS. `ldaps://` URLs negotiate TLS implicitly; plain `ldap://` URLs use STARTTLS when this is true. |

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
| `INVENTORY_SERVICE_URL` | `http://inventory:8000` | reservations, execution, ai-orchestrator, cabling (device-group boundary check) |
| `CABLING_SERVICE_URL` | `http://cabling:8000` | execution, ai-orchestrator, reservations (connectivity validation via `/validate/internal`) |
| `RESERVATIONS_SERVICE_URL` | `http://reservations:8000` | ai-orchestrator, inventory (apply scheduler checks reservation activity via `/internal/{id}`) |
| `EXECUTION_SERVICE_URL` | `http://execution:8000` | ai-orchestrator, inventory (apply scheduler dispatches configure runs) |
| `ACL_SERVICE_URL` | `http://acl:8000` | inventory, execution (carve-out check for non-admin configure on managed devices) |
| `USER_PROFILE_SERVICE_URL` | `http://user-profile:8000` | notifications (read prefs via internal endpoint and proxy PUT/GET) |

If you run a service on a different host or port, update the URL in `.env` or the config UI. Paths should NOT include `/api/<service>` prefix; that's Traefik's prefix, not the app's route.

## Web / CORS / TLS

| Variable | Default | Purpose |
|---|---|---|
| `CORS_ORIGINS` | `https://localhost` | Comma-separated origins allowed by every backend's CORS middleware. Add your real hostname or IP to enable cross-host access. |

TLS is handled by Traefik with certs in `infra/traefik/certs/`; there is no env var for cert paths. See [OPERATIONS.md](OPERATIONS.md#tls-certificate-rotation).

## NATS

| Variable | Default | Purpose |
|---|---|---|
| `NATS_URL` | `nats://nats:4222` | Connection string. Absence is non-fatal at startup; services log a warning and run without event-driven features. |

## Logging

| Variable | Default | Purpose |
|---|---|---|
| `LOG_LEVEL` | `INFO` | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. Per-service. |

## Config service (optional overrides)

| Variable | Default | Purpose |
|---|---|---|
| `CONFIG_PASSWORD` | `admin123!` | Initial password for the config page. Must be changed on first login. Config service uses its own auth separate from HERD JWT. |

## AI orchestrator

The orchestrator supports two backends via `AI_PROVIDER`: `anthropic` (the AsyncAnthropic SDK against the Anthropic API) and `openai_compat` (the AsyncOpenAI SDK against any compatible chat-completions endpoint, including vLLM, Ollama, LM Studio, OpenAI proper, and Azure OpenAI). All three AI endpoints gate on `ai_is_configured()` and return 503 when the active provider is not configured.

| Variable | Default | Purpose |
|---|---|---|
| `AI_PROVIDER` | `anthropic` | Backend selector: `anthropic` or `openai_compat`. |
| `AI_BASE_URL` | (empty) | Endpoint URL when `AI_PROVIDER=openai_compat`, e.g. `http://vllm:8000/v1` or `http://ollama:11434/v1`. Ignored when `AI_PROVIDER=anthropic`. |
| `AI_API_KEY` | (empty) | Canonical API key. Local servers (vLLM, Ollama, LM Studio) typically accept any non-empty placeholder; the orchestrator sends `EMPTY` when this is blank under `openai_compat`. |
| `AI_MODEL` | `claude-sonnet-4-6` | Model identifier passed to the provider. Format is provider-specific: `claude-*` for `anthropic`; provider-and-deployment-specific for `openai_compat` (e.g. `Qwen/Qwen3-35B-Instruct` on vLLM, `gpt-4o-mini` on OpenAI proper). |
| `AI_MAX_TOKENS` | `4096` | Per-call token cap. |
| `AI_TLS_VERIFY` | `true` | Verify the TLS certificate of `AI_BASE_URL`. Set `false` only for an `openai_compat` endpoint behind a self-signed certificate (e.g. an on-prem vLLM server); the connection otherwise fails certificate verification before auth. Ignored for the `anthropic` provider. Prefer `AI_CA_CERT` over this for a known on-prem endpoint. |
| `AI_CA_CERT` | (empty) | Path (inside the container) to a CA bundle to verify `AI_BASE_URL` against, e.g. a pinned self-signed on-prem certificate. Takes precedence over `AI_TLS_VERIFY`: verification stays on and fails closed, which is preferable to disabling verification. Mount the cert into the orchestrator container (see `docker-compose.yml`) and set this to its in-container path. Ignored for the `anthropic` provider. |
| `ANTHROPIC_API_KEY` | (empty) | **Deprecated.** Use `AI_API_KEY` instead. Honored as a fallback for `AI_API_KEY` for one release with a startup warning; removed in the next release. |
| `UPLOAD_MAX_FILE_BYTES` | `5242880` (5 MB) | Per-file cap for AI reference uploads. |
| `UPLOAD_MAX_FILES` | `5` | Max files per AI request. |
| `UPLOAD_MAX_EXTRACTED_CHARS` | `80000` | Aggregate text extracted from all files; per-file `truncated` flag appears in the response. |
| `ASSISTANT_MAX_TOOL_ITERATIONS` | `8` | Reservation assistant tool-use loop cap. On hit, one final call without tools forces a graceful answer. |
| `ASSISTANT_TOOL_RESULT_CHAR_CAP` | `8000` | Per-tool-result truncation ceiling. Larger payloads are clipped with a `[truncated: N chars omitted]` marker before reaching the model. |
| `ASSISTANT_OVERALL_DEADLINE_S` | `90.0` | Hard deadline for the reservation assistant route. 504 above this. |
| `ASSISTANT_PER_CALL_TIMEOUT_S` | `20.0` | Per-call timeout inside the assistant loop. 502 above this. |
| `AI_WRITE_TOOLS_ENABLED` | `false` | Expose the iter-3 write tools (propose_config_change, schedule_config_apply) to the reservation assistant. The tools always default to dry_run=true and route through the existing inventory schedule endpoint with full ACL gating; even with the flag on, no real apply runs without a user confirming the dry-run transcript via the UI. |
| `ASSISTANT_CONVERSATION_TTL_HOURS` | `24` | Multi-turn assistant conversations idle past this are deleted by the hourly sweeper. Reopening a reservation modal within TTL resumes the prior thread via sessionStorage. |
| `ASSISTANT_MAX_TURNS` | `40` | Hard cap on total messages (user + assistant + tool_result) per conversation. When exceeded, the oldest user+assistant pair drops; the position-0 seed message is pinned. |
| `ASSISTANT_HISTORY_TOKEN_BUDGET` | `60000` | Approximate input-token budget per conversation (chars/4 estimate, no tokenizer dependency). When exceeded, eviction runs to bring history back under the budget. |
| `ASSISTANT_SWEEPER_INTERVAL_SECONDS` | `3600` | Background sweeper interval. Each cycle deletes conversations older than the TTL. |

### Database (multi-turn assistant)

The orchestrator gained its first DB-backed feature in the multi-turn chat work; it persists conversations + messages in the `ai_orchestrator` schema in the shared Postgres. The schema is created by `infra/postgres/init.sql` on a fresh install and managed by Alembic at `services/ai-orchestrator/migrations/`. Run `make migrate-ai-orchestrator` (or `make migrate`) to apply pending revisions.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | (required) | Async Postgres URL, e.g. `postgresql+asyncpg://herd:...@postgres:5432/herd`. Provided by docker-compose. |
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
| `DRIVER_CACHE_PATH` | `/data/driver-cache` | Local driver cache path. Volume-backed. |
| `DRIVER_RLIMIT_AS_BYTES` | `268435456` | POSIX `RLIMIT_AS` (address space) for the driver subprocess, in bytes; 256 MB default. `0` disables. Raise or disable for numpy/pandas/BLAS drivers, which reserve large virtual address space. |
| `DRIVER_RLIMIT_CPU_SECONDS` | `60` | POSIX `RLIMIT_CPU` for the driver subprocess, in seconds. `0` disables. |
| `DRIVER_RLIMIT_NOFILE` | `256` | POSIX `RLIMIT_NOFILE` (open files) for the driver subprocess. `0` disables. |
| `DRIVER_RLIMIT_NPROC` | `64` | POSIX `RLIMIT_NPROC` (processes, per service uid) for the driver subprocess. `0` disables. |
| `ALLOW_DRIVER_PIP_INSTALL` | `false` | When `true`, a driver package's `requirements.txt` is `pip install`ed at execution time. Off by default: a runtime install pulls network code as the service user. When off, vendor deps into the package's `_deps/`. |

The execution service also hosts the periodic health-poll scheduler (ROADMAP #13). Each polled device runs the existing driver `login`, `status`, `logout` sequence on its configured cadence; outcomes drop into `device_health_status` and history persists in `execution_runs`.

| Variable | Default | Purpose |
|---|---|---|
| `HEALTH_POLL_SCHEDULER_ENABLED` | `true` | Enable the in-process asyncio scheduler. Set to `false` for read-only replicas or debugging. |
| `HEALTH_POLL_SCHEDULER_TICK_SECONDS` | `30` | Tick cadence. Each tick scans `device_health_status` for due rows and fires up to 10. Lowering this finds due rows faster but increases DB load. |
| `HEALTH_POLL_REGISTRY_REFRESH_SECONDS` | `300` | How often the scheduler re-fetches the device list from inventory's `/devices/health-config` endpoint. New devices or interval changes take effect within roughly this window. |
| `HEALTH_POLL_MAX_CONSECUTIVE_FAILURES` | `3` | Failures past this threshold trigger exponential backoff with jitter, so an UNREACHABLE device does not flood `execution_runs`. |
| `HEALTH_POLL_BACKOFF_CAP_SECONDS` | `3600` | Upper bound on backoff between polls. |
| `HEALTH_POLL_MINIMUM_INTERVAL_SECONDS` | `30` | Floor enforced by inventory's schema validators when setting `poll_interval_seconds` on devices or templates. |
| `HEALTH_POLL_NOTIFY_ENABLED` | `true` | Publish a `device.health_transition` NATS event when a device crosses the failure threshold (bad_news) or recovers. Set to `false` to silence alerts without rolling back the publisher code. |

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
| `RESERVATION_START_GRACE_SECONDS` | `300` | On create, a `start_time` earlier than now minus this grace is rejected (422), so a user cannot book a window that already passed. The grace tolerates clock skew and "start now"; the expiration loop still activates PENDING reservations whose start has ticked past. |
| `RESERVATION_MAX_DURATION_SECONDS` | `2592000` | On create, a window longer than this (default 30 days) is rejected (422), guarding against runaway or typo'd bookings. `0` disables the cap. |

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
