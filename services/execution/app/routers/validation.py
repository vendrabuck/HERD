"""Internal package-validation endpoint (ADR 0005, issue #28, phase 1).

Validates an unapproved recipe package without loading it as a driver: no
DriverCache row, no cache-path extraction, nothing survives the request.
The caller (ai-orchestrator's drafting loop in phase 2, or any future
validate-on-upload hook) gets a structured per-section report back.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.routers.executions import _require_internal_token
from app.services.package_validator import (
    SUPPORTED_CONNECTION_TYPES,
    PackageDecodeError,
    validate_package,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["validation"])


class ValidatePackageRequest(BaseModel):
    package_b64: str
    filename: str = Field(
        default="package.zip",
        description="Archive filename; the suffix selects zip or tar.gz extraction",
    )
    connection_type: str = "Hypervisor"


class SectionReport(BaseModel):
    passed: bool
    errors: list[str] = []


class SchemaReport(BaseModel):
    present: bool
    json_schema: dict | None = Field(default=None, alias="schema")
    error: str | None = None

    model_config = {"populate_by_name": True}


class DryRunMethodReport(BaseModel):
    action: str
    passed: bool
    success: bool
    output: dict | None = None
    error: str | None = None
    duration_ms: int | None = None
    transcript: list[dict] = []


class DryRunReport(BaseModel):
    passed: bool
    methods: list[DryRunMethodReport] = []
    error: str | None = None


class ValidationReport(BaseModel):
    valid: bool
    structural: SectionReport
    policy: SectionReport
    schema_report: SchemaReport = Field(alias="schema")
    dry_run: DryRunReport

    model_config = {"populate_by_name": True}


@router.post(
    "/internal/validate-package",
    response_model=ValidationReport,
    response_model_by_alias=True,
)
async def validate_package_endpoint(
    body: ValidatePackageRequest,
    _: None = Depends(_require_internal_token),
):
    if body.connection_type not in SUPPORTED_CONNECTION_TYPES:
        raise HTTPException(
            status_code=422,
            detail="Only the Hypervisor connection type is supported for package validation",
        )
    try:
        # The pipeline is blocking (archive extraction plus up to six sandbox
        # subprocesses); run it off the event loop.
        report = await asyncio.to_thread(
            validate_package, body.package_b64, body.filename, body.connection_type
        )
    except PackageDecodeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return report
