# Security Policy

## Supported Versions

HERD is released from `main`. There is no long-term-support branch today; please run a recent commit.

| Version | Supported |
| ------- | --------- |
| main (latest commit) | yes |
| older | no |

If you are operating an older checkout, pull `main` before filing a security report so we're both working from the same code.

## Reporting a Vulnerability

Please do **not** open a public issue for suspected security vulnerabilities.

Use GitHub's private vulnerability reporting (the "Report a vulnerability" button under the repository's Security tab) to send a description of the issue and reproduction steps to the maintainer privately. If that is unavailable, open a GitHub issue titled "Security contact request" (no details) and the maintainer will reach out privately to take the report off the public tracker.

You can expect an acknowledgment within one week. We aim to triage and provide an initial assessment within two weeks. Accepted issues will be patched on `main`; a write-up is published after the fix ships.

## What counts as a security issue

- Authentication or authorization bypass.
- Ability to read or modify another user's data when your role or device-group permissions should have blocked it.
- Remote code execution via driver upload, AI-generated config, or any other input path.
- Secrets leaked in logs, error responses, or the frontend.
- Denial-of-service caused by a single authenticated request.

Bugs that cause incorrect data but are gated behind admin or superadmin access are still worth reporting but are lower priority; file them as a regular issue unless you can escalate via a user-role account.

## Threat model summary

- **JWT**: signed with a shared `AUTH_SECRET_KEY`; every service verifies locally. Rotating the secret invalidates every live session.
- **Internal service-to-service calls**: authenticated with `X-Internal-Token`. Endpoints that accept the internal token are named with `/internal`, `/internal-download`, or similar suffixes. Internal-token endpoints are not meant to be reachable from the public internet; Traefik routes them through `/api/<service>` like any other endpoint, so anyone with the token + HTTP access to the service can call them. Treat the token as a shared secret.
- **AI-generated configs** (from the LLM) are validated against a small allowlist per connection type before any call to the execution service; unknown keys are rejected at the orchestrator boundary. See [docs/AI_GENERATE.md](docs/AI_GENERATE.md).
- **Driver code** runs in a subprocess sandbox with a configurable timeout. The sandbox is NOT a full security boundary: driver code can read the device context (including credentials), make outbound network calls, and in principle do anything a Python process can do. Only upload drivers you trust.
- **Config service**: the first-run configuration UI (`/api/config`) has its own auth, separate from the HERD JWT. Its session token is signed with a per-process random key (or a pinned `CONFIG_SESSION_SECRET`), never a source-visible constant. The login password comes from `CONFIG_ADMIN_PASSWORD`; when unset a random one-time password is generated and logged on first boot, and the config write and apply endpoints stay locked (HTTP 403) until that seeded password is changed. The write/apply surface is routed through the public gateway like any other endpoint, so treat the config password as a privileged credential (it gates the ability to rewrite `.env` values and restart the stack).
- **Secrets service**: named credentials are AES-GCM envelope-encrypted at rest; the key-encryption key comes only from the `SECRETS_KEK` environment variable (no default, the service refuses to boot without it), so a database dump alone never yields plaintext. Reveal is gated on an ACL `manage` grant or admin role; plaintext appears only in value responses, never in logs or metadata. Note that per-device `field_data` password fields predate this store and are redacted rather than encrypted; migrating them is a tracked follow-up. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
- **Reverse proxy**: Traefik terminates TLS with a custom PKI chain. Install `infra/traefik/certs/root-ca.crt` as a trusted root on client machines. Never expose the Traefik dashboard (`:8080`) publicly.

## What is out of scope

- Vulnerabilities in upstream dependencies (FastAPI, SQLAlchemy, React, etc.) unless HERD's usage amplifies them; please report those to the upstream project.
- Attacks requiring an attacker-controlled admin or superadmin account. Admin and superadmin are trusted roles by definition.
- Attacks requiring physical access to the deployment host.
- Denial of service by overwhelming the backend with authenticated requests at expected rate limits.
