"""Fetches an inventory summary the AI can reason about.

Uses the caller's JWT to respect device visibility; non-admin users only get
templates whose DUTs they can actually reserve. This matches the reservation
create path so the AI cannot propose something the user is not allowed to use.
"""

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

TEMPLATES_PAGE_SIZE = 500
DEVICES_PAGE_SIZE = 1


class InventorySummary:
    """Template name -> available count, plus name -> id and name -> (vendor, model) maps."""

    def __init__(
        self,
        template_counts: dict[str, int],
        template_ids: dict[str, str] | None = None,
        template_identity: dict[str, tuple[str, str]] | None = None,
    ):
        self.template_counts = template_counts
        self.template_ids = template_ids or {}
        self.template_identity = template_identity or {}

    @property
    def template_names(self) -> set[str]:
        return set(self.template_counts.keys())

    def to_prompt_block(self) -> str:
        """Render as a compact bulleted list for the system prompt."""
        if not self.template_counts:
            return "(no templates available)"
        lines: list[str] = []
        for name, count in sorted(self.template_counts.items()):
            identity = self.template_identity.get(name)
            if identity and identity[0] != "unknown" and identity[1] != "unknown":
                vendor, model = identity
                lines.append(f"- {name} ({vendor} {model}): {count} available")
            else:
                lines.append(f"- {name}: {count} available")
        return "\n".join(lines)


async def fetch_inventory_summary(user_bearer_token: str) -> InventorySummary:
    """Fetch device templates and AVAILABLE DUT counts using the caller's JWT.

    Makes one /templates call and then one small /devices call per template,
    which is acceptable for the typical <100 templates. A dedicated aggregate
    endpoint can replace this later if the count becomes a bottleneck.
    """
    headers = {"Authorization": f"Bearer {user_bearer_token}"}
    base = settings.inventory_service_url.rstrip("/")
    template_counts: dict[str, int] = {}
    template_ids: dict[str, str] = {}
    template_identity: dict[str, tuple[str, str]] = {}

    async with httpx.AsyncClient(timeout=15.0) as client:
        t_resp = await client.get(
            f"{base}/templates",
            params={"template_type": "device", "limit": TEMPLATES_PAGE_SIZE},
            headers=headers,
        )
        t_resp.raise_for_status()
        templates = t_resp.json().get("items", [])

        for tpl in templates:
            tpl_id = tpl["id"]
            tpl_name = tpl["name"]
            d_resp = await client.get(
                f"{base}/devices",
                params={
                    "template_id": tpl_id,
                    "status": "AVAILABLE",
                    "dut_only": "true",
                    "limit": DEVICES_PAGE_SIZE,
                },
                headers=headers,
            )
            d_resp.raise_for_status()
            total = int(d_resp.json().get("total", 0))
            template_counts[tpl_name] = total
            template_ids[tpl_name] = tpl_id
            template_identity[tpl_name] = (
                tpl.get("vendor") or "unknown",
                tpl.get("model") or "unknown",
            )

    return InventorySummary(template_counts, template_ids, template_identity)


async def fetch_available_devices(
    user_bearer_token: str, template_id: str, count: int
) -> list[dict[str, Any]]:
    """Fetch up to `count` AVAILABLE DUT devices for the given template.

    Uses the caller's JWT so visibility rules apply. The returned dicts are
    the raw `DeviceResponse` payloads from the inventory service, suitable
    for rendering directly as React Flow nodes on the frontend.
    """
    if count <= 0:
        return []

    headers = {"Authorization": f"Bearer {user_bearer_token}"}
    base = settings.inventory_service_url.rstrip("/")

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{base}/devices",
            params={
                "template_id": template_id,
                "status": "AVAILABLE",
                "dut_only": "true",
                "limit": count,
            },
            headers=headers,
        )
        resp.raise_for_status()
        return list(resp.json().get("items", []))
