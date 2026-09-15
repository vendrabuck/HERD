"""Opt-in scored evaluation harness for the AI topology generator.

This is a MEASUREMENT, not a gate: it does not assert a pass rate, only that
the harness ran and wrote a report. See docs/AI_GENERATE.md, "Measuring
proposal wireability", for why this exists (the resolver in
services/ai-orchestrator/app/services/generator.py assigns the first N
AVAILABLE devices per template without consulting the cabling graph, so a
proposal can come back with device pairs that are correct at the template
level but have no physical path between them; today that is only ever
discovered downstream, at reservation-create time).

Requires a running, seeded HERD stack (`make seed`) with an AI provider
configured, reached host-side the same way tests/integration/ reaches it.
Skips by default; opt in with HERD_AI_EVAL=1. See the env vars below and
docs/AI_GENERATE.md for the full list.

Reuse note: BASE_URL, SEED_EMAIL, SEED_PASSWORD, and the `_login` helper are
imported from tests.integration.conftest rather than re-typed here, following
the same cross-directory import already used by
tests/unit/test_e2e_seed_gate.py (`from tests.e2e.conftest import ...`) and
tests/unit/test_openapi_fetch_retry.py (`from tests.contract.test_openapi_schema
import _fetch_openapi`). Importing that module runs its module-level
`_load_repo_env()`, which only sets os.environ defaults from the repo-root
.env (existing environment variables win), the same side effect that happens
whenever tests/integration/ itself is collected; nothing else in that module
executes on import (its fixtures are plain functions until pytest binds
them, which does not happen here since this suite has its own flow and does
not depend on tests/integration/'s fixtures). Provider configuration is
checked against the live /api/ai/status response rather than a re-typed copy
of ai_is_configured()'s env-var resolution, so this suite cannot drift from
what the server actually reports.
"""

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.ai_eval.scoring import build_canvas_data, classify_run, summarize_runs
from tests.integration.conftest import BASE_URL, SEED_EMAIL, SEED_PASSWORD, _login

if os.environ.get("HERD_AI_EVAL") != "1":
    pytest.skip(
        "opt-in live measurement against a running, seeded stack with an AI "
        "provider configured; set HERD_AI_EVAL=1 to run (see "
        "docs/AI_GENERATE.md, 'Measuring proposal wireability')",
        allow_module_level=True,
    )

PROMPTS_PATH = Path(__file__).parent / "prompts.json"
N_RUNS = int(os.environ.get("HERD_AI_EVAL_N", "3"))
OUT_PATH = Path(os.environ.get("HERD_AI_EVAL_OUT", "ai-eval-results.json"))
# Every throwaway topology this suite creates carries this prefix so a
# leftover (a run interrupted before its finally block) is identifiable and
# safe to bulk-delete by name later.
TOPOLOGY_MARKER = "ai-eval-"
# A generate call is a real LLM round trip; matches the 120s client timeout
# tests/integration/test_ai_assistant_multi_turn.py uses for the same reason,
# with pytest's own per-test timeout raised to match (see the marker below).
REQUEST_TIMEOUT = 120.0

# A real run is many sequential LLM calls (prompts x HERD_AI_EVAL_N), each up
# to REQUEST_TIMEOUT itself; pytest carries no default per-test timeout here
# (unlike tests/integration/, this suite's Makefile target passes no
# --timeout), so this is a generous outer safety net, not a tight budget.
pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(1800)]


def _load_prompts() -> list[dict[str, Any]]:
    prompts = json.loads(PROMPTS_PATH.read_text())
    for entry in prompts:
        for key in ("id", "prompt", "min_devices", "min_edges"):
            if key not in entry:
                raise ValueError(f"prompts.json entry missing {key!r}: {entry}")
    return prompts


async def _run_one(
    client: httpx.AsyncClient, prompt: dict[str, Any], run_index: int
) -> dict[str, Any]:
    """Run one generate-to-validate cycle for one prompt; never raises.

    Returns a record with `prompt_id`, `run_index`, `http_status`,
    `latency_s`, `output_tokens` (always None: GenerateResponse carries no
    usage field, and /api/ai/usage is a per-user daily aggregate that cannot
    be attributed back to a single call, so this harness does not try), plus
    either the classify_run fields on success or `error`/failure defaults.
    """
    record: dict[str, Any] = {
        "prompt_id": prompt["id"],
        "run_index": run_index,
        "http_status": None,
        "latency_s": None,
        "output_tokens": None,
        "passed": False,
        "invalid_edge_reasons": {},
        "n_devices": 0,
        "n_edges": 0,
        "error": None,
    }

    started = time.monotonic()
    try:
        resp = await client.post("/ai/generate", data={"prompt": prompt["prompt"]})
    except httpx.HTTPError as exc:
        record["latency_s"] = time.monotonic() - started
        record["error"] = f"request failed: {exc}"
        return record
    record["latency_s"] = time.monotonic() - started
    record["http_status"] = resp.status_code

    if resp.status_code != 200:
        record["error"] = resp.text[:2000]
        return record

    body = resp.json()
    try:
        canvas = build_canvas_data(body)
    except ValueError as exc:
        record["error"] = f"unresolved proposal: {exc}"
        return record

    topology_id: str | None = None
    try:
        create = await client.post(
            "/cabling/topologies",
            json={"name": f"{TOPOLOGY_MARKER}{prompt['id']}-{run_index}-{uuid.uuid4().hex[:8]}"},
        )
        create.raise_for_status()
        topology_id = create.json()["id"]

        put = await client.put(
            f"/cabling/topologies/{topology_id}",
            json={"canvas_data": canvas},
        )
        put.raise_for_status()

        validate = await client.post(f"/cabling/topologies/{topology_id}/validate")
        validate.raise_for_status()

        record.update(
            classify_run(canvas, validate.json(), prompt["min_devices"], prompt["min_edges"])
        )
    except httpx.HTTPStatusError as exc:
        record["error"] = (
            f"cabling call failed: {exc.response.status_code} {exc.response.text[:500]}"
        )
    finally:
        if topology_id is not None:
            try:
                await client.delete(f"/cabling/topologies/{topology_id}")
            except httpx.HTTPError:
                pass

    return record


async def test_generate_topology_wireability():
    """Measure the generator's proposal-wireability pass rate; not a gate.

    Runs every prompt in prompts.json HERD_AI_EVAL_N times each against a live
    stack, scores every run, writes every record plus a summary to
    HERD_AI_EVAL_OUT, and prints the summary. Asserts only that at least one
    run completed (http_status == 200) and that the report file was written;
    the pass rate itself is not asserted, since this suite exists to measure
    it before and after a resolver fix, not to gate on a number that is
    expected to change.
    """
    prompts = _load_prompts()
    tokens = await _login(BASE_URL, SEED_EMAIL, SEED_PASSWORD)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    records: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        base_url=BASE_URL, verify=False, timeout=REQUEST_TIMEOUT, headers=headers
    ) as client:
        # /api/ai/status is unauthenticated by design (it drives conditional
        # UI); the bearer header above is harmless extra context for it.
        status_resp = await client.get("/ai/status")
        status_resp.raise_for_status()
        if not status_resp.json().get("enabled"):
            pytest.skip("/api/ai/status reports the AI provider is not enabled on this stack")

        for prompt in prompts:
            for run_index in range(N_RUNS):
                record = await _run_one(client, prompt, run_index)
                records.append(record)
                print(
                    f"[{prompt['id']}] run {run_index}: "
                    f"http={record['http_status']} passed={record['passed']} "
                    f"devices={record['n_devices']} edges={record['n_edges']} "
                    f"latency={record['latency_s']:.1f}s"
                    + (f" error={record['error']}" if record["error"] else "")
                )

    summary = summarize_runs(records)
    print("\n=== AI generate wireability summary ===")
    print(json.dumps(summary, indent=2, default=dict))

    report = {"records": records, "summary": summary}
    OUT_PATH.write_text(json.dumps(report, indent=2, default=dict))
    print(f"\nWrote {len(records)} run records and summary to {OUT_PATH}")

    assert summary["http_ok"] >= 1, "no generate call returned 200; nothing was measured"
    assert OUT_PATH.is_file(), f"report was not written to {OUT_PATH}"
