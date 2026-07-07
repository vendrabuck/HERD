"""Live boundary-enforcement check for AI recipe authoring (issue #28 phase 2).

The stack default is AI_RECIPE_AUTHORING_ENABLED=false, so through the
gateway the drafting endpoints must refuse a real admin with the pinned 403
detail (the write-tools discipline: refused at dispatch, not merely
undocumented), and /ai/status must advertise recipe_authoring: false for
conditional UI. No LLM involvement.
"""

import pytest

pytestmark = pytest.mark.asyncio


async def test_recipe_draft_refused_when_flag_off(admin_client):
    resp = await admin_client.post(
        "/ai/recipes/draft",
        json={"prompt": "draft a proxmox recipe"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "AI recipe authoring is disabled"


async def test_status_advertises_recipe_authoring_flag(admin_client):
    resp = await admin_client.get("/ai/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["recipe_authoring"] is False
