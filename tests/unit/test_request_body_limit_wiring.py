"""Every service app bounds its request bodies, with its setting; every route whose
body may hold more declares it; every body one HERD service sends another fits the
receiving route (issue #1466); and every streaming route reads its body before it
streams, or is listed (issue #1483).

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
- the routes marked with `extends_body_limit` are exactly the routes named in
  `UPLOAD_ROUTES`, `LARGE_JSON_ROUTES` (issue #1484), and `FORWARD_ROUTES` below, both
  ways (a mark no table names fails, and so does a row whose route carries no mark),
  and each one's extra bytes are the sum of its rows: a route that legitimately takes
  a large JSON body adds `LARGE_JSON_BODY_BYTES`, a forward route
  `FORWARD_ENVELOPE_BYTES`, and a forward route whose sender admitted the body through
  a large-JSON route both. Neither JSON table names a form route. Every route that
  takes a form body (any `File(...)` or `Form(...)` parameter, as FastAPI decides it:
  `body_field.field_info` is a `params.Form`) is marked, unless it is named in
  `FORM_EXEMPT` with a reason (none today: no HERD route takes a small form); a
  marked upload route that takes no form is one of `JSON_UPLOAD_ROUTES` (a JSON body
  that carries a file as base64). The upload bytes follow the settings the routes
  enforce (`DRIVER_MAX_SIZE_BYTES`, `UPLOAD_MAX_FILES` times `UPLOAD_MAX_FILE_BYTES`,
  the base64 length of `VALIDATE_PACKAGE_MAX_BYTES`), set here to odd values to prove
  it;
- every JSON route whose schema admits a body larger than the shipped default carries
  a mark. `schema_body_estimate` reads the app's OpenAPI document and gives an upper
  bound on the compact body a standard encoder writes (its docstring states every
  assumption: canonical UUIDs and timestamps, 6 bytes a character of a free string, 24
  a number). A schema that bounds nothing (a free-form mapping, a model that keeps
  extra keys, a string or list with no bound, `Any`, a bound a validator checks in
  code) has no estimate: every unmarked JSON route with none is listed, both ways, in
  `UNBOUNDED_AT_THE_DEFAULT` with why its body is small, or measured in
  `MEASURED_UNDER_THE_DEFAULT`, whose bodies the services' suites build and send. So
  a new unbounded field on an unmarked route fails until someone decides. A route
  left out of the OpenAPI document is not estimated;
- `docker-compose.yml` passes `MAX_REQUEST_BODY_BYTES` to all twelve services as
  `${MAX_REQUEST_BODY_BYTES:-<default>}` with the shared default (the Settings-based
  services are also held by `test_compose_settings_wiring.py`; config is not, since
  it has no Settings model);
- the area specification names every marked route;
- every route that answers with a streaming response takes its body as a parameter,
  or is listed in `STREAMING_WITHOUT_BODY` with a reason (issue #1483, below).

Streaming routes. A streaming response (Starlette's `StreamingResponse`, or
sse-starlette's `EventSourceResponse`) runs a disconnect listener beside its stream,
and when the route never read its body that listener makes the first read; behind the
request logger a body over the limit then ends in a 500 or a stream cut short
(OPS-VALID-9). The invariant: every streaming route either has FastAPI read its body
before the route runs (`body_field` is set: a JSON or form body parameter), or is
listed by decision. `streaming_reasons` decides that a built route streams in three
ways, each a class whose MRO holds a class named `StreamingResponse` or
`EventSourceResponse` (so a subclass counts):

- its `response_class`, declared on the route or inherited from its router, an
  `include_router` default, or the app (also on a route added with `add_api_route`);
- its endpoint's return annotation, also inside a union, `Optional`, or `Annotated`;
  an annotation left as text (a string or a forward reference that does not resolve)
  counts when any name in it is one of the two;
- a returned call of such a class (`_returned_streaming_calls`). Read from the source
  of the endpoint (unwrapped through `__wrapped__`), of every function its closure
  holds (so the real endpoint under a decorator without `functools.wraps`), and of a
  lambda endpoint (its body). In a function's own body, not in a function, lambda, or
  class defined inside it, each `return` value is followed through both branches of
  a conditional expression, every operand of `and` and `or`, `await`, a walrus, and a
  local name bound exactly once (by `=`, `: T =`, `+=`, a walrus, or a tuple or list
  unpacking matched element by element). The callee is resolved in the function's
  module (a name, or an attribute of one), a `functools.partial` object or a local
  `partial(...)` call is unwrapped to the class it wraps, and a callee the module does
  not hold counts when its own name is one of the two or an alias an import gives one
  of them (`from sse_starlette.sse import EventSourceResponse as Sse`, in the function
  or anywhere in its module, installed or not).

Limits of the streaming scan. It does not see: a streaming response built in another
function and returned (`return make_stream()`, `return await make_stream()`), returned
from a dependency, raised, or sent through the ASGI `send`; a local name bound more
than once, or bound by a `for` or `with` target; a value unpacked from anything but a
tuple or list literal of the same length; a partial built by another helper than
`functools.partial`; an alias made by plain assignment (`Sse = EventSourceResponse`
when the module lacks the class); a response class with a disconnect listener of
another name that does not derive from either (a `Response` subclass whose `__call__`
reads `receive`); a decorator that keeps the endpoint somewhere other than
`__wrapped__` or its closure; an endpoint, or a lambda spread over several lines,
whose source `inspect` cannot read or parse; and a route FastAPI does not enumerate
(see Limits). A route that reads its body by hand (`await request.body()`) before it
streams has no body parameter, so it is flagged and must be listed; so is a `GET`
route that streams, since a client may still send it a body. Fixture sources in this
module prove each spelling both ways.

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
  with something added; every receiving route it names must be in `FORWARD_ROUTES`,
  and each is paired with its source, the route that admitted what travels (or None
  when the sender builds a body of the receiver's own schema from fields it bounded):
  the receiver must add at least what its source adds plus `FORWARD_ENVELOPE_BYTES`,
  both read from the tables (`envelope_shortfalls`);
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
something the scan computes; a receiver named in an "envelope" entry is checked
to be marked, not checked to be the URL the code calls; and its source is checked to
be a route of its service, not checked to be where the forwarded body came from.
"""

from __future__ import annotations

import ast
import builtins
import functools
import importlib.util
import inspect
import json
import os
import re
import subprocess
import sys
import textwrap
import typing
from functools import lru_cache
from pathlib import Path

import pytest
import yaml
from herd_common.validation_errors import (
    DEFAULT_MAX_REQUEST_BODY_BYTES,
    FORWARD_ENVELOPE_BYTES,
    LARGE_JSON_BODY_BYTES,
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

# Every route that legitimately takes a large JSON body (issue #1484), each with the
# large-JSON allowance. A route that is also a forward route takes both: its mark adds
# LARGE_JSON_BODY_BYTES and FORWARD_ENVELOPE_BYTES.
LARGE_JSON_ROUTES = {
    ("ai-orchestrator", "POST", "/commit"): LARGE_JSON_BODY_BYTES,
    ("cabling", "PUT", "/topologies/{topology_id}"): LARGE_JSON_BODY_BYTES,
    ("cabling", "POST", "/templates"): LARGE_JSON_BODY_BYTES,
    ("cabling", "PUT", "/templates/{template_id}"): LARGE_JSON_BODY_BYTES,
    ("cabling", "POST", "/connections/bulk"): LARGE_JSON_BODY_BYTES,
    ("cabling", "PUT", "/internal/forks/{reservation_id}/canvas"): LARGE_JSON_BODY_BYTES,
    ("cabling", "POST", "/internal/forks/{reservation_id}/save"): LARGE_JSON_BODY_BYTES,
    ("execution", "POST", "/execute"): LARGE_JSON_BODY_BYTES,
    ("execution", "POST", "/execute/internal"): LARGE_JSON_BODY_BYTES,
    ("inventory", "POST", "/devices/{device_id}/config-versions"): LARGE_JSON_BODY_BYTES,
    ("reservations", "PUT", "/{reservation_id}/fork/canvas"): LARGE_JSON_BODY_BYTES,
    ("reservations", "POST", "/{reservation_id}/fork/save"): LARGE_JSON_BODY_BYTES,
}

# Every route another HERD service forwards a body to, each with the envelope allowance.
FORWARD_ROUTES = {
    ("cabling", "PUT", "/internal/forks/{reservation_id}/canvas"): FORWARD_ENVELOPE_BYTES,
    ("cabling", "POST", "/internal/forks/{reservation_id}/save"): FORWARD_ENVELOPE_BYTES,
    ("execution", "POST", "/execute"): FORWARD_ENVELOPE_BYTES,
    ("execution", "POST", "/execute/internal"): FORWARD_ENVELOPE_BYTES,
    ("reservations", "POST", "/"): FORWARD_ENVELOPE_BYTES,
    ("user-profile", "PATCH", "/preferences"): FORWARD_ENVELOPE_BYTES,
}

# The routes whose largest legitimate body was measured to fit under the default (issue
# #1484), each with that measure in bytes and where it comes from. Each service's
# `test_request_body_limit.py` builds that body and sends it.
MEASURED_UNDER_THE_DEFAULT = {
    ("cabling", "POST", "/pathfind/batch"): (
        236_011,
        "MAX_BATCH_PAIRS (2,000) pairs of two device ids, compact JSON",
    ),
    ("user-profile", "PUT", "/preferences"): (
        139_319,
        "two blobs at their 64 KiB cap by the validator's own json.dumps, and 200 page"
        " sizes under 32-character keys (the schema bounds no key's length), as json.dumps"
        " writes it",
    ),
    ("user-profile", "PATCH", "/preferences"): (
        139_319,
        "the same three fields as the PUT, each optional",
    ),
}


# Every unmarked JSON route whose schema bounds nothing (`schema_body_estimate` is
# None), each with why its legitimate body stays far under the default (issue #1484):
# the routes that stay at the default by decision. A route measured instead is in
# MEASURED_UNDER_THE_DEFAULT. The guard fails on an unbounded unmarked route missing
# here, and on a row whose route is gone, marked, or bounded now.
_IDS = "ids and short words (a resource type, a permission)"
_SHORT = "a few short strings; a token, an email, a password, or a name"
UNBOUNDED_AT_THE_DEFAULT = {
    ("acl", "POST", "/grants"): _IDS,
    ("acl", "POST", "/check"): _IDS,
    ("acl", "POST", "/check/batch"): "at most 500 resource ids (about 20 KB) and short words",
    ("acl", "POST", "/internal/check"): _IDS,
    ("ai-orchestrator", "POST", "/templates/suggest-identity"): (
        "a template's name, description, and field sections, as inventory's template"
        " create takes them (a form's fields)"
    ),
    ("ai-orchestrator", "POST", "/classify-purpose/preview"): (
        "a 2,000-character purpose, at most 64 category names, 200 device ids, and the"
        " capped dynamic requests"
    ),
    ("ai-orchestrator", "POST", "/internal/classify-purpose"): (
        "reservations' bounded call: a 2,000-character purpose, 64 categories, 200 ids"
    ),
    ("auth", "POST", "/register"): _SHORT,
    ("auth", "POST", "/refresh"): "one refresh token",
    ("auth", "POST", "/logout"): "one refresh token",
    ("auth", "PATCH", "/me/profile"): "a name, a contact email, and a phone number",
    ("auth", "PATCH", "/users/{user_id}/profile"): "a name, a contact email, and a phone number",
    ("auth", "POST", "/admin/federated-identities/rewrite-issuer"): "two issuer URLs and a flag",
    ("auth", "PUT", "/users/{user_id}/federated-identity"): "an issuer, a subject, and an id",
    ("auth", "POST", "/tokens/exchange"): "one token",
    ("cabling", "POST", "/internal/forks"): (
        "reservations' bounded call: three ids and at most 250 member device ids (11 KB)"
    ),
    ("cabling", "POST", "/internal/forks/{reservation_id}/prune-devices"): (
        "reservations' bounded call: at most 250 device ids (10 KB)"
    ),
    ("cabling", "POST", "/templates/{template_id}/instantiate"): (
        "a name and one device id per role of the template: about 80 bytes a role, so"
        " about 150 KB for the 1,800 devices the canvas allowance holds"
    ),
    ("config", "POST", "/login"): "one password",
    ("config", "POST", "/change-password"): "one password",
    ("config", "PUT", "/settings"): "the values of CONFIG_SCHEMA's keys, a few kilobytes",
    ("integration", "POST", "/reservations"): (
        "a reservation create: at most 200 device ids, a 2,000-character purpose, times"
    ),
    ("integration", "POST", "/webhooks"): "a URL, a secret, a description, and event type names",
    ("inventory", "POST", "/devices/{device_id}/config-versions/{version_id}/restore"): (
        "one description"
    ),
    ("inventory", "POST", "/devices"): (
        "by decision: a device's name and field_data, the values of its template's"
        " typed fields (a form's values)"
    ),
    ("inventory", "POST", "/devices/internal"): (
        "execution's call: two ids and the field_data a hypervisor driver returns"
    ),
    ("inventory", "POST", "/devices/resolve-by-name"): (
        "cabling's import sends names in batches of at most 32 KiB (RESOLVE_BATCH_BYTES)"
    ),
    ("inventory", "PUT", "/devices/{device_id}"): "by decision: as the device create",
    ("inventory", "PUT", "/drivers/{driver_id}"): "a name, a description, a connection type",
    ("inventory", "POST", "/hypervisors"): "a name, an endpoint, a type, and ids",
    ("inventory", "PUT", "/hypervisors/{hypervisor_id}"): "a name, an endpoint, a type, and ids",
    ("inventory", "POST", "/devices/{device_id}/ports"): "a port's name and field_data",
    ("inventory", "POST", "/devices/{device_id}/ports/bulk"): (
        "one name prefix, two counts, and one field_data copied to every port"
    ),
    ("inventory", "PUT", "/ports/{port_id}"): "a port's name and field_data",
    ("inventory", "POST", "/templates"): (
        "by decision: a template's identity and field sections (a form's fields)"
    ),
    ("inventory", "PUT", "/templates/{template_id}"): "by decision: as the template create",
    ("notifications", "PUT", "/notifications/preferences"): (
        "channel settings and one flag per event type"
    ),
    ("reservations", "POST", "/internal/{reservation_id}/provision-result"): (
        "execution's call: at most 50 device ids and a class-name error"
    ),
    ("reservations", "PATCH", "/{reservation_id}/purpose-category"): "one category name",
    ("reservations", "POST", "/admin/purpose-review/{reservation_id}/accept"): (
        "one category name"
    ),
    ("secrets", "POST", "/secrets"): (
        "by decision: a key, a password, a token, or a generic secret; 256 KiB holds a"
        " full public CA bundle in PEM"
    ),
    ("secrets", "PUT", "/secrets/{secret_id}"): "by decision: as the secret create",
}


def route_extra_bytes(route: tuple[str, str, str]) -> int:
    """What the tables say a route adds to the default: the sum of its rows."""
    return sum(table.get(route, 0) for table in (UPLOAD_ROUTES, LARGE_JSON_ROUTES, FORWARD_ROUTES))


# A route that takes a form and needs no larger limit (a small form, not an upload):
# (service, method, path) to the reason. None today.
FORM_EXEMPT: dict[tuple[str, str, str], str] = {}

# A route that answers with a streaming response and has no body parameter, so the
# response's disconnect listener, not FastAPI, makes the first read of its body (issue
# #1483, OPS-VALID-9): (service, method, path) to the reason it is allowed. None today.
STREAMING_WITHOUT_BODY: dict[tuple[str, str, str], str] = {}

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
# For "envelope" the detail is a list of (receiving route, source), the source being
# the route that admitted what travels (a body, or a stored document written through
# it), or None when the sender builds a body of the receiver's own schema from fields
# it bounded itself; for "bounded" it is (bytes, why).
_CONFIG_VERSIONS = ("inventory", "POST", "/devices/{device_id}/config-versions")
_ENVELOPE = "envelope"
FORWARDS: dict[tuple[str, str, str], tuple[str, object]] = {
    (
        "services/ai-orchestrator/app/services/cabling_client.py",
        "fetch_pathfind_batch",
        'post POST f"{settings.cabling_service_url.rstrip("/")}/pathfind/batch"',
    ): ("bounded", (24000, "PATHFIND_BATCH_CHUNK (200) pairs of two device ids")),
    (
        "services/ai-orchestrator/app/services/committer.py",
        "_apply_configs",
        'post POST f"{settings.execution_service_url.rstrip("/")}/execute"',
    ): ("envelope", [(("execution", "POST", "/execute"), ("ai-orchestrator", "POST", "/commit"))]),
    (
        "services/ai-orchestrator/app/services/committer.py",
        "_create_reservation",
        'post POST f"{settings.reservations_service_url.rstrip("/")}/"',
    ): ("envelope", [(("reservations", "POST", "/"), None)]),
    (
        "services/ai-orchestrator/app/services/committer.py",
        "_create_topology",
        'post POST f"{settings.cabling_service_url.rstrip("/")}/topologies"',
    ): ("bounded", (1300, "a topology name of at most 200 characters")),
    (
        "services/ai-orchestrator/app/services/committer.py",
        "_update_topology_canvas",
        'put PUT f"{settings.cabling_service_url.rstrip("/")}/topologies/{topology_id}"',
    ): (
        "capped",
        "commit_proposal measures the canvas (canvas_put_bytes) against cabling's limit for"
        " the route, MAX_REQUEST_BODY_BYTES plus LARGE_JSON_BODY_BYTES (canvas_put_limit),"
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
        'post POST f"{base}/devices/batch"',
    ): ("bounded", (10000, "a reservation's device ids, at most 200 plus 50 dynamic")),
    (
        "services/ai-orchestrator/app/services/recipe_author.py",
        "validate_with_execution",
        'post POST f"{settings.execution_service_url}/internal/validate-package"',
    ): ("marked", [("execution", "POST", "/internal/validate-package")]),
    (
        "services/ai-orchestrator/app/services/tools.py",
        "ToolDispatcher._tool_find_path",
        'post POST f"{settings.cabling_service_url.rstrip("/")}/pathfind"',
    ): ("bounded", (200, "two device ids")),
    (
        "services/ai-orchestrator/app/services/tools.py",
        "ToolDispatcher._tool_propose_config_change",
        'post POST f"{settings.inventory_service_url.rstrip("'
        '/")}/devices/{device_id}/config-versions"',
    ): ("bounded", (None, "the model's tool arguments, bounded by AI_MAX_TOKENS output tokens")),
    (
        "services/ai-orchestrator/app/services/tools.py",
        "ToolDispatcher._tool_schedule_config_apply",
        'post POST f"{settings.inventory_service_url.rstrip("/")}/devices'
        '/{device_id}/config-versions/{version_id}/schedule"',
    ): ("bounded", (300, "a timestamp, a flag, and a reservation id")),
    ("services/auth/app/services/oidc_service.py", "_exchange_code", "post POST token_endpoint"): (
        "external",
        "the identity provider's token endpoint",
    ),
    (
        "services/cabling/app/services/device_resolver.py",
        "resolve_device_names",
        'post POST f"{settings.inventory_service_url.rstrip("/")}/devices/resolve-by-name"',
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
        'request METHOD f"{base_url}{path}"',
    ): ("transport", "the shared service-to-service transport; its callers are classified"),
    ("services/config/app/docker_ctl.py", "restart_services", "request POST RESTART_PATH"): (
        "external",
        "the restarter, not a FastAPI HERD service, with an empty body",
    ),
    (
        "services/execution/app/services/nats_consumer.py",
        "_create_dynamic_device",
        'post POST f"{settings.inventory_service_url}/devices/internal"',
    ): (
        "bounded",
        (None, "a template id, a reservation id, and the hypervisor driver's field_data"),
    ),
    (
        "services/execution/app/services/nats_consumer.py",
        "_post_provision_result",
        'post POST f"{settings.reservations_service_url}'
        '/internal/{reservation_id}/provision-result"',
    ): ("bounded", (4000, "at most 50 device ids and a class-name error")),
    (
        "services/integration/app/routers/reservations.py",
        "_forward",
        'request METHOD f"{settings.reservations_service_url}{path}"',
    ): ("transport", "the /api/v1 facade's forwarder; its callers are classified"),
    ("services/integration/app/routers/reservations.py", "create_reservation", "_forward POST /"): (
        "envelope",
        [(("reservations", "POST", "/"), ("integration", "POST", "/reservations"))],
    ),
    (
        "services/integration/app/services/delivery.py",
        "deliver_one._post",
        "post POST target.target_url",
    ): ("external", "a webhook subscriber's target URL"),
    (
        "services/inventory/app/routers/device_configs.py",
        "apply_config_version",
        'post POST f"{settings.execution_service_url.rstrip("/")}/execute"',
    ): ("envelope", [(("execution", "POST", "/execute"), _CONFIG_VERSIONS)]),
    (
        "services/inventory/app/services/apply_scheduler.py",
        "_post_internal_execute",
        'post POST f"{settings.execution_service_url.rstrip("/")}/execute/internal"',
    ): ("envelope", [(("execution", "POST", "/execute/internal"), _CONFIG_VERSIONS)]),
    (
        "services/notifications/app/routers/notifications.py",
        "put_preferences",
        'patch PATCH f"{settings.user_profile_service_url.rstrip("/")}/preferences"',
    ): (
        "envelope",
        [
            (
                ("user-profile", "PATCH", "/preferences"),
                ("notifications", "PUT", "/notifications/preferences"),
            )
        ],
    ),
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
        '_cabling_fork_call POST f"/internal/forks/{reservation_id}/save"',
    ): (
        "envelope",
        [
            (
                ("cabling", "POST", "/internal/forks/{reservation_id}/save"),
                ("reservations", "POST", "/{reservation_id}/fork/save"),
            )
        ],
    ),
    (
        "services/reservations/app/routers/reservations.py",
        "update_reservation_fork_canvas",
        '_cabling_fork_call PUT f"/internal/forks/{reservation_id}/canvas"',
    ): (
        "envelope",
        [
            (
                ("cabling", "PUT", "/internal/forks/{reservation_id}/canvas"),
                ("reservations", "PUT", "/{reservation_id}/fork/canvas"),
            )
        ],
    ),
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
        'post POST f"{settings.auth_service_url}/groups/users/groups"',
    ): ("capped", "batches of USER_GROUPS_BATCH (500) user ids, about 20 KB each"),
    (
        "services/reservations/app/services/reservation_service.py",
        "_cabling_fork_call",
        "call_service METHOD path",
    ): ("transport", "the cabling fork forwarder; its callers are classified"),
    (
        "services/reservations/app/services/reservation_service.py",
        "_create_reservation_fork",
        'post POST f"{settings.cabling_service_url}/internal/forks"',
    ): ("bounded", (11000, "three ids and at most 250 member device ids")),
    (
        "services/reservations/app/services/reservation_service.py",
        "_execution_wiring_call",
        "call_service METHOD path",
    ): ("transport", "the execution wiring forwarder; it sends no body today"),
    (
        "services/reservations/app/services/reservation_service.py",
        "_fetch_devices",
        'post POST f"{settings.inventory_service_url}/devices/batch"',
    ): ("bounded", (10000, "a reservation's device ids, at most 250")),
    (
        "services/reservations/app/services/reservation_service.py",
        "_lazy_create_reservation_fork",
        "_cabling_fork_call POST /internal/forks",
    ): ("bounded", (11000, "three ids and at most 250 member device ids")),
    (
        "services/reservations/app/services/reservation_service.py",
        "_prune_removed_devices_from_fork",
        '_cabling_fork_call POST f"/internal/forks/{reservation_id}/prune-devices"',
    ): ("bounded", (10000, "at most 250 device ids")),
    (
        "services/reservations/app/services/reservation_service.py",
        "_update_device_statuses.update_one",
        'post POST f"{settings.inventory_service_url}/devices/{device_id}/status"',
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

_GUARD_ENV = "HERD_BODY_LIMIT_GUARD"

_INTROSPECT = """
import importlib
import importlib.util
import json
import os

from fastapi import params
from fastapi.routing import iter_route_contexts

from app.main import app

# This module, loaded by path, for `streaming_reasons` (one implementation for the
# built apps and the fixtures).
guard_path = os.environ["HERD_BODY_LIMIT_GUARD"]
spec = importlib.util.spec_from_file_location("body_limit_guard", guard_path)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

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
            "body": body_field is not None,
            "streaming": guard.streaming_reasons(route),
        }
    )
document = app.openapi()
json_bodies = {}
for path, operations in document.get("paths", {}).items():
    for method, operation in operations.items():
        content = (operation.get("requestBody") or {}).get("content", {})
        if "application/json" in content:
            json_bodies[method.upper() + " " + path] = content["application/json"]["schema"]
print(
    json.dumps(
        {
            "middleware": [
                [module, name, {k: v for k, v in kwargs.items() if isinstance(v, int)}]
                for module, name, kwargs in middleware
            ],
            "routes": routes,
            "json_bodies": json_bodies,
            "components": document.get("components", {}).get("schemas", {}),
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
    env[_GUARD_ENV] = str(Path(__file__).resolve())
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
    assert marked_route_problems(service, routes) == []


def marked_route_problems(service: str, routes: list[dict]) -> list[str]:
    """What is wrong with a service's marks against the tables: a marked route no table
    names, a table row whose route carries no mark, a mark whose bytes are not the sum
    of its rows, or a JSON-only row on a form route. Empty when the two agree."""
    forms = {(m, r["path"]) for r in routes if r["form"] for m in r["methods"]}
    marked = {
        (m, r["path"]): r["extra_bytes"]
        for r in routes
        if r["extra_bytes"] is not None
        for m in r["methods"]
    }
    tabled = {
        (m, p)
        for table in (UPLOAD_ROUTES, LARGE_JSON_ROUTES, FORWARD_ROUTES)
        for s, m, p in table
        if s == service
    }
    problems = [f"marked, in no table: {r}" for r in sorted(set(marked) - tabled)]
    problems += [f"in a table, not marked: {r}" for r in sorted(tabled - set(marked))]
    for route in sorted(set(marked) & tabled):
        expected = route_extra_bytes((service, *route))
        if marked[route] != expected:
            problems.append(f"{route} adds {marked[route]}, its rows add {expected}")
    uploads = {(m, p) for s, m, p in UPLOAD_ROUTES if s == service}
    json_uploads = {(m, p) for s, m, p in JSON_UPLOAD_ROUTES if s == service}
    if uploads - forms != json_uploads:
        problems.append(f"JSON uploads {sorted(uploads - forms)} are not {sorted(json_uploads)}")
    for name, table in (
        ("LARGE_JSON_ROUTES", LARGE_JSON_ROUTES),
        ("FORWARD_ROUTES", FORWARD_ROUTES),
    ):
        on_forms = {(m, p) for s, m, p in table if s == service} & forms
        if on_forms:
            problems.append(f"{name} names a form route: {sorted(on_forms)}")
    return problems


def test_the_mark_check_fails_both_ways():
    """A mark with no row, a row with no mark, and a mark that adds the wrong bytes each
    fail; the real tables against routes built from them pass."""
    canvas = ("PUT", "/topologies/{topology_id}")
    honest = [
        {
            "methods": [m],
            "path": p,
            "form": False,
            "extra_bytes": route_extra_bytes(("cabling", m, p)),
        }
        for s, m, p in {*LARGE_JSON_ROUTES, *FORWARD_ROUTES, *UPLOAD_ROUTES}
        if s == "cabling"
    ]
    for r in honest:
        if r["path"] == "/topologies/import":
            r["form"] = True
    assert marked_route_problems("cabling", honest) == []
    unmarked = [r for r in honest if (r["methods"][0], r["path"]) != canvas]
    assert marked_route_problems("cabling", unmarked) == [f"in a table, not marked: {canvas}"]
    extra = [*honest, {"methods": ["POST"], "path": "/x", "form": False, "extra_bytes": 5}]
    assert marked_route_problems("cabling", extra) == ["marked, in no table: ('POST', '/x')"]
    short = [
        {**r, "extra_bytes": FORWARD_ENVELOPE_BYTES} if r["path"].endswith("/canvas") else r
        for r in honest
    ]
    assert marked_route_problems("cabling", short) == [
        f"('PUT', '/internal/forks/{{reservation_id}}/canvas') adds {FORWARD_ENVELOPE_BYTES},"
        f" its rows add {LARGE_JSON_BODY_BYTES + FORWARD_ENVELOPE_BYTES}"
    ]


def test_the_route_tables_name_only_existing_services_and_routes():
    assert {s for s, _, _ in UPLOAD_ROUTES} <= set(SERVICES)
    assert {s for s, _, _ in LARGE_JSON_ROUTES} <= set(SERVICES)
    assert {s for s, _, _ in FORWARD_ROUTES} <= set(SERVICES)
    assert JSON_UPLOAD_ROUTES <= set(UPLOAD_ROUTES)
    assert not set(UPLOAD_ROUTES) & set(FORWARD_ROUTES)
    assert not set(UPLOAD_ROUTES) & set(LARGE_JSON_ROUTES)
    assert set(LARGE_JSON_ROUTES.values()) == {LARGE_JSON_BODY_BYTES}
    assert set(FORWARD_ROUTES.values()) == {FORWARD_ENVELOPE_BYTES}
    assert all(b > 0 for b in UPLOAD_ROUTES.values())


# -- The largest body a bounded schema admits (issue #1484) --------------------------

# Bytes of a JSON string of each format, quotes included, in the canonical form every
# HERD client sends: a hyphenated UUID (pydantic also takes the braced form and the
# `urn:uuid:` form, up to 45 characters), and RFC 3339 dates and times with
# microseconds and an offset (pydantic also takes any number of fraction digits).
_FORMAT_BYTES = {"uuid": 38, "date-time": 34, "date": 12, "time": 23}
# Bytes one character of a string may take in a body: 4 for a character outside the
# Basic Multilingual Plane written as raw UTF-8, 6 for a control character, which a
# JSON encoder must write as a `\u00XX` escape.
_CHAR_BYTES = 6
# Bytes of a number as JSON.stringify or Python's json writes a double (the longest
# is -1.7976931348623157e+308, 24 characters).
_NUMBER_BYTES = 24


def _ascii_pattern_bytes(pattern: str) -> int | None:
    """Bytes per character of a string whose `pattern` admits only ASCII characters
    that JSON writes as themselves: 1, or 2 when `"` or a backslash is among them;
    None when the pattern may admit any other character (a `.`, a negated class, a
    class escape such as `\\w`, `\\d`, or `\\S`, which match beyond ASCII, a
    hex or Unicode escape, or a non-ASCII or control-character literal)."""
    if not pattern.isascii() or any(ord(c) < 0x20 for c in pattern):
        return None
    literal = re.sub(r"\\[^A-Za-z0-9]", "", pattern)
    if "." in literal or "[^" in literal or re.search(r"\\[A-Za-z0-9]", literal):
        return None
    return 2 if '"' in pattern or "\\\\" in pattern else 1


def schema_body_estimate(
    schema: dict, components: dict, seen: frozenset[str] = frozenset()
) -> int | None:
    """An upper bound on the bytes of a compact JSON body (no whitespace between
    tokens) that a standard encoder (JSON.stringify, or Python's json with
    `ensure_ascii=False`) writes for any value `schema` admits, or None when the
    schema bounds nothing. What it assumes, exactly:

    - every declared property is present (an optional one may be sent); a key the
      model ignores (pydantic's default `extra="ignore"`) is not counted, while a model
      that keeps extra keys (`extra="allow"`, so `additionalProperties` is true or a
      schema) is unbounded;
    - a string with `maxLength` takes 6 bytes a character (`_CHAR_BYTES`), unless its
      alphabet is fixed: a `format` in `_FORMAT_BYTES` (in its canonical form; the
      other forms pydantic accepts are longer and not counted), an `enum` or `const`
      (its longest value as written), or a `pattern` that admits only ASCII
      (`_ascii_pattern_bytes`: 1 byte a character, 2 when `"` or a backslash is
      allowed); a string with neither is unbounded;
    - an integer with both bounds takes the longer bound's digits; any other number 24
      bytes (`_NUMBER_BYTES`), as an encoder writes a double;
    - a union (`anyOf`, `oneOf`) is its largest member, an `allOf` its smallest
      bounded member (the body satisfies every member); a list needs `maxItems`; a
      model that refers to itself is unbounded.

    A validator's own bound (a serialized size checked in code) is not in the schema,
    so such a route is unbounded here; MEASURED_UNDER_THE_DEFAULT records those that
    were measured, and UNBOUNDED_AT_THE_DEFAULT why each other unbounded route is
    small. Not counted: whitespace a client adds (pretty-printed JSON), escapes a
    client writes where an encoder would not, and numbers written with more digits
    than an encoder writes."""
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        if name in seen:
            return None
        return schema_body_estimate(components[name], components, seen | {name})
    for key in ("anyOf", "oneOf"):
        if key in schema:
            sizes = [schema_body_estimate(s, components, seen) for s in schema[key]]
            return None if None in sizes else max(sizes)
    if "allOf" in schema:
        sizes = [schema_body_estimate(s, components, seen) for s in schema["allOf"]]
        bounded = [size for size in sizes if size is not None]
        return min(bounded) if bounded else None
    if "const" in schema:
        return len(json.dumps(schema["const"], separators=(",", ":"), ensure_ascii=False).encode())
    if "enum" in schema:
        return max(
            len(json.dumps(v, separators=(",", ":"), ensure_ascii=False).encode())
            for v in schema["enum"]
        )
    kind = schema.get("type")
    if kind == "string":
        if schema.get("format") in _FORMAT_BYTES:
            return _FORMAT_BYTES[schema["format"]]
        if "maxLength" not in schema:
            return None
        per_char = _CHAR_BYTES
        if "pattern" in schema:
            per_char = _ascii_pattern_bytes(schema["pattern"]) or _CHAR_BYTES
        return schema["maxLength"] * per_char + 2
    if kind == "integer" and "minimum" in schema and "maximum" in schema:
        return max(len(str(schema["minimum"])), len(str(schema["maximum"])))
    if kind in ("integer", "number"):
        return _NUMBER_BYTES
    if kind == "boolean":
        return 5
    if kind == "null":
        return 4
    if kind == "array":
        if "maxItems" not in schema:
            return None
        count = schema["maxItems"]
        if count == 0:
            return 2
        item = schema_body_estimate(schema.get("items", {}), components, seen)
        return None if item is None else 2 + count * item + count - 1
    if kind == "object" or "properties" in schema:
        extra = schema.get("additionalProperties")
        if extra is not None and extra is not False:
            return None  # a free-form mapping, or a model that keeps extra keys
        properties = schema.get("properties", {})
        if not properties:
            return None if extra is None else 2
        sizes = [schema_body_estimate(s, components, seen) for s in properties.values()]
        if None in sizes:
            return None
        keys = sum(len(json.dumps(k, ensure_ascii=False).encode()) + 1 for k in properties)
        return 2 + keys + sum(sizes) + len(properties) - 1
    return None


def json_body_estimates(service: str) -> dict[tuple[str, str], int | None]:
    built = built_app(service)
    return {
        tuple(key.split(" ", 1)): schema_body_estimate(schema, built["components"])
        for key, schema in built["json_bodies"].items()
    }


@pytest.mark.parametrize("service", SERVICES)
def test_every_bounded_json_route_over_the_default_is_marked(service):
    """A JSON route whose schema admits a body larger than the shipped default must
    carry a mark (a large-JSON or a forward row); one whose schema bounds nothing is a
    reviewed decision instead (OPS-VALID-10)."""
    marked = {
        (m, r["path"])
        for r in built_app(service)["routes"]
        if r["extra_bytes"] is not None
        for m in r["methods"]
    }
    over = {
        route: size
        for route, size in json_body_estimates(service).items()
        if size is not None and size > DEFAULT_MAX_REQUEST_BODY_BYTES
    }
    assert set(over) <= marked, f"{service}: unmarked JSON routes over the default {over}"


def unbounded_problems(
    service: str,
    estimates: dict[tuple[str, str], int | None],
    marked: set[tuple[str, str]],
    table: dict[tuple[str, str, str], str],
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """(unlisted, stale) for a service: the unmarked JSON routes whose schema bounds
    nothing that neither `table` nor MEASURED_UNDER_THE_DEFAULT names, and the routes
    `table` names that are not such a route (gone, marked, or bounded now)."""
    unbounded = {route for route, size in estimates.items() if size is None} - marked
    listed = {(m, p) for s, m, p in table if s == service}
    measured = {(m, p) for s, m, p in MEASURED_UNDER_THE_DEFAULT if s == service}
    return unbounded - listed - measured, listed - unbounded


def _marked(service: str) -> set[tuple[str, str]]:
    return {
        (m, r["path"])
        for r in built_app(service)["routes"]
        if r["extra_bytes"] is not None
        for m in r["methods"]
    }


@pytest.mark.parametrize("service", SERVICES)
def test_every_unbounded_unmarked_json_route_is_listed_with_a_reason(service):
    """A route whose schema bounds nothing stays at the default only by decision: it
    is in UNBOUNDED_AT_THE_DEFAULT with why its body is small (or measured). A new
    unbounded field on an unmarked route (a canvas added to a create) fails here until
    someone decides."""
    unlisted, stale = unbounded_problems(
        service, json_body_estimates(service), _marked(service), UNBOUNDED_AT_THE_DEFAULT
    )
    assert unlisted == set(), f"{service}: unbounded unmarked JSON routes not listed"
    assert stale == set(), f"{service}: stale UNBOUNDED_AT_THE_DEFAULT rows"


def test_the_unbounded_table_names_existing_services_with_reasons():
    assert {s for s, _, _ in UNBOUNDED_AT_THE_DEFAULT} <= set(SERVICES)
    assert all(reason.strip() for reason in UNBOUNDED_AT_THE_DEFAULT.values())
    assert not set(UNBOUNDED_AT_THE_DEFAULT) & set(MEASURED_UNDER_THE_DEFAULT)
    assert not set(UNBOUNDED_AT_THE_DEFAULT) & set(LARGE_JSON_ROUTES)


def test_the_unbounded_check_fails_both_ways():
    table = {("svc", "POST", "/a"): "small"}
    estimates = {("POST", "/a"): None, ("POST", "/b"): 100, ("PUT", "/c"): None}
    marked = {("PUT", "/c")}
    assert unbounded_problems("svc", estimates, marked, table) == (set(), set())
    # A new unbounded route, unmarked and unlisted.
    grown = {**estimates, ("POST", "/b"): None}
    assert unbounded_problems("svc", grown, marked, table) == ({("POST", "/b")}, set())
    # A listed route that became bounded, was marked, or is gone.
    assert unbounded_problems("svc", {**estimates, ("POST", "/a"): 9}, marked, table) == (
        set(),
        {("POST", "/a")},
    )
    assert unbounded_problems("svc", estimates, {*marked, ("POST", "/a")}, table) == (
        set(),
        {("POST", "/a")},
    )
    assert unbounded_problems("svc", {("POST", "/b"): 1}, set(), table) == (set(), {("POST", "/a")})


def test_the_estimate_matches_each_bounded_measure():
    """The pathfinding batch, the one bounded schema the measures list, is estimated at
    exactly its measured worst body, which cabling's suite builds and sends; the bulk
    connection create, the one bounded schema over the default, is estimated over it."""
    cabling = json_body_estimates("cabling")
    size, _ = MEASURED_UNDER_THE_DEFAULT[("cabling", "POST", "/pathfind/batch")]
    assert cabling[("POST", "/pathfind/batch")] == size
    assert cabling[("POST", "/connections/bulk")] > DEFAULT_MAX_REQUEST_BODY_BYTES


_COMPONENTS = {
    "Pair": {
        "type": "object",
        "properties": {
            "a": {"type": "string", "format": "uuid"},
            "n": {"type": "integer", "minimum": 1, "maximum": 500},
        },
    },
    "Loop": {"type": "object", "properties": {"next": {"$ref": "#/components/schemas/Loop"}}},
}
_ESTIMATES = {
    # Six bytes a character: a control character is written as a \u00XX escape.
    "bounded_string": ({"type": "string", "maxLength": 10}, 62),
    "unbounded_string": ({"type": "string"}, None),
    "uuid": ({"type": "string", "format": "uuid"}, 38),
    "unknown_format_with_a_bound": ({"type": "string", "format": "email", "maxLength": 5}, 32),
    "unknown_format_without_a_bound": ({"type": "string", "format": "email"}, None),
    "ascii_pattern": ({"type": "string", "maxLength": 10, "pattern": "^[a-zA-Z0-9_-]+$"}, 12),
    "ascii_pattern_with_a_quote": ({"type": "string", "maxLength": 10, "pattern": '^[a"]+$'}, 22),
    "dot_pattern": ({"type": "string", "maxLength": 10, "pattern": "^.+$"}, 62),
    "word_class_pattern": ({"type": "string", "maxLength": 10, "pattern": "^\\w+$"}, 62),
    "negated_class_pattern": ({"type": "string", "maxLength": 10, "pattern": "^[^ ]+$"}, 62),
    "enum": ({"enum": ["ab", "abcd"]}, 6),
    "non_ascii_enum": ({"enum": ["\u00e9t\u00e9"]}, 7),
    "bounded_integer": ({"type": "integer", "minimum": -3, "maximum": 1000}, 4),
    "integer": ({"type": "integer"}, 24),
    "bounded_number_is_still_a_double": ({"type": "number", "minimum": 0, "maximum": 1}, 24),
    "optional": ({"anyOf": [{"type": "string", "maxLength": 3}, {"type": "null"}]}, 20),
    "union_with_an_unbounded_member": ({"anyOf": [{"type": "string"}, {"type": "null"}]}, None),
    "all_of_takes_its_smallest_bounded_member": (
        {"allOf": [{"type": "string"}, {"type": "boolean"}, {"type": "integer"}]},
        5,
    ),
    "array": ({"type": "array", "maxItems": 3, "items": {"type": "boolean"}}, 2 + 15 + 2),
    "empty_array": ({"type": "array", "maxItems": 0}, 2),
    "unbounded_array": ({"type": "array", "items": {"type": "boolean"}}, None),
    "free_form_mapping": ({"type": "object", "additionalProperties": True}, None),
    "model_that_keeps_extra_keys": (
        {"type": "object", "properties": {"a": {"type": "boolean"}}, "additionalProperties": True},
        None,
    ),
    "model_that_forbids_extra_keys": (
        {"type": "object", "properties": {"a": {"type": "boolean"}}, "additionalProperties": False},
        11,
    ),
    "empty_closed_object": ({"type": "object", "additionalProperties": False}, 2),
    "any": ({}, None),
    # {"a":"<36>","n":500}: 2 + 4 + 38 + 1 + 4 + 3 = 52.
    "model": ({"$ref": "#/components/schemas/Pair"}, 52),
    "self_reference": ({"$ref": "#/components/schemas/Loop"}, None),
}


@pytest.mark.parametrize(("schema", "expected"), _ESTIMATES.values(), ids=_ESTIMATES.keys())
def test_schema_body_estimate(schema, expected):
    assert schema_body_estimate(schema, _COMPONENTS) == expected


def test_the_estimate_is_the_compact_json_of_the_largest_body():
    pair = {"a": "00000000-0000-0000-0000-000000000000", "n": 500}
    body = {"pairs": [pair] * 7}
    schema = {
        "type": "object",
        "properties": {
            "pairs": {
                "type": "array",
                "maxItems": 7,
                "items": {"$ref": "#/components/schemas/Pair"},
            }
        },
    }
    assert schema_body_estimate(schema, _COMPONENTS) == len(json.dumps(body, separators=(",", ":")))


def test_each_measured_route_fits_the_default_and_is_not_a_large_json_route():
    for route, (size, reason) in MEASURED_UNDER_THE_DEFAULT.items():
        assert reason
        assert size <= DEFAULT_MAX_REQUEST_BODY_BYTES, route
        assert route not in LARGE_JSON_ROUTES, route


@pytest.mark.parametrize("service", SERVICES)
def test_compose_passes_the_setting_with_the_shared_default(service):
    environment = yaml.safe_load(COMPOSE.read_text())["services"][service]["environment"]
    assert environment[LIMIT_ENV] == f"${{{LIMIT_ENV}:-{DEFAULT_MAX_REQUEST_BODY_BYTES}}}"


def test_the_specification_names_every_marked_route():
    text = SPEC.read_text(encoding="utf-8")
    for service, method, path in [*UPLOAD_ROUTES, *LARGE_JSON_ROUTES, *FORWARD_ROUTES]:
        assert f"`{method} {path}`" in text, (service, method, path)


# -- Streaming routes (issue #1483) -------------------------------------------------

# The response classes whose `__call__` reads the request (a disconnect listener beside
# the stream): Starlette's `StreamingResponse`, and sse-starlette's
# `EventSourceResponse` (not installed in HERD, so known by name). A class is one of
# them when any class in its MRO has one of these names, so a subclass counts.
STREAMING_CLASS_NAMES = {"StreamingResponse", "EventSourceResponse"}


def _is_streaming_class(value: object) -> bool:
    return inspect.isclass(value) and any(
        base.__name__ in STREAMING_CLASS_NAMES for base in value.__mro__
    )


def _names_a_streaming_class(text: str) -> bool:
    """Whether an annotation written as text names a streaming class: any name or
    attribute in it whose last part is one of STREAMING_CLASS_NAMES."""
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        return False
    return any(
        (isinstance(n, ast.Name) and n.id in STREAMING_CLASS_NAMES)
        or (isinstance(n, ast.Attribute) and n.attr in STREAMING_CLASS_NAMES)
        for n in ast.walk(tree)
    )


def _annotation_streams(annotation: object) -> bool:
    """A class, or a union, `Optional`, or `Annotated` with a streaming member; an
    annotation left as text (a string, or a forward reference no namespace resolves)
    counts when it names one."""
    if _is_streaming_class(annotation):
        return True
    if isinstance(annotation, str):
        return _names_a_streaming_class(annotation)
    if isinstance(annotation, typing.ForwardRef):
        return _names_a_streaming_class(annotation.__forward_arg__)
    return any(_annotation_streams(arg) for arg in typing.get_args(annotation))


def _return_annotation(endpoint: object) -> object:
    try:
        return typing.get_type_hints(endpoint, include_extras=True).get("return")
    except Exception:  # an unresolvable name: fall back to the raw annotation
        return getattr(endpoint, "__annotations__", {}).get("return")


def _resolve(node: ast.AST, namespace: dict) -> object:
    """The object a callee names in the endpoint's module, or None."""
    if isinstance(node, ast.Name):
        return namespace.get(node.id, getattr(builtins, node.id, None))
    if isinstance(node, ast.Attribute):
        owner = _resolve(node.value, namespace)
        return None if owner is None else getattr(owner, node.attr, None)
    return None


def _own_statements(function: ast.AST) -> list[ast.AST]:
    """Every node of the function's own body, not of a function, lambda, or class
    defined inside it."""
    found: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        found.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return found


def _bind(target: ast.AST, value: ast.AST | None, assigned: dict[str, list]) -> None:
    """Record what an assignment binds: a name to its value, each name of a tuple or
    list target to the matching element of a tuple or list value, and any other name
    in the target to an unknown value (None), so it counts as a binding."""
    if isinstance(target, ast.Name):
        assigned.setdefault(target.id, []).append(value)
    elif isinstance(target, (ast.Tuple, ast.List)):
        elements = value.elts if isinstance(value, (ast.Tuple, ast.List)) else None
        if elements is not None and len(elements) == len(target.elts):
            for part, element in zip(target.elts, elements):
                _bind(part, element, assigned)
        else:
            for part in target.elts:
                _bind(part, None, assigned)
    elif isinstance(target, ast.Starred):
        _bind(target.value, None, assigned)


def _bindings(nodes: list[ast.AST]) -> dict[str, list]:
    """Every name the nodes bind, to the values bound: `=`, `: T =`, `+=`, a walrus
    (`:=`), a `for` target, and a `with ... as` target."""
    assigned: dict[str, list] = {}
    for node in nodes:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                _bind(target, node.value, assigned)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            _bind(node.target, getattr(node, "value", None), assigned)
        elif isinstance(node, ast.NamedExpr):
            _bind(node.target, node.value, assigned)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            _bind(node.target, None, assigned)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            _bind(node.optional_vars, None, assigned)
    return assigned


def _streaming_aliases(nodes: list[ast.AST]) -> set[str]:
    """Names an import binds to a streaming class (`from x import EventSourceResponse
    as Sse`), so a call of the alias counts when the module cannot be imported."""
    return {
        alias.asname
        for node in nodes
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.asname and alias.name in STREAMING_CLASS_NAMES
    }


def _streaming_value(
    value: ast.AST | None,
    namespace: dict,
    assigned: dict[str, list],
    aliases: set[str],
    depth: int = 0,
) -> bool:
    """Whether a returned expression is a call of a streaming class: through both
    branches of a conditional expression and every operand of `and`/`or`, a local name
    bound once, and `functools.partial` (a partial object in the module, or a local
    `partial(...)` call) unwrapped to the class it wraps."""
    if value is None or depth > 20:
        return False
    if isinstance(value, ast.IfExp):
        return any(
            _streaming_value(v, namespace, assigned, aliases, depth + 1)
            for v in (value.body, value.orelse)
        )
    if isinstance(value, ast.BoolOp):
        return any(
            _streaming_value(v, namespace, assigned, aliases, depth + 1) for v in value.values
        )
    if isinstance(value, ast.Await):
        return _streaming_value(value.value, namespace, assigned, aliases, depth + 1)
    if isinstance(value, ast.NamedExpr):
        return _streaming_value(value.value, namespace, assigned, aliases, depth + 1)
    if isinstance(value, ast.Name) and len(assigned.get(value.id, [])) == 1:
        return _streaming_value(assigned[value.id][0], namespace, assigned, aliases, depth + 1)
    if not isinstance(value, ast.Call):
        return False
    return _streaming_callee(value.func, namespace, assigned, aliases, depth + 1)


def _streaming_callee(
    func: ast.AST, namespace: dict, assigned: dict[str, list], aliases: set[str], depth: int
) -> bool:
    if depth > 20:
        return False
    if isinstance(func, ast.Name) and len(assigned.get(func.id, [])) == 1:
        bound = assigned[func.id][0]
        if isinstance(bound, ast.Call) and _is_partial_call(bound, namespace) and bound.args:
            return _streaming_callee(bound.args[0], namespace, assigned, aliases, depth + 1)
        if isinstance(bound, (ast.Name, ast.Attribute)):
            return _streaming_callee(bound, namespace, assigned, aliases, depth + 1)
    if isinstance(func, ast.Call) and _is_partial_call(func, namespace) and func.args:
        return _streaming_callee(func.args[0], namespace, assigned, aliases, depth + 1)
    resolved = _resolve(func, namespace)
    while isinstance(resolved, functools.partial):
        resolved = resolved.func
    if resolved is not None:
        return _is_streaming_class(resolved)
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    return name in STREAMING_CLASS_NAMES or name in aliases  # a name the module lacks


def _is_partial_call(call: ast.Call, namespace: dict) -> bool:
    return _resolve(call.func, namespace) is functools.partial or (_callee(call) == "partial")


def _scanned_functions(endpoint: object) -> list[object]:
    """The endpoint, unwrapped through `__wrapped__`, and every function its closure
    holds, followed through closures again: a decorator that does not use
    `functools.wraps` leaves the real endpoint in its wrapper's closure."""
    found: list[object] = []
    stack = [inspect.unwrap(endpoint)]
    while stack and len(found) < 20:
        function = stack.pop()
        if function in found or not callable(function):
            continue
        found.append(function)
        for cell in getattr(function, "__closure__", None) or ():
            try:
                content = cell.cell_contents
            except ValueError:  # an empty cell
                continue
            if inspect.isfunction(content):
                stack.append(inspect.unwrap(content))
    return found


def _function_returns_a_stream(function: object) -> bool:
    try:
        source = textwrap.dedent(inspect.getsource(function))
    except (OSError, TypeError):
        return False
    try:
        tree = ast.parse(source)
    except SyntaxError:  # a lambda inside an expression that spans lines
        return False
    namespace = getattr(function, "__globals__", {})
    module_aliases: set[str] = set()
    try:
        module_source = Path(inspect.getsourcefile(function) or "").read_text(encoding="utf-8")
        module_aliases = _streaming_aliases(list(ast.walk(ast.parse(module_source))))
    except (OSError, TypeError, SyntaxError):
        pass
    if getattr(function, "__name__", "") == "<lambda>":
        # The source is the line that holds the lambda: its body is what it returns.
        aliases = module_aliases
        return any(
            _streaming_value(node.body, namespace, {}, aliases)
            for node in ast.walk(tree)
            if isinstance(node, ast.Lambda)
        )
    definition = next(
        (n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))), None
    )
    if definition is None:
        return False
    nodes = _own_statements(definition)
    assigned = _bindings(nodes)
    aliases = module_aliases | _streaming_aliases(nodes)
    return any(
        _streaming_value(node.value, namespace, assigned, aliases)
        for node in nodes
        if isinstance(node, ast.Return)
    )


def _returned_streaming_calls(endpoint: object) -> bool:
    """Whether the endpoint returns a call of a streaming class from its own body (see
    the module docstring for each spelling followed), or a lambda endpoint's body is
    one, or a function the endpoint's closure holds does (a decorator without
    `functools.wraps`)."""
    return any(_function_returns_a_stream(f) for f in _scanned_functions(endpoint))


def streaming_reasons(route: object) -> list[str]:
    """Why a route answers with a streaming response, by what the scan sees: its
    `response_class` (declared on the route, its router, or the app), its endpoint's
    return annotation, and a returned call (`_returned_streaming_calls`). Empty when
    none of the three shows one."""
    reasons = []
    response_class = getattr(route, "response_class", None)
    response_class = getattr(response_class, "value", response_class)  # a DefaultPlaceholder
    if _is_streaming_class(response_class):
        reasons.append("response_class")
    endpoint = getattr(route, "endpoint", None)
    if endpoint is None:
        return reasons
    if _annotation_streams(_return_annotation(endpoint)):
        reasons.append("return annotation")
    if _returned_streaming_calls(endpoint):
        reasons.append("returned call")
    return reasons


def streaming_without_body(routes: list[dict]) -> set[tuple[str, str]]:
    """(method, path) of every streaming route that has no body parameter."""
    return {
        (method, r["path"])
        for r in routes
        if r["streaming"] and not r["body"]
        for method in r["methods"]
    }


def streaming_problems(
    service: str, routes: list[dict], table: dict[tuple[str, str, str], str]
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """(unlisted, stale): the service's streaming routes with no body parameter that
    `table` does not list, and the routes `table` lists for it that are not one."""
    found = streaming_without_body(routes)
    listed = {(m, p) for s, m, p in table if s == service}
    return found - listed, listed - found


@pytest.mark.parametrize("service", SERVICES)
def test_every_streaming_route_reads_its_body_first_or_is_listed(service):
    """A streaming route with no body parameter, given a body over the limit, fails
    behind the request logger (a 500, or a stream cut short; OPS-VALID-9). Each one
    must take its body as a parameter, so FastAPI reads it before the route runs, or
    be listed in STREAMING_WITHOUT_BODY with a reason; a listed route that no longer
    streams without a body fails too."""
    unlisted, stale = streaming_problems(
        service, built_app(service)["routes"], STREAMING_WITHOUT_BODY
    )
    assert unlisted == set(), f"{service}: streaming routes with no body parameter"
    assert stale == set(), f"{service}: stale STREAMING_WITHOUT_BODY entries"


def test_the_streaming_exemptions_name_only_existing_services_with_reasons():
    assert {s for s, _, _ in STREAMING_WITHOUT_BODY} <= set(SERVICES)
    assert all(reason.strip() for reason in STREAMING_WITHOUT_BODY.values())


def test_the_scan_sees_the_one_streaming_route_herd_has():
    """Not vacuous on the real code: the reservation assistant's stream is seen twice
    over (its annotation and its returned call) and takes its body as a parameter."""
    routes = built_app("ai-orchestrator")["routes"]
    streaming = {
        (m, r["path"]): (r["streaming"], r["body"])
        for r in routes
        if r["streaming"]
        for m in r["methods"]
    }
    assert streaming == {
        ("POST", "/reservations/{reservation_id}/assistant/stream"): (
            ["return annotation", "returned call"],
            True,
        )
    }


# Fixture endpoints, each in a module of its own written to disk (the scan reads the
# endpoint's source). BROKEN must be flagged; HONEST must not.
_FIXTURE_HEAD = """
from typing import Annotated, Optional

from fastapi import APIRouter, Body, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from starlette import responses


class Ask(BaseModel):
    text: str


class Feed(StreamingResponse):
    pass


class EventSourceResponse(JSONResponse):
    # sse-starlette's class, known by name: this stand-in is not a StreamingResponse.
    pass


def chunks():
    yield b"abc"


app = FastAPI()
"""

_BROKEN = {
    "a_returned_call": """
@app.post("/r")
async def route():
    return StreamingResponse(chunks())
""",
    "a_return_annotation": """
@app.get("/r")
async def route() -> StreamingResponse:
    return make_it()


def make_it():
    return StreamingResponse(chunks())
""",
    "an_optional_return_annotation": """
@app.get("/r", response_model=None)
async def route() -> Optional[StreamingResponse]:
    return None
""",
    "a_response_class": """
@app.post("/r", response_class=StreamingResponse)
async def route():
    return chunks()
""",
    "a_router_default_response_class": """
router = APIRouter(default_response_class=StreamingResponse)


@router.post("/r")
async def route():
    return chunks()


app.include_router(router)
""",
    "a_subclass": """
@app.post("/r")
async def route():
    return Feed(chunks())
""",
    "a_module_attribute": """
@app.post("/r")
async def route():
    return responses.StreamingResponse(chunks())
""",
    "an_event_source_response_by_name": """
@app.post("/r")
async def route():
    return EventSourceResponse({})
""",
    "an_event_source_response_the_module_does_not_hold": """
@app.post("/r")
async def route():
    from sse_starlette import sse  # never run: not installed, and not in the module

    return sse.EventSourceResponse(chunks())
""",
    "a_conditional_expression": """
@app.post("/r")
async def route(flag: bool = False):
    return StreamingResponse(chunks()) if flag else JSONResponse({})
""",
    "the_other_branch_of_a_conditional_expression": """
@app.post("/r")
async def route(flag: bool = False):
    return JSONResponse({}) if flag else StreamingResponse(chunks())
""",
    "an_or_expression": """
@app.post("/r")
async def route():
    return None or StreamingResponse(chunks())
""",
    "a_tuple_unpacking": """
@app.post("/r")
async def route():
    response, status = StreamingResponse(chunks()), 200
    return response
""",
    "a_walrus": """
@app.post("/r")
async def route():
    if (response := StreamingResponse(chunks())) is not None:
        return response
""",
    "a_module_partial": """
import functools

Stream = functools.partial(StreamingResponse, media_type="text/event-stream")


@app.post("/r")
async def route():
    return Stream(chunks())
""",
    "a_local_partial": """
from functools import partial


@app.post("/r")
async def route():
    stream = partial(StreamingResponse, media_type="text/event-stream")
    return stream(chunks())
""",
    "a_lambda_endpoint": """
app.add_api_route("/r", lambda: StreamingResponse(chunks()), methods=["POST"])
""",
    "a_decorator_without_wraps": """
def logged(endpoint):
    async def wrapper():
        return await endpoint()

    return wrapper


@app.post("/r")
@logged
async def route():
    return StreamingResponse(chunks())
""",
    "an_unresolvable_string_annotation": """
@app.post("/r", response_model=None)
async def route() -> "Missing | StreamingResponse":
    return None
""",
    "an_aliased_import_that_is_not_installed": """
try:
    from sse_starlette.sse import EventSourceResponse as Sse
except ImportError:
    Sse = None


@app.post("/r")
async def route():
    return Sse(chunks())
""",
    "an_assigned_response": """
@app.post("/r")
async def route(request: Request):
    response = StreamingResponse(chunks())
    response.headers["x-a"] = "b"
    return response
""",
    "a_body_read_by_hand": """
@app.post("/r")
async def route(request: Request):
    await request.body()
    return StreamingResponse(chunks())
""",
}

_HONEST = {
    "a_body_parameter": """
@app.post("/r")
async def route(body: Ask) -> StreamingResponse:
    return StreamingResponse(chunks())
""",
    "a_body_by_keyword": """
@app.post("/r")
async def route(payload: Annotated[dict, Body()]):
    return StreamingResponse(chunks())
""",
    "a_conditional_expression_of_json": """
@app.post("/r")
async def route(flag: bool = False):
    return JSONResponse({}) if flag else {"a": 1}
""",
    "a_decorator_without_wraps_around_json": """
def logged(endpoint):
    async def wrapper():
        return await endpoint()

    return wrapper


@app.post("/r")
@logged
async def route():
    return {"a": 1}
""",
    "a_lambda_endpoint_of_json": """
app.add_api_route("/r", lambda: {"a": 1}, methods=["POST"])
""",
    "json_with_no_body": """
@app.post("/r")
async def route():
    return {"a": 1}
""",
    "a_json_response_with_no_body": """
@app.post("/r")
async def route() -> JSONResponse:
    return JSONResponse({"a": 1})
""",
    "a_stream_only_in_a_nested_function": """
@app.post("/r")
async def route():
    def later():
        return StreamingResponse(chunks())

    return {"a": 1}
""",
}


def _fixture_routes(tmp_path: Path, name: str, source: str) -> list[dict]:
    path = tmp_path / f"streaming_fixture_{name}.py"
    path.write_text(_FIXTURE_HEAD + source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from fastapi.routing import iter_route_contexts

    return [
        {
            "methods": sorted(route.methods or ()),
            "path": route.path,
            "body": getattr(route, "body_field", None) is not None,
            "streaming": streaming_reasons(route),
        }
        for route in iter_route_contexts(module.app.routes)
        if route.path == "/r"
    ]


@pytest.mark.parametrize("name", sorted(_BROKEN))
def test_a_streaming_route_with_no_body_parameter_is_flagged(tmp_path, name):
    routes = _fixture_routes(tmp_path, name, _BROKEN[name])
    assert len(routes) == 1
    assert len(streaming_without_body(routes)) == 1, routes


@pytest.mark.parametrize("name", sorted(_HONEST))
def test_an_honest_route_is_not_flagged(tmp_path, name):
    routes = _fixture_routes(tmp_path, name, _HONEST[name])
    assert len(routes) == 1
    assert streaming_without_body(routes) == set(), routes


def test_a_flagged_route_passes_only_while_it_is_listed():
    flagged = [{"methods": ["POST"], "path": "/r", "body": False, "streaming": ["returned call"]}]
    honest = [{**flagged[0], "body": True}]
    entry = {("svc", "POST", "/r"): "an export whose client sends no body"}
    assert streaming_problems("svc", flagged, {}) == ({("POST", "/r")}, set())
    assert streaming_problems("svc", flagged, entry) == (set(), set())
    assert streaming_problems("svc", honest, entry) == (set(), {("POST", "/r")})
    assert streaming_problems("other", flagged, entry) == ({("POST", "/r")}, set())


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


_OPERATORS = {
    ast.Add: "+",
    ast.Sub: "-",
    ast.Mult: "*",
    ast.Div: "/",
    ast.FloorDiv: "//",
    ast.Mod: "%",
    ast.Pow: "**",
}


def render(node: ast.AST | None) -> str:
    """A text for an expression built from the AST alone, the same on every Python.

    `ast.unparse` is not used: its quoting of f-strings changed between patch releases
    of 3.12 (3.12.3 and 3.12.16 render the same f-string differently), so a key built
    from it is not portable. Here a string is its value in double quotes, an f-string
    is its literal parts with each formatted expression in braces, and the other
    shapes a URL or method argument takes (names, attributes, calls, subscripts,
    arithmetic) are written out the same way; anything else is `<NodeType>`."""
    if node is None:
        return "?"
    if isinstance(node, ast.Constant):
        return f'"{node.value}"' if isinstance(node.value, str) else str(node.value)
    if isinstance(node, ast.JoinedStr):
        return 'f"' + "".join(_fstring_part(part) for part in node.values) + '"'
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{render(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        args = [render(arg) for arg in node.args]
        args += [
            f"{k.arg}={render(k.value)}" if k.arg else f"**{render(k.value)}" for k in node.keywords
        ]
        return f"{render(node.func)}({', '.join(args)})"
    if isinstance(node, ast.Subscript):
        return f"{render(node.value)}[{render(node.slice)}]"
    if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
        return f"{render(node.left)} {_OPERATORS[type(node.op)]} {render(node.right)}"
    return f"<{type(node).__name__}>"


def _fstring_part(part: ast.AST) -> str:
    if isinstance(part, ast.Constant) and isinstance(part.value, str):
        return part.value
    if isinstance(part, ast.FormattedValue):
        conversion = f"!{chr(part.conversion)}" if part.conversion != -1 else ""
        spec = ""
        if part.format_spec is not None:
            spec = ":" + "".join(_fstring_part(p) for p in part.format_spec.values)
        return "{" + render(part.value) + conversion + spec + "}"
    return f"<{type(part).__name__}>"


def _text(node: ast.AST | None, assigned: dict[str, list[ast.expr]]) -> str:
    """A stable text for a method or URL argument: a string literal's value, a local
    name assigned exactly once in the same function resolved to what it holds, any
    other expression rendered by `render`, `?` when absent."""
    if node is None:
        return "?"
    if isinstance(node, ast.Name) and len(assigned.get(node.id, [])) == 1:
        node = assigned[node.id][0]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return render(node)


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
        assert detail and all(route in FORWARD_ROUTES for route, _ in detail), detail
        assert envelope_shortfalls(detail) == [], detail
    elif kind == "marked":
        assert detail and all(route in UPLOAD_ROUTES for route in detail), detail
    elif kind == "bounded":
        size, reason = detail
        assert reason
        assert size is None or size <= MIN_MAX_REQUEST_BODY_BYTES
    else:
        assert isinstance(detail, str) and detail


def envelope_shortfalls(detail: list) -> list[tuple]:
    """Each (receiver, source) of an "envelope" entry whose receiver does not take what
    its source admitted plus the envelope: the receiver's extra bytes must be at least
    the source's plus FORWARD_ENVELOPE_BYTES (a source of None adds nothing). Both
    read from the tables, which the built apps are checked against. So a forward of a
    body a large-JSON route admitted needs the large allowance plus the envelope."""
    return [
        (receiver, source)
        for receiver, source in detail
        if route_extra_bytes(receiver)
        < (0 if source is None else route_extra_bytes(source)) + FORWARD_ENVELOPE_BYTES
    ]


def test_the_envelope_check_fails_both_ways():
    canvas = ("cabling", "PUT", "/internal/forks/{reservation_id}/canvas")
    fork_canvas = ("reservations", "PUT", "/{reservation_id}/fork/canvas")
    create = ("reservations", "POST", "/")
    assert envelope_shortfalls([(canvas, fork_canvas), (create, None)]) == []
    # A large-JSON source into a receiver with the envelope alone falls short.
    assert envelope_shortfalls([(create, fork_canvas)]) == [(create, fork_canvas)]
    facade = ("integration", "POST", "/reservations")
    assert envelope_shortfalls([(create, facade)]) == []


def test_every_forward_route_has_a_sender():
    receivers = {
        route for kind, detail in FORWARDS.values() if kind == "envelope" for route, _ in detail
    }
    assert receivers == set(FORWARD_ROUTES)


def test_every_envelope_source_is_a_route_of_its_service():
    sources = {
        source
        for kind, detail in FORWARDS.values()
        if kind == "envelope"
        for _, source in detail
        if source is not None
    }
    for service, method, path in sorted(sources):
        routes = built_app(service)["routes"]
        assert any(r["path"] == path and method in r["methods"] for r in routes), (
            service,
            method,
            path,
        )


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
            'post POST f"{base}/x"',
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


# Literal expected keys, so a change in how any Python renders an expression fails
# here, loudly, on every version (3.12.3 and 3.12.16 render the same f-string
# differently through `ast.unparse`, which is why the keys do not use it).
_RENDERED = {
    "plain_string": ("'/internal/forks'", '"/internal/forks"'),
    "f_string": (
        "f'{settings.inventory_service_url}/devices/batch'",
        'f"{settings.inventory_service_url}/devices/batch"',
    ),
    "f_string_with_nested_quotes": (
        "f'{settings.cabling_service_url.rstrip(\"/\")}/topologies/{topology_id}'",
        'f"{settings.cabling_service_url.rstrip("/")}/topologies/{topology_id}"',
    ),
    "f_string_double_quoted_with_nested_single_quotes": (
        "f\"{settings.execution_service_url.rstrip('/')}/execute\"",
        'f"{settings.execution_service_url.rstrip("/")}/execute"',
    ),
    "conversion_and_format_spec": ("f'{a!r}-{b:>4}'", 'f"{a!r}-{b:>4}"'),
    "arithmetic_and_subscript": ("base + paths[0]", "base + paths[0]"),
    "other_expression": ("[u for u in urls]", "<ListComp>"),
}


@pytest.mark.parametrize(("expression", "expected"), _RENDERED.values(), ids=_RENDERED.keys())
def test_render_gives_the_same_text_on_every_python(expression, expected):
    assert render(ast.parse(expression, mode="eval").body) == expected


def test_an_f_string_receiver_key_is_pinned_literally():
    source = (
        "async def f(settings):\n"
        "    url = f\"{settings.execution_service_url.rstrip('/')}/execute\"\n"
        "    await client.post(url, json=body)\n"
        "    await call_service(settings.x, 'POST', '/internal/check', json_body=body)\n"
    )
    assert body_calls(source) == [
        ("f", 'post POST f"{settings.execution_service_url.rstrip("/")}/execute"'),
        ("f", "call_service POST /internal/check"),
    ]


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
