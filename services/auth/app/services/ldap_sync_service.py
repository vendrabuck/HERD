"""Directory group reconciler (ADR 0011 phase 3) plus the deactivation and
reactivation sweep (ADR 0011 phase 4).

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
  counted and excluded without failing the group's reconcile. The one
  exception is the removal pass itself (see _reconcile_mapping): a member
  the directory still lists but could not identify (missing_email or
  missing_username) makes the whole group's removal set unprovable, since
  that unresolvable entry could be any current row.
- Apply failures are isolated per operation: one racing add or remove is
  counted and the loop continues; it never fails the run.

Changes apply ONLY through group_service._add_member_resolved (the same
core logic add_member's public wrapper delegates to, issue #513 item 9) /
remove_member so sync reproduces manual admin behavior (the "Not Grouped"
auto-remove included),
and only auth_source == "ldap" AND is_active users are visible to the sync
in either direction (an admin-deactivated user is invisible to membership
sync just like a local account; phase 4's sweep owns is_active transitions).
Run rows are committed with status "running" before any directory work so a
crashed pass leaves a visible corpse.

Run serialization (sync-now and the phase 5 interval loop share ONE
running-row invariant) also lives here: an in-process asyncio.Lock plus, on
Postgres, a session-scoped advisory lock so scaled-out auth replicas cannot
execute concurrent directory-wide writes. The advisory key is the single
constant below; changing it would let two builds sync concurrently across a
rolling deploy, so it is part of the cross-replica contract, not a tunable.

_run_deactivation_sweep runs after the mapping loop, gated on its own
settings.ldap_sync_deactivation_enabled: proves presence via ONE paged
directory enumeration (ldap_service.present_emails) plus this run's
group-presence credit set, deactivates every proven-absent-or-disabled
auth_source=ldap user below a two-term circuit breaker (strict-exceeds on
BOTH max_percent and min_count; boundary-equal never aborts), and reactivates
every sync-deactivated user proven present regardless of the breaker
(reactivation is the safe direction, exempt from abort). Provenance
(deactivated_by_sync) gates reactivation: an admin-deactivated user is never
auto-reactivated, and the manual endpoints (routers/admin.py) always write
deactivated_by_sync=False, admin intent outranking the directory.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from herd_common import advisory_lock
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app import database
from app.config import settings
from app.models.group import GroupMember
from app.models.ldap_group_mapping import DIRECTORY_NAME_MAX, LdapGroupMapping
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
MEMBER_SKIP_USER_INACTIVE = "user_inactive"
# Not a skip: a provisioning IntegrityError whose retry-as-lookup found the
# row (concurrent JIT login). Recorded in detail, member proceeds.
PROVISION_RACE_RECOVERED = "provision_race_recovered"

# ADR 0011 phase 4: deactivation-sweep detail reasons (category "sweep").
SWEEP_REASON_ENUMERATION_UNAVAILABLE = "enumeration_unavailable"
SWEEP_REASON_BREAKER_TRIPPED = "breaker_tripped"

# C4: a member the directory still lists but could not identify makes the
# whole group's removal set unprovable, since the unresolvable entry could
# be any current row. A proven not_found skip does NOT belong here: a
# proven-absent DN shields nobody, so it never blocks removals.
_REMOVAL_SUPPRESSING_REASONS = frozenset(
    {ldap_service.MEMBER_SKIP_MISSING_EMAIL, ldap_service.MEMBER_SKIP_MISSING_USERNAME}
)


@dataclass(frozen=True)
class _RunIdentityOutcome:
    """One email's _ensure_ldap_user classification, memoized for the rest
    of the run (issue #513 item 2): a user in N mapped groups is resolved
    once, and every later group's occurrence replays this outcome against
    ITS OWN group_dn/member_dn instead of repeating the DB round trip.

    user_id is None for a skip (identity_cache holds skip_reason too).
    Whether an outcome is safe to cache is NOT decided here or at each call
    site: see _cache_outcome and _CACHEABLE_SKIP_REASONS below, the single
    structural gate every write to identity_cache goes through (issue #513
    round-3 item 1). A resolved success is always cached: the identity
    email-to-user_id mapping is a structural fact (an email does not
    change which row it names mid-run). A skip is cached only when its
    reason names something this app treats as immutable once a user is
    created (auth_source; verified by grep, no code path reassigns it).
    MEMBER_SKIP_USER_INACTIVE and MEMBER_SKIP_USERNAME_TAKEN are both
    EXCLUDED: is_active is flipped by a live, concurrent admin endpoint
    this reconciler does not coordinate with, and a cached inactive skip
    combined with _reconcile_mapping's LIVE current_ids query stretches
    what was a millisecond-scale race at HEAD into a whole-run-scale one
    (a reactivation mid-run could spuriously remove a member; see
    _reconcile_mapping's stale-add/stale-remove re-verification for the
    other half of that fix). USERNAME_TAKEN is not stable either: a later
    member's drift repair can free the blocked username (round-2 item 5).
    PROVISION_RACE_RECOVERED and a drift collision are not cached AS a
    distinct outcome kind: both resolve to plain success (user_id set,
    skip_reason None), exactly what an uncached second occurrence's own
    independent lookup would have observed anyway (get_user_by_email finds
    the already-settled row), so caching success for them costs nothing.
    """

    user_id: uuid.UUID | None
    skip_reason: str | None = None


# The ONLY skip reasons safe to memoize across the whole run (issue #513
# round-3 item 1): each one names a fact this application treats as
# immutable once a user row exists. auth_source is set once at creation
# (create_user / create_ldap_user) and never reassigned by any router or
# service in this codebase (verified by grep over services/auth/app);
# MEMBER_SKIP_EMAIL_OWNED_BY_LOCAL_ACCOUNT is therefore safe. Deliberately
# NOT here: MEMBER_SKIP_USER_INACTIVE (is_active is admin-mutable live, see
# _RunIdentityOutcome) and MEMBER_SKIP_USERNAME_TAKEN (not stable, round-2
# item 5). A future skip reason is UNCACHED by default: it must be added
# here deliberately, with the same immutability argument, not by
# copy-pasting an existing cache-write call site.
_CACHEABLE_SKIP_REASONS: frozenset[str] = frozenset({MEMBER_SKIP_EMAIL_OWNED_BY_LOCAL_ACCOUNT})


def _cache_outcome(
    identity_cache: dict[str, _RunIdentityOutcome],
    email: str,
    user_id: uuid.UUID | None,
    skip_reason: str | None = None,
) -> None:
    """The SINGLE point that writes into identity_cache. A success
    (skip_reason None) is always cached; a skip is cached only when
    skip_reason is in _CACHEABLE_SKIP_REASONS. Every classification branch
    in this module (_ensure_ldap_user's snapshot path, _finalize_ldap_user,
    _provision_or_recover) calls this instead of constructing
    _RunIdentityOutcome and assigning into identity_cache directly, so the
    cacheable set is enforced in ONE place, not re-decided (and
    potentially miscopied) at each of what used to be seven call sites.
    """
    if skip_reason is None or skip_reason in _CACHEABLE_SKIP_REASONS:
        identity_cache[email] = _RunIdentityOutcome(user_id, skip_reason)


# NotGroupedResolver lives in auth_service (issue #513 round-3 item 3): it
# is constructed here (once per run) but also consumed by
# auth_service.create_ldap_user's internal auto-assign, so BOTH paths share
# the SAME object and no sentinel value is needed anywhere. See its
# docstring in auth_service.py for the full rationale.


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

    degraded distinguishes "partial" from "success" and is DERIVED: any
    category other than provision_races holding a record or overflow makes
    the run partial (a recovered provisioning race is not a skip, nothing
    was skipped, the member proceeded). "deactivated" and "reactivated" are
    likewise excluded: those are the deactivation sweep's normal, intended
    outcome, not degradation, so a clean sweep that deactivated someone must
    not read as "partial". Categories are created lazily via _category, so a
    new category needs no constructor edit unless it also needs excluding
    here.
    """

    _NON_DEGRADING_CATEGORIES = frozenset({"provision_races", "deactivated", "reactivated"})

    def __init__(self) -> None:
        self.users_provisioned = 0
        self.members_added = 0
        self.members_removed = 0
        self.members_skipped = 0
        self.users_deactivated = 0
        self.users_reactivated = 0
        # ADR 0011 phase 4: the circuit breaker overrides success/partial to
        # "aborted" (execute_run reads these two after the sweep runs).
        self.aborted = False
        self.abort_error: str | None = None
        self._categories: dict[str, _CappedCategory] = {}

    def _category(self, name: str) -> _CappedCategory:
        return self._categories.setdefault(name, _CappedCategory())

    @property
    def degraded(self) -> bool:
        return any(
            cat.records or cat.overflow
            for name, cat in self._categories.items()
            if name not in self._NON_DEGRADING_CATEGORIES
        )

    def skip_group(self, group_dn: str, reason: str, error: str | None = None) -> None:
        record: dict = {"group_dn": group_dn, "reason": reason}
        if error is not None:
            record["error"] = error
        self._category("skipped_groups").add(record)
        logger.warning(
            "LDAP sync skipped group %s: %s",
            group_dn,
            reason,
            extra={"action": "ldap_sync_group_skipped", "group_dn": group_dn, "reason": reason},
        )

    def skip_member(self, group_dn: str, member_dn: str, reason: str) -> None:
        self.members_skipped += 1
        self._category("skipped_members").add(
            {"group_dn": group_dn, "member_dn": member_dn, "reason": reason}
        )

    def op_failure(self, group_dn: str, op: str, user_id: uuid.UUID, error: str) -> None:
        self._category("op_failures").add(
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
        self._category("provision_races").add(
            {"group_dn": group_dn, "email": email, "reason": PROVISION_RACE_RECOVERED}
        )

    def drift_collision(self, group_dn: str, member_dn: str, reason: str) -> None:
        # C6: recorded apart from skip_member. A repair WAS skipped (the run
        # is still partial via degraded), but the member is a proven
        # directory answer and reconciles into the group regardless, so it
        # must not double-count as both skipped and added.
        self._category("drift_collisions").add(
            {"group_dn": group_dn, "member_dn": member_dn, "reason": reason}
        )

    def suppress_removals(self, group_dn: str, unresolved: int, would_remove: int) -> None:
        self._category("suppressed_removals").add(
            {"group_dn": group_dn, "unresolved": unresolved, "would_remove": would_remove}
        )

    def sweep_note(self, reason: str, **fields) -> None:
        """A "sweep" detail record for the deactivation pass itself
        (enumeration_unavailable, breaker_tripped): degrading events distinct
        from any individual user's outcome."""
        self._category("sweep").add({"reason": reason, **fields})
        logger.warning(
            "LDAP deactivation sweep note: %s",
            reason,
            extra={"action": "ldap_sync_sweep_note", "reason": reason, **fields},
        )

    def record_deactivated(self, user_id: uuid.UUID, email: str) -> None:
        self.users_deactivated += 1
        self._category("deactivated").add({"user_id": str(user_id), "email": email})
        logger.info(
            "LDAP sync deactivated user %s (%s): proven absent or disabled",
            user_id,
            email,
            extra={"action": "ldap_sync_user_deactivated", "user_id": str(user_id)},
        )

    def record_reactivated(self, user_id: uuid.UUID, email: str) -> None:
        self.users_reactivated += 1
        self._category("reactivated").add({"user_id": str(user_id), "email": email})
        logger.info(
            "LDAP sync reactivated user %s (%s): proven present",
            user_id,
            email,
            extra={"action": "ldap_sync_user_reactivated", "user_id": str(user_id)},
        )

    def detail(self) -> dict:
        return {name: cat.dump() for name, cat in self._categories.items() if cat.records}

    def apply_to(self, run: LdapSyncRun) -> None:
        """Write the counters and detail onto the run row. Status, error,
        and finished_at are run-level machinery outcomes, not tally state,
        and stay the caller's responsibility."""
        run.users_provisioned = self.users_provisioned
        run.members_added = self.members_added
        run.members_removed = self.members_removed
        run.members_skipped = self.members_skipped
        run.users_deactivated = self.users_deactivated
        run.users_reactivated = self.users_reactivated
        run.detail = self.detail()


def _classify_auth_source_and_active(
    group_dn: str,
    identity: ldap_service.LdapIdentity,
    tally: _Tally,
    identity_cache: dict[str, _RunIdentityOutcome],
    record,
) -> bool:
    """Check record.auth_source and record.is_active, recording and
    caching (via _cache_outcome) a skip when either fails. Returns True
    when record passed both checks (the caller should proceed: read
    record.id/.username for a snapshot, or continue on to the username
    drift check for a live ORM instance).

    record is duck-typed: a plain auth_service.UserSnapshot (the batch
    path, never mutated) or a live ORM User (the fresh-fetch/race-recovery
    path, about to be possibly mutated by the caller's drift check) both
    expose .auth_source/.is_active, and this function only READS them, so
    one implementation serves _ensure_ldap_user's snapshot branch and
    _finalize_ldap_user's live-instance branch identically (issue #513
    round-3 item 6: these were two copies of the same two checks).
    """
    if record.auth_source != "ldap":
        # Mirrors _authenticate_ldap: an email owned by a local account is
        # refused, never silently converted.
        tally.skip_member(group_dn, identity.dn, MEMBER_SKIP_EMAIL_OWNED_BY_LOCAL_ACCOUNT)
        _cache_outcome(
            identity_cache, identity.email, None, MEMBER_SKIP_EMAIL_OWNED_BY_LOCAL_ACCOUNT
        )
        return False
    if not record.is_active:
        # C5: inactive users are invisible to membership sync in both
        # directions, exactly like local accounts. Never re-added here; the
        # current_ids query below excludes their existing rows too, so an
        # admin deactivation is never undone by a later sync pass. Phase 4's
        # sweep is the only sync writer of is_active. NOT cached (round-3
        # item 1): is_active can flip mid-run via a concurrent admin
        # action, so every group's occurrence re-checks it fresh.
        tally.skip_member(group_dn, identity.dn, MEMBER_SKIP_USER_INACTIVE)
        _cache_outcome(identity_cache, identity.email, None, MEMBER_SKIP_USER_INACTIVE)
        return False
    return True


async def _finalize_ldap_user(
    db: AsyncSession,
    group_dn: str,
    identity: ldap_service.LdapIdentity,
    tally: _Tally,
    identity_cache: dict[str, _RunIdentityOutcome],
    user: User,
) -> uuid.UUID | None:
    """Given a FRESH, live ORM User row for identity.email (never a stale
    batch snapshot), apply the auth_source/is_active checks and the
    username drift repair, cache the outcome (via _cache_outcome), and
    return the resolved id (or None for a skip).

    Shared by _ensure_ldap_user's own-snapshot mutation path and
    _provision_or_recover's race-recovery path: both end up holding a live
    ORM instance at this point and need identical finishing logic (issue
    #513 item 1's regression fix keeps this the ONLY place that reads
    .auth_source/.is_active/.username off a live ORM instance and possibly
    mutates one, so a rollback anywhere else in the run can never strand a
    caller mid-attribute-access).
    """
    if not _classify_auth_source_and_active(group_dn, identity, tally, identity_cache, user):
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
            tally.drift_collision(group_dn, identity.dn, MEMBER_SKIP_USERNAME_DRIFT_COLLISION)
    _cache_outcome(identity_cache, identity.email, user_id)
    return user_id


async def _provision_or_recover(
    db: AsyncSession,
    group_dn: str,
    identity: ldap_service.LdapIdentity,
    tally: _Tally,
    identity_cache: dict[str, _RunIdentityOutcome],
    not_grouped: "auth_service.NotGroupedResolver",
) -> uuid.UUID | None:
    """Create a new HERD user for identity, or recover a concurrently
    created one via IntegrityError-retry-as-lookup.

    Shared by _ensure_ldap_user's "no existing row" path (a batch miss) and
    its rare "existing row vanished between the batch snapshot and a drift
    repair" path, so both go through identical provisioning/race logic.
    not_grouped (item 6) is the run-level lazy resolver threaded all the
    way into create_ldap_user, so this provisioning path and the
    reconciler's own explicit add loop agree on the same resolved
    "Not Grouped" id within one run.
    """
    try:
        # not_grouped is the SAME resolver object create_ldap_user's
        # internal auto-assign consults (issue #513 round-3 item 3): no
        # separate .get(db) call is needed here, since the resolver caches
        # its own result and create_ldap_user calls it exactly once.
        user = await auth_service.create_ldap_user(
            db, identity.email, identity.username, not_grouped=not_grouped
        )
        tally.users_provisioned += 1
        # Freshly provisioned from this identity: source, username, and
        # is_active (default True) are correct by construction, no further
        # checks needed.
        _cache_outcome(identity_cache, identity.email, user.id)
        return user.id
    except IntegrityError:
        # Either a concurrent JIT login provisioned this email (retry as a
        # lookup and proceed) or the username belongs to another account
        # (skip; never take over an existing identity).
        user = await auth_service.get_user_by_email(db, identity.email)
        if user is None:
            tally.skip_member(group_dn, identity.dn, MEMBER_SKIP_USERNAME_TAKEN)
            # Round-2 item 5: deliberately NOT cacheable (see
            # _CACHEABLE_SKIP_REASONS). Not stable within a run: a LATER
            # member's drift repair can rename away the blocking account
            # and free the username, so a repeat occurrence of this same
            # email in a later group must re-attempt resolution from
            # scratch rather than replay a skip that may no longer hold.
            return None
        tally.provision_race(group_dn, identity.email)
    return await _finalize_ldap_user(db, group_dn, identity, tally, identity_cache, user)


async def _ensure_ldap_user(
    db: AsyncSession,
    group_dn: str,
    identity: ldap_service.LdapIdentity,
    tally: _Tally,
    known_users: "dict[str, auth_service.UserSnapshot]",
    identity_cache: dict[str, _RunIdentityOutcome],
    not_grouped: "auth_service.NotGroupedResolver",
) -> uuid.UUID | None:
    """Resolve one desired identity to a HERD user id, provisioning if needed.

    Lookup is by EMAIL, the key JIT provisioning and login resolution trust.
    Returns None when the member must be skipped (collision categories per
    ADR 0011); a recovered provisioning race proceeds.

    known_users (issue #513 item 1) is the caller's batched email-to-
    UserSnapshot dict for this group's proven members (one WHERE email IN
    (...) query per group instead of one SELECT per member), of plain
    SNAPSHOTS, never ORM instances: a snapshot cannot be expired by a LATER
    member's rollback elsewhere in this same loop (the regression the
    original item-1 cut shipped: an ORM instance held here could be
    expired by create_ldap_user's or the drift-repair's own rollback,
    raising MissingGreenlet on the NEXT member's attribute read, outside
    any per-op try, failing the WHOLE run). A batch miss, or a snapshot
    that needs a MUTATING repair, falls back to a fresh, live ORM fetch via
    _provision_or_recover / _finalize_ldap_user, since only a live instance
    can be safely written to.

    identity_cache (item 2) is the run-level memo: on a hit, this function
    replays the CACHED classification against this call's own
    group_dn/identity.dn (skip_member for a skip; a bare return for
    success) without touching the DB again, and returns without falling
    through to any of the branches below. See _RunIdentityOutcome and
    _cache_outcome for exactly which outcomes are cacheable at all (round-3
    item 1: MEMBER_SKIP_USER_INACTIVE is NOT, since is_active is
    admin-mutable mid-run; _reconcile_mapping's add loop carries the other
    half of that fix, a live re-verification right before an add so a
    stale cache-hit success cannot add a since-deactivated user).

    not_grouped (item 6) is threaded into the provisioning path so
    create_ldap_user's internal "Not Grouped" auto-assign agrees with this
    run's cached id, the same one _reconcile_mapping's explicit add loop
    uses.

    Cross-reference (S5): auth_service._authenticate_ldap resolves the same
    identity shape on login but never repairs username drift and never
    retries a provisioning race as a lookup; it refuses instead. That
    divergence is DESIGNED: this function runs inside a long-lived
    reconcile pass whose job is to repair drift and recover races proactively,
    while login is a narrow, latency-sensitive path that must fail closed
    rather than silently mutate an account mid-authentication. Do not "fix"
    one to match the other.
    """
    cached = identity_cache.get(identity.email)
    if cached is not None:
        if cached.skip_reason is not None:
            tally.skip_member(group_dn, identity.dn, cached.skip_reason)
        return cached.user_id

    snapshot = known_users.get(identity.email)
    if snapshot is None:
        return await _provision_or_recover(
            db, group_dn, identity, tally, identity_cache, not_grouped
        )

    if not _classify_auth_source_and_active(group_dn, identity, tally, identity_cache, snapshot):
        return None

    if snapshot.username == identity.username:
        # No mutation needed: the snapshot's fields are already final for
        # this member, so no fresh ORM fetch is spent at all (the whole
        # point of item 1's batching: most members need no per-member
        # query beyond the group's one batched fetch).
        _cache_outcome(identity_cache, identity.email, snapshot.id)
        return snapshot.id

    # Username differs: a mutating repair is needed. A snapshot cannot be
    # re-attached to the session for a write, and reusing its (possibly
    # now-stale) in-memory data for one risks clobbering a concurrent
    # change, so a fresh, live ORM instance is loaded right here.
    user = await auth_service.get_user_by_email(db, identity.email)
    if user is None:
        # Deleted between the batch snapshot and this repair attempt: a
        # genuine concurrent race, not the expired-instance bug item 1
        # fixes. Fall through to the same path a fresh batch miss takes.
        return await _provision_or_recover(
            db, group_dn, identity, tally, identity_cache, not_grouped
        )
    return await _finalize_ldap_user(db, group_dn, identity, tally, identity_cache, user)


async def _reconcile_mapping(
    db: AsyncSession,
    *,
    mapping_id: uuid.UUID,
    group_dn: str,
    herd_group_id: uuid.UUID,
    directory_name: str,
    tally: _Tally,
    identity_cache: dict[str, _RunIdentityOutcome],
    not_grouped: "auth_service.NotGroupedResolver",
    run_holder: "ldap_service.RunConnection | None" = None,
) -> set[uuid.UUID]:
    """Reconcile one mapped group; returns the desired_ids resolved this
    pass (the ADR 0011 phase 4 group-presence credit set), empty on any
    fail-closed skip. Accumulated by execute_run across all mappings so the
    deactivation sweep can treat "resolved as a member this run" as proof of
    presence without a second directory search.

    identity_cache and not_grouped are run-level state threaded through
    from execute_run (issue #513 items 2, 4, 6): identity_cache lets a user
    in multiple mapped groups resolve once for the whole run, and
    not_grouped lazily resolves and caches the "Not Grouped" group id so
    both this function's explicit adds and _ensure_ldap_user's provisioning
    path (via create_ldap_user's own internal auto-assign) agree on one
    value, resolved only once, on whichever of them needs it first.
    run_holder (item 2/3) is the run-scoped shared connection to reuse for
    fetch_group/resolve_members, explicitly passed rather than read from
    any module state.
    """
    try:
        entry = await ldap_service.fetch_group(group_dn, run_holder=run_holder)
    except ldap_service.LdapUnavailableError as exc:
        tally.skip_group(group_dn, GROUP_SKIP_DIRECTORY_UNAVAILABLE, str(exc))
        return set()
    if entry is None:
        # The directory answered: the DN resolves nothing (rename or OU
        # move). Fail-closed; the admin re-creates the mapping.
        tally.skip_group(group_dn, GROUP_SKIP_DANGLING_DN)
        return set()

    new_name = entry.name[:DIRECTORY_NAME_MAX]
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
        resolutions = await ldap_service.resolve_members(entry.member_dns, run_holder=run_holder)
    except ldap_service.LdapUnavailableError as exc:
        # A raise mid-batch discarded the whole batch; a half-resolved
        # desired set must never drive removals.
        tally.skip_group(group_dn, GROUP_SKIP_MEMBER_RESOLUTION_UNAVAILABLE, str(exc))
        return set()

    desired_ids: set[uuid.UUID] = set()
    unresolved_existing_count = 0
    proven_identities: list[ldap_service.LdapIdentity] = []
    for member_dn, resolution in zip(entry.member_dns, resolutions):
        if resolution.identity is None:
            # An answer the directory gave (entry gone, or present without
            # the attributes JIT needs): counted, does not fail the group.
            reason = resolution.skip_reason or "unknown"
            tally.skip_member(group_dn, member_dn, reason)
            if reason in _REMOVAL_SUPPRESSING_REASONS:
                unresolved_existing_count += 1
            continue
        proven_identities.append(resolution.identity)

    # Item 1: one batched WHERE email IN (...) per group instead of one
    # get_user_by_email SELECT per member (of plain UserSnapshot rows, see
    # auth_service.get_users_by_emails and _ensure_ldap_user's docstring
    # for why: an ORM instance here would be exposed to expiry by a LATER
    # member's rollback in this same loop). Emails already classified
    # earlier THIS RUN (item 2's memo) are excluded: _ensure_ldap_user will
    # hit the cache for them without ever consulting known_users, so
    # fetching them again here would be pure waste.
    emails_to_fetch = {
        identity.email for identity in proven_identities if identity.email not in identity_cache
    }
    known_users = await auth_service.get_users_by_emails(db, emails_to_fetch)

    member_dn_by_user_id: dict[uuid.UUID, str] = {}
    for identity in proven_identities:
        user_id = await _ensure_ldap_user(
            db, group_dn, identity, tally, known_users, identity_cache, not_grouped
        )
        if user_id is not None:
            desired_ids.add(user_id)
            member_dn_by_user_id.setdefault(user_id, identity.dn)

    current_ids = set(
        (
            await db.execute(
                select(GroupMember.user_id)
                .join(User, GroupMember.user_id == User.id)
                .where(
                    GroupMember.group_id == herd_group_id,
                    User.auth_source == "ldap",
                    # C5: an inactive user's existing membership is invisible
                    # to the diff, so it is never removed either.
                    User.is_active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )

    # Not_grouped resolved (or cache-hit) ONCE for the whole add loop
    # (issue #513 round-3 item 6), not once per add.
    not_grouped_id = await not_grouped.get(db)

    # Sorted for deterministic apply order (and deterministic detail).
    to_add = sorted(desired_ids - current_ids, key=str)
    still_active_ids: set[uuid.UUID] = set()
    if to_add:
        # Round-3 item 1: a cache-hit SUCCESS in identity_cache can be
        # stale by the time this specific group's add actually runs, if an
        # admin deactivated the user between an EARLIER group's
        # classification (which cached the success) and now; a fresh
        # classification would catch a since-deactivated user, but a cache
        # hit deliberately skips the DB round trip to avoid the very N+1
        # items 1/2 exist to kill. One extra batched live check here,
        # bounded by the number of ids actually about to be ADDED (never
        # the whole membership), closes that gap without reintroducing it.
        still_active_ids = set(
            (await db.execute(select(User.id).where(User.id.in_(to_add), User.is_active.is_(True))))
            .scalars()
            .all()
        )
    for user_id in to_add:
        if user_id not in still_active_ids:
            # Proven active when classified, proven inactive now: record
            # exactly like a fresh classification would have, but never
            # cache it (this is a runtime re-check, not identity_cache).
            tally.skip_member(
                group_dn,
                member_dn_by_user_id.get(user_id, str(user_id)),
                MEMBER_SKIP_USER_INACTIVE,
            )
            continue
        try:
            await group_service._add_member_resolved(db, herd_group_id, user_id, not_grouped_id)
            tally.members_added += 1
        except IntegrityError as exc:
            # C3: not every IntegrityError here is the benign concurrent
            # duplicate. A commit-time FK violation (the user or group
            # vanished mid-run) raises IntegrityError too, and silently
            # treating it as a no-op would void the add while the run
            # reports success. Disambiguate by proof: the row exists means
            # a concurrent admin already added it (benign); absent means
            # the add never landed (a genuine apply failure).
            await db.rollback()
            exists = (
                await db.execute(
                    select(GroupMember.user_id).where(
                        GroupMember.group_id == herd_group_id,
                        GroupMember.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                tally.op_failure(group_dn, "add", user_id, str(exc))
        except Exception as exc:
            await db.rollback()
            tally.op_failure(group_dn, "add", user_id, str(exc))

    removals = current_ids - desired_ids
    if unresolved_existing_count:
        # C4: the removal set for this group is unprovable this pass (an
        # unresolvable existing entry could be any current row), so the
        # remove pass does not run at all. Only record the suppression when
        # it would have mattered (a nonempty removal set); the run is
        # already degraded via the member skips above either way.
        if removals:
            tally.suppress_removals(group_dn, unresolved_existing_count, len(removals))
    else:
        for user_id in sorted(removals, key=str):
            try:
                if await group_service.remove_member(db, herd_group_id, user_id):
                    tally.members_removed += 1
            except Exception as exc:
                await db.rollback()
                tally.op_failure(group_dn, "remove", user_id, str(exc))

    return desired_ids


async def _run_deactivation_sweep(
    db: AsyncSession,
    tally: _Tally,
    credited_user_ids: set[uuid.UUID],
    run_holder: "ldap_service.RunConnection | None" = None,
) -> None:
    """ADR 0011 phase 4: absence-proof deactivation, disabled-filter, and the
    two-term circuit breaker, with an abort-exempt reactivation pass.

    Gated on settings.ldap_sync_deactivation_enabled: a disabled deployment
    is an unconditional no-op with no detail record (recording a "sweep"
    category here would mark every plain reconcile-only run "partial" for a
    feature the deployment never opted into).

    run_holder (issue #513 item 7): shares the run's connection with
    present_emails/disabled_emails exactly like _reconcile_mapping's
    fetch_group/resolve_members calls, so a run with mappings AND an
    enabled sweep pays only ONE connect+TLS+bind cycle for the whole run.
    """
    if not settings.ldap_sync_deactivation_enabled:
        return

    try:
        present = await ldap_service.present_emails(run_holder=run_holder)
        disabled = (
            await ldap_service.disabled_emails(run_holder=run_holder)
            if settings.ldap_disabled_filter
            else frozenset()
        )
    except (ldap_service.LdapUnavailableError, ValueError) as exc:
        # Error is never absence: reactivations are ALSO skipped here, since
        # presence was not proven either. Zero user changes this pass.
        tally.sweep_note(SWEEP_REASON_ENUMERATION_UNAVAILABLE, error=str(exc))
        return

    candidates = (await db.execute(select(User).where(User.auth_source == "ldap"))).scalars().all()
    swept = len(candidates)

    def present_for(user: User) -> bool:
        email = user.email.lower()
        # Group-presence credit counts as present but does NOT override the
        # disabled filter (checked LAST, so it can veto a credit-based
        # presence): a disabled account still listed in a mapped group must
        # still deactivate, matching the ADR's disabled-equals-proven-absent
        # rule. This ordering is load-bearing.
        credited_or_seen = user.id in credited_user_ids or email in present
        return credited_or_seen and email not in disabled

    to_deactivate = sorted(
        (u for u in candidates if u.is_active and not present_for(u)), key=lambda u: str(u.id)
    )
    to_reactivate = sorted(
        (u for u in candidates if not u.is_active and u.deactivated_by_sync and present_for(u)),
        key=lambda u: str(u.id),
    )

    # Breaker check BEFORE applying anything: neither list has been touched
    # yet. STRICT exceeds on both terms; boundary-equal never aborts.
    count = len(to_deactivate)
    percent = (count / swept * 100) if swept else 0.0
    aborted = count > (swept * settings.ldap_sync_deactivation_max_percent / 100) and (
        count > settings.ldap_sync_deactivation_min_count
    )

    if aborted:
        percent_display = round(percent, 2)
        tally.aborted = True
        tally.abort_error = (
            f"LDAP deactivation sweep aborted: {count} of {swept} candidates proven "
            f"absent or disabled ({percent_display}%), exceeding both "
            f"max_percent={settings.ldap_sync_deactivation_max_percent} and "
            f"min_count={settings.ldap_sync_deactivation_min_count}; no deactivations applied."
        )
        tally.sweep_note(
            SWEEP_REASON_BREAKER_TRIPPED, count=count, swept=swept, percent=percent_display
        )
    # Both directions apply as GUARDED updates (the #412 discipline): the
    # WHERE clause re-checks the gate at write time, so a concurrent admin
    # activate/deactivate on another replica (which the run advisory lock
    # does not cover) wins instead of being clobbered by this sweep's stale
    # in-memory snapshot. In particular a reactivation re-checks the
    # deactivated_by_sync provenance IN the update, making admin-outranks-
    # directory DB-enforced, not just snapshot-derived. Rows are recorded
    # in the tally only AFTER the commit succeeds and only when the
    # database reports the row actually changed: a failed commit must
    # never leave an audit row claiming flips that were rolled back.
    applied_deactivated, applied_reactivated = await _apply_sweep_flips(
        db,
        to_deactivate=[] if aborted else to_deactivate,
        to_reactivate=to_reactivate,
    )
    for user_id, email in applied_deactivated:
        tally.record_deactivated(user_id, email)
    for user_id, email in applied_reactivated:
        tally.record_reactivated(user_id, email)


async def _apply_sweep_flips(
    db: AsyncSession,
    *,
    to_deactivate: list[User],
    to_reactivate: list[User],
) -> tuple[list[tuple[uuid.UUID, str]], list[tuple[uuid.UUID, str]]]:
    """Apply sweep flag flips as guarded updates; return what actually landed.

    Reactivations are EXEMPT from the breaker (callers pass an empty
    to_deactivate on abort; the safe direction still applies).
    """
    applied_deactivated: list[tuple[uuid.UUID, str]] = []
    applied_reactivated: list[tuple[uuid.UUID, str]] = []
    for user in to_deactivate:
        result = await db.execute(
            update(User)
            .where(User.id == user.id, User.is_active.is_(True))
            .values(is_active=False, deactivated_by_sync=True)
        )
        if result.rowcount == 1:
            applied_deactivated.append((user.id, user.email))
    # The guarded WHERE re-excludes admin-deactivated rows
    # (deactivated_by_sync False) even if the row changed after this
    # sweep's snapshot was taken.
    for user in to_reactivate:
        result = await db.execute(
            update(User)
            .where(
                User.id == user.id,
                User.is_active.is_(False),
                User.deactivated_by_sync.is_(True),
            )
            .values(is_active=True, deactivated_by_sync=False)
        )
        if result.rowcount == 1:
            applied_reactivated.append((user.id, user.email))
    if applied_deactivated or applied_reactivated:
        # One commit for the whole sweep: simple column flips with no
        # cross-row invariants, unlike the per-op membership applies above.
        # A commit failure propagates BEFORE anything is recorded, so the
        # audit row can never claim flips that were rolled back.
        await db.commit()
    return applied_deactivated, applied_reactivated


async def execute_run(db: AsyncSession, run: LdapSyncRun) -> LdapSyncRun:
    """Execute the reconcile pass against an already-committed run row.

    Finalizes the row in a finally (finished_at is always set): "success"
    when nothing was skipped or failed, "partial" on any skip or per-op
    failure, "aborted" when the phase 4 circuit breaker tripped (overrides
    success/partial), "failed" only when the run-level machinery itself
    raised, in which case the exception is recorded and NOT re-raised (and
    takes precedence over "aborted" too, since it means something broke
    after the sweep already ran).

    A task cancellation (asyncio.CancelledError, e.g. the phase 5 interval
    loop's task being cancelled mid-tick during service shutdown) is the one
    exception to "not re-raised": CancelledError is a BaseException, not an
    Exception, so the except Exception arm below never catches it, and
    without a dedicated arm it would skip straight past this function's
    "failed runs record their cause" contract, committing status "failed"
    with error left None. The dedicated except records a fixed cause string
    and RE-RAISES (cancellation must keep propagating so the caller's
    task-cancel/await actually completes), while the finally below still
    runs first and commits the row exactly like any other failure: status
    "failed" (its default, since a mid-tick cancellation never reaches the
    success/partial/aborted assignment), with that cause string as the
    recorded error. No new status value: the five-valued vocabulary
    (running/success/partial/aborted/failed) is unchanged.
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
        # ADR 0011 phase 4: accumulate every user id resolved as a member by
        # any mapping this run, the group-presence credit set the
        # deactivation sweep below treats as proven present without a
        # second directory search.
        credited_user_ids: set[uuid.UUID] = set()
        # Item 2: memoizes _ensure_ldap_user's classification per email for
        # the whole run. Items 4/6: auth_service.NotGroupedResolver resolves "Not
        # Grouped" LAZILY, on whichever of _reconcile_mapping's explicit
        # adds or _ensure_ldap_user's provisioning calls needs it first, so
        # a run with zero adds and zero provisioning never queries for it
        # at all, and both paths agree on the SAME resolved value.
        identity_cache: dict[str, _RunIdentityOutcome] = {}
        not_grouped = auth_service.NotGroupedResolver()
        # Item 3/7: one shared connection for every fetch_group/
        # resolve_members/present_emails/disabled_emails call this run
        # instead of one connect+TLS+bind cycle per call, covering both the
        # mapping loop and the sweep. run_connection() itself is a no-op
        # (opens nothing, closes nothing) when neither ever gets called, so
        # a mapping-less run with the sweep disabled costs nothing extra;
        # no separate needs_connection branch is needed here (issue #513
        # round-3 item 2).
        async with ldap_service.run_connection() as run_holder:
            for mapping_id, group_dn, herd_group_id, directory_name in mappings:
                desired_ids = await _reconcile_mapping(
                    db,
                    mapping_id=mapping_id,
                    group_dn=group_dn,
                    herd_group_id=herd_group_id,
                    directory_name=directory_name,
                    tally=tally,
                    identity_cache=identity_cache,
                    not_grouped=not_grouped,
                    run_holder=run_holder,
                )
                credited_user_ids |= desired_ids
            await _run_deactivation_sweep(db, tally, credited_user_ids, run_holder=run_holder)
        status = "partial" if tally.degraded else "success"
        if tally.aborted:
            # Overrides success/partial; a later exception in this try block
            # (none currently possible after this point, but kept as the
            # documented ordering) would still win via the except branch.
            status = "aborted"
            error = tally.abort_error
    except asyncio.CancelledError:
        # Mirrors the Exception arm below: a cancel delivered between a
        # per-op db.add and its commit (e.g. mid _reconcile_mapping) leaves a
        # torn, uncommitted write on the session. Without this rollback the
        # finally's commit would flush that partial write together with the
        # run-row finalization, silently landing a half-applied membership
        # change alongside a "failed" run.
        await db.rollback()
        error = "cancelled during service shutdown"
        raise
    except Exception as exc:
        await db.rollback()
        error = str(exc)
        logger.exception(
            "LDAP sync run failed",
            extra={"action": "ldap_sync_run_failed", "run_id": str(run_id)},
        )
    finally:
        tally.apply_to(run)
        run.status = status
        run.error = error
        run.finished_at = datetime.now(timezone.utc)
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
                "users_deactivated": tally.users_deactivated,
                "users_reactivated": tally.users_reactivated,
            },
        )
    return run


async def create_run(db: AsyncSession, trigger: str) -> LdapSyncRun:
    """Create and commit a run row with status "running" (the crash-corpse
    invariant: a run row exists and is visible before any directory work
    starts). Both start_background_run and run_sync go through this single
    source so the guarantee cannot drift between the two callers."""
    run = LdapSyncRun(trigger=trigger, status="running")
    db.add(run)
    await db.commit()
    return run


# ---------------------------------------------------------------------------
# Run serialization (S1). One invariant, ONE implementation: a run is a run
# whether started from the router's sync-now endpoint, called directly
# (run_sync, used by the live tests and any script), or the phase 5 interval
# loop (app/tasks/ldap_sync_loop.py, started from main.py's lifespan). Two
# layers: an in-process
# asyncio.Lock (blocks a second call in THIS process) and, on Postgres only,
# a session-scoped advisory lock (blocks a second call on ANOTHER replica).
# SQLite (unit tests) has no advisory locks, so tests exercise only the
# asyncio-lock layer.
# ---------------------------------------------------------------------------

# Rolling-deploy invariant: this STRING constant, not the SQL used to send
# it to Postgres, is what must stay byte-identical across every build.
# Changing it would let two builds sync concurrently mid-rollout, since old
# and new replicas would then hold different advisory locks for what is
# meant to be one run invariant (see herd_common.advisory_lock's module
# docstring, issue #513 item 5).
_ADVISORY_LOCK_KEY = "herd_ldap_group_sync"

_sync_lock = asyncio.Lock()
# Strong references so a scheduled run is never garbage-collected mid-flight.
_background_tasks: set[asyncio.Task] = set()


class SyncBusyError(Exception):
    """Raised instead of acquiring the run-serialization slot when it is
    already held. .reason is "in_process" (this process is already running
    a sync) or "replica" (the Postgres advisory lock is held elsewhere);
    callers map these to their own presentation (the router to a 409)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"LDAP sync busy: {reason}")


async def _release_sync_locks(lock_conn: AsyncConnection | None) -> None:
    """Release the advisory lock (on the SAME connection that acquired it;
    advisory locks are session-scoped) and then the in-process lock.

    lock_conn is not None only when the advisory lock was acquired on it.
    C2: a failed unlock invalidates the connection instead of politely
    closing it. Pooled close() only rolls back and returns the connection to
    the pool, and a session-scoped advisory lock survives rollback, so a
    lock could ride an idle pooled connection until process restart.
    invalidate() is a hard DBAPI close that drops server-side session state,
    including the lock. The except is BaseException, not Exception, so a
    task cancellation arriving mid-unlock still triggers the invalidate
    before the cancellation propagates; anything that is not a plain
    Exception (cancellation, SystemExit, ...) is re-raised after cleanup so
    it is never swallowed. The asyncio lock release always happens in the
    outermost finally, regardless of how this function exits.
    """
    try:
        if lock_conn is not None:
            try:
                await advisory_lock.session_unlock(lock_conn, _ADVISORY_LOCK_KEY)
                await lock_conn.close()
            except BaseException as exc:
                logger.exception(
                    "advisory unlock failed; invalidating connection to drop the session lock",
                    extra={"action": "ldap_sync_advisory_unlock_failed"},
                )
                try:
                    await lock_conn.invalidate()
                except Exception:
                    logger.exception(
                        "sync lock connection invalidate failed",
                        extra={"action": "ldap_sync_lock_conn_invalidate_failed"},
                    )
                if not isinstance(exc, Exception):
                    raise
    finally:
        _sync_lock.release()


class _SyncSlot:
    """Async context manager for the run-serialization slot.

    __aenter__ raises SyncBusyError instead of acquiring when the slot is
    held, and releases anything it already acquired before raising (the
    in-process lock is only held between a successful asyncio.Lock.acquire
    and either a raised SyncBusyError or a caller-driven release). By
    default __aexit__ releases both locks; a caller handing the connection
    off to a background task calls hand_off() first so __aexit__ becomes a
    no-op and the background task's own finally owns the release instead.
    """

    def __init__(self) -> None:
        self.lock_conn: AsyncConnection | None = None
        self._handed_off = False

    async def __aenter__(self) -> "_SyncSlot":
        if _sync_lock.locked():
            raise SyncBusyError("in_process")
        # No await sits between the locked() check and this acquire, and an
        # uncontended asyncio.Lock.acquire completes without suspending, so
        # two rapid callers cannot both pass the check and both acquire.
        await _sync_lock.acquire()
        try:
            # Cross-replica layer, on a dedicated connection held for the
            # whole run because advisory locks are session-scoped: acquire
            # and release must happen on one connection.
            if advisory_lock.is_postgres_dialect(database.engine.dialect.name):
                self.lock_conn = await database.engine.connect()
                acquired = await advisory_lock.session_try_lock(self.lock_conn, _ADVISORY_LOCK_KEY)
                if not acquired:
                    # Invariant: lock_conn is not None only while the
                    # advisory lock is held on it, so release paths can
                    # unlock unconditionally. Not acquired: close and forget
                    # it first.
                    conn, self.lock_conn = self.lock_conn, None
                    await conn.close()
                    raise SyncBusyError("replica")
                # C1: pg_try_advisory_lock autobegins a transaction on this
                # connection; left open, it idles in transaction for the
                # whole run and a server-side idle_in_transaction_session_
                # timeout can kill it, silently dropping the cross-replica
                # lock mid-run. Session-level advisory locks survive
                # transaction end, so committing here is safe and required.
                await self.lock_conn.commit()
        except BaseException:
            # Every early exit from here (replica busy, connect failure,
            # cancellation) still owns the asyncio lock and must release
            # exactly what was acquired.
            await _release_sync_locks(self.lock_conn)
            raise
        return self

    def hand_off(self) -> None:
        self._handed_off = True

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if not self._handed_off:
            await _release_sync_locks(self.lock_conn)


async def _run_in_background(run_id: uuid.UUID, lock_conn: AsyncConnection | None) -> None:
    """Background half of start_background_run: owns both locks from the
    moment it is scheduled and must release them on every path.

    The pass runs on its own session (database.AsyncSessionLocal), never the
    caller's, which may already be closed by the time this executes.
    execute_run finalizes the run row itself (finished_at, status) even on
    failure; the except here is a belt for machinery outside it, so the task
    never ends with an unretrieved exception.
    """
    try:
        try:
            async with database.AsyncSessionLocal() as session:
                run = await session.get(LdapSyncRun, run_id)
                if run is None:
                    # The handler committed the row before scheduling; only
                    # external deletion explains this.
                    logger.error(
                        "sync run row not found for background execution",
                        extra={"action": "ldap_sync_run_missing", "run_id": str(run_id)},
                    )
                    return
                await execute_run(session, run)
        finally:
            await _release_sync_locks(lock_conn)
    except Exception:
        logger.exception(
            "LDAP sync background task failed",
            extra={"action": "ldap_sync_task_failed", "run_id": str(run_id)},
        )


async def start_background_run(trigger: str) -> uuid.UUID:
    """Acquire the run-serialization slot, create the run row, and schedule
    the reconcile pass as a background task that owns the slot from here on.

    Raises SyncBusyError (never an HTTP code; that mapping is the router's
    job) when the slot is already held, either in this process or (fail-safe
    fallback for the router) on another replica.
    """
    async with _SyncSlot() as slot:
        async with database.AsyncSessionLocal() as session:
            run = await create_run(session, trigger)
            run_id = run.id
        task = asyncio.create_task(_run_in_background(run_id, slot.lock_conn))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        # From here the task owns both locks and the lock connection.
        slot.hand_off()
        return run_id


async def run_sync(db: AsyncSession, *, trigger: str = "manual") -> LdapSyncRun:
    """Create a run row (committed first, so a crash leaves a visible
    "running" corpse) and execute the reconcile pass against it, serialized
    through the same run-invariant slot start_background_run uses (S1): the
    obvious direct-call API (used by the live tests and any script) is
    serialized exactly like the router's sync-now endpoint. On SQLite the
    advisory-lock layer is dialect-gated out, so tests exercise only the
    uncontended asyncio-lock layer and behave exactly as before."""
    async with _SyncSlot():
        run = await create_run(db, trigger)
        return await execute_run(db, run)
