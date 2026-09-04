"""Unit tests for the network-element additions to app.schemas.generate
(issue #632): ProposedElement/CommitElement, and the additive `elements`
fields on GenerateResponse/CommitRequest. Plain pydantic validation, no
route or DB involved.
"""

import pytest
from app.schemas.generate import (
    CommitDevice,
    CommitElement,
    CommitRequest,
    GenerateResponse,
    ProposedDevice,
    ProposedElement,
)
from pydantic import ValidationError


def test_generate_response_elements_defaults_to_empty():
    resp = GenerateResponse(
        purpose="p",
        devices=[ProposedDevice(role="a", template_name="EX3400")],
        edges=[],
    )
    assert resp.elements == []


def test_commit_request_elements_defaults_to_empty():
    from datetime import datetime, timedelta, timezone

    start = datetime.now(timezone.utc)
    req = CommitRequest(
        topology_name="t",
        start_time=start,
        end_time=start + timedelta(hours=1),
        devices=[CommitDevice(role="a", device_id="00000000-0000-0000-0000-000000000001")],
    )
    assert req.elements == []


def test_proposed_element_unknown_element_type_is_a_validation_error():
    with pytest.raises(ValidationError) as exc:
        ProposedElement(role="seg-a", element_type="not_a_real_type", label="Seg")
    assert "element_type" in str(exc.value)


def test_commit_element_unknown_element_type_is_a_validation_error():
    with pytest.raises(ValidationError) as exc:
        CommitElement(role="seg-a", element_type="not_a_real_type", label="Seg")
    assert "element_type" in str(exc.value)


def test_proposed_element_accepts_every_v1_element_type():
    for element_type in ("vlan_segment", "subnet", "external_cloud", "patch_trunk"):
        element = ProposedElement(role="seg-a", element_type=element_type, label="Seg")
        assert element.element_type == element_type


def test_proposed_element_attrs_defaults_to_empty_dict():
    element = ProposedElement(role="seg-a", element_type="vlan_segment", label="Seg")
    assert element.attrs == {}


def test_generate_response_with_devices_and_no_elements_still_validates():
    """A device-only response (elements key entirely absent from the payload,
    as any pre-#632 caller would send) still validates unchanged."""
    resp = GenerateResponse.model_validate(
        {
            "purpose": "device only",
            "devices": [{"role": "a", "template_name": "EX3400"}],
            "edges": [],
        }
    )
    assert resp.elements == []


def test_commit_request_with_devices_and_no_elements_still_validates():
    from datetime import datetime, timedelta, timezone

    start = datetime.now(timezone.utc)
    req = CommitRequest.model_validate(
        {
            "topology_name": "t",
            "start_time": start.isoformat(),
            "end_time": (start + timedelta(hours=1)).isoformat(),
            "devices": [{"role": "a", "device_id": "00000000-0000-0000-0000-000000000001"}],
        }
    )
    assert req.elements == []
