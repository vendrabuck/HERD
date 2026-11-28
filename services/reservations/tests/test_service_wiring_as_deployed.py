"""The reservations service wired the way it is deployed (issue #1527).

The rest of this suite runs with the list default of `PURPOSE_CATEGORIES`, an empty
`CORS_ORIGINS`, and an `app.main` imported once under the test environment, so it never
sees the comma string compose passes, the origins compose passes, or the logging
`app.main` sets up at import.

`PURPOSE_CATEGORIES`. Compose always passes it as one string
(`${PURPOSE_CATEGORIES:-<default>}` in `docker-compose.yml`), so every deployment goes
through `Settings._split_purpose_categories`. These tests build a fresh `Settings()`
from that string in the environment (monkeypatch restores it) and never touch the
module-level `settings`, except where `monkeypatch.setattr` swaps its list for one
test. What the documentation decides: a comma-separated list that replaces the
default whole, read in order (`docs/ENV_VARS.md`, ADR 0013), and a category must be in
it (RES-PURPOSE-1). What no rule decides, so nothing here pins it: white space around
an item, an empty item (`a,,b`, a trailing comma), an empty string (which compose's
`:-` form never passes: it substitutes the default), an all-blank string, and a
repeated item.

Logging, access log, and CORS. `setup_logging` runs and the middleware is added when
`app.main` is imported, so the module this suite already imported cannot show a
missing call. `_run_probe` imports `app.main` in a subprocess of its own with
`LOG_LEVEL`, `CORS_ORIGINS` (the compose default origin plus one more), and
`DB_SCHEMA` in its environment as compose sets them, no config file, and a
`DATABASE_URL` nothing connects to (the lifespan is not run), after building
uvicorn's `Config`, which applies uvicorn's own logging setup first, as the
container's `uvicorn app.main:app` does. The probe sends requests
through `httpx.ASGITransport` and the test reads what the process wrote to stdout and
stderr. Not seen: the lifespan, uvicorn's server loop and its own access log,
anything outside the app (the gateway).
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from app.config import Settings, settings

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_DIR = Path(__file__).resolve().parents[1]

_COMPOSE_LINE = re.compile(
    r"^\s+PURPOSE_CATEGORIES: \$\{PURPOSE_CATEGORIES:-([^}]*)\}\s*$", re.MULTILINE
)


def _compose_default() -> str:
    matches = _COMPOSE_LINE.findall((REPO_ROOT / "docker-compose.yml").read_text())
    assert len(matches) == 1, (
        "expected one `PURPOSE_CATEGORIES: ${PURPOSE_CATEGORIES:-<default>}` line in "
        f"docker-compose.yml, found {len(matches)}; update _COMPOSE_LINE if the form changed"
    )
    return matches[0]


@pytest.fixture
def settings_from_env(monkeypatch, tmp_path):
    """Build a fresh Settings from PURPOSE_CATEGORIES set to `value`, with no .env file
    and no config file, so only the environment decides."""
    import herd_common.config_loader as config_loader

    monkeypatch.setattr(config_loader, "CONFIG_FILE", str(tmp_path / "absent.json"))

    def build(value: str) -> Settings:
        monkeypatch.setenv("PURPOSE_CATEGORIES", value)
        return Settings(_env_file=None)

    return build


def test_the_compose_default_string_is_the_default_list(settings_from_env):
    """The string compose passes when `.env` sets nothing gives the field's default,
    the seven categories `docs/ENV_VARS.md` documents, in that order."""
    documented = [
        "qa_regression",
        "support_case_replication",
        "feature_development",
        "customer_demo_poc",
        "training",
        "performance_benchmark",
        "other",
    ]
    assert Settings.model_fields["purpose_categories"].default == documented
    assert settings_from_env(_compose_default()).purpose_categories == documented


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("training", ["training"]),
        ("zeta_lab,alpha_lab", ["zeta_lab", "alpha_lab"]),
        ("other,qa_regression,new_kind", ["other", "qa_regression", "new_kind"]),
    ],
)
def test_a_comma_string_replaces_the_list_whole_and_in_order(settings_from_env, value, expected):
    assert settings_from_env(value).purpose_categories == expected


def test_the_list_a_category_must_be_in_is_the_one_the_string_gives(settings_from_env, monkeypatch):
    """RES-PURPOSE-1 through the environment: a category in the string is accepted,
    and one that is only in the default list is refused with the pinned wording,
    which names the allowed values from the string."""
    from app.services.purpose_service import validate_purpose_category

    built = settings_from_env("zeta_lab,alpha_lab")
    monkeypatch.setattr(settings, "purpose_categories", built.purpose_categories)
    assert validate_purpose_category("alpha_lab") == "alpha_lab"
    with pytest.raises(ValueError) as refused:
        validate_purpose_category("qa_regression")
    assert str(refused.value) == "Unknown purpose_category; allowed: zeta_lab, alpha_lab"


# The probe. Sentinels mark what must never reach a log line: a query value, a header
# value, and a body.
_QUERY = "query-sentinel-7f3a"
_HEADER = "header-sentinel-91c2"
_BODY = "body-sentinel-5d08"
_LISTED = "https://lab.example"
_UNLISTED = "https://other.example"
_HELD_AT_WARNING = ("uvicorn.access", "uvicorn.error", "sqlalchemy.engine")

_PROBE = f"""
import asyncio
import json
import logging

import httpx
from uvicorn.config import Config

Config("app.main:app")  # applies uvicorn's logging setup, as `uvicorn app.main:app` does
import app.main


async def send():
    transport = httpx.ASGITransport(app=app.main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://probe") as client:
        responses = [
            await client.get(
                "/health?token={_QUERY}",
                headers={{"Authorization": "Bearer {_HEADER}", "Origin": "{_LISTED}"}},
            ),
            await client.options(
                "/health",
                headers={{"Origin": "{_LISTED}", "Access-Control-Request-Method": "GET"}},
            ),
            await client.options(
                "/health",
                headers={{"Origin": "{_UNLISTED}", "Access-Control-Request-Method": "GET"}},
            ),
            await client.post("/health", content=b"{_BODY}"),
            await client.get("/calendar"),
        ]
    return [
        [r.status_code, r.headers.get("access-control-allow-origin")] for r in responses
    ]


answers = asyncio.run(send())
# One line per logger and level. The five arguments are the shape uvicorn's access
# formatter unpacks, so the uvicorn.access handler can format the record too.
for name in ("app.probe",) + {_HELD_AT_WARNING!r}:
    for level in (logging.DEBUG, logging.INFO, logging.WARNING):
        text = name + " " + logging.getLevelName(level) + "-PROBE"
        logging.getLogger(name).log(level, '%s - "%s %s HTTP/%s" %d', "p", "GET", text, "1.1", 299)
print("PROBE-RESULT " + json.dumps(answers), flush=True)
"""

# (method, path, status) of each request the probe sends, in order.
_REQUESTS = [
    ("GET", "/health", 200),
    ("OPTIONS", "/health", 200),
    ("OPTIONS", "/health", 400),
    ("POST", "/health", 405),
    ("GET", "/calendar", 401),
]


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    """`probe(log_level)` gives (answers, lines) for one probe run at that level; each
    level runs once per module."""
    runs: dict[str, tuple[list, list[str]]] = {}
    config_dir = tmp_path_factory.mktemp("no-config")

    def run(log_level: str) -> tuple[list, list[str]]:
        if log_level not in runs:
            runs[log_level] = _run_probe(config_dir, log_level)
        return runs[log_level]

    return run


def _run_probe(config_dir: Path, log_level: str) -> tuple[list, list[str]]:
    env = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}
    env.update(
        {
            "LOG_LEVEL": log_level,
            "CORS_ORIGINS": f"https://localhost,{_LISTED}",
            "DB_SCHEMA": "reservations",
            "DATABASE_URL": "postgresql+asyncpg://probe:probe@127.0.0.1:9/probe",
            "HERD_CONFIG_FILE": str(config_dir / "absent.json"),
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=SERVICE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    lines = (result.stdout + result.stderr).splitlines()
    marker = [line for line in lines if line.startswith("PROBE-RESULT ")]
    assert len(marker) == 1, lines
    return json.loads(marker[0].removeprefix("PROBE-RESULT ")), lines


def _as_record(line: str) -> dict | None:
    try:
        record = json.loads(line)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def _lines_holding(lines: list[str], text: str) -> list[str]:
    return [line for line in lines if text in line]


def _access_records(lines: list[str]) -> list[dict]:
    records = (_as_record(line) for line in lines)
    return [r for r in records if r is not None and r.get("logger") == "herd.access"]


def test_every_request_logs_one_access_line_through_the_json_handler(probe):
    """OPS-LOG-7 and OPS-LOG-1 as deployed: each request, the two CORS preflights, a
    405, and a 401 included, writes exactly one `herd.access` line, a JSON object
    carrying `timestamp`, `level`, `service`, `logger`, and `message`, with `method`,
    `path` (no query string), `status_code`, and a numeric `duration_ms`. No header,
    query value, or body appears in any line the process wrote, the probe's own
    client's included: `setup_logging` holds the `httpx` logger at WARNING, so its
    per-request INFO line is not written."""
    answers, lines = probe("INFO")
    assert [status for status, _ in answers] == [status for *_, status in _REQUESTS]

    access = _access_records(lines)
    assert [(r["method"], r["path"], r["status_code"]) for r in access] == _REQUESTS
    for record in access:
        assert {"timestamp", "level", "service", "logger", "message"} <= record.keys()
        assert record["service"] == "reservations"
        assert record["level"] == "INFO"
        assert isinstance(record["duration_ms"], int | float)
        assert record["duration_ms"] >= 0

    for sentinel in (_QUERY, _HEADER, _BODY):
        assert not _lines_holding(lines, sentinel), sentinel


def test_the_root_logger_is_the_json_handler_at_log_level(probe):
    """OPS-LOG-8 as deployed, after uvicorn's own logging setup: at `LOG_LEVEL=INFO`
    an application logger's INFO line is written once, as JSON, and its DEBUG line is
    not; `uvicorn.access`, `uvicorn.error`, and `sqlalchemy.engine` each write their
    WARNING line once and no INFO line. At `LOG_LEVEL=WARNING` the application
    logger's INFO line and every access line are gone, and its WARNING line is still
    written once, as JSON."""
    _, lines = probe("INFO")
    info = _lines_holding(lines, "app.probe INFO-PROBE")
    assert len(info) == 1
    assert (_as_record(info[0]) or {}).get("level") == "INFO"
    assert (_as_record(info[0]) or {}).get("service") == "reservations"
    assert not _lines_holding(lines, "app.probe DEBUG-PROBE")
    for held in _HELD_AT_WARNING:
        assert not _lines_holding(lines, f"{held} INFO-PROBE"), held
        assert len(_lines_holding(lines, f"{held} WARNING-PROBE")) == 1, held

    _, lines = probe("WARNING")
    assert not _lines_holding(lines, "app.probe INFO-PROBE")
    assert not _access_records(lines)
    warning = _lines_holding(lines, "app.probe WARNING-PROBE")
    assert len(warning) == 1
    assert (_as_record(warning[0]) or {}).get("level") == "WARNING"


def test_an_origin_in_cors_origins_is_allowed_and_another_is_not(probe):
    """`CORS_ORIGINS` is the comma-separated list of origins the CORS middleware
    allows (`docs/ENV_VARS.md`; `docs/specs/CONVENTIONS.md`: every service but config
    calls `add_cors_middleware(app, settings.cors_origins)`). With the compose default
    `https://localhost` plus one more origin, a request and a preflight from the
    added origin get it back in `Access-Control-Allow-Origin`; a preflight from an
    origin not in the list is refused with none."""
    answers, _ = probe("INFO")
    get, listed_preflight, unlisted_preflight = answers[0], answers[1], answers[2]
    assert get == [200, _LISTED]
    assert listed_preflight == [200, _LISTED]
    assert unlisted_preflight == [400, None]
