"""The editor opens a seeded topology whose node has no device (issue #989).

`seedtools/topologies.py` builds four deliberately invalid topologies whose
canvas holds a node typed `deviceNode` with empty data (no `data.device`).
Opening "BROKEN - Half-Wired Chain" used to land on the ErrorBoundary with
"Cannot use 'in' operator to search for 'id' in undefined", thrown by
`persistableDevice` on the device-less node. Each of the four must now open:
no ErrorBoundary, the device-less node drawn as "Unknown device", and the
validator (read back over the API) reporting `missing_device` for its edges.

The topologies exist only after `make seed`, so the test skips on an unseeded
stack (the first e2e phase of the gate) and runs in the seeded pass. Nothing is
mutated: the editor is opened read-only in effect (no save is clicked).
"""

import pytest
from playwright.sync_api import expect

from .conftest import HOST_BASE_URL, pw_api, pw_login

DEVICE_LESS_TOPOLOGIES = [
    ("BROKEN - Missing Device Ref", 1),
    ("BROKEN - Empty Node", 1),
    ("BROKEN - Two Empty Nodes", 2),
    ("BROKEN - Half-Wired Chain", 1),
]


def _topology_by_name(page, name: str) -> dict | None:
    resp = pw_api(page, "GET", "/cabling/topologies", params={"search": name, "limit": 50})
    for item in resp.json().get("items", []):
        if item.get("name") == name:
            return item
    return None


@pytest.mark.parametrize("name,device_less", DEVICE_LESS_TOPOLOGIES)
def test_editor_opens_topology_with_device_less_node(pw_page, name, device_less):
    pw_login(pw_page)
    topology = _topology_by_name(pw_page, name)
    if topology is None:
        pytest.skip(f"seeded topology {name!r} not present (unseeded stack)")

    errors: list[str] = []
    pw_page.on("pageerror", lambda exc: errors.append(str(exc)))
    pw_page.goto(f"{HOST_BASE_URL}/topology/{topology['id']}")

    expect(pw_page.get_by_text(name, exact=True).first).to_be_visible()
    expect(pw_page.get_by_text("Unknown device", exact=True)).to_have_count(device_less)
    expect(pw_page.get_by_text("Something went wrong", exact=True)).to_have_count(0)
    assert not [e for e in errors if "in' operator" in e], errors

    # The validator still reports the device-less node's edges.
    result = pw_api(pw_page, "POST", f"/cabling/topologies/{topology['id']}/validate").json()
    assert result["valid"] is False
    reasons = {edge.get("reason") for edge in result.get("invalid_edges", [])}
    assert "missing_device" in reasons, result
