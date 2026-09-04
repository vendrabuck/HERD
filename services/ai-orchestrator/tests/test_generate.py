import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from app import config as config_module
from app.database import Base, engine
from app.main import app
from app.routes.generate import _inventory_provider
from app.services import generator as generator_module
from app.services import usage_repo
from app.services.ai_client import (
    AI_NOT_CONFIGURED_DETAIL,
    AI_PROVIDER_UNREACHABLE_DETAIL,
    AIError,
    AIProviderUnavailableError,
    get_ai_client,
)
from app.services.inventory_client import InventorySummary
from app.services.llm_provider import Usage
from httpx import ASGITransport, AsyncClient
from jose import jwt
from sqlalchemy.ext.asyncio import async_sessionmaker

_USER_ID = str(uuid.uuid4())
_TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


def _user_token(role: str = "user") -> str:
    payload = {
        "sub": _USER_ID,
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
    monkeypatch.setattr(config_module.settings, "ai_api_key", "sk-ant-fake")
    yield
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
async def setup_db():
    # The generate route now opens a DB session (for the quota hooks); create
    # the ai_usage table so quota-enabled tests can read and write it. Quota is
    # disabled by default, so the hooks are no-ops unless a test sets it.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


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
            return response, Usage(input_tokens=10, output_tokens=20)

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
            return responses[idx], Usage(input_tokens=10, output_tokens=20)

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
    monkeypatch.setattr(config_module.settings, "ai_api_key", "")
    monkeypatch.setattr(config_module.settings, "ai_base_url", "")
    _override_inventory({"EX3400": 10})
    _override_resolver(monkeypatch)
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post("/generate", data={"prompt": "hi"}, headers=headers)
    assert resp.status_code == 503


async def test_generate_503_on_unknown_provider(async_client, monkeypatch):
    """AI_PROVIDER set to an unrecognized value (a typo like 'athropic') degrades
    to 503 at the get_ai_client dependency, not the 500 issue #245 reported. The
    AIClient dependency is deliberately NOT overridden so this exercises the real
    dependency-resolution path that runs BEFORE the route's ai_is_configured gate.
    """
    monkeypatch.setattr(config_module.settings, "ai_provider", "athropic")
    _override_inventory({"EX3400": 10})
    _override_resolver(monkeypatch)
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post("/generate", data={"prompt": "hi"}, headers=headers)
    assert resp.status_code == 503
    assert resp.json()["detail"] == AI_NOT_CONFIGURED_DETAIL


async def test_generate_503_on_construction_failure(async_client, monkeypatch):
    """A provider that blows up while being CONSTRUCTED (issue #280: an ai_ca_cert
    path that does not exist) degrades to 503 at the dependency boundary rather
    than a 500. Dependency NOT overridden, so the real construction path runs."""
    monkeypatch.setattr(config_module.settings, "ai_provider", "anthropic")
    monkeypatch.setattr(config_module.settings, "ai_base_url", "")
    monkeypatch.setattr(config_module.settings, "ai_ca_cert", "/nonexistent/ca-bundle.pem")
    _override_inventory({"EX3400": 10})
    _override_resolver(monkeypatch)
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post("/generate", data={"prompt": "hi"}, headers=headers)
    assert resp.status_code == 503
    assert resp.json()["detail"] == AI_NOT_CONFIGURED_DETAIL


async def test_generate_503_on_provider_unreachable(async_client, monkeypatch):
    """A configured provider whose endpoint is unreachable (the provider raised
    AIProviderUnavailableError) surfaces as 503, matching the issue #131
    upstream-unreachable standardization, not the 502 used for a live provider
    that returned an unusable response."""
    _override_inventory({"EX3400": 1})
    _override_resolver(monkeypatch)
    _override_ai(raises=AIProviderUnavailableError("endpoint unreachable"))
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post("/generate", data={"prompt": "x"}, headers=headers)
    assert resp.status_code == 503
    assert resp.json()["detail"] == AI_PROVIDER_UNREACHABLE_DETAIL


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


# --- Network elements (issue #632) --------------------------------------


async def test_generate_rejects_duplicate_role_across_device_and_element(async_client, monkeypatch):
    """D4: roles are unique across devices AND elements, not just devices."""
    _override_inventory({"EX3400": 10})
    _override_resolver(monkeypatch)
    _override_ai(
        {
            "purpose": "dup role across device+element",
            "devices": [{"role": "seg-a", "template_name": "EX3400"}],
            "edges": [],
            "elements": [{"role": "seg-a", "element_type": "vlan_segment", "label": "Seg"}],
        }
    )
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post("/generate", data={"prompt": "x"}, headers=headers)
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "duplicate role" in detail
    assert "seg-a" in detail


async def test_generate_rejects_element_to_element_edge(async_client, monkeypatch):
    """D4: an edge whose both ends are elements is rejected as element_to_element."""
    _override_inventory({"EX3400": 10})
    _override_resolver(monkeypatch)
    _override_ai(
        {
            "purpose": "element to element",
            "devices": [{"role": "a", "template_name": "EX3400"}],
            "edges": [{"source_role": "seg-a", "target_role": "seg-b", "layer": "L2"}],
            "elements": [
                {"role": "seg-a", "element_type": "vlan_segment", "label": "A"},
                {"role": "seg-b", "element_type": "subnet", "label": "B"},
            ],
        }
    )
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post("/generate", data={"prompt": "x"}, headers=headers)
    assert resp.status_code == 502
    assert "element_to_element" in resp.json()["detail"]


async def test_generate_allows_edge_from_device_role_to_element_role(async_client, monkeypatch):
    """An edge from a valid device role to a valid element role passes
    validation and both the element and the edge are returned untouched."""
    _override_inventory({"EX3400": 10})
    _override_resolver(monkeypatch)
    _override_ai(
        {
            "purpose": "device attaches to element",
            "devices": [{"role": "a", "template_name": "EX3400"}],
            "edges": [{"source_role": "a", "target_role": "seg-a", "layer": "L2"}],
            "elements": [
                {
                    "role": "seg-a",
                    "element_type": "vlan_segment",
                    "label": "Seg",
                    "attrs": {"vlan_id": 10},
                }
            ],
        }
    )
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post("/generate", data={"prompt": "x"}, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["elements"] == [
        {"role": "seg-a", "element_type": "vlan_segment", "label": "Seg", "attrs": {"vlan_id": 10}}
    ]
    assert body["edges"] == [{"source_role": "a", "target_role": "seg-a", "layer": "L2"}]


async def test_generate_device_only_proposal_defaults_elements_to_empty(async_client, monkeypatch):
    """A device-only proposal (no "elements" key at all) still validates: the
    field is additive and optional (D1)."""
    _override_inventory({"EX3400": 4})
    _override_resolver(monkeypatch)
    _override_ai(
        {
            "purpose": "device only",
            "devices": [{"role": "a", "template_name": "EX3400"}],
            "edges": [],
        }
    )
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post("/generate", data={"prompt": "x"}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["elements"] == []


async def test_generate_repair_loop_caps_at_max_repair_attempts_for_element_error(
    async_client, monkeypatch
):
    """The element_to_element rejection is repairable (fed back via
    _repair_feedback) and retried once (MAX_REPAIR_ATTEMPTS=1); a model that
    keeps making the same mistake exhausts retries after exactly
    MAX_REPAIR_ATTEMPTS + 1 total calls, not more."""
    calls: list[str] = []

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
            calls.append(repair_feedback)
            return (
                {
                    "purpose": "always bad",
                    "devices": [{"role": "a", "template_name": "EX3400"}],
                    "edges": [{"source_role": "seg-a", "target_role": "seg-b", "layer": "L2"}],
                    "elements": [
                        {"role": "seg-a", "element_type": "vlan_segment", "label": "A"},
                        {"role": "seg-b", "element_type": "subnet", "label": "B"},
                    ],
                },
                Usage(input_tokens=10, output_tokens=20),
            )

    app.dependency_overrides[get_ai_client] = lambda: StubAI()
    _override_inventory({"EX3400": 10})
    _override_resolver(monkeypatch)

    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post("/generate", data={"prompt": "x"}, headers=headers)

    assert resp.status_code == 502
    assert len(calls) == generator_module.MAX_REPAIR_ATTEMPTS + 1
    # The retry call carries the exact corrective wording the model can act
    # on: the element_to_element identifier plus the "never connect two
    # elements" guidance from _repair_feedback.
    assert calls[0] == ""
    assert "element_to_element" in calls[1]
    assert "never connect two elements directly" in calls[1]


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


async def test_generate_409_when_inventory_has_no_templates(async_client, monkeypatch):
    # No templates at all: guard must fire before the AI is ever called.
    _override_inventory({})
    _override_ai(raises=AssertionError("AI must not be called on empty inventory"))
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post("/generate", data={"prompt": "x"}, headers=headers)
    assert resp.status_code == 409
    assert "No device templates with available devices" in resp.json()["detail"]


async def test_generate_409_when_templates_have_no_available_devices(async_client, monkeypatch):
    # Templates exist but every available count is 0: still an impossible prompt.
    _override_inventory({"EX3400": 0, "Ubuntu Client": 0})
    _override_ai(raises=AssertionError("AI must not be called when nothing is available"))
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post("/generate", data={"prompt": "x"}, headers=headers)
    assert resp.status_code == 409
    assert "No device templates with available devices" in resp.json()["detail"]


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
            return (
                {
                    "purpose": "with-files",
                    "devices": [{"role": "a", "template_name": "EX3400"}],
                    "edges": [],
                },
                Usage(input_tokens=10, output_tokens=20),
            )

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


async def test_generate_over_quota_returns_429_without_calling_provider(async_client, monkeypatch):
    """At/over quota, the route returns 429 and never invokes the AI provider."""
    monkeypatch.setattr(config_module.settings, "ai_daily_token_quota", 100)
    async with _TestSessionLocal() as db:
        await usage_repo.add_tokens(db, uuid.UUID(_USER_ID), input_tokens=100, output_tokens=0)

    class ExplodingAI:
        async def propose_topology(self, **kwargs):
            raise AssertionError("provider must not be called when over quota")

    app.dependency_overrides[get_ai_client] = lambda: ExplodingAI()
    _override_inventory({"EX3400": 4})
    _override_resolver(monkeypatch)

    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post("/generate", data={"prompt": "anything"}, headers=headers)
    assert resp.status_code == 429
    detail = resp.json()["detail"]
    assert detail["limit"] == 100
    assert detail["used"] == 100
    assert detail["remaining"] == 0


async def test_generate_records_usage_when_quota_enabled(async_client, monkeypatch):
    """A successful generation books the provider's reported tokens against the quota."""
    monkeypatch.setattr(config_module.settings, "ai_daily_token_quota", 1000)
    _override_inventory({"EX3400": 4})
    _override_resolver(monkeypatch)
    _override_ai(
        {
            "purpose": "metered",
            "devices": [{"role": "a", "template_name": "EX3400"}],
            "edges": [],
        }
    )
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post("/generate", data={"prompt": "go"}, headers=headers)
    assert resp.status_code == 200, resp.text
    # _override_ai stubs Usage(input_tokens=10, output_tokens=20) -> 30 total.
    async with _TestSessionLocal() as db:
        assert await usage_repo.get_today_total(db, uuid.UUID(_USER_ID)) == 30
