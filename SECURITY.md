# Security Policy

## Supported Versions

HERD is developed on `main` and tagged for release (the latest is v0.6.0, with a `release/0.6.0` branch at the tagged commit). There is no long-term-support branch today; fixes land on `main`, so please run the latest release or a recent commit.

| Version | Supported |
| ------- | --------- |
| main (latest commit) | yes |
| older | no |

If you are operating an older checkout, upgrade to the latest release or pull `main` before filing a security report so we're both working from the same code.

## Reporting a Vulnerability

Please do **not** open a public issue for suspected security vulnerabilities.

Use GitHub's private vulnerability reporting (the "Report a vulnerability" button under the repository's Security tab) to send a description of the issue and reproduction steps to the maintainer privately. If that is unavailable, open a GitHub issue titled "Security contact request" (no details) and the maintainer will reach out privately to take the report off the public tracker.

You can expect an acknowledgment within one week. We aim to triage and provide an initial assessment within two weeks. Accepted issues will be patched on `main`; a write-up is published after the fix ships. Published advisories are listed at <https://github.com/vendrabuck/HERD/security/advisories>; three were published with v0.6.0.

## What counts as a security issue

- Authentication or authorization bypass.
- Ability to read or modify another user's data when your role or device-group permissions should have blocked it.
- Remote code execution via driver upload, AI-generated config, or any other input path.
- Secrets leaked in logs, error responses, or the frontend.
- Denial-of-service caused by a single authenticated request.

Bugs that cause incorrect data but are gated behind admin or superadmin access are still worth reporting but are lower priority; file them as a regular issue unless you can escalate via a user-role account.

## Threat model summary

- **JWT**: signed with a shared `AUTH_SECRET_KEY`; every service verifies locally. Rotating the secret invalidates every live access token, but not the sessions behind them: refresh tokens are opaque random values stored as SHA-256 hashes and do not depend on the signing key, so a client holding an unexpired refresh token obtains a new access token signed with the new key, and API tokens keep exchanging too. To end sessions, revoke the refresh tokens as well: a logout revokes the presented refresh token, deactivating an account stops its refresh and API token exchange, `DELETE /api/auth/tokens/{id}` revokes an API token, and there is no API route that revokes every refresh token at once (set `revoked` on the rows of the auth schema's `refresh_tokens` table). Inside the auth service every authorization decision uses the lower of the token's `role` claim and the account's current database role, so a demotion binds there immediately; the other services trust the claim until the token expires.
- **Host ports**: only Traefik's 80 and 443 are published on all interfaces. The base `docker-compose.yml` binds Postgres (5433), NATS (4222 and the 8222 monitoring port), and the Traefik dashboard (8080) to `127.0.0.1`. NATS itself has no broker authentication, so anything that can reach its port can publish events; keep it off untrusted networks and reach it remotely only through an SSH port-forward. Execution corroborates each reservation lifecycle event with the reservations service before acting on it, which limits what a forged event can do.
- **Internal service-to-service calls**: authenticated with `X-Internal-Token`. Endpoints that accept the internal token are named with `/internal`, `/internal-download`, or similar suffixes. Internal-token endpoints are not meant to be reachable from the public internet; Traefik routes them through `/api/<service>` like any other endpoint, so anyone with the token + HTTP access to the service can call them. Treat the token as a shared secret.
- **AI-generated configs** (from the LLM) are validated against a small allowlist per connection type before any call to the execution service; unknown keys are rejected at the orchestrator boundary. See [docs/AI_GENERATE.md](docs/AI_GENERATE.md).
- **Driver code** runs in a subprocess sandbox with a configurable timeout. The sandbox is NOT a full security boundary: driver code can read the device context (including credentials), make outbound network calls, and in principle do anything a Python process can do. Only upload drivers you trust.
- **Config service**: the first-run configuration UI (`/api/config`) has its own auth, separate from the HERD JWT. Its session token is signed with a per-process random key (or a pinned `CONFIG_SESSION_SECRET`), never a source-visible constant. The login password comes from `CONFIG_ADMIN_PASSWORD`; when unset a random one-time password is generated and logged on first boot, and the config write and apply endpoints stay locked (HTTP 403) until that seeded password is changed. The write/apply surface is routed through the public gateway like any other endpoint, so treat the config password as a privileged credential (it gates the ability to rewrite `.env` values and restart the stack).
- **Secrets service**: named credentials are AES-GCM envelope-encrypted at rest; the key-encryption key comes only from the `SECRETS_KEK` environment variable (no default, the service refuses to boot without it), so a database dump alone never yields plaintext. Reveal is gated on an ACL `manage` grant or admin role; plaintext appears only in value responses, never in logs or metadata. Note that per-device `field_data` password fields predate this store and are redacted rather than encrypted; migrating them is a tracked follow-up. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
- **Reverse proxy**: Traefik terminates TLS with a custom PKI chain. Install `infra/traefik/certs/root-ca.crt` as a trusted root on client machines. The Traefik dashboard (`:8080`) is unauthenticated and bound to loopback by default; never publish it on a public interface.

## What is out of scope

- Vulnerabilities in upstream dependencies (FastAPI, SQLAlchemy, React, etc.) unless HERD's usage amplifies them; please report those to the upstream project.
- Attacks requiring an attacker-controlled admin or superadmin account. Admin and superadmin are trusted roles by definition.
- Attacks requiring physical access to the deployment host.
- Denial of service by overwhelming the backend with authenticated requests at expected rate limits.
