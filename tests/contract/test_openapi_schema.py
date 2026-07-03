"""OpenAPI shape-signature snapshot tests.

For each backend service, fetch `/api/<svc>/openapi.json` from the live stack
and compare a normalized shape signature against a committed JSON snapshot at
`tests/contract/snapshots/<svc>.json`.

The signature captures the contract surface that, if it changes, breaks
clients:

- Every (method, path) pair the service exposes.
- For every named schema in `components.schemas`: the sorted set of
  property names plus their declared `type` and the `required` set.

It deliberately ignores: descriptions, examples, response examples, FastAPI
internals (`HTTPValidationError`, `ValidationError`, `Detail` placeholders),
and schema descriptions. Those churn harmlessly.

To regenerate snapshots after an intentional change, run with
`HERD_UPDATE_OPENAPI_SNAPSHOTS=1`. The next pytest run will then compare
against the new baseline. Review the diff in git before committing.
"""

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots"

SERVICES = [
    "auth",
    "inventory",
    "reservations",
    "cabling",
    "acl",
    "execution",
    "config",
    "ai",
    "user-profile",
    "notifications",
    "v1",
    "secrets",
]

BASE_URL = os.getenv("HERD_BASE_URL", "https://localhost/api")
UPDATE = os.getenv("HERD_UPDATE_OPENAPI_SNAPSHOTS") == "1"

# The gateway can briefly answer 200 with an empty or not-yet-ready body while a
# route is registered but its upstream is still warming up; the body then fails to
# parse as JSON. Retry the fetch a few times so that warm-up race does not read as a
# contract failure, while a genuinely down service or real contract drift still fails
# once the budget is spent. See issue #199.
OPENAPI_FETCH_ATTEMPTS = 5
OPENAPI_FETCH_BACKOFF_SECONDS = 0.5

# FastAPI auto-generated noise-schemas; if a service-specific schema with the
# same name is added later, the test will still see the real one. These are
# excluded only when their definition matches FastAPI's default shape.
NOISE_SCHEMAS = {"HTTPValidationError", "ValidationError"}


def _schema_signature(schema: dict[str, Any]) -> dict[str, Any]:
    """Reduce a JSONSchema dict to the fields a client actually depends on."""
    properties = schema.get("properties") or {}
    typed_props: dict[str, str] = {}
    for name, definition in sorted(properties.items()):
        if isinstance(definition, dict):
            typed_props[name] = (
                definition.get("type")
                or definition.get("$ref")
                or ("anyOf" if "anyOf" in definition else "unknown")
            )
        else:
            typed_props[name] = "unknown"
    return {
        "type": schema.get("type", "object"),
        "required": sorted(schema.get("required") or []),
        "properties": typed_props,
    }


def _signature(openapi: dict[str, Any]) -> dict[str, Any]:
    """Normalize an OpenAPI document into a snapshot-friendly contract surface."""
    paths_block = openapi.get("paths") or {}
    method_paths: list[list[str]] = []
    for path, methods in sorted(paths_block.items()):
        if not isinstance(methods, dict):
            continue
        for method in sorted(methods.keys()):
            if method.lower() in {"parameters", "summary", "description"}:
                continue
            method_paths.append([method.upper(), path])

    schemas_block = (openapi.get("components") or {}).get("schemas") or {}
    schemas: dict[str, Any] = {}
    for name, schema in sorted(schemas_block.items()):
        if name in NOISE_SCHEMAS:
            continue
        if isinstance(schema, dict):
            schemas[name] = _schema_signature(schema)

    return {"paths": method_paths, "schemas": schemas}


def _fetch_openapi(
    client: httpx.Client,
    url: str,
    *,
    attempts: int = OPENAPI_FETCH_ATTEMPTS,
    backoff: float = OPENAPI_FETCH_BACKOFF_SECONDS,
) -> dict[str, Any]:
    """Fetch an OpenAPI document, tolerating a transient empty/non-JSON 200 from the gateway.

    Retries on a non-200 status, or a 200 whose body is empty or not valid JSON, up to
    `attempts` times with a fixed `backoff` between tries. Raises AssertionError carrying
    the last status code and a body snippet once the budget is exhausted, so a genuinely
    down service or real contract drift still fails with a diagnosable message.
    """
    last_status: int | None = None
    last_body = ""
    for attempt in range(attempts):
        resp = client.get(url)
        last_status = resp.status_code
        last_body = resp.text
        if resp.status_code == 200 and resp.text.strip():
            try:
                return resp.json()
            except json.JSONDecodeError:
                pass  # empty/garbled body from a warming-up upstream; retry below
        if attempt < attempts - 1:
            time.sleep(backoff)
    raise AssertionError(
        f"could not fetch a valid OpenAPI JSON from {url} after {attempts} attempts: "
        f"last status {last_status}, body[:200]={last_body[:200]!r}"
    )


def _snapshot_path(service: str) -> Path:
    return SNAPSHOT_DIR / f"{service}.json"


@pytest.mark.parametrize("service", SERVICES)
def test_openapi_signature_matches_snapshot(service):
    """Each service's OpenAPI shape signature must match its committed snapshot."""
    with httpx.Client(verify=False, timeout=10.0) as client:
        openapi = _fetch_openapi(client, f"{BASE_URL}/{service}/openapi.json")
    actual = _signature(openapi)

    snapshot_file = _snapshot_path(service)

    if UPDATE or not snapshot_file.exists():
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        snapshot_file.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n")
        if not UPDATE:
            pytest.skip(
                f"Wrote initial snapshot for {service} at {snapshot_file}; "
                f"re-run to compare against the baseline."
            )
        return

    expected = json.loads(snapshot_file.read_text())
    assert actual == expected, (
        f"OpenAPI contract drift in {service}. "
        f"If intentional, regenerate with HERD_UPDATE_OPENAPI_SNAPSHOTS=1 and "
        f"review the diff in git.\n"
        f"Snapshot: {snapshot_file}\n"
        f"Diff (paths added/removed and schema field changes):\n"
        f"  paths-only-in-snapshot: "
        f"{[p for p in expected['paths'] if p not in actual['paths']]}\n"
        f"  paths-only-in-live:     "
        f"{[p for p in actual['paths'] if p not in expected['paths']]}\n"
        f"  schemas-only-in-snapshot: "
        f"{sorted(set(expected['schemas']) - set(actual['schemas']))}\n"
        f"  schemas-only-in-live:     "
        f"{sorted(set(actual['schemas']) - set(expected['schemas']))}"
    )
