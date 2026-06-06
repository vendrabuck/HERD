from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from app import config as config_module
from app.main import app
from app.routes.generate import _inventory_provider
from app.services import generator as generator_module
from app.services.ai_client import AIError, get_ai_client
from app.services.inventory_client import InventorySummary
from httpx import ASGITransport, AsyncClient
from jose import jwt


def _user_token(role: str = "user") -> str:
    payload = {
        "sub": "test-user",
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    return jwt.encode(
        payload,
        config_module.settings.secret_key,
        algorithm=config_module.settings.algorithm,
    )


@pytest.fixture
def async_client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture(autouse=True)
def set_api_key(monkeypatch):
    monkeypatch.setattr(config_module.settings, "anthropic_api_key", "sk-ant-fake")
    yield
    app.dependency_overrides.clear()


def _override_inventory(counts: dict[str, int]) -> None:
    ids = {name: f"tpl-{name}" for name in counts}

    async def _stub() -> InventorySummary:
        return InventorySummary(counts, ids)

    app.dependency_overrides[_inventory_provider] = _stub


def _override_ai(response: dict[str, Any] | None = None, *, raises: Exception | None = None):
    class StubAI:
        async def propose_topology(
            self,
            *,
            inventory_block: str,
            user_prompt: str,
            file_context: str = "",
            template_names: list[str] | None = None,
            repair_feedback: str = "",
        ):
            # Same canned response on every call (including any repair retry),
            # so a repairable rejection still surfaces after retries exhaust.
            if raises is not None:
                raise raises
            return response

    app.dependency_overrides[get_ai_client] = lambda: StubAI()


def _override_ai_sequence(responses: list[dict[str, Any]]):
    """Return a different canned proposal per call; the last repeats if exceeded.

    Lets a test simulate a bad first proposal that the model repairs on retry.
    """

    class SequenceAI:
        def __init__(self) -> None:
            self._calls = 0

        async def propose_topology(
            self,
            *,
            inventory_block: str,
            user_prompt: str,
            file_context: str = "",
            template_names: list[str] | None = None,
            repair_feedback: str = "",
        ):
            idx = min(self._calls, len(responses) - 1)
            self._calls += 1
            return responses[idx]

    app.dependency_overrides[get_ai_client] = lambda: SequenceAI()


def _override_resolver(monkeypatch, *, shortfall_template: str | None = None):
    """Stub fetch_available_devices to return fake Device dicts.

    If `shortfall_template` is set, that template returns 0 devices to simulate
    inventory shifting between the summary call and the resolver.
    """

    async def _fake(token: str, template_id: str, count: int):
        template_name = template_id.removeprefix("tpl-")
        if shortfall_template and template_name == shortfall_template:
            return []
        return [
            {
                "id": f"dev-{template_name}-{i}",
                "name": f"{template_name}-{i}",
                "template_id": template_id,
                "template_name": template_name,
                "topology_type": "PHYSICAL",
                "status": "AVAILABLE",
            }
            for i in range(count)
        ]

    monkeypatch.setattr(generator_module, "fetch_available_devices", _fake)


async def test_health(async_client):
    async with async_client as client:
        resp = await client.get("/health")
    assert resp.status_code == 200


async def test_generate_requires_auth(async_client, monkeypatch):
    async with async_client as client:
        resp = await client.post("/generate", data={"prompt": "hi"})
    assert resp.status_code == 401


async def test_generate_503_when_key_blank(async_client, monkeypatch):
    monkeypatch.setattr(config_module.settings, "anthropic_api_key", "")
    _override_inventory({"EX3400": 10})
    _override_resolver(monkeypatch)
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post("/generate", data={"prompt": "hi"}, headers=headers)
    assert resp.status_code == 503


async def test_generate_returns_proposal_when_ai_succeeds(async_client, monkeypatch):
    _override_inventory({"EX3400": 4, "Ubuntu Client": 2})
    _override_resolver(monkeypatch)
    _override_ai(
        {
            "purpose": "HA pair with a client",
            "devices": [
                {"role": "fw-a", "template_name": "EX3400"},
                {"role": "fw-b", "template_name": "EX3400"},
                {"role": "client", "template_name": "Ubuntu Client"},
            ],
            "edges": [
                {"source_role": "fw-a", "target_role": "fw-b", "layer": "L2"},
                {"source_role": "client", "target_role": "fw-a", "layer": "L2"},
            ],
            "notes": "",
        }
    )
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(
            "/generate",
            data={"prompt": "Build an HA pair with a client"},
            headers=headers,
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {d["role"] for d in body["devices"]} == {"fw-a", "fw-b", "client"}
    assert len(body["edges"]) == 2
    # Every proposed device must have a resolved device payload
    for d in body["devices"]:
        assert d["device"] is not None
        assert d["device"]["id"].startswith("dev-")
    # Two EX3400 roles must get two DISTINCT device ids
    pa_ids = {d["device"]["id"] for d in body["devices"] if d["template_name"] == "EX3400"}
    assert len(pa_ids) == 2


async def test_generate_rejects_unknown_template(async_client, monkeypatch):
    _override_inventory({"EX3400": 4})
    _override_resolver(monkeypatch)
    _override_ai(
        {
            "purpose": "bad",
            "devices": [{"role": "a", "template_name": "Cisco-9000"}],
            "edges": [],
        }
    )
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post("/generate", data={"prompt": "x"}, headers=headers)
    assert resp.status_code == 502
    assert "Cisco-9000" in resp.json()["detail"]


async def test_generate_repairs_unknown_template_on_retry(async_client, monkeypatch):
    """A bad first proposal is repaired on the retry and succeeds with 200."""
    _override_inventory({"EX3400": 4})
    _override_resolver(monkeypatch)
    _override_ai_sequence(
        [
            # Attempt 1: hallucinated template -> repairable rejection.
            {
                "purpose": "first try",
                "devices": [{"role": "a", "template_name": "Cisco-9000"}],
                "edges": [],
            },
            # Attempt 2 (repair): valid template -> succeeds.
            {
                "purpose": "repaired",
                "devices": [{"role": "a", "template_name": "EX3400"}],
                "edges": [],
            },
        ]
    )
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post("/generate", data={"prompt": "x"}, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [d["template_name"] for d in body["devices"]] == ["EX3400"]
    assert body["devices"][0]["device"] is not None


async def test_generate_rejects_overcommit(async_client, monkeypatch):
    _override_inventory({"EX3400": 1})
    _override_resolver(monkeypatch)
    _override_ai(
        {
            "purpose": "too many",
            "devices": [
                {"role": "a", "template_name": "EX3400"},
                {"role": "b", "template_name": "EX3400"},
            ],
            "edges": [],
        }
    )
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post("/generate", data={"prompt": "x"}, headers=headers)
    assert resp.status_code == 502
    assert "more devices than are available" in resp.json()["detail"]


async def test_generate_rejects_duplicate_roles(async_client, monkeypatch):
    _override_inventory({"EX3400": 10})
    _override_resolver(monkeypatch)
    _override_ai(
        {
            "purpose": "dup roles",
            "devices": [
                {"role": "fw", "template_name": "EX3400"},
                {"role": "fw", "template_name": "EX3400"},
            ],
            "edges": [],
        }
    )
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post("/generate", data={"prompt": "x"}, headers=headers)
    assert resp.status_code == 502
    assert "duplicate role" in resp.json()["detail"]


async def test_generate_rejects_edge_with_unknown_role(async_client, monkeypatch):
    _override_inventory({"EX3400": 10})
    _override_resolver(monkeypatch)
    _override_ai(
        {
            "purpose": "bad edge",
            "devices": [{"role": "a", "template_name": "EX3400"}],
            "edges": [{"source_role": "a", "target_role": "ghost", "layer": "L2"}],
        }
    )
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post("/generate", data={"prompt": "x"}, headers=headers)
    assert resp.status_code == 502
    assert "unknown role" in resp.json()["detail"]


async def test_generate_surfaces_ai_error(async_client, monkeypatch):
    _override_inventory({"EX3400": 1})
    _override_resolver(monkeypatch)
    _override_ai(raises=AIError("timed out"))
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post("/generate", data={"prompt": "x"}, headers=headers)
    assert resp.status_code == 502


async def test_generate_rejects_empty_prompt(async_client, monkeypatch):
    _override_inventory({"EX3400": 1})
    _override_resolver(monkeypatch)
    _override_ai({"purpose": "", "devices": [], "edges": []})
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post("/generate", data={"prompt": ""}, headers=headers)
    assert resp.status_code == 422


async def test_generate_returns_409_when_inventory_shifts(async_client, monkeypatch):
    # Summary says 2 available, but the resolver returns 0 to simulate a race
    _override_inventory({"EX3400": 2})
    _override_resolver(monkeypatch, shortfall_template="EX3400")
    _override_ai(
        {
            "purpose": "race",
            "devices": [
                {"role": "a", "template_name": "EX3400"},
                {"role": "b", "template_name": "EX3400"},
            ],
            "edges": [],
        }
    )
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post("/generate", data={"prompt": "x"}, headers=headers)
    assert resp.status_code == 409
    assert "Inventory shifted" in resp.json()["detail"]


async def test_generate_forwards_file_context_to_ai(async_client, monkeypatch):
    """Uploaded text must appear in the AI prompt and the response summary."""
    captured: dict[str, str] = {}

    class StubAI:
        async def propose_topology(
            self,
            *,
            inventory_block: str,
            user_prompt: str,
            file_context: str = "",
            template_names: list[str] | None = None,
            repair_feedback: str = "",
        ):
            captured["inventory_block"] = inventory_block
            captured["user_prompt"] = user_prompt
            captured["file_context"] = file_context
            return {
                "purpose": "with-files",
                "devices": [{"role": "a", "template_name": "EX3400"}],
                "edges": [],
            }

    app.dependency_overrides[get_ai_client] = lambda: StubAI()
    _override_inventory({"EX3400": 4})
    _override_resolver(monkeypatch)

    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(
            "/generate",
            data={"prompt": "Use the attached config"},
            files=[("files", ("case.txt", b"support case body", "text/plain"))],
            headers=headers,
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert captured["file_context"]
    assert "support case body" in captured["file_context"]
    assert "case.txt" in captured["file_context"]
    assert body["file_summaries"] == [
        {"filename": "case.txt", "chars": len("support case body"), "truncated": False}
    ]


async def test_generate_rejects_unsupported_file_extension(async_client, monkeypatch):
    _override_inventory({"EX3400": 4})
    _override_resolver(monkeypatch)
    _override_ai({"purpose": "unused", "devices": [], "edges": []})

    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(
            "/generate",
            data={"prompt": "x"},
            files=[("files", ("bad.exe", b"mz", "application/octet-stream"))],
            headers=headers,
        )
    assert resp.status_code == 400
    assert "Unsupported" in resp.json()["detail"]


async def test_generate_rejects_oversized_upload(async_client, monkeypatch):
    monkeypatch.setattr(config_module.settings, "upload_max_file_bytes", 10)
    _override_inventory({"EX3400": 4})
    _override_resolver(monkeypatch)
    _override_ai({"purpose": "unused", "devices": [], "edges": []})

    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(
            "/generate",
            data={"prompt": "x"},
            files=[("files", ("big.txt", b"x" * 100, "text/plain"))],
            headers=headers,
        )
    assert resp.status_code == 400
    assert "exceeds limit" in resp.json()["detail"]


async def test_generate_works_without_files(async_client, monkeypatch):
    """Empty files list must not change existing behavior."""
    _override_inventory({"EX3400": 4})
    _override_resolver(monkeypatch)
    _override_ai(
        {
            "purpose": "no-files",
            "devices": [{"role": "a", "template_name": "EX3400"}],
            "edges": [],
        }
    )
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(
            "/generate",
            data={"prompt": "just text"},
            headers=headers,
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["file_summaries"] == []
