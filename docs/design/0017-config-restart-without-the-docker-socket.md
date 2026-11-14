# Decision: Save and Restart Without the Docker Socket in the Config Service, Issue #1326

Status: Accepted 2026-11-14 and built in the same pull request. The owner chose the
restarter sidecar (option B below) with the non-root config user, an allowlist restart
set, no token, the project name from compose, single flight, a documented one-time
volume fix, and a nightly run of the real restart path. Traefik's own socket is out of
scope and tracked as issue #1424. Context verified against the HERD-public tree at
`86765c9e` on 2026-11-14.

## Context

The config service is the first-run configuration page. It is routed through the
public gateway (`PathPrefix(/api/config)` in `docker-compose.yml`), answers some
routes without a session (`/health`, `/version`, `/status`, `/login`, `/schema`), and
has its own password, separate from HERD accounts. It has had two authentication
defects before, both fixed (#246, a forgeable session key; #256, a source-visible
default password).

Before this change it held the host's Docker socket, through a bind mount in both
compose files, and ran as root (its Dockerfile was the only service image without a
`USER` line). Its one use of the socket was Save and Restart: `POST /apply` called
`restart_services()` in `services/config/app/docker_ctl.py`, which, through the docker
SDK, asked the daemon for its version, inspected its own container to learn its
compose project, listed that project's containers (inspecting each one), and
restarted every listed container whose service was not in a five-name skip list.
Nothing from the request reached those calls.

Two facts make that a problem:

- The Docker Engine API is equivalent to root on the host. Docker's documentation says
  only trusted users should control the daemon, and that membership of the `docker`
  group grants root-level privileges. A `:ro` bind of the socket stops the file from
  being replaced; it does not limit what can be sent over it.
- So the ceiling of any future flaw in the config service, or in a library it loads,
  was the host and every other compose project on it, not HERD. That holds whatever
  the flaw is, because the socket was reachable from the process itself, not only
  through `restart_services()`.

The issue states the invariant as "a gateway-routed container never holds an
unrestricted Docker API". Traefik also holds the socket (for discovery) and is the
most exposed container. The owner kept Traefik out of this change and restated the
goal as: **no container that runs HERD request handlers holds the socket**; Traefik is
issue #1424.

What did not change, and why it bounds the value of any option: the config operator's
designed power is to rewrite HERD's settings and restart the services that read them
(SECURITY.md, Config service). A flaw that yields a config session already yields that
power, with or without Docker. Every option below is judged by what a flaw in the
config service yields beyond it.

## Decision

### D1. A restarter sidecar holds the socket; the config service holds none

A new container, `restarter`, built from `infra/restarter/` (one standard-library
Python file, no third-party package, no `herd_common`), is the only HERD container
that holds the socket. It is infrastructure, not a thirteenth service: no database, no
FastAPI app, no Alembic chain, no gateway route, so it lives under `infra/` and not
under `services/`.

Its whole API:

- `POST /restart`, no parameters. The query string, the body, and every header are
  ignored. It answers 200 `{"restarted": [...], "errors": [...]}` (the shape
  `POST /apply` already answered), or 409 while another restart runs (D5).
- `GET /health`, liveness only, with no Docker call.
- Any other path is 404; any other method on those two paths is 405. Every answer is
  JSON and closes the connection.

What it asks the daemon, with the API version fixed at `v1.44` (Docker Engine 25 and
later), and nothing else:

- `GET /v1.44/containers/json` with exactly the filter
  `{"label":["com.docker.compose.project=P"]}`;
- `POST /v1.44/containers/<id>/restart?t=30` for each listed container whose own
  labels name project P and a service in the allowlist A (D3), where `<id>` must be
  64 lowercase hex characters, in service order.

It reads each daemon answer within one deadline (10 seconds for the list, 60 for each
restart), so a hung or slow daemon cannot hold the single-flight lock past a bounded
worst case (670 seconds with eleven services). Its errors keep the class-name-only
wording the config service used (`Cannot connect to Docker: <Class>`,
`Cannot list containers: <Class>`, `Failed to restart <service>: <Class>`).

The config service's `docker_ctl.py` becomes a standard-library HTTP client of
`http://restarter:8000/restart`, with a 720 second timeout (above the restarter's
worst case). It passes a 200 through only when the body is exactly
`{restarted, errors}` with two lists of short printable strings, and otherwise answers
a fixed sentence: `Cannot reach the restart service: <Class>`,
`The restart service answered HTTP <status>`,
`The restart service answered an unexpected body`, or, for the 409,
`A restart is already in progress`. The `docker` package leaves the config service's
dependencies, and the self-inspect by hostname is gone.

### D2. Network isolation, no token

A new compose network, `herd-ctl`, declared `internal: true`, is joined by `config`
and `restarter` only. The restarter is on no other network, publishes no port, and
carries no `traefik.*` label. There is no shared secret: the endpoint is
parameterless, and its effect equals what an authorized `POST /apply` already does,
so a token would add a required setting for no change in what a holder can cause.
`INTERNAL_API_TOKEN` is not reused: the config write surface can rewrite it, and every
service holds it.

### D3. The restart set is an allowlist equal to the readers of config.json

A is exactly the compose services that mount `herd-config:/etc/herd:ro`, the services
that read `config.json` (eleven today). The restarter holds A as a literal constant,
and a repo-root guard recomputes A from both compose files and fails on any
difference. The previous skip list failed open: a new service, or Selenium on the e2e
profile, was restarted by default. An allowlist fails closed: a new service is not
restarted until it reads the config file, at which point the guard makes the
constant follow.

### D4. The project comes from compose, not from the container

The restarter reads P from `HERD_COMPOSE_PROJECT`, which compose sets to
`${COMPOSE_PROJECT_NAME:?...}`, the same interpolation Traefik's project constraint
uses. Compose then refuses to render the file when the name is empty, and the
restarter itself refuses a restart, making no Docker request, when the variable is
unset or blank. Per-worktree gate stacks (`-p <dir>-gate`) and the nightly's
`COMPOSE_PROJECT_NAME=herd` scope themselves with no change.

### D5. Single flight

One restart pass runs at a time. A second `POST /restart` while one runs is 409
`A restart is already in progress`, and the config service passes that sentence
through as its only `errors` entry. The lock is released when the pass ends, whether
it succeeded, reported errors, or failed unexpectedly.

### D6. The config service runs as a non-root user

`services/config/Dockerfile` adds `appuser` and `USER appuser`, and creates
`/data/herd-config` owned by it, so a fresh `herd-config` volume copies that
ownership. A volume an earlier image created holds root-owned files. There is no root
entrypoint that fixes this at start (it would keep root in the container at boot);
instead:

- the operator runs a one-time command, documented in `docs/OPERATIONS.md` and the
  CHANGELOG;
- at startup the service checks that it can write the data directory and its files;
  when it cannot, it logs ONE error line naming the directory, its uid, and that
  command, skips the first-boot bootstrap (which writes), and keeps serving reads;
- a write the directory then refuses is 503
  `The config data directory is not writable; the config service log names the one-time fix`,
  never a 500, and the log line repeats the remedy.

### D7. The restarter runs as root, hardened

The host socket is owned by root with mode 0660. Uid 0 opens it with no capability. A
non-root user in the docker group would hold the same root-equivalent API and add a
host group id setting, so the restarter runs as root inside, with `read_only: true`,
`cap_drop: [ALL]`, and `no-new-privileges:true`. The Dockerfile user guard records
this exemption by name with its reason.

### D8. The real restart path runs every night

`tests/e2e/test_config_playwright.py::test_config_save_and_restart_gated`, gated by
`HERD_E2E_RESTART=1`, runs as the last test step of nightly.yml's full-stack job and
asserts the Restart Result panel lists exactly A and no error. Before this change the
real restart path had never run in any automated job.

## Invariants and what pins them

Let D(c) be the set of Docker API requests a process in container c can cause.

| Invariant | Pinned by |
|---|---|
| D(config) is exactly one parameterless `POST /restart` to the restarter: the config service imports no Docker client, holds no socket, and passes nothing from its request on | `services/config/tests/test_docker_ctl.py` (`test_the_one_call_is_a_bare_post_restart`, `test_apply_passes_nothing_from_the_request_on`, `test_no_module_under_app_imports_docker`); the socket-holder guard below |
| Nothing in a restarter request (query, body, headers, path suffix) reaches a Docker call | `infra/restarter/tests/test_restarter_http.py` (`test_no_request_parameter_reaches_docker`, `test_any_other_path_is_404_and_reaches_no_docker_call`) |
| The restarter's Docker requests are a subset of {the list with exactly the project filter, a restart of a 64-hex id with `t=30`}, and the restarted ids are exactly A intersected with containers(P) | `infra/restarter/tests/test_restarter_docker.py` (`test_restarts_exactly_the_allowlist_within_the_project`, `test_an_odd_container_id_never_reaches_a_path`) |
| An unset or blank project makes no Docker request | `infra/restarter/tests/test_restarter_docker.py` (`test_unset_or_blank_project_refuses_with_no_docker_request`) |
| One restart pass at a time | `infra/restarter/tests/test_restarter_http.py` (`test_single_flight_under_twenty_concurrent_calls`) |
| Socket holders in both compose files are exactly {restarter, traefik} | `tests/unit/test_restarter_compose_guard.py` (`test_socket_holders_are_exactly_restarter_and_traefik`) |
| The restarter is on `herd-ctl` only, with no ports and no `traefik.*` label; only config and restarter join `herd-ctl`, which is internal | `tests/unit/test_restarter_compose_guard.py` (`test_restarter_is_on_herd_ctl_only_with_no_ports_or_gateway_route`, `test_only_config_and_restarter_join_the_internal_herd_ctl`) |
| A equals the services that mount `herd-config:/etc/herd:ro` | `tests/unit/test_restarter_compose_guard.py` (`test_restart_allowlist_equals_the_services_that_read_config_json`) |
| The restarter runs read-only with no capabilities and no-new-privileges | `tests/unit/test_restarter_compose_guard.py` (`test_restarter_runs_read_only_without_capabilities`) |
| Every `services/*/Dockerfile` runs as a non-root user; the restarter's exemption is by name | `tests/unit/test_dockerfile_users.py` (`test_the_image_runs_as_a_non_root_user_unless_exempt`) |

## Options rejected

- **A filtering socket proxy for the config service.** Two proxies were read. One
  cannot express "list, inspect, and restart only": its POST gate is evaluated before
  its restart switch, and its containers switch admits every method under that path,
  container creation included. The other can be limited by regular expressions over
  the URL path, but that scopes by syntax, not meaning: under a flaw in the config
  service it still allows restarting and inspecting any container on the host, other
  compose projects included, and it puts a third-party component with a recent
  critical advisory in front of the socket. The restarter scopes by meaning (one
  project, one allowlist) in about three hundred lines of in-repository code.
- **No socket at all; the operator runs the restart.** Strictly smaller (D(config)
  empty), but it removes the one-click Save and Restart, so a remote config edit then
  needs a shell on the host, and it rewrites four specification rules and the page.
  The security it adds over the restarter is the difference between "a flaw in the
  config service can restart P's config readers" and nothing, where an authorized
  operator can already do the former.
- **A non-root config user with the docker group, alone.** The docker group is root
  equivalent, so the worst case under a flaw does not change. It is kept as part of
  the decision (D6) because it matters once the socket is gone.
- **The socket only in an opt-in compose overlay, absent in `make prod`.** It secures
  the stack that runs least and leaves the dev, gate, CI, and demo stacks as they
  were, and every bare `docker compose` call would have to name three files
  consistently.
- **Services restart themselves on a config epoch.** No socket anywhere but Traefik,
  but it changes the lifecycle of eleven services, needs a watcher in `herd_common`
  that must stay off in tests, migrations, and the poller-only replica, and the
  result panel could no longer list what restarted. Too large for a narrow problem.

## Consequences

- One more container in every stack (dev, gate, CI, nightly, prod). It is small, has
  no state, and needs no setting.
- Upgrading needs a rebuild, not a restart: `make up` or `make prod` builds the
  restarter and recreates config without the socket; `make restart` swaps no image.
  An existing `herd-config` volume needs the one-time `chown` in
  `docs/OPERATIONS.md`; until then the config page reads but does not save.
- Save and Restart no longer restarts Selenium (it never read the config file).
- The restart set changes only through the compose files and the restarter's
  constant together; the guard fails until both agree.
- Specification moves: OPS-CONFIG-13, OPS-CONFIG-14, and OPS-CONFIG-16 are restated
  around the restarter, OPS-CONFIG-19 gains its nightly run, OPS-CONFIG-24 to
  OPS-CONFIG-29 are new, and OPS-GUARD-6 and OPS-GUARD-7 pin the compose and
  Dockerfile clauses (`docs/specs/operations-and-observability.md`).
- Residual risk: a flaw in the restarter itself is host root. Its input is one
  parameterless request from one peer on an internal network, parsed by the standard
  library, which is the smallest surface any socket holder in this design can have.
  Traefik still holds the full socket (issue #1424).

## Revisit when

- a second caller needs Docker (a new endpoint on the restarter needs its own
  invariant and guard, not a parameter on `POST /restart`);
- the deployment moves to rootless Docker (the socket then belongs to the rootless
  user, and D7's reasoning changes) or off Compose;
- issue #1424 lands and Traefik's socket goes behind a read-only proxy.
