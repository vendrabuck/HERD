"""Tests for the shared UUID serialization aliases (issue #596).

Pins the behavior `_types.py`'s `UUIDStr`, `OptionalUUIDStr`, and `UUIDStrList`
must reproduce exactly, since they replace 27 hand-written `@field_serializer`
methods: a UUID serializes to its string form, `None` stays `None` for the
optional alias, a list of UUIDs serializes to a list of strings, and the
generated JSON schema keeps `format: uuid` in validation mode while omitting it
in serialization mode (the shape the old `@field_serializer` methods produced,
and what the published OpenAPI contract for these response models has always
been). Also pins that one real model per touched schema file still round-trips
through `model_validate` from both a `uuid.UUID` and a UUID string.
"""

import uuid

from app.schemas._types import OptionalUUIDStr, UUIDStr, UUIDStrList
from app.schemas.connection import ConnectionResponse
from app.schemas.fabric import FabricResponse
from app.schemas.fork import ActiveForkListResponse, ForkCreateResponse
from app.schemas.pathfind import PathHop
from app.schemas.template import TemplateResponse
from app.schemas.topology import TopologyResponse
from pydantic import BaseModel


class _Scalar(BaseModel):
    value: UUIDStr


class _Optional(BaseModel):
    value: OptionalUUIDStr = None


class _ListModel(BaseModel):
    value: UUIDStrList


# --- UUIDStr -----------------------------------------------------------------


def test_uuidstr_serializes_to_string_via_model_dump():
    u = uuid.uuid4()
    m = _Scalar(value=u)
    dumped = m.model_dump()
    assert dumped == {"value": str(u)}
    assert isinstance(dumped["value"], str)


def test_uuidstr_serializes_to_string_via_model_dump_json():
    u = uuid.uuid4()
    m = _Scalar(value=u)
    assert m.model_dump_json() == f'{{"value":"{u}"}}'


def test_uuidstr_schema_type_string_format_uuid_both_modes():
    ser = _Scalar.model_json_schema(mode="serialization")
    val = _Scalar.model_json_schema(mode="validation")
    assert ser["properties"]["value"]["type"] == "string"
    assert "format" not in ser["properties"]["value"]
    assert val["properties"]["value"] == {"title": "Value", "type": "string", "format": "uuid"}


# --- OptionalUUIDStr -----------------------------------------------------------


def test_optional_uuidstr_serializes_uuid_to_string():
    u = uuid.uuid4()
    m = _Optional(value=u)
    assert m.model_dump() == {"value": str(u)}


def test_optional_uuidstr_none_stays_none():
    m = _Optional(value=None)
    assert m.model_dump() == {"value": None}
    assert m.model_dump_json() == '{"value":null}'


def test_optional_uuidstr_schema_type_string_format_uuid_both_modes():
    ser = _Optional.model_json_schema(mode="serialization")
    val = _Optional.model_json_schema(mode="validation")
    ser_prop = ser["properties"]["value"]
    val_prop = val["properties"]["value"]
    assert ser_prop["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert val_prop["anyOf"] == [{"type": "string", "format": "uuid"}, {"type": "null"}]


# --- UUIDStrList -----------------------------------------------------------------


def test_uuidstrlist_serializes_to_list_of_strings():
    ids = [uuid.uuid4(), uuid.uuid4()]
    m = _ListModel(value=ids)
    dumped = m.model_dump()
    assert dumped == {"value": [str(u) for u in ids]}
    assert all(isinstance(v, str) for v in dumped["value"])


def test_uuidstrlist_empty_list():
    m = _ListModel(value=[])
    assert m.model_dump() == {"value": []}


def test_uuidstrlist_schema_items_format_uuid_both_modes():
    ser = _ListModel.model_json_schema(mode="serialization")
    val = _ListModel.model_json_schema(mode="validation")
    ser_items = ser["properties"]["value"]["items"]
    val_items = val["properties"]["value"]["items"]
    assert ser_items == {"type": "string"}
    assert val_items == {"type": "string", "format": "uuid"}


# --- Round-trip through model_validate on one real model per touched file ------


def test_connection_response_round_trips_from_uuid_and_string():
    u = uuid.uuid4()
    payload = dict(
        id=u,
        device_a_id=u,
        port_a="eth0",
        device_b_id=u,
        port_b="eth1",
        connection_type="ethernet",
        notes=None,
        created_by="alice",
        created_at="2026-01-01T00:00:00",
    )
    from_uuid = ConnectionResponse.model_validate(payload)
    assert from_uuid.id == u
    assert from_uuid.model_dump()["id"] == str(u)

    payload_str = dict(payload, id=str(u), device_a_id=str(u), device_b_id=str(u))
    from_str = ConnectionResponse.model_validate(payload_str)
    assert from_str.id == u
    assert from_str.model_dump()["id"] == str(u)


def test_fabric_response_round_trips_from_uuid_and_string():
    device_id = uuid.uuid4()
    fabric_id = uuid.uuid4()
    from_uuid = FabricResponse(device_id=device_id, fabric_id=fabric_id, component_size=3)
    assert from_uuid.model_dump() == {
        "device_id": str(device_id),
        "fabric_id": str(fabric_id),
        "component_size": 3,
    }

    from_str = FabricResponse.model_validate(
        {"device_id": str(device_id), "fabric_id": str(fabric_id), "component_size": 3}
    )
    assert from_str.device_id == device_id
    assert from_str.model_dump()["device_id"] == str(device_id)


def test_fork_create_response_round_trips_from_uuid_and_string():
    fork_id = uuid.uuid4()
    from_uuid = ForkCreateResponse(fork_id=fork_id, version_number=1)
    assert from_uuid.model_dump() == {"fork_id": str(fork_id), "version_number": 1}

    from_str = ForkCreateResponse.model_validate({"fork_id": str(fork_id), "version_number": 1})
    assert from_str.fork_id == fork_id
    assert from_str.model_dump()["fork_id"] == str(fork_id)


def test_active_fork_list_response_list_field_round_trips():
    ids = [uuid.uuid4(), uuid.uuid4()]
    m = ActiveForkListResponse(reservation_ids=ids, forks=[], total=0, skip=0, limit=50)
    assert m.model_dump()["reservation_ids"] == [str(u) for u in ids]

    from_str = ActiveForkListResponse.model_validate(
        {
            "reservation_ids": [str(u) for u in ids],
            "forks": [],
            "total": 0,
            "skip": 0,
            "limit": 50,
        }
    )
    assert from_str.reservation_ids == ids
    assert from_str.model_dump()["reservation_ids"] == [str(u) for u in ids]


def test_pathfind_pathhop_round_trips_from_uuid_and_string():
    device_id = uuid.uuid4()
    from_uuid = PathHop(device_id=device_id)
    assert from_uuid.model_dump()["device_id"] == str(device_id)

    from_str = PathHop.model_validate({"device_id": str(device_id)})
    assert from_str.device_id == device_id
    assert from_str.model_dump()["device_id"] == str(device_id)


def test_template_response_round_trips_from_uuid_and_string():
    tid = uuid.uuid4()
    created_by = uuid.uuid4()
    payload = dict(
        id=tid,
        name="tmpl",
        created_by=created_by,
        owner_name="alice",
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    )
    from_uuid = TemplateResponse.model_validate(payload)
    assert from_uuid.model_dump()["id"] == str(tid)

    payload_str = dict(payload, id=str(tid), created_by=str(created_by))
    from_str = TemplateResponse.model_validate(payload_str)
    assert from_str.id == tid
    assert from_str.model_dump()["id"] == str(tid)


def test_topology_response_round_trips_including_optional_uuid():
    tid = uuid.uuid4()
    created_by = uuid.uuid4()
    modified_by = uuid.uuid4()
    payload = dict(
        id=tid,
        name="topo",
        created_by=created_by,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        modified_by=modified_by,
    )
    from_uuid = TopologyResponse.model_validate(payload)
    dumped = from_uuid.model_dump()
    assert dumped["id"] == str(tid)
    assert dumped["modified_by"] == str(modified_by)

    payload_str = dict(
        payload, id=str(tid), created_by=str(created_by), modified_by=str(modified_by)
    )
    from_str = TopologyResponse.model_validate(payload_str)
    assert from_str.modified_by == modified_by

    # modified_by omitted defaults to None and serializes to None, not "None".
    payload_none = dict(payload)
    payload_none.pop("modified_by")
    from_none = TopologyResponse.model_validate(payload_none)
    assert from_none.model_dump()["modified_by"] is None
