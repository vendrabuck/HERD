"""Every service app bounds its request bodies, with its setting, and every upload
route declares its own larger limit (issue #1466).

`herd_common.validation_errors.install_validation_errors` installs `LimitRequestBody`
just outside `RefuseLoneSurrogates`; config installs its standalone copy's. The AST
guard `test_validation_errors_wiring.py` pins the install line's source; this guard
pins the BUILT apps, so it sees what the source scan cannot.

What is checked. The services are every directory under `services/` with an
`app/main.py`, found from the directory (today twelve). Each one's `app.main` is
imported in a subprocess of its own (every service's package is named `app`, so two
cannot share a process; the reason is spelled out in
`test_compose_settings_wiring._load_settings_fields`), with `MAX_REQUEST_BODY_BYTES`
and the upload settings set to values no default has. For each built app:

- the last two entries of `app.user_middleware` are `LimitRequestBody` and then
  `RefuseLoneSurrogates` (config: `LimitRequestBody` is the last, since it installs
  without the surrogate check), both from the module the service must use, so the
  limit runs before the surrogate scan and nothing else sits between them and the
  routes;
- the limit it was given is the environment's `MAX_REQUEST_BODY_BYTES`, so the
  setting reaches the middleware in every service (config through
  `app/request_limits.py`, the others through `HerdBaseSettings`);
- every route that takes a form body (any `File(...)` or `Form(...)` parameter, as
  FastAPI decides it: `body_field.field_info` is a `params.Form`) carries an
  `accepts_upload` mark; a marked route that takes no form is one of
  `JSON_UPLOAD_ROUTES` (a JSON body that carries a file as base64); and the marked
  routes, with each one's upload bytes, equal `UPLOAD_ROUTES` below. The bytes follow
  the settings the routes enforce (`DRIVER_MAX_SIZE_BYTES`, `UPLOAD_MAX_FILES` times
  `UPLOAD_MAX_FILE_BYTES`, the base64 length of `VALIDATE_PACKAGE_MAX_BYTES`), set here
  to odd values to prove it;
- `docker-compose.yml` passes `MAX_REQUEST_BODY_BYTES` to all twelve services as
  `${MAX_REQUEST_BODY_BYTES:-<default>}` with the shared default (the Settings-based
  services are also held by `test_compose_settings_wiring.py`; config is not, since
  it has no Settings model);
- the area specification names every upload route.

Limits. Only routes FastAPI routes are enumerated (`fastapi.routing.iter_route_contexts`
over the built app). Not seen: a route that reads a form or a stream by hand through
`Request` (`await request.form()`, `request.stream()`), which takes the default limit
like any other body; none reads a form or stream that way today, and the one raw read,
integration's test-stack webhook sink (`await request.body()`), takes the default.
A sub-application mounted with `app.mount` would not be enumerated, and is refused by
`test_validation_errors_wiring.py` (no second FastAPI app under `app/`).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pytest
import yaml
from herd_common.validation_errors import DEFAULT_MAX_REQUEST_BODY_BYTES

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
# The marked routes whose body is JSON, not a form: each carries a file as base64.
JSON_UPLOAD_ROUTES = {("execution", "POST", "/internal/validate-package")}

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
attribute = importlib.import_module(limit_module).UPLOAD_BYTES_ATTRIBUTE
routes = []
for route in iter_route_contexts(app.routes):
    body_field = getattr(route, "body_field", None)
    mark = getattr(route.endpoint, attribute, None)
    routes.append(
        {
            "methods": sorted(route.methods or ()),
            "path": route.path,
            "form": body_field is not None and isinstance(body_field.field_info, params.Form),
            "upload_bytes": None if mark is None else mark(),
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
def test_the_limit_sits_just_outside_the_surrogate_check(service):
    module = expected_module(service)
    tail = [(m, n) for m, n, _ in built_app(service)["middleware"]][-2:]
    if service == "config":
        assert tail[-1] == (module, "LimitRequestBody")
        assert (module, "RefuseLoneSurrogates") not in tail
    else:
        assert tail == [(module, "LimitRequestBody"), (module, "RefuseLoneSurrogates")]


@pytest.mark.parametrize("service", SERVICES)
def test_the_limit_is_the_services_setting(service):
    limits = [k for _, n, k in built_app(service)["middleware"] if n == "LimitRequestBody"]
    assert limits == [{"max_body_bytes": PROBE_LIMIT}]


@pytest.mark.parametrize("service", SERVICES)
def test_every_form_route_carries_an_upload_limit_and_the_table_is_complete(service):
    routes = built_app(service)["routes"]
    forms = {(m, r["path"]) for r in routes if r["form"] for m in r["methods"]}
    marked = {
        (m, r["path"]): r["upload_bytes"]
        for r in routes
        if r["upload_bytes"] is not None
        for m in r["methods"]
    }
    json_uploads = {(m, p) for s, m, p in JSON_UPLOAD_ROUTES if s == service}
    assert forms <= set(marked), f"{service}: unmarked forms {sorted(forms - set(marked))}"
    assert set(marked) - forms == json_uploads
    expected = {(m, p): b for (s, m, p), b in UPLOAD_ROUTES.items() if s == service}
    assert marked == expected
    assert all(b > 0 for b in marked.values())


def test_the_upload_tables_name_only_existing_services_and_routes():
    assert {s for s, _, _ in UPLOAD_ROUTES} <= set(SERVICES)
    assert JSON_UPLOAD_ROUTES <= set(UPLOAD_ROUTES)


@pytest.mark.parametrize("service", SERVICES)
def test_compose_passes_the_setting_with_the_shared_default(service):
    environment = yaml.safe_load(COMPOSE.read_text())["services"][service]["environment"]
    assert environment[LIMIT_ENV] == f"${{{LIMIT_ENV}:-{DEFAULT_MAX_REQUEST_BODY_BYTES}}}"


def test_the_specification_names_every_upload_route():
    text = SPEC.read_text(encoding="utf-8")
    for service, method, path in UPLOAD_ROUTES:
        assert f"`{method} {path}`" in text, (service, method, path)
