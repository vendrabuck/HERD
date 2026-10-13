"""Static pin: CI runs the frontend unit suite under a non-UTC time zone too.

The GitHub runner's clock is UTC, so a frontend date bug that only shows in a local
zone or across a daylight saving change (the `getWeekRange` case the 2026-10-10 test
sweep fixed in `dateUtils.test.ts`, #1142) stays green when vitest runs only there.
ci.yml's `frontend` job therefore runs the same vitest command a second time with
`TZ` set in the step's `env:` to a zone that observes daylight saving time.

What it reads:

- `.github/workflows/ci.yml`, parsed with PyYAML: the workflow `env`, the `frontend`
  job's `env`, and each of its steps' `name`, `env`, `run`, `if`, and
  `continue-on-error`.
- `frontend/package.json`'s `scripts.test`, so `npm test` is known to mean a single
  `vitest run` rather than watch mode.

What it requires:

- `scripts.test` starts with `vitest run`.
- Neither the workflow nor the job sets `TZ`, so a step without its own `TZ` runs
  under the runner's UTC clock.
- At least one baseline step runs vitest with no `TZ` of its own (the UTC run).
- At least one step sets `TZ` in its `env:` to a zone that is not empty, not a UTC or
  GMT alias, known to `zoneinfo`, and whose UTC offset differs between January and
  July (it observes daylight saving time); its `run` equals a baseline step's `run`
  exactly after trimming; it has no `if:` and is not `continue-on-error`.

A step "runs vitest" when its `run` text calls `npm test`, `npm run test` (not
`test:watch` or another script with that prefix), or `vitest run`.

Limits:

- A `TZ` set in a shell prefix (`TZ=... npm test`), exported earlier in the `run`
  text, set through `$GITHUB_ENV` by an earlier step, set at the job or workflow level
  for the non-UTC run, or set inside a composite or reusable action is not seen; the
  guard requires the step-level `env:` key.
- Whether the step actually runs is not judged beyond the absence of `if:` and
  `continue-on-error`; a job-level `if:`, a job-level `continue-on-error`, or a
  ruleset that stops requiring the `frontend` check is not seen.
- The `run` comparison is textual: two commands that do the same thing spelled
  differently are refused, and the guard does not check that vitest's own config
  leaves `TZ` alone (`frontend/vite.config.ts` sets no `TZ` today).
- The DST check uses the host's `zoneinfo` database, not the runner's.
"""

import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PACKAGE_JSON = REPO_ROOT / "frontend" / "package.json"

UTC_ALIASES = {
    "utc",
    "etc/utc",
    "etc/universal",
    "universal",
    "zulu",
    "etc/zulu",
    "z",
    "gmt",
    "etc/gmt",
    "etc/gmt0",
    "etc/gmt+0",
    "etc/gmt-0",
    "gmt0",
    "gmt+0",
    "gmt-0",
    "greenwich",
    "etc/greenwich",
    "utc0",
    "uct",
    "etc/uct",
}

_RUNS_VITEST = re.compile(r"\bnpm\s+(?:run\s+)?test(?![\w:.-])|\bvitest\s+run\b")


def _workflow() -> dict:
    return yaml.safe_load(CI.read_text())


def _frontend_job(data: dict) -> dict:
    jobs = data.get("jobs") or {}
    assert "frontend" in jobs, "ci.yml has no `frontend` job"
    return jobs["frontend"]


def _runs_vitest(step: dict) -> bool:
    return bool(_RUNS_VITEST.search(str(step.get("run", ""))))


def _step_tz(step: dict):
    env = step.get("env") or {}
    return env.get("TZ")


def _observes_dst(zone: str) -> bool:
    tz = ZoneInfo(zone)
    jan = datetime(2026, 1, 15, 12, tzinfo=tz).utcoffset()
    jul = datetime(2026, 7, 15, 12, tzinfo=tz).utcoffset()
    return jan != jul


def test_npm_test_runs_vitest_once():
    scripts = json.loads(PACKAGE_JSON.read_text()).get("scripts") or {}
    assert str(scripts.get("test", "")).startswith("vitest run"), scripts.get("test")


def test_frontend_job_inherits_no_tz():
    data = _workflow()
    job = _frontend_job(data)
    assert "TZ" not in (data.get("env") or {}), "ci.yml sets TZ at the workflow level"
    assert "TZ" not in (job.get("env") or {}), "the frontend job sets TZ at the job level"


def test_frontend_job_runs_vitest_under_utc_and_a_dst_zone():
    steps = _frontend_job(_workflow()).get("steps") or []
    vitest_steps = [s for s in steps if _runs_vitest(s)]
    baseline_runs = {str(s["run"]).strip() for s in vitest_steps if _step_tz(s) is None}
    assert baseline_runs, "the frontend job has no vitest step without TZ (the UTC run)"

    tz_steps = [s for s in steps if _step_tz(s) is not None]
    assert tz_steps, "the frontend job has no step that sets TZ in its env"

    problems = []
    for step in tz_steps:
        name = step.get("name", "<unnamed>")
        zone = str(_step_tz(step)).strip()
        if not zone:
            problems.append(f"{name}: TZ is empty")
            continue
        if zone.lower() in UTC_ALIASES:
            problems.append(f"{name}: TZ {zone!r} is UTC")
            continue
        try:
            dst = _observes_dst(zone)
        except (ZoneInfoNotFoundError, ValueError):
            problems.append(f"{name}: TZ {zone!r} is not a known zone")
            continue
        if not dst:
            problems.append(f"{name}: TZ {zone!r} does not observe daylight saving time")
            continue
        run = str(step.get("run", "")).strip()
        if run not in baseline_runs:
            problems.append(
                f"{name}: run {run!r} is not the UTC step's vitest command "
                f"{sorted(baseline_runs)!r}"
            )
            continue
        if "if" in step or step.get("continue-on-error"):
            problems.append(f"{name}: carries `if:` or `continue-on-error`")
            continue
        return
    raise AssertionError(
        "no frontend step runs the UTC vitest command under a DST zone: " + "; ".join(problems)
    )
