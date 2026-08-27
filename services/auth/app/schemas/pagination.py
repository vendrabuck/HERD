from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class Paginated(BaseModel, Generic[T]):
    """Generic skip/limit page shape shared by auth's four paginated responses.

    auth-local by decision (issue #511): four structurally identical shells
    (PaginatedUserResponse, PaginatedGroupResponse, PaginatedMappingResponse,
    PaginatedSyncRunResponse) carried the same items/total/skip/limit fields
    with no shared base. Promotion to herd_common waits for a second service
    to want this shape; until then it stays here rather than presuming a
    cross-service contract that does not exist yet.

    Never used directly as a response_model: each of the four concrete
    responses SUBCLASSES Paginated[SomeItem] instead (optionally adding its
    own extra field), because subclassing is what keeps the generated OpenAPI
    component name as the concrete class name (PaginatedUserResponse, and so
    on) and the field order identical to the pre-generic shape, so the
    checked-in contract snapshot does not change.
    """

    items: list[T]
    total: int
    skip: int
    limit: int
