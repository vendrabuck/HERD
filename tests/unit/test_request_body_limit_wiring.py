"""Every service app bounds its request bodies, with its setting; every route whose
body may hold more declares it; and every body one HERD service sends another fits
the receiving route (issue #1466).

`herd_common.validation_errors.install_validation_errors` installs `LimitRequestBody`
just outside `RefuseLoneSurrogates`; config installs its standalone copy's. The AST
guard `test_validation_errors_wiring.py` pins the install line's source; this guard
pins the BUILT apps, so it sees what the source scan cannot, and scans every
service's outbound HTTP calls.

What is checked. The services are every directory under `services/` with an
`app/main.py`, found from the directory (today twelve). Each one's `app.main` is
imported in a subprocess of its own (every service's package is named `app`, so two
cannot share a process; the reason is spelled out in
`test_compose_settings_wiring._load_settings_fields`), with `MAX_REQUEST_BODY_BYTES`
and the upload settings set to values no default has. For each built app:

- `app.user_middleware`, in order, is exactly `MIDDLEWARE` below: the limit and then
  the surrogate check last (config: the limit last, since it installs without the
  surrogate check), each from the module the service must use, with nothing between
  them and the routes. A new middleware, anywhere in the list, fails until someone
  adds it here. This proves which middleware classes are installed and in what order;
  it does not prove what a middleware does (that none outside the limit reads the
  body is a fact about the request logger and CORS, which read no body today), and it
  does not see Starlette's own ServerErrorMiddleware and ExceptionMiddleware, which
  read no body, or anything outside the app (uvicorn, the gateway);
- the limit it was given is the environment's `MAX_REQUEST_BODY_BYTES`, so the
  setting reaches the middleware in every service (config through
  `app/request_limits.py`, the others through `HerdBaseSettings`);
- the routes marked with `extends_body_limit`, with each one's extra bytes, equal
  `UPLOAD_ROUTES` plus `FORWARD_ROUTES` below. Every route that takes a form body
  (any `File(...)` or `Form(...)` parameter, as FastAPI decides it:
  `body_field.field_info` is a `params.Form`) is marked, unless it is named in
  `FORM_EXEMPT` with a reason (none today: no HERD route takes a small form); a
  marked upload route that takes no form is one of `JSON_UPLOAD_ROUTES` (a JSON body
  that carries a file as base64). The upload bytes follow the settings the routes
  enforce (`DRIVER_MAX_SIZE_BYTES`, `UPLOAD_MAX_FILES` times `UPLOAD_MAX_FILE_BYTES`,
  the base64 length of `VALIDATE_PACKAGE_MAX_BYTES`), set here to odd values to prove
  it; a forward route's extra bytes are `FORWARD_ENVELOPE_BYTES`;
- `docker-compose.yml` passes `MAX_REQUEST_BODY_BYTES` to all twelve services as
  `${MAX_REQUEST_BODY_BYTES:-<default>}` with the shared default (the Settings-based
  services are also held by `test_compose_settings_wiring.py`; config is not, since
  it has no Settings model);
- the area specification names every marked route.

The forwarding table. `outbound_body_calls` scans every module under a service's
`app/` and `herd_common`, and finds each call that sends a body:

- any call with a `json=` keyword, or a `json_body=` keyword that is not `None`;
- a call to `.post`, `.put`, `.patch`, `.delete`, `.request`, `.stream`,
  `.build_request`, or `Request` (`httpx.Request`) with a `content=`, `data=`,
  `files=`, `body=`, or `json=` keyword, or with a `**` splat;
- a call to one of HERD's own forwarders in `TRANSPORTS` (`call_service`,
  `_cabling_fork_call`, `_execution_wiring_call`, `_forward`) with a body passed by
  keyword, positionally where the forwarder takes it positionally, or through a `**`
  splat.

Each call site is keyed by its module, the function that makes it, and its receiver:
the callee, the HTTP method, and the URL or path as written (a local name assigned
once in the function is resolved to what it holds), with an ordinal when one function
makes the same call again. No line number is part of the key, so moving a call, or the
function that holds it, does not change it; a second body call in a classified
function, even to the same receiver, is a new key. That set must equal `FORWARDS`
below, both ways, so a new outbound body fails until it is classified:

- "envelope": it forwards a body the sending service admitted, or a stored document,
  with something added; every receiving route it names must be in `FORWARD_ROUTES`
  (marked with `FORWARD_ENVELOPE_BYTES`);
- "marked": its receiver is an upload route in `UPLOAD_ROUTES`;
- "capped": the sender bounds what it sends itself (named in the reason);
- "bounded": the body is bounded by schemas and caps; the stated bound must be at
  most `MIN_MAX_REQUEST_BODY_BYTES`, so no setting can make the call fail, or it is
  `None` with the reason the bound comes from elsewhere (model or driver output);
- "transport": a generic forwarder whose body comes from its callers, which are
  classified themselves;
- "external": the receiver is not a HERD service.

Limits. Only FastAPI routes are enumerated (`fastapi.routing.iter_route_contexts`
over the built app). Not seen: a route that reads a form or a stream by hand through
`Request` (`await request.form()`, `request.stream()`), which takes the default limit
like any other body; none reads a form or stream that way today, and the one raw read,
integration's test-stack webhook sink (`await request.body()`), takes the default.
A sub-application mounted with `app.mount` would not be enumerated, and is refused by
`test_validation_errors_wiring.py` (no second FastAPI app under `app/`). The scan
reads source, and does not see: a body passed positionally to an HTTP client method
(`client.post(url, payload)`; httpx takes bodies by keyword only, so this is a call
httpx would refuse) or to a new forwarder not yet named in `TRANSPORTS`; a body sent
through a client method of another name, or through a non-httpx client; a call made
through `getattr`, a stored bound method, or a function passed in and called by
another name; and a body built elsewhere and sent with `client.send(request)` unless
the `Request(...)` or `build_request(...)` that built it is in a scanned module. The
receiver in a key is the text the code wrote, not the URL it resolves to at run time
(a URL held in an attribute or computed by a helper is keyed by that expression); the
classification of each call (its kind and its bound) is a reviewed statement, not
something the scan computes; and a receiver named in an "envelope" entry is checked
to be marked, not checked to be the URL the code calls.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pytest
import yaml
from herd_common.validation_errors import (
    DEFAULT_MAX_REQUEST_BODY_BYTES,
    FORWARD_ENVELOPE_BYTES,
    MIN_MAX_REQUEST_BODY_BYTES,
)

REPO = Path(__file__).resolve().parents[2]
SERVICES_DIR = REPO / "services"
COMPOSE = REPO / "docker-compose.yml"
SPEC = REPO / "docs" / "specs" / "operations-and-observability.md"

LIMIT_ENV = "MAX_REQUEST_BODY_BYTES"
PROBE_LIMIT = 1_234_567
PROBE_DRIVER_BYTES = 777_001
PROBE_UPLOAD_FILES = 3
PROBE_UPLOAD_FILE_BYTES = 100_003
PROBE_PACKAGE_BYTES = 300_001
IMPORT_UPLOAD_BYTES = 10 * 1024 * 1024

# Every upload route in HERD and the bytes it may hold beyond the default limit.
UPLOAD_ROUTES = {
    ("ai-orchestrator", "POST", "/generate"): PROBE_UPLOAD_FILES * PROBE_UPLOAD_FILE_BYTES,
    ("cabling", "POST", "/topologies/import"): IMPORT_UPLOAD_BYTES,
    ("inventory", "POST", "/devices/import"): IMPORT_UPLOAD_BYTES,
    ("inventory", "POST", "/templates/import"): IMPORT_UPLOAD_BYTES,
    ("inventory", "POST", "/drivers"): PROBE_DRIVER_BYTES,
    ("inventory", "PUT", "/drivers/{driver_id}/file"): PROBE_DRIVER_BYTES,
    ("execution", "POST", "/internal/validate-package"): 4 * ((PROBE_PACKAGE_BYTES + 2) // 3),
}
# The marked upload routes whose body is JSON, not a form: each carries a file as base64.
JSON_UPLOAD_ROUTES = {("execution", "POST", "/internal/validate-package")}

# Every route another HERD service forwards a body to, each with the envelope allowance.
FORWARD_ROUTES = {
    ("cabling", "PUT", "/internal/forks/{reservation_id}/canvas"): FORWARD_ENVELOPE_BYTES,
    ("cabling", "POST", "/internal/forks/{reservation_id}/save"): FORWARD_ENVELOPE_BYTES,
    ("execution", "POST", "/execute"): FORWARD_ENVELOPE_BYTES,
    ("execution", "POST", "/execute/internal"): FORWARD_ENVELOPE_BYTES,
    ("reservations", "POST", "/"): FORWARD_ENVELOPE_BYTES,
    ("user-profile", "PATCH", "/preferences"): FORWARD_ENVELOPE_BYTES,
}

# A route that takes a form and needs no larger limit (a small form, not an upload):
# (service, method, path) to the reason. None today.
FORM_EXEMPT: dict[tuple[str, str, str], str] = {}

_STANDARD = [
    "RequestLoggingMiddleware",
    "CORSMiddleware",
    "LimitRequestBody",
    "RefuseLoneSurrogates",
]
# Each service's built `app.user_middleware`, outermost first.
MIDDLEWARE = {service: _STANDARD for service in ("acl", "ai-orchestrator", "auth")}
MIDDLEWARE.update(
    {
        service: _STANDARD
        for service in (
            "cabling",
            "execution",
            "integration",
            "inventory",
            "notifications",
            "reservations",
            "secrets",
            "user-profile",
        )
    }
)
MIDDLEWARE["config"] = ["CORSMiddleware", "LimitRequestBody"]

# Every outbound call that sends a body, keyed by (module, function): (kind, detail).
# For "envelope" the detail is the receiving routes; for "bounded" it is (bytes, why).
_ENVELOPE = "envelope"
FORWARDS: dict[tuple[str, str, str], tuple[str, object]] = {
    (
        "services/ai-orchestrator/app/services/cabling_client.py",
        "fetch_pathfind_batch",
        "post POST f'{settings.cabling_service_url.rstrip('/')}/pathfind/batch'",
    ): ("bounded", (24000, "PATHFIND_BATCH_CHUNK (200) pairs of two device ids")),
    (
        "services/ai-orchestrator/app/services/committer.py",
        "_apply_configs",
        "post POST f'{settings.execution_service_url.rstrip('/')}/execute'",
    ): ("envelope", [("execution", "POST", "/execute")]),
    (
        "services/ai-orchestrator/app/services/committer.py",
        "_create_reservation",
        "post POST f'{settings.reservations_service_url.rstrip('/')}/'",
    ): ("envelope", [("reservations", "POST", "/")]),
    (
        "services/ai-orchestrator/app/services/committer.py",
        "_create_topology",
        "post POST f'{settings.cabling_service_url.rstrip('/')}/topologies'",
    ): ("bounded", (1300, "a topology name of at most 200 characters")),
    (
        "services/ai-orchestrator/app/services/committer.py",
        "_update_topology_canvas",
        "put PUT f'{settings.cabling_service_url.rstrip('/')}/topologies/{topology_id}'",
    ): (
        "capped",
        "commit_proposal measures the canvas (canvas_put_bytes) against MAX_REQUEST_BODY_BYTES"
        " and answers 413 before anything is written",
    ),
    (
        "services/ai-orchestrator/app/services/providers/anthropic_provider.py",
        "AnthropicProvider.call_stream",
        "stream ? ?",
    ): ("external", "the model provider's SDK"),
    (
        "services/ai-orchestrator/app/services/purpose_signals.py",
        "_fetch_devices_batch_preview",
        "post POST f'{base}/devices/batch'",
    ): ("bounded", (10000, "a reservation's device ids, at most 200 plus 50 dynamic")),
    (
        "services/ai-orchestrator/app/services/recipe_author.py",
        "validate_with_execution",
        "post POST f'{settings.execution_service_url}/internal/validate-package'",
    ): ("marked", [("execution", "POST", "/internal/validate-package")]),
    (
        "services/ai-orchestrator/app/services/tools.py",
        "ToolDispatcher._tool_find_path",
        "post POST f'{settings.cabling_service_url.rstrip('/')}/pathfind'",
    ): ("bounded", (200, "two device ids")),
    (
        "services/ai-orchestrator/app/services/tools.py",
        "ToolDispatcher._tool_propose_config_change",
        "post POST f'{settings.inventory_service_url.rstrip('"
        "/')}/devices/{device_id}/config-versions'",
    ): ("bounded", (None, "the model's tool arguments, bounded by AI_MAX_TOKENS output tokens")),
    (
        "services/ai-orchestrator/app/services/tools.py",
        "ToolDispatcher._tool_schedule_config_apply",
        "post POST f'{settings.inventory_service_url.rstrip('/')}/devices"
        "/{device_id}/config-versions/{version_id}/schedule'",
    ): ("bounded", (300, "a timestamp, a flag, and a reservation id")),
    ("services/auth/app/services/oidc_service.py", "_exchange_code", "post POST token_endpoint"): (
        "external",
        "the identity provider's token endpoint",
    ),
    (
        "services/cabling/app/services/device_resolver.py",
        "resolve_device_names",
        "post POST f'{settings.inventory_service_url.rstrip('/')}/devices/resolve-by-name'",
    ): ("capped", "name_batches keeps each body under RESOLVE_BATCH_BYTES (32 KiB)"),
    (
        "services/cabling/app/services/l3_validation.py",
        "L3InventoryContext._fetch_device_type_chunk",
        "call_service POST /internal/devices/batch",
    ): ("bounded", (20000, "_INVENTORY_BATCH_CHUNK_SIZE (500) device ids")),
    (
        "services/common/herd_common/acl.py",
        "_explicit_acl_manage_internal",
        "call_service POST /internal/check",
    ): ("bounded", (300, "a user, a resource, and a permission")),
    ("services/common/herd_common/acl.py", "user_has_grant", "call_service POST /check"): (
        "bounded",
        (300, "a user, a resource, and a permission"),
    ),
    (
        "services/common/herd_common/internal_client.py",
        "call_service",
        "request METHOD f'{base_url}{path}'",
    ): ("transport", "the shared service-to-service transport; its callers are classified"),
    ("services/config/app/docker_ctl.py", "restart_services", "request POST RESTART_PATH"): (
        "external",
        "the restarter, not a FastAPI HERD service, with an empty body",
    ),
    (
        "services/execution/app/services/nats_consumer.py",
        "_create_dynamic_device",
        "post POST f'{settings.inventory_service_url}/devices/internal'",
    ): (
        "bounded",
        (None, "a template id, a reservation id, and the hypervisor driver's field_data"),
    ),
    (
        "services/execution/app/services/nats_consumer.py",
        "_post_provision_result",
        "post POST f'{settings.reservations_service_url}"
        "/internal/{reservation_id}/provision-result'",
    ): ("bounded", (4000, "at most 50 device ids and a class-name error")),
    (
        "services/integration/app/routers/reservations.py",
        "_forward",
        "request METHOD f'{settings.reservations_service_url}{path}'",
    ): ("transport", "the /api/v1 facade's forwarder; its callers are classified"),
    ("services/integration/app/routers/reservations.py", "create_reservation", "_forward POST /"): (
        "envelope",
        [("reservations", "POST", "/")],
    ),
    (
        "services/integration/app/services/delivery.py",
        "deliver_one._post",
        "post POST target.target_url",
    ): ("external", "a webhook subscriber's target URL"),
    (
        "services/inventory/app/routers/device_configs.py",
        "apply_config_version",
        "post POST f'{settings.execution_service_url.rstrip('/')}/execute'",
    ): ("envelope", [("execution", "POST", "/execute")]),
    (
        "services/inventory/app/services/apply_scheduler.py",
        "_post_internal_execute",
        "post POST f'{settings.execution_service_url.rstrip('/')}/execute/internal'",
    ): ("envelope", [("execution", "POST", "/execute/internal")]),
    (
        "services/notifications/app/routers/notifications.py",
        "put_preferences",
        "patch PATCH f'{settings.user_profile_service_url.rstrip('/')}/preferences'",
    ): ("envelope", [("user-profile", "PATCH", "/preferences")]),
    (
        "services/notifications/app/services/dispatchers/chat.py",
        "ChatDispatcher.send._do_send",
        "post POST settings.chat_webhook_url",
    ): ("external", "a chat webhook"),
    (
        "services/notifications/app/services/dispatchers/webhook.py",
        "WebhookDispatcher.send._do_send",
        "post POST settings.outbound_webhook_url",
    ): ("external", "a notification webhook"),
    (
        "services/reservations/app/routers/reservations.py",
        "save_reservation_fork",
        "_cabling_fork_call POST f'/internal/forks/{reservation_id}/save'",
    ): ("envelope", [("cabling", "POST", "/internal/forks/{reservation_id}/save")]),
    (
        "services/reservations/app/routers/reservations.py",
        "update_reservation_fork_canvas",
        "_cabling_fork_call PUT f'/internal/forks/{reservation_id}/canvas'",
    ): ("envelope", [("cabling", "PUT", "/internal/forks/{reservation_id}/canvas")]),
    (
        "services/reservations/app/services/purpose_service.py",
        "classify_purpose_one",
        "call_service POST /internal/classify-purpose",
    ): ("bounded", (40000, "a purpose of at most 2,000 characters, 250 ids, 50 dynamic requests")),
    (
        "services/reservations/app/services/reporting_service.py",
        "_cabling_fork_devices_batch",
        "call_service POST /internal/forks/devices/batch",
    ): ("bounded", (20000, "at most 500 reservation ids")),
    (
        "services/reservations/app/services/reporting_service.py",
        "fetch_user_groups_map",
        "post POST f'{settings.auth_service_url}/groups/users/groups'",
    ): ("capped", "batches of USER_GROUPS_BATCH (500) user ids, about 20 KB each"),
    (
        "services/reservations/app/services/reservation_service.py",
        "_cabling_fork_call",
        "call_service METHOD path",
    ): ("transport", "the cabling fork forwarder; its callers are classified"),
    (
        "services/reservations/app/services/reservation_service.py",
        "_create_reservation_fork",
        "post POST f'{settings.cabling_service_url}/internal/forks'",
    ): ("bounded", (11000, "three ids and at most 250 member device ids")),
    (
        "services/reservations/app/services/reservation_service.py",
        "_execution_wiring_call",
        "call_service METHOD path",
    ): ("transport", "the execution wiring forwarder; it sends no body today"),
    (
        "services/reservations/app/services/reservation_service.py",
        "_fetch_devices",
        "post POST f'{settings.inventory_service_url}/devices/batch'",
    ): ("bounded", (10000, "a reservation's device ids, at most 250")),
    (
        "services/reservations/app/services/reservation_service.py",
        "_lazy_create_reservation_fork",
        "_cabling_fork_call POST /internal/forks",
    ): ("bounded", (11000, "three ids and at most 250 member device ids")),
    (
        "services/reservations/app/services/reservation_service.py",
        "_prune_removed_devices_from_fork",
        "_cabling_fork_call POST f'/internal/forks/{reservation_id}/prune-devices'",
    ): ("bounded", (10000, "at most 250 device ids")),
    (
        "services/reservations/app/services/reservation_service.py",
        "_update_device_statuses.update_one",
        "post POST f'{settings.inventory_service_url}/devices/{device_id}/status'",
    ): ("bounded", (100, "a status word")),
}
FORWARD_KINDS = {"envelope", "marked", "capped", "bounded", "transport", "external"}

_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://dummy:dummy@dummy-host:5432/dummy",
    "SECRET_KEY": "dummy-test-secret-key-not-real-0123456789abcdef",
    LIMIT_ENV: str(PROBE_LIMIT),
    "DRIVER_MAX_SIZE_BYTES": str(PROBE_DRIVER_BYTES),
    "UPLOAD_MAX_FILES": str(PROBE_UPLOAD_FILES),
    "UPLOAD_MAX_FILE_BYTES": str(PROBE_UPLOAD_FILE_BYTES),
    "VALIDATE_PACKAGE_MAX_BYTES": str(PROBE_PACKAGE_BYTES),
    "HERD_CONFIG_FILE": "/nonexistent/herd-config.json",
}

_INTROSPECT = """
import importlib
import json

from fastapi import params
from fastapi.routing import iter_route_contexts

from app.main import app

middleware = [(m.cls.__module__, m.cls.__qualname__, m.kwargs) for m in app.user_middleware]
limit_module = next(module for module, name, _ in middleware if name == "LimitRequestBody")
attribute = importlib.import_module(limit_module).EXTRA_BODY_BYTES_ATTRIBUTE
routes = []
for route in iter_route_contexts(app.routes):
    body_field = getattr(route, "body_field", None)
    mark = getattr(route.endpoint, attribute, None)
    routes.append(
        {
            "methods": sorted(route.methods or ()),
            "path": route.path,
            "form": body_field is not None and isinstance(body_field.field_info, params.Form),
            "extra_bytes": None if mark is None else mark(),
        }
    )
print(
    json.dumps(
        {
            "middleware": [
                [module, name, {k: v for k, v in kwargs.items() if isinstance(v, int)}]
                for module, name, kwargs in middleware
            ],
            "routes": routes,
        }
    )
)
"""


def service_names() -> list[str]:
    return sorted(p.parent.parent.name for p in SERVICES_DIR.glob("*/app/main.py"))


SERVICES = service_names()


def expected_module(service: str) -> str:
    return "app.validation_errors" if service == "config" else "herd_common.validation_errors"


@lru_cache(maxsize=None)
def built_app(service: str) -> dict:
    env = dict(os.environ)
    env.update(_ENV)
    env.pop("VIRTUAL_ENV", None)
    result = subprocess.run(
        [sys.executable, "-c", _INTROSPECT],
        cwd=SERVICES_DIR / service,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"{service}: {result.stderr[-3000:]}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_every_service_is_found():
    assert set(SERVICES) >= {
        "acl",
        "ai-orchestrator",
        "auth",
        "cabling",
        "config",
        "execution",
        "integration",
        "inventory",
        "notifications",
        "reservations",
        "secrets",
        "user-profile",
    }
    assert "common" not in SERVICES


@pytest.mark.parametrize("service", SERVICES)
def test_the_installed_middleware_is_exactly_the_declared_list(service):
    installed = built_app(service)["middleware"]
    assert [name for _, name, _ in installed] == MIDDLEWARE[service]
    module = expected_module(service)
    for owner, name, _ in installed:
        if name in ("LimitRequestBody", "RefuseLoneSurrogates"):
            assert owner == module, (service, name, owner)


def test_the_middleware_table_names_every_service():
    assert set(MIDDLEWARE) == set(SERVICES)


@pytest.mark.parametrize("service", SERVICES)
def test_the_limit_is_the_services_setting(service):
    limits = [k for _, n, k in built_app(service)["middleware"] if n == "LimitRequestBody"]
    assert limits == [{"max_body_bytes": PROBE_LIMIT}]


@pytest.mark.parametrize("service", SERVICES)
def test_every_marked_route_is_in_the_tables_and_every_form_is_marked(service):
    routes = built_app(service)["routes"]
    forms = {(m, r["path"]) for r in routes if r["form"] for m in r["methods"]}
    marked = {
        (m, r["path"]): r["extra_bytes"]
        for r in routes
        if r["extra_bytes"] is not None
        for m in r["methods"]
    }
    exempt = {(m, p) for s, m, p in FORM_EXEMPT if s == service}
    assert forms - exempt <= set(marked), f"{service}: unmarked forms {sorted(forms - set(marked))}"
    assert not exempt & set(marked), f"{service}: an exempt form is marked"
    assert exempt <= forms, f"{service}: a stale form exemption"
    uploads = {(m, p): b for (s, m, p), b in UPLOAD_ROUTES.items() if s == service}
    forwards = {(m, p): b for (s, m, p), b in FORWARD_ROUTES.items() if s == service}
    json_uploads = {(m, p) for s, m, p in JSON_UPLOAD_ROUTES if s == service}
    assert set(uploads) - forms == json_uploads
    assert not set(forwards) & forms
    assert marked == {**uploads, **forwards}
    assert all(b > 0 for b in marked.values())


def test_the_route_tables_name_only_existing_services_and_routes():
    assert {s for s, _, _ in UPLOAD_ROUTES} <= set(SERVICES)
    assert {s for s, _, _ in FORWARD_ROUTES} <= set(SERVICES)
    assert JSON_UPLOAD_ROUTES <= set(UPLOAD_ROUTES)
    assert not set(UPLOAD_ROUTES) & set(FORWARD_ROUTES)


@pytest.mark.parametrize("service", SERVICES)
def test_compose_passes_the_setting_with_the_shared_default(service):
    environment = yaml.safe_load(COMPOSE.read_text())["services"][service]["environment"]
    assert environment[LIMIT_ENV] == f"${{{LIMIT_ENV}:-{DEFAULT_MAX_REQUEST_BODY_BYTES}}}"


def test_the_specification_names_every_marked_route():
    text = SPEC.read_text(encoding="utf-8")
    for service, method, path in [*UPLOAD_ROUTES, *FORWARD_ROUTES]:
        assert f"`{method} {path}`" in text, (service, method, path)


# -- The forwarding table ----------------------------------------------------------

# The callees that build or send an HTTP request. For the first set the method is the
# callee's own name and the URL its first argument; for the second the method is the
# first argument and the URL the second (`client.request`, `httpx.Request`,
# `client.build_request`, `client.stream`).
_METHOD_NAMED = {"post", "put", "patch", "delete"}
_METHOD_FIRST = {"request", "stream", "build_request", "Request"}
_HTTP_CALLEES = _METHOD_NAMED | _METHOD_FIRST
_BODY_KEYWORDS = {"json", "content", "data", "files", "body"}
# HERD's own forwarders, by name: (method index, path index, positional body index or
# None). A body passed to one positionally is seen too.
TRANSPORTS = {
    "call_service": (1, 2, None),
    "_cabling_fork_call": (0, 1, 2),
    "_execution_wiring_call": (0, 1, 2),
    "_forward": (0, 1, None),
}


def _callee(call: ast.Call) -> str | None:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)


def _sends_body(call: ast.Call, name: str | None) -> bool:
    """Whether the call sends a body (the docstring's rule)."""
    keywords = {k.arg: k.value for k in call.keywords if k.arg}
    if (name in _HTTP_CALLEES or name in TRANSPORTS) and any(k.arg is None for k in call.keywords):
        return True  # a `**` splat into an HTTP call may carry a body
    json_body = keywords.get("json_body")
    if json_body is not None and not (
        isinstance(json_body, ast.Constant) and json_body.value is None
    ):
        return True
    if "json" in keywords:
        return True
    if name in _HTTP_CALLEES and _BODY_KEYWORDS & set(keywords):
        return True
    if name in TRANSPORTS:
        body_index = TRANSPORTS[name][2]
        return body_index is not None and len(call.args) > body_index
    return False


def _text(node: ast.AST | None, assigned: dict[str, list[ast.expr]]) -> str:
    """A stable text for a method or URL argument: a string literal's value, a local
    name assigned exactly once in the same function resolved to what it holds, any
    other expression as source text, `?` when absent."""
    if node is None:
        return "?"
    if isinstance(node, ast.Name) and len(assigned.get(node.id, [])) == 1:
        node = assigned[node.id][0]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ast.unparse(node)


def _receiver(call: ast.Call, name: str | None, assigned: dict[str, list[ast.expr]]) -> str:
    def arg(index: int) -> ast.AST | None:
        return call.args[index] if len(call.args) > index else None

    if name in TRANSPORTS:
        method, path = arg(TRANSPORTS[name][0]), arg(TRANSPORTS[name][1])
    elif name in _METHOD_NAMED:
        return f"{name} {name.upper()} {_text(arg(0), assigned)}"
    elif name in _METHOD_FIRST:
        method, path = arg(0), arg(1)
    else:
        method = path = None
    return f"{name} {_text(method, assigned).upper()} {_text(path, assigned)}"


def body_calls(source: str) -> list[tuple[str, str]]:
    """(function, receiver) for every call in `source` that sends a body, in source
    order. `function` is the dotted name of the enclosing def or class chain;
    `receiver` is the callee, the method, and the URL or path as written (a local
    name resolved), with ` #2`, ` #3` added when one function makes the same call
    again. Neither holds a line number, so moving a call does not change its key."""
    found: list[tuple[str, str]] = []

    def assignments(scope: ast.AST) -> dict[str, list[ast.expr]]:
        assigned: dict[str, list[ast.expr]] = {}
        for node in ast.walk(scope):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assigned.setdefault(target.id, []).append(node.value)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(
                node.target, ast.Name
            ):
                assigned.setdefault(node.target.id, []).append(
                    node.value if node.value is not None else node
                )
        return assigned

    def visit(node: ast.AST, owner: str, assigned: dict[str, list[ast.expr]]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            owner = f"{owner}.{node.name}" if owner else node.name
            assigned = assignments(node)
        if isinstance(node, ast.Call):
            name = _callee(node)
            if _sends_body(node, name):
                found.append((owner, _receiver(node, name, assigned)))
        for child in ast.iter_child_nodes(node):
            visit(child, owner, assigned)

    visit(ast.parse(source), "", {})
    counted: dict[tuple[str, str], int] = {}
    keyed: list[tuple[str, str]] = []
    for owner, receiver in found:
        counted[(owner, receiver)] = counted.get((owner, receiver), 0) + 1
        n = counted[(owner, receiver)]
        keyed.append((owner, receiver if n == 1 else f"{receiver} #{n}"))
    return keyed


def _scanned_modules() -> list[Path]:
    return sorted(SERVICES_DIR.glob("*/app/**/*.py")) + sorted(
        (SERVICES_DIR / "common" / "herd_common").glob("*.py")
    )


def outbound_body_calls() -> set[tuple[str, str, str]]:
    calls: set[tuple[str, str, str]] = set()
    for path in _scanned_modules():
        relative = path.relative_to(REPO).as_posix()
        for owner, receiver in body_calls(path.read_text(encoding="utf-8")):
            calls.add((relative, owner, receiver))
    return calls


def unclassified(module: str, source: str) -> set[tuple[str, str, str]]:
    """The body calls in `source`, read as `module`, that `FORWARDS` does not list."""
    return {(module, owner, receiver) for owner, receiver in body_calls(source)} - set(FORWARDS)


def test_every_outbound_body_is_classified_and_every_entry_exists():
    found = outbound_body_calls()
    assert found - set(FORWARDS) == set(), "unclassified outbound bodies"
    assert set(FORWARDS) - found == set(), "stale FORWARDS entries"


@pytest.mark.parametrize("call", sorted(FORWARDS), ids=lambda c: f"{c[0]}:{c[1]}")
def test_each_classified_call_is_covered(call):
    kind, detail = FORWARDS[call]
    assert kind in FORWARD_KINDS
    if kind == "envelope":
        assert detail and all(route in FORWARD_ROUTES for route in detail), detail
    elif kind == "marked":
        assert detail and all(route in UPLOAD_ROUTES for route in detail), detail
    elif kind == "bounded":
        size, reason = detail
        assert reason
        assert size is None or size <= MIN_MAX_REQUEST_BODY_BYTES
    else:
        assert isinstance(detail, str) and detail


def test_every_forward_route_has_a_sender():
    receivers = {
        route for kind, detail in FORWARDS.values() if kind == "envelope" for route in detail
    }
    assert receivers == set(FORWARD_ROUTES)


@pytest.mark.parametrize(
    ("source", "found"),
    [
        ("async def f(c):\n    await c.post(u, json={'a': 1})\n", ["f"]),
        ("async def f(c):\n    await c.put(u, content=b'x')\n", ["f"]),
        ("async def f(c):\n    await c.request('POST', u, data=d)\n", ["f"]),
        ("async def f():\n    await call_service(u, 'POST', p, json_body=b)\n", ["f"]),
        ("async def f():\n    await wrapper('POST', p, json=b)\n", ["f"]),
        ("class C:\n    async def m(self):\n        await self.h.patch(u, json=b)\n", ["C.m"]),
        ("async def f():\n    async def g():\n        await c.post(u, files=x)\n", ["f.g"]),
        ("async def f():\n    await call_service(u, 'GET', p, json_body=None)\n", []),
        ("async def f(c):\n    await c.get(u, params=p)\n", []),
        ("def f():\n    return Response(content=b'x')\n", []),
        ("def f():\n    return Message(role='user', content=[x])\n", []),
    ],
)
def test_the_body_call_scan_both_ways(source, found):
    assert [owner for owner, _ in body_calls(source)] == found


# -- The scan against the review's bypasses (#1466 second review, finding M1) -------------

_FORK_ROUTER = "services/reservations/app/routers/reservations.py"
_CANVAS_FORWARD = """        resp = await _cabling_fork_call(
            "PUT",
            f"/internal/forks/{reservation_id}/canvas",
            json_body={"canvas_data": body.canvas_data},
        )
"""


def _source(module: str) -> str:
    return (REPO / module).read_text(encoding="utf-8")


def _with_second_forward(source: str, call: str) -> str:
    assert source.count(_CANVAS_FORWARD) == 1
    return source.replace(_CANVAS_FORWARD, _CANVAS_FORWARD + call)


_BYPASSES = {
    "the_same_forward_again_in_a_classified_function": lambda s: _with_second_forward(
        s,
        "        await _cabling_fork_call(\n"
        '            "PUT", f"/internal/forks/{reservation_id}/canvas",\n'
        '            json_body={"canvas_data": body.canvas_data},\n'
        "        )\n",
    ),
    "another_forward_in_a_classified_function": lambda s: _with_second_forward(
        s,
        '        await _cabling_fork_call("PUT", "/topologies/x", json_body={"canvas_data": {}})\n',
    ),
    "a_positional_body_to_a_transport": lambda s: (
        s + "\n\nasync def push_topology(canvas):\n"
        '    return await _cabling_fork_call("PUT", "/topologies/x", {"canvas_data": canvas})\n'
    ),
    "a_prebuilt_request_then_send": lambda s: (
        s + "\n\nasync def send_big(client, url, big):\n"
        '    request = httpx.Request("PUT", url, content=big)\n'
        "    return await client.send(request)\n"
    ),
    "build_request_with_files": lambda s: (
        s + "\n\nasync def upload(client, url, upload_file):\n"
        '    request = client.build_request("POST", url, files={"f": upload_file})\n'
        "    return await client.send(request)\n"
    ),
}


def test_the_real_module_has_no_unclassified_body_call():
    assert unclassified(_FORK_ROUTER, _source(_FORK_ROUTER)) == set()


@pytest.mark.parametrize("mutate", _BYPASSES.values(), ids=_BYPASSES.keys())
def test_each_bypass_the_review_found_is_an_unclassified_call(mutate):
    """Each of these passed the guard keyed on (module, function): a second body call
    in a classified function, a body passed positionally to a HERD forwarder, and a
    request built with a body and then sent."""
    found = unclassified(_FORK_ROUTER, mutate(_source(_FORK_ROUTER)))
    assert len(found) == 1, found


def _move_to_end(source: str, function: str) -> str:
    """`source` with one top-level function (and its decorators) cut and appended."""
    tree = ast.parse(source)
    node = next(
        n
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == function
    )
    lines = source.split("\n")
    first = min([node.lineno, *(d.lineno for d in node.decorator_list)]) - 1
    block = lines[first : node.end_lineno]
    rest = lines[:first] + lines[node.end_lineno :]
    return "\n".join(rest + ["", ""] + block) + "\n"


_REFACTORS = {
    "lines_inserted_above": lambda s: "# a comment\n" * 40 + s,
    "the_function_moved_to_the_end": lambda s: _move_to_end(s, "update_reservation_fork_canvas"),
    "both_forwards_moved": lambda s: _move_to_end(
        _move_to_end(s, "save_reservation_fork"), "update_reservation_fork_canvas"
    ),
}


@pytest.mark.parametrize("refactor", _REFACTORS.values(), ids=_REFACTORS.keys())
def test_moving_a_call_without_changing_it_keeps_its_key(refactor):
    original = body_calls(_source(_FORK_ROUTER))
    moved = body_calls(refactor(_source(_FORK_ROUTER)))
    assert sorted(moved) == sorted(original)
    assert unclassified(_FORK_ROUTER, refactor(_source(_FORK_ROUTER))) == set()


@pytest.mark.parametrize(
    ("source", "receiver"),
    [
        (
            "async def f():\n    url = f'{base}/x'\n    await c.post(url, json=b)\n",
            "post POST f'{base}/x'",
        ),
        (
            "async def f():\n    await call_service(u, 'POST', '/p', json_body=b)\n",
            "call_service POST /p",
        ),
        (
            "async def f():\n    await _cabling_fork_call('PUT', '/p', b)\n",
            "_cabling_fork_call PUT /p",
        ),
        ("async def f():\n    httpx.Request('PUT', u, content=b)\n", "Request PUT u"),
        ("async def f():\n    c.build_request('POST', u, files=x)\n", "build_request POST u"),
        ("async def f():\n    await c.request(**options)\n", "request ? ?"),
    ],
)
def test_the_receiver_is_the_method_and_the_url_as_written(source, receiver):
    assert body_calls(source) == [("f", receiver)]


def test_a_repeated_call_gets_an_ordinal():
    source = "async def f():\n    await c.post('/a', json=1)\n    await c.post('/a', json=2)\n"
    assert body_calls(source) == [("f", "post POST /a"), ("f", "post POST /a #2")]


def test_a_transport_call_without_a_body_is_not_a_body_call():
    for source in (
        "async def f():\n    await _cabling_fork_call('POST', '/archive')\n",
        "async def f():\n    await _execution_wiring_call('GET', '/status')\n",
        "async def f():\n    httpx.Request('GET', u)\n",
    ):
        assert body_calls(source) == []
