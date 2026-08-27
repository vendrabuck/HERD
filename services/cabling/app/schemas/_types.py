"""Shared UUID-to-string serialization aliases for cabling's response schemas.

Cabling's response models declare UUID fields as `uuid.UUID` (so validation still
enforces UUID shape and inbound values may arrive as either a UUID object or a
string), but every response serializes them to plain strings, historically via 27
hand-written `@field_serializer` methods (issue #596). These three `Annotated`
aliases collapse that pattern to one definition per shape: scalar, optional
scalar, and list.

`PlainSerializer` only affects serialization-mode JSON schema and actual
`model_dump`/`model_dump_json` output; validation-mode schema and behavior are
untouched, since the underlying type annotation is still `uuid.UUID` (or
`uuid.UUID | None`, or `list[uuid.UUID]`). This matches exactly what the
`@field_serializer` methods it replaces produced: a bare `{"type": "string"}` in
the serialization-mode (i.e. response/OpenAPI) schema, with `format: uuid`
surviving only in validation mode. Do not add `WithJsonSchema` to force
`format: uuid` into the serialization-mode schema; that would change the
published OpenAPI contract from what it is today.
"""

import uuid
from typing import Annotated

from pydantic import PlainSerializer

UUIDStr = Annotated[uuid.UUID, PlainSerializer(str, return_type=str)]


def _serialize_optional_uuid(value: uuid.UUID | None) -> str | None:
    return str(value) if value is not None else None


OptionalUUIDStr = Annotated[
    uuid.UUID | None,
    PlainSerializer(_serialize_optional_uuid, return_type=str | None),
]


def _serialize_uuid_list(value: list[uuid.UUID]) -> list[str]:
    return [str(v) for v in value]


UUIDStrList = Annotated[
    list[uuid.UUID],
    PlainSerializer(_serialize_uuid_list, return_type=list[str]),
]
