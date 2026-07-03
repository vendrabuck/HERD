# Decision: Encrypted-at-Rest Credential Store, Issue #39

Status: Accepted 2026-07-03; implemented the same day as the `secrets` service
(thirteenth `services/` directory). The four decision points below were
resolved to their recommended defaults: manage-only reveal, one encrypted blob,
refuse-to-boot on missing key material, API-only v1. No code in this doc.
Context verified against the live HERD-public tree on 2026-07-03.

## Context

Device secrets today live as password-typed fields inside inventory
`field_data`, protected only by downstream stripping: redaction on non-admin
device reads (`services/inventory/app/routers/devices.py:31-68`), exclusion
from driver environment variables
(`services/execution/app/services/driver_sandbox.py:19-48,196-201`), and
removal from LLM payloads
(`services/ai-orchestrator/app/services/tools.py:740-787`). The plaintext is
still stored in the database. Issue #39 asks for one access-controlled,
encrypted-at-rest home for provisioning credentials, as the prerequisite for
dynamic resources (#32).

Relevant existing fabric, verified:

- ACL grants: `resource_grants` table keyed by
  (group_id, resource_type, resource_id, permission), permissions `view` and
  `manage`, resource types validated against `VALID_RESOURCE_TYPES = {"device",
  "topology", "reservation"}` (`services/acl/app/schemas/grant.py:6-7`).
  Consumers call `POST /check` fail-closed via
  `services/common/herd_common/acl.py` (network error, non-200, or malformed
  response all deny).
- Auth: `make_auth_dependencies` in `services/common/herd_common/auth.py`
  (JWT 401, role 403); internal endpoints check `X-Internal-Token` and return
  403 on mismatch (convention across services).
- Crypto: the `cryptography` package is already a transitive dependency of
  every service via `python-jose[cryptography]`.
- Scaffold: the `integration` service (added 2026-06-29) is the reference for
  adding a service; its touchpoint list is reproduced below.
- Lesson from #246: never ship a hardcoded or defaulted key; missing key
  material must fail loudly.

## Decision

### A new `secrets` service

A dedicated `services/secrets/` FastAPI service with its own `secrets`
Postgres schema and Alembic chain, consistent with the schema-per-service
boundary. Secret storage is not bolted onto inventory; inventory's scattered
redaction sites are the evidence that inventory was never a secret store.

### Data model

Two tables in the `secrets` schema:

- `secrets`: id UUID PK, name (unique), type (string: api_key, ssh_key,
  password, token, generic), description, created_by, updated_by, created_at,
  updated_at, ciphertext BYTEA, nonce BYTEA, key_version INT (FK by value to
  key_versions.version, same schema).
- `key_versions`: version INT PK, wrapped_dek BYTEA, created_at,
  retired_at NULLABLE.

### Envelope encryption with AES-GCM

- A random 256-bit data-encryption key (DEK) encrypts secret values with
  AES-GCM. The DEK is stored only wrapped (itself AES-GCM-encrypted) by a
  key-encryption key (KEK) supplied via `SECRETS_KEK` (base64, 32 bytes).
  An external KMS can later replace the env-supplied KEK without schema
  change.
- Rotation asymmetry is the reason for two layers. KEK rotation (the common
  case) is O(number of key versions): re-wrap DEKs, never touch secret rows.
  DEK rotation (rare) creates a new key_version and is O(number of secrets);
  old versions stay decryptable until re-encryption completes.
- Associated data (AAD) for every encryption is the secret id plus
  key_version. Invariant: a ciphertext is valid only in the row it was
  written for; swapping ciphertexts between rows fails the GCM auth tag.
- Nonces are 96-bit random per encryption. Collision probability is about
  2^-33 at 2^30 encryptions per key version; not a constraint at HERD scale.
- A database dump alone never yields plaintext: it contains ciphertext,
  nonces, and wrapped DEKs, all useless without the KEK from the environment.
- Dependency: declare `cryptography` directly in the new service's
  pyproject; it is already in the image via python-jose.

### Authorization rides the existing ACL fabric

No grant storage in the new service. Add `"secret"` to
`VALID_RESOURCE_TYPES` in the ACL service (the set plus its three
field validators), regenerate the ACL contract snapshot, and consume
`POST /check` with the caller JWT exactly as `herd_common/acl.py` does,
fail-closed. Non-admin listing filters through the existing ACL
`GET /resources` endpoint.

### API surface

- `POST /secrets`, `PUT /secrets/{id}`, `DELETE /secrets/{id}`: admin JWT;
  responses carry metadata only.
- `GET /secrets`, `GET /secrets/{id}`: user JWT; ACL-filtered; metadata only,
  never plaintext.
- `GET /secrets/{id}/value`: user JWT plus ACL grant (see decision point 1);
  returns plaintext; response bodies excluded from logs.
- `GET /internal/secrets/{id}/value`: `X-Internal-Token`; 403 on missing or
  wrong token; the retrieval contract dynamic provisioning (#32) will consume.
- `GET /health`: unauthenticated.

Plaintext never appears in logs: `RequestLoggingMiddleware` already logs only
method, path, status, and duration; a test pins the property by scanning
captured logs for the plaintext across a full request cycle.

## Decision points (defaults chosen, awaiting sign-off)

1. Reveal gate: which ACL permission reveals plaintext to a user.
   Default: `manage` only; `view` sees metadata. Tighter default that can be
   loosened later without a breaking change.
2. Value shape: one encrypted blob (the whole secret dict JSON-serialized and
   encrypted as a single ciphertext) versus per-field ciphertexts with
   visible field names. Default: one blob; one invariant, one round-trip,
   field names leak nothing.
3. Missing or malformed `SECRETS_KEK` at startup: refuse to boot versus a
   503 gate on secret endpoints. Default: refuse to boot; the compose
   healthcheck stays red and no half-alive service can accept writes it
   cannot encrypt.
4. v1 UI scope: API-only versus an admin secrets page. Default: API-only;
   the UI lands when a consumer exists (#32).

## Testing

- Unit (SQLite in-memory, no stack): encrypt/decrypt round-trip; tampered
  ciphertext rejected via auth tag; AAD swap between rows rejected; KEK
  re-wrap preserves decryption of values written under prior key versions;
  grant-denied paths; internal-token 403 wording; no plaintext in captured
  logs.
- Contract: new `secrets` OpenAPI snapshot; regenerated ACL snapshot.
- Integration (live stack): admin creates a secret; grants to a group; a
  member reveals; a non-member is denied; internal fetch with the token;
  rotation end to end.
- Load: skipped deliberately; secret reads are not a hot path.

## Scaffold checklist (from the integration-service precedent)

1. `services/secrets/` app, config, models, routers, migrations, tests,
   Dockerfile, pyproject.
2. `docker-compose.yml` service block (env, herd-config mount, healthcheck,
   depends_on) plus `docker-compose.override.yml` dev overrides.
3. Traefik labels for `/api/secrets` with strip-prefix middleware.
4. `infra/postgres/init.sql`: create and grant the `secrets` schema.
5. Makefile `SERVICES` and `DB_SERVICES` lists (auto-generates test-secrets,
   coverage-secrets, migrate-secrets, shell-secrets).
6. CI is covered via the Makefile lists; no workflow edits expected.
7. `tests/contract/snapshots/secrets.json` plus the contract suite's service
   list.
8. Docs: `docs/ARCHITECTURE.md` service table and description,
   `docs/ROLES.md` permission matrix rows, `docs/ENV_VARS.md` for
   `SECRETS_KEK`, `FEATURES.md` and `PLANNED_FEATURES.md` status flip.

## Out of scope

- Migrating existing inventory `field_data` passwords. The seam is left
  open: a later `secret://<uuid>` reference convention in field values.
- External KMS or HSM as a requirement; the design allows it behind the KEK
  boundary.
- Vault-style dynamic or leased credentials.
- The dynamic-resources feature itself (#32); this store is its
  prerequisite, not its implementation.
