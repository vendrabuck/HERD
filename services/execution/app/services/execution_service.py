import json
import logging
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import HTTPException
from herd_common.device_config import (
    ConfigValidationError,
    PublishedSchemaError,
    validate_device_config,
    validate_device_config_with_schema,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.execution_command import ExecutionCommand
from app.models.execution_run import ExecutionRun
from app.services.driver_loader import (
    get_driver_config_schema,
    get_driver_metadata,
    load_driver,
)
from app.services.driver_sandbox import DryRunRefused, execute_driver_method

logger = logging.getLogger(__name__)


async def create_execution_run(
    db: AsyncSession,
    device_id: uuid.UUID,
    driver_id: uuid.UUID,
    driver_sha256: str,
    action: str,
    user_id: uuid.UUID,
    input_params: dict,
    reservation_id: uuid.UUID | None = None,
    port_a: str | None = None,
    port_b: str | None = None,
    method_kwargs: dict | None = None,
) -> ExecutionRun:
    # Store method_kwargs in input_params for queryability
    if method_kwargs:
        input_params = dict(input_params)
        input_params["method_kwargs"] = method_kwargs
    run = ExecutionRun(
        device_id=device_id,
        driver_id=driver_id,
        driver_sha256=driver_sha256,
        action=action,
        status="PENDING",
        reservation_id=reservation_id,
        user_id=user_id,
        input_params=input_params,
        port_a=port_a,
        port_b=port_b,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


async def update_execution_run(
    db: AsyncSession,
    run: ExecutionRun,
    status: str,
    output: str | None = None,
    error: str | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    duration_ms: int | None = None,
) -> ExecutionRun:
    run.status = status
    if output is not None:
        run.output = output
    if error is not None:
        run.error = error
    if started_at is not None:
        run.started_at = started_at
    if completed_at is not None:
        run.completed_at = completed_at
    if duration_ms is not None:
        run.duration_ms = duration_ms
    await db.commit()
    await db.refresh(run)
    return run


async def get_execution_run(db: AsyncSession, run_id: uuid.UUID) -> ExecutionRun | None:
    return await db.get(ExecutionRun, run_id)


async def insert_command_log(
    db: AsyncSession,
    run_id: uuid.UUID,
    rows: list[dict],
) -> int:
    """Bulk-insert per-command transcript rows for a run.

    `rows` comes straight from `driver_sandbox.execute_driver_method`'s return
    dict under the `transcript` key. Each row may have any subset of keys
    {command, response, duration_ms, exit_status}; only `command` is required.
    Rows missing `command` are skipped with a warning. Returns the number of
    rows actually inserted.

    The seq is assigned by insertion order (1-based). Callers should not rely
    on this ordering reflecting wall-clock time across concurrent runs of the
    same driver; per-run order is preserved.
    """
    if not rows:
        return 0
    objs: list[ExecutionCommand] = []
    seq = 0
    for row in rows:
        command = row.get("command")
        if not command:
            logger.warning("Skipping command-log row with no command: %r", row)
            continue
        seq += 1
        objs.append(
            ExecutionCommand(
                run_id=run_id,
                seq=seq,
                command=str(command),
                response=row.get("response"),
                duration_ms=row.get("duration_ms"),
                exit_status=str(row.get("exit_status") or "ok")[:20],
            )
        )
    if not objs:
        return 0
    db.add_all(objs)
    await db.commit()
    return len(objs)


async def list_command_log(
    db: AsyncSession,
    run_id: uuid.UUID,
) -> list[ExecutionCommand]:
    result = await db.execute(
        select(ExecutionCommand)
        .where(ExecutionCommand.run_id == run_id)
        .order_by(ExecutionCommand.seq)
    )
    return list(result.scalars().all())


async def list_execution_runs(
    db: AsyncSession,
    device_id: uuid.UUID | None = None,
    reservation_id: uuid.UUID | None = None,
    status_filter: str | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[ExecutionRun], int]:
    query = select(ExecutionRun)
    if device_id is not None:
        query = query.where(ExecutionRun.device_id == device_id)
    if reservation_id is not None:
        query = query.where(ExecutionRun.reservation_id == reservation_id)
    if status_filter is not None:
        query = query.where(ExecutionRun.status == status_filter)
    if created_after is not None:
        query = query.where(ExecutionRun.created_at >= created_after)
    if created_before is not None:
        query = query.where(ExecutionRun.created_at < created_before)

    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar() or 0

    result = await db.execute(
        query.order_by(ExecutionRun.created_at.desc()).offset(skip).limit(limit)
    )
    return list(result.scalars().all()), total


def build_context(
    device_data: dict,
    device_id: uuid.UUID,
    user_id: uuid.UUID,
    reservation_id: uuid.UUID | None = None,
) -> dict:
    """Build the context dict passed to the Driver.__init__().

    All device field_data keys are prefixed with HERD_ to distinguish them
    from user-defined variables in driver code.
    """
    context = {}
    field_data = device_data.get("field_data", {})
    for key, value in field_data.items():
        context[f"HERD_{key}"] = value

    context["HERD_device_id"] = str(device_id)
    context["HERD_device_name"] = device_data.get("name", "")
    context["HERD_connection_type"] = device_data.get("connection_type", "")
    context["HERD_reservation_id"] = str(reservation_id) if reservation_id else None
    context["HERD_user_id"] = str(user_id)

    return context


def redact_context_for_logging(context: dict, password_keys: set[str]) -> dict:
    """Return a copy of context with password fields redacted.

    password_keys should contain the HERD_-prefixed keys that correspond to
    template fields of type "password".
    """
    redacted = {}
    for key, value in context.items():
        if key in password_keys:
            redacted[key] = "***REDACTED***"
        else:
            redacted[key] = value
    return redacted


def extract_password_keys(template_data: dict) -> set[str]:
    """Extract the set of HERD_-prefixed keys for password-type fields from template sections."""
    keys = set()
    for section in template_data.get("sections", []):
        for field in section.get("fields", []):
            if field.get("type") == "password":
                keys.add(f"HERD_{field['key']}")
    return keys


async def fetch_device(device_id: uuid.UUID) -> dict:
    """Fetch device details from inventory service.

    Uses the token-guarded /internal route: this is a service-to-service call
    (the health scheduler has no user JWT, and on-demand execution authenticates
    downstream with X-Internal-Token), so it must not hit the Bearer-only
    /devices/{id} route, which returns 401 for a token-only caller.
    """
    url = f"{settings.inventory_service_url}/devices/{device_id}/internal"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                headers={"X-Internal-Token": settings.internal_api_token},
                timeout=10.0,
            )
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail=f"Device {device_id} not found")
            resp.raise_for_status()
            return resp.json()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch device: {exc}")


async def fetch_template(template_id: str) -> dict:
    """Fetch template details from inventory service.

    Uses the token-guarded /internal route for the same reason as fetch_device:
    a token-only caller gets 401 from the Bearer-only /templates/{id} route.
    """
    url = f"{settings.inventory_service_url}/templates/{template_id}/internal"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                headers={"X-Internal-Token": settings.internal_api_token},
                timeout=10.0,
            )
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail=f"Template {template_id} not found")
            resp.raise_for_status()
            return resp.json()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch template: {exc}")


async def run_driver_action(
    db: AsyncSession,
    device_data: dict,
    template_data: dict,
    action: str,
    user_id: uuid.UUID,
    reservation_id: uuid.UUID | None = None,
    port_a: str | None = None,
    port_b: str | None = None,
    method_kwargs: dict | None = None,
    dry_run: bool = False,
) -> ExecutionRun:
    """Core execution logic: build context, load driver, run in sandbox, record result."""
    device_id = uuid.UUID(device_data["id"])
    driver_id = uuid.UUID(device_data["driver_id"])
    driver_sha256 = device_data.get("driver_sha256", "unknown")
    driver_filename = device_data.get("driver_filename", "driver.zip")
    connection_type = device_data.get("connection_type", "")

    # Build context
    context = build_context(device_data, device_id, user_id, reservation_id)
    password_keys = extract_password_keys(template_data)
    redacted = redact_context_for_logging(context, password_keys)

    logger.info(
        "Starting driver execution",
        extra={
            "device_id": str(device_id),
            "driver_id": str(driver_id),
            "action": action,
            "context": redacted,
            "port_a": port_a,
            "port_b": port_b,
            "method_kwargs": method_kwargs,
        },
    )

    # Create execution run record
    run = await create_execution_run(
        db,
        device_id=device_id,
        driver_id=driver_id,
        driver_sha256=driver_sha256,
        action=action,
        user_id=user_id,
        input_params=redacted,
        reservation_id=reservation_id,
        port_a=port_a,
        port_b=port_b,
        method_kwargs=method_kwargs,
    )

    # Load driver
    try:
        driver_path = await load_driver(
            db, driver_id, driver_sha256, driver_filename, connection_type
        )
    except (ValueError, RuntimeError) as e:
        run = await update_execution_run(db, run, status="FAILED", error=str(e))
        logger.error("Driver load failed", extra={"run_id": str(run.id), "error": str(e)})
        return run

    # Method-kwargs allowlist: for `configure`, the kwargs ARE the device config
    # and must satisfy the same validation the inventory write path enforces on
    # create_config_version. Prefer a driver-published config schema (issue #23)
    # over the neutral registry schema, so a driver like the FRR Management driver
    # can accept {commands: [...]} that the registry's additionalProperties:False
    # would reject. The published schema is only cached once load_driver has run
    # for the current SHA, so this MUST stay after the load above; reading it
    # before the load would see a cold cache (or a stale row evicted on a SHA
    # change) and silently fall back to the registry.
    if action == "configure":
        try:
            published_schema = await get_driver_config_schema(db, driver_id)
            try:
                validate_device_config_with_schema(
                    connection_type or None,
                    method_kwargs,
                    schema=published_schema,
                    role=device_data.get("name") or None,
                )
            except PublishedSchemaError as exc:
                # A driver must never break validation by shipping an unsafe or
                # malformed config_schema(); fall back to the registry, matching
                # the inventory write path's posture.
                logger.warning(
                    "Driver-published schema unusable; falling back to registry",
                    extra={"driver_id": str(driver_id), "error": str(exc)},
                )
                validate_device_config(
                    connection_type or None,
                    method_kwargs,
                    role=device_data.get("name") or None,
                )
        except ConfigValidationError as exc:
            run = await update_execution_run(db, run, status="FAILED", error=str(exc))
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Fetch the driver's metadata for the dry-run gate (cheap: cached row read).
    driver_metadata = await get_driver_metadata(db, driver_id)

    # Execute in sandbox
    started_at = datetime.now(timezone.utc)
    run = await update_execution_run(db, run, status="RUNNING", started_at=started_at)

    try:
        result = execute_driver_method(
            driver_path=driver_path,
            action=action,
            context=context,
            port_a=port_a,
            port_b=port_b,
            method_kwargs=method_kwargs,
            dry_run=dry_run,
            driver_metadata=driver_metadata,
            password_keys=password_keys,
        )
    except DryRunRefused as exc:
        run = await update_execution_run(
            db,
            run,
            status="FAILED",
            error=f"dry-run refused: {exc}",
            completed_at=datetime.now(timezone.utc),
        )
        logger.warning(
            "Dry-run refused for driver",
            extra={"run_id": str(run.id), "driver_id": str(driver_id)},
        )
        return run

    # Persist the per-command transcript. Observational; never fail a run on insert errors.
    transcript = result.get("transcript") or []
    if transcript:
        try:
            await insert_command_log(db, run.id, transcript)
        except Exception as exc:
            logger.warning(
                "Failed to persist command-log rows",
                extra={"run_id": str(run.id), "error": str(exc), "row_count": len(transcript)},
            )

    completed_at = datetime.now(timezone.utc)
    if result["success"]:
        run = await update_execution_run(
            db,
            run,
            status="SUCCESS",
            output=json.dumps(result["output"], default=str) if result["output"] else None,
            error=result.get("error"),
            completed_at=completed_at,
            duration_ms=result["duration_ms"],
        )
    else:
        error_msg = result.get("error", "Unknown error")
        is_timeout = "timed out" in (error_msg or "")
        run = await update_execution_run(
            db,
            run,
            status="TIMEOUT" if is_timeout else "FAILED",
            error=error_msg,
            completed_at=completed_at,
            duration_ms=result["duration_ms"],
        )

    logger.info(
        "Driver execution completed",
        extra={
            "run_id": str(run.id),
            "status": run.status,
            "duration_ms": run.duration_ms,
        },
    )
    return run
