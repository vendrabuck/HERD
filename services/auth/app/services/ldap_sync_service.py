"""Directory group reconciler (ADR 0011 phase 3).

One pass mirrors every LdapGroupMapping's directory membership into its HERD
group via fail-closed set arithmetic. The error taxonomy is deliberately
asymmetric; each tier acts on exactly what was proven:

- Transport errors (LdapUnavailableError) prove nothing, so they skip the
  WHOLE group and mark the run partial: an unreadable or half-readable
  directory must never strip a team's membership (the #460 rule). This
  covers the group fetch and any error inside the member batch, since
  resolve_members raising mid-batch discards the whole batch by design.
- Directory ANSWERS (a dangling group DN, a member resolved with a
  skip_reason) act only on what was answered about: a dangling group is
  skipped whole (its membership is unknowable), while a skipped member is
  counted and excluded without failing the group's reconcile.
- Apply failures are isolated per operation: one racing add or remove is
  counted and the loop continues; it never fails the run.

Changes apply ONLY through group_service.add_member / remove_member so sync
reproduces manual admin behavior (the "Not Grouped" auto-remove included),
and only auth_source == "ldap" users are visible to the sync in either
direction. Run rows are committed with status "running" before any
directory work so a crashed pass leaves a visible corpse.
"""

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.group import GroupMember
from app.models.ldap_group_mapping import LdapGroupMapping
from app.models.ldap_sync_run import LdapSyncRun
from app.models.user import User
from app.services import auth_service, group_service, ldap_service

logger = logging.getLogger(__name__)

# Per-category cap on records persisted in ldap_sync_runs.detail; overflow is
# summarized by a trailing {"truncated": N} marker so a pathological run (a
# directory answering thousands of skips) cannot bloat the audit row.
DETAIL_CAP = 20

# Group-level skip reasons (persisted in detail; treat as a small API).
GROUP_SKIP_DIRECTORY_UNAVAILABLE = "directory_unavailable"
GROUP_SKIP_DANGLING_DN = "dangling_dn"
GROUP_SKIP_MEMBER_RESOLUTION_UNAVAILABLE = "member_resolution_unavailable"

# Member-level skip reasons added by the reconciler, alongside the
# ldap_service.MEMBER_SKIP_* vocabulary recorded verbatim.
MEMBER_SKIP_USERNAME_TAKEN = "username_taken"
MEMBER_SKIP_EMAIL_OWNED_BY_LOCAL_ACCOUNT = "email_owned_by_local_account"
MEMBER_SKIP_USERNAME_DRIFT_COLLISION = "username_drift_collision"
# Not a skip: a provisioning IntegrityError whose retry-as-lookup found the
# row (concurrent JIT login). Recorded in detail, member proceeds.
PROVISION_RACE_RECOVERED = "provision_race_recovered"

# Mirrors the column cap on LdapGroupMapping.directory_name (display cache).
_DIRECTORY_NAME_MAX = 255


class _CappedCategory:
    """A detail category holding at most DETAIL_CAP records plus overflow."""

    def __init__(self) -> None:
        self.records: list[dict] = []
        self.overflow = 0

    def add(self, record: dict) -> None:
        if len(self.records) < DETAIL_CAP:
            self.records.append(record)
        else:
            self.overflow += 1

    def dump(self) -> list[dict]:
        out = list(self.records)
        if self.overflow:
            out.append({"truncated": self.overflow})
        return out


class _Tally:
    """Counters and capped detail for one run.

    degraded distinguishes "partial" from "success": any group skip, member
    skip, or per-op failure degrades the run; a recovered provisioning race
    does not (nothing was skipped, the member proceeded).
    """

    def __init__(self) -> None:
        self.users_provisioned = 0
        self.members_added = 0
        self.members_removed = 0
        self.members_skipped = 0
        self.degraded = False
        self._categories: dict[str, _CappedCategory] = {
            "skipped_groups": _CappedCategory(),
            "skipped_members": _CappedCategory(),
            "op_failures": _CappedCategory(),
            "provision_races": _CappedCategory(),
        }

    def skip_group(self, group_dn: str, reason: str, error: str | None = None) -> None:
        self.degraded = True
        record: dict = {"group_dn": group_dn, "reason": reason}
        if error is not None:
            record["error"] = error
        self._categories["skipped_groups"].add(record)
        logger.warning(
            "LDAP sync skipped group %s: %s",
            group_dn,
            reason,
            extra={"action": "ldap_sync_group_skipped", "group_dn": group_dn, "reason": reason},
        )

    def skip_member(self, group_dn: str, member_dn: str, reason: str) -> None:
        self.degraded = True
        self.members_skipped += 1
        self._categories["skipped_members"].add(
            {"group_dn": group_dn, "member_dn": member_dn, "reason": reason}
        )

    def op_failure(self, group_dn: str, op: str, user_id: uuid.UUID, error: str) -> None:
        self.degraded = True
        self._categories["op_failures"].add(
            {"group_dn": group_dn, "op": op, "user_id": str(user_id), "error": error}
        )
        logger.warning(
            "LDAP sync %s failed for user %s in %s: %s",
            op,
            user_id,
            group_dn,
            error,
            extra={"action": "ldap_sync_op_failed", "group_dn": group_dn, "op": op},
        )

    def provision_race(self, group_dn: str, email: str) -> None:
        self._categories["provision_races"].add(
            {"group_dn": group_dn, "email": email, "reason": PROVISION_RACE_RECOVERED}
        )

    def detail(self) -> dict:
        return {name: cat.dump() for name, cat in self._categories.items() if cat.records}


async def _ensure_ldap_user(
    db: AsyncSession, group_dn: str, identity: ldap_service.LdapIdentity, tally: _Tally
) -> uuid.UUID | None:
    """Resolve one desired identity to a HERD user id, provisioning if needed.

    Lookup is by EMAIL, the key JIT provisioning and login resolution trust.
    Returns None when the member must be skipped (collision categories per
    ADR 0011); a recovered provisioning race proceeds. Returns scalars, not
    the ORM instance: rollbacks below expire every instance in the session,
    and attribute access on an expired instance requires IO the async caller
    cannot perform implicitly.
    """
    user = await auth_service.get_user_by_email(db, identity.email)
    if user is None:
        try:
            user = await auth_service.create_ldap_user(db, identity.email, identity.username)
            tally.users_provisioned += 1
            # Freshly provisioned from this identity: source and username are
            # correct by construction, no drift check needed.
            return user.id
        except IntegrityError:
            # Either a concurrent JIT login provisioned this email (retry as
            # a lookup and proceed) or the username belongs to another
            # account (skip; never take over an existing identity).
            user = await auth_service.get_user_by_email(db, identity.email)
            if user is None:
                tally.skip_member(group_dn, identity.dn, MEMBER_SKIP_USERNAME_TAKEN)
                return None
            tally.provision_race(group_dn, identity.email)
    if user.auth_source != "ldap":
        # Mirrors _authenticate_ldap: an email owned by a local account is
        # refused, never silently converted.
        tally.skip_member(group_dn, identity.dn, MEMBER_SKIP_EMAIL_OWNED_BY_LOCAL_ACCOUNT)
        return None
    user_id = user.id
    if user.username != identity.username:
        # Username drift repair: login never refreshes the stored username,
        # so directory renames drift. A uniqueness collision skips only the
        # repair; the member is still a proven directory answer and its
        # membership still reconciles.
        old_username = user.username
        user.username = identity.username
        try:
            await db.commit()
            logger.info(
                "LDAP sync repaired username drift: %s to %s",
                old_username,
                identity.username,
                extra={"action": "ldap_sync_username_repaired", "user_id": str(user_id)},
            )
        except IntegrityError:
            await db.rollback()
            tally.skip_member(group_dn, identity.dn, MEMBER_SKIP_USERNAME_DRIFT_COLLISION)
    return user_id


async def _reconcile_mapping(
    db: AsyncSession,
    *,
    mapping_id: uuid.UUID,
    group_dn: str,
    herd_group_id: uuid.UUID,
    directory_name: str,
    tally: _Tally,
) -> None:
    # Scalars, not the ORM instance: rollbacks in this pass expire every
    # instance in the session, and expired attribute access needs IO the
    # async path cannot perform implicitly (see _ensure_ldap_user).
    try:
        entry = await ldap_service.fetch_group(group_dn)
    except ldap_service.LdapUnavailableError as exc:
        tally.skip_group(group_dn, GROUP_SKIP_DIRECTORY_UNAVAILABLE, str(exc))
        return
    if entry is None:
        # The directory answered: the DN resolves nothing (rename or OU
        # move). Fail-closed; the admin re-creates the mapping.
        tally.skip_group(group_dn, GROUP_SKIP_DANGLING_DN)
        return

    new_name = entry.name[:_DIRECTORY_NAME_MAX]
    if new_name and new_name != directory_name:
        # Display-cache refresh on every successful fetch; a failed fetch
        # above kept the last cached value by returning early.
        await db.execute(
            update(LdapGroupMapping)
            .where(LdapGroupMapping.id == mapping_id)
            .values(directory_name=new_name)
        )
        await db.commit()

    try:
        resolutions = await ldap_service.resolve_members(entry.member_dns)
    except ldap_service.LdapUnavailableError as exc:
        # A raise mid-batch discarded the whole batch; a half-resolved
        # desired set must never drive removals.
        tally.skip_group(group_dn, GROUP_SKIP_MEMBER_RESOLUTION_UNAVAILABLE, str(exc))
        return

    desired_ids: set[uuid.UUID] = set()
    for member_dn, resolution in zip(entry.member_dns, resolutions):
        if resolution.identity is None:
            # An answer the directory gave (entry gone, or present without
            # the attributes JIT needs): counted, does not fail the group.
            tally.skip_member(group_dn, member_dn, resolution.skip_reason or "unknown")
            continue
        user_id = await _ensure_ldap_user(db, group_dn, resolution.identity, tally)
        if user_id is not None:
            desired_ids.add(user_id)

    current_ids = set(
        (
            await db.execute(
                select(GroupMember.user_id)
                .join(User, GroupMember.user_id == User.id)
                .where(GroupMember.group_id == herd_group_id, User.auth_source == "ldap")
            )
        )
        .scalars()
        .all()
    )

    # Sorted for deterministic apply order (and deterministic detail).
    for user_id in sorted(desired_ids - current_ids, key=str):
        try:
            await group_service.add_member(db, herd_group_id, user_id)
            tally.members_added += 1
        except IntegrityError:
            # Concurrent admin already added the row: benign no-op.
            await db.rollback()
        except Exception as exc:
            await db.rollback()
            tally.op_failure(group_dn, "add", user_id, str(exc))
    for user_id in sorted(current_ids - desired_ids, key=str):
        try:
            if await group_service.remove_member(db, herd_group_id, user_id):
                tally.members_removed += 1
        except Exception as exc:
            await db.rollback()
            tally.op_failure(group_dn, "remove", user_id, str(exc))


async def execute_run(db: AsyncSession, run: LdapSyncRun) -> LdapSyncRun:
    """Execute the reconcile pass against an already-committed run row.

    Finalizes the row in a finally (finished_at is always set): "success"
    when nothing was skipped or failed, "partial" on any skip or per-op
    failure, "failed" only when the run-level machinery itself raised, in
    which case the exception is recorded and NOT re-raised.
    """
    run_id = run.id
    logger.info(
        "LDAP sync run started",
        extra={"action": "ldap_sync_run_started", "run_id": str(run_id), "trigger": run.trigger},
    )
    tally = _Tally()
    status = "failed"
    error: str | None = None
    try:
        mappings = (
            await db.execute(
                select(
                    LdapGroupMapping.id,
                    LdapGroupMapping.group_dn,
                    LdapGroupMapping.herd_group_id,
                    LdapGroupMapping.directory_name,
                ).order_by(LdapGroupMapping.created_at, LdapGroupMapping.id)
            )
        ).all()
        for mapping_id, group_dn, herd_group_id, directory_name in mappings:
            await _reconcile_mapping(
                db,
                mapping_id=mapping_id,
                group_dn=group_dn,
                herd_group_id=herd_group_id,
                directory_name=directory_name,
                tally=tally,
            )
        status = "partial" if tally.degraded else "success"
    except Exception as exc:
        await db.rollback()
        error = str(exc)
        logger.exception(
            "LDAP sync run failed",
            extra={"action": "ldap_sync_run_failed", "run_id": str(run_id)},
        )
    finally:
        run.status = status
        run.error = error
        run.finished_at = datetime.now(timezone.utc)
        run.users_provisioned = tally.users_provisioned
        run.members_added = tally.members_added
        run.members_removed = tally.members_removed
        run.members_skipped = tally.members_skipped
        run.detail = tally.detail()
        await db.commit()
        logger.info(
            "LDAP sync run finished: %s",
            status,
            extra={
                "action": "ldap_sync_run_finished",
                "run_id": str(run_id),
                "status": status,
                "users_provisioned": tally.users_provisioned,
                "members_added": tally.members_added,
                "members_removed": tally.members_removed,
                "members_skipped": tally.members_skipped,
            },
        )
    return run


async def run_sync(db: AsyncSession, *, trigger: str = "manual") -> LdapSyncRun:
    """Create a run row (committed first, so a crash leaves a visible
    "running" corpse) and execute the reconcile pass against it."""
    run = LdapSyncRun(trigger=trigger, status="running")
    db.add(run)
    await db.commit()
    return await execute_run(db, run)
