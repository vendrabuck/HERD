"""Search and filters on GET / (issue #959).

The list takes search, status (repeatable), purpose_category (a configured category or
"none"), and a half-open time window (starts_after, starts_before, ends_after,
ends_before). Every filter is ANDed onto the caller's visibility clause, so the tests
here pin three things: each parameter's own semantics, that `total` is the filtered
total under every combination with sort and paging, and (the enumeration at the bottom)
that no combination of parameters ever shows a non-admin a row owned by someone else.

Rows are inserted directly against the harness session, as the sort tests in
test_reservations.py do, so each filter-relevant field lands exactly where the test
puts it.
"""

import itertools
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from app.models.reservation import Reservation, ReservationStatus
from app.services.reservation_service import _id_prefix_term
from herd_common.enums import TopologyType

from tests._harness import TestSessionLocal

USER_A = str(uuid.uuid4())
USER_B = str(uuid.uuid4())
NOW = datetime(2030, 6, 1, 12, 0, tzinfo=timezone.utc)

A_USER = {"sub": USER_A, "username": "alice", "role": "user"}
B_USER = {"sub": USER_B, "username": "bob", "role": "user"}
A_ADMIN = {"sub": USER_A, "username": "alice", "role": "admin"}


@dataclass
class Row:
    id: str
    owner: str
    purpose: str | None
    status: ReservationStatus
    purpose_category: str | None
    start: datetime
    end: datetime


async def _seed(
    owner: str,
    *,
    purpose: str | None = "seeded",
    status: ReservationStatus = ReservationStatus.ACTIVE,
    purpose_category: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    rid: uuid.UUID | None = None,
) -> Row:
    start = start or NOW + timedelta(hours=1)
    end = end or start + timedelta(hours=2)
    async with TestSessionLocal() as db:
        res = Reservation(
            id=rid or uuid.uuid4(),
            user_id=uuid.UUID(owner),
            device_ids=[str(uuid.uuid4())],
            topology_type=TopologyType.PHYSICAL,
            purpose=purpose,
            purpose_category=purpose_category,
            start_time=start,
            end_time=end,
            status=status,
        )
        db.add(res)
        await db.commit()
        await db.refresh(res)
        return Row(str(res.id), owner, purpose, status, purpose_category, start, end)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _ids(resp) -> set[str]:
    assert resp.status_code == 200, resp.text
    return {r["id"] for r in resp.json()["items"]}


# --- _id_prefix_term (pure) ---


@pytest.mark.parametrize(
    ("term", "expected"),
    [
        ("1234abcd", "1234abcd"),
        ("1234ABCD", "1234abcd"),
        ("1234abcd-ef01", "1234abcdef01"),
        ("1234abc", None),  # seven hex digits: too short to be the short form
        ("deadbeefx", None),  # not hex
        ("bad", None),
        ("", None),
        ("0" * 33, None),  # longer than a uuid
        ("12345678-1234-1234-1234-123456789abc", "12345678123412341234123456789abc"),
    ],
)
def test_id_prefix_term(term, expected):
    assert _id_prefix_term(term) == expected


# --- search ---


async def test_search_is_case_insensitive_substring_on_purpose(make_client):
    hit = await _seed(USER_A, purpose="Lab Regression Run")
    await _seed(USER_A, purpose="customer demo")
    await _seed(USER_A, purpose=None)
    async with make_client(A_USER) as c:
        resp = await c.get("/", params={"search": "regression"})
        assert _ids(resp) == {hit.id}
        assert resp.json()["total"] == 1
        assert _ids(await c.get("/", params={"search": "LAB REG"})) == {hit.id}


async def test_search_treats_like_metacharacters_literally(make_client):
    pct = await _seed(USER_A, purpose="load 100% now")
    await _seed(USER_A, purpose="load 1000 now")
    under = await _seed(USER_A, purpose="a_b")
    await _seed(USER_A, purpose="axb")
    async with make_client(A_USER) as c:
        assert _ids(await c.get("/", params={"search": "100%"})) == {pct.id}
        assert _ids(await c.get("/", params={"search": "a_b"})) == {under.id}


async def test_blank_search_is_no_filter(make_client):
    rows = [await _seed(USER_A, purpose=p) for p in ("one", "two")]
    async with make_client(A_USER) as c:
        for term in ("", "   "):
            resp = await c.get("/", params={"search": term})
            assert _ids(resp) == {r.id for r in rows}


async def test_search_too_long_is_422(make_client):
    async with make_client(A_USER) as c:
        resp = await c.get("/", params={"search": "x" * 201})
    assert resp.status_code == 422


async def test_search_matches_short_id_prefix_and_full_id(make_client):
    target = await _seed(USER_A, rid=uuid.UUID("1234abcd-0000-4000-8000-000000000001"))
    await _seed(USER_A, rid=uuid.UUID("99990000-0000-4000-8000-000000000002"))
    async with make_client(A_USER) as c:
        assert _ids(await c.get("/", params={"search": "1234abcd"})) == {target.id}
        assert _ids(await c.get("/", params={"search": "1234ABCD"})) == {target.id}
        assert _ids(await c.get("/", params={"search": target.id})) == {target.id}
        assert _ids(await c.get("/", params={"search": "1234abcd-0000"})) == {target.id}
        # Seven hex digits is a purpose-only search: no purpose contains it.
        assert _ids(await c.get("/", params={"search": "1234abc"})) == set()


async def test_search_by_id_never_reveals_another_users_row(make_client):
    foreign = await _seed(USER_B, rid=uuid.UUID("abcdef12-0000-4000-8000-000000000003"))
    async with make_client(A_USER) as c:
        resp = await c.get("/", params={"search": foreign.id[:8]})
        assert _ids(resp) == set()
        assert resp.json()["total"] == 0
        resp = await c.get("/", params={"search": foreign.id})
        assert _ids(resp) == set()


# --- status ---


async def test_status_single_and_repeated(make_client):
    by_status = {st: await _seed(USER_A, status=st) for st in ReservationStatus}
    async with make_client(A_USER) as c:
        for st, row in by_status.items():
            resp = await c.get("/", params={"status": st.value})
            assert _ids(resp) == {row.id}
            assert resp.json()["total"] == 1
        resp = await c.get(
            "/",
            params=[("status", "ACTIVE"), ("status", "CANCELLED"), ("status", "PENDING")],
        )
        assert _ids(resp) == {
            by_status[ReservationStatus.ACTIVE].id,
            by_status[ReservationStatus.CANCELLED].id,
            by_status[ReservationStatus.PENDING].id,
        }
        assert resp.json()["total"] == 3


@pytest.mark.parametrize("bad", ["active", "BOGUS", ""])
async def test_status_unknown_value_is_422(make_client, bad):
    async with make_client(A_USER) as c:
        resp = await c.get("/", params={"status": bad})
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["query", "status", 0]


# --- purpose_category ---


async def test_purpose_category_value_and_none(make_client):
    qa = await _seed(USER_A, purpose_category="qa_regression")
    await _seed(USER_A, purpose_category="training")
    blank = await _seed(USER_A, purpose_category=None)
    async with make_client(A_USER) as c:
        assert _ids(await c.get("/", params={"purpose_category": "qa_regression"})) == {qa.id}
        assert _ids(await c.get("/", params={"purpose_category": "none"})) == {blank.id}


@pytest.mark.parametrize("bad", ["not_a_category", "QA_REGRESSION", ""])
async def test_purpose_category_unknown_is_422_with_pinned_message(make_client, bad):
    async with make_client(A_USER) as c:
        resp = await c.get("/", params={"purpose_category": bad})
    assert resp.status_code == 422
    assert resp.json()["detail"].startswith(f"Unknown purpose_category '{bad}'; allowed: ")


async def test_purpose_category_dropped_from_taxonomy_is_422(make_client, monkeypatch):
    """A row keeps a category later dropped from the list, but the filter refuses it:
    the decision is 422, not a silently empty or silently matching page."""
    from app.config import settings

    await _seed(USER_A, purpose_category="legacy_cat")
    monkeypatch.setattr(settings, "purpose_categories", ["qa_regression"])
    async with make_client(A_USER) as c:
        resp = await c.get("/", params={"purpose_category": "legacy_cat"})
    assert resp.status_code == 422


# --- time window ---


async def test_window_bounds_are_half_open(make_client):
    at = await _seed(USER_A, start=NOW, end=NOW + timedelta(hours=1))
    async with make_client(A_USER) as c:
        # *_after is inclusive, *_before is exclusive.
        assert _ids(await c.get("/", params={"starts_after": _iso(NOW)})) == {at.id}
        assert _ids(await c.get("/", params={"starts_before": _iso(NOW)})) == set()
        end = NOW + timedelta(hours=1)
        assert _ids(await c.get("/", params={"ends_after": _iso(end)})) == {at.id}
        assert _ids(await c.get("/", params={"ends_before": _iso(end)})) == set()


async def test_window_bound_with_offset_is_compared_by_instant(make_client):
    row = await _seed(USER_A, start=NOW, end=NOW + timedelta(hours=1))
    plus5 = timezone(timedelta(hours=5))
    # NOW expressed at +05:00, one minute later: excludes the row on starts_after.
    later = (NOW + timedelta(minutes=1)).astimezone(plus5)
    earlier = (NOW - timedelta(minutes=1)).astimezone(plus5)
    async with make_client(A_USER) as c:
        assert _ids(await c.get("/", params={"starts_after": later.isoformat()})) == set()
        assert _ids(await c.get("/", params={"starts_after": earlier.isoformat()})) == {row.id}


@pytest.mark.parametrize("param", ["starts_after", "starts_before", "ends_after", "ends_before"])
@pytest.mark.parametrize("bad", ["2030-06-01T12:00:00", "yesterday", "2030-13-01T00:00:00Z"])
async def test_window_bad_or_naive_timestamp_is_422(make_client, param, bad):
    async with make_client(A_USER) as c:
        resp = await c.get("/", params={param: bad})
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["query", param]


async def test_period_views_partition_the_rows(make_client):
    """The frontend's upcoming, current, and past views, computed at one instant,
    are disjoint and together cover every row."""
    past = await _seed(USER_A, start=NOW - timedelta(hours=3), end=NOW - timedelta(hours=1))
    current = await _seed(USER_A, start=NOW - timedelta(hours=1), end=NOW + timedelta(hours=1))
    ends_now = await _seed(USER_A, start=NOW - timedelta(hours=1), end=NOW)
    starts_now = await _seed(USER_A, start=NOW, end=NOW + timedelta(hours=1))
    upcoming = await _seed(USER_A, start=NOW + timedelta(hours=1), end=NOW + timedelta(hours=2))
    n = _iso(NOW)
    async with make_client(A_USER) as c:
        up = _ids(await c.get("/", params={"starts_after": n}))
        cur = _ids(await c.get("/", params={"starts_before": n, "ends_after": n}))
        pst = _ids(await c.get("/", params={"ends_before": n}))
    assert up == {starts_now.id, upcoming.id}
    assert cur == {current.id, ends_now.id}
    assert pst == {past.id}
    assert up | cur | pst == {r.id for r in (past, current, ends_now, starts_now, upcoming)}


# --- composition: total, paging, sort, all ---


async def test_total_is_filtered_total_across_pages_and_sort(make_client):
    hits = [
        await _seed(USER_A, purpose=f"match {i}", start=NOW + timedelta(hours=i)) for i in range(5)
    ]
    for i in range(4):
        await _seed(USER_A, purpose=f"other {i}", start=NOW + timedelta(hours=i))
    async with make_client(A_USER) as c:
        seen: list[str] = []
        for skip in (0, 2, 4):
            resp = await c.get(
                "/",
                params={
                    "search": "match",
                    "sort_by": "start_time",
                    "sort_dir": "asc",
                    "skip": skip,
                    "limit": 2,
                },
            )
            assert resp.status_code == 200
            assert resp.json()["total"] == 5
            seen += [r["id"] for r in resp.json()["items"]]
    assert seen == [h.id for h in hits]


async def test_filters_compose_with_admin_all(make_client):
    a = await _seed(USER_A, purpose="shared", status=ReservationStatus.ACTIVE)
    b = await _seed(USER_B, purpose="shared", status=ReservationStatus.ACTIVE)
    await _seed(USER_B, purpose="shared", status=ReservationStatus.CANCELLED)
    async with make_client(A_ADMIN) as c:
        resp = await c.get("/", params={"all": "true", "search": "shared", "status": "ACTIVE"})
        assert _ids(resp) == {a.id, b.id}
        assert resp.json()["total"] == 2
        # Without all, the admin's own scoped view still applies.
        own = await c.get("/", params={"search": "shared", "status": "ACTIVE"})
        assert _ids(own) == {a.id}


@pytest.mark.parametrize(
    "params",
    [
        {"search": "x"},
        {"status": "ACTIVE"},
        {"purpose_category": "none"},
        {"starts_after": NOW.isoformat()},
        {"ends_before": NOW.isoformat()},
    ],
)
async def test_all_stays_admin_only_with_any_filter(make_client, params):
    async with make_client(A_USER) as c:
        resp = await c.get("/", params={"all": "true", **params})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Only admins can list all reservations"


# --- visibility enumeration ---


async def _two_user_fixture() -> list[Row]:
    rows: list[Row] = []
    specs = [
        ("shared alpha", ReservationStatus.ACTIVE, "qa_regression", -1, 1),
        ("shared alpha", ReservationStatus.CANCELLED, None, 2, 3),
        ("alpha only", ReservationStatus.PENDING, "training", 4, 6),
        ("Shared Alpha", ReservationStatus.COMPLETED, None, -6, -4),
    ]
    for owner, word in ((USER_A, "alpha"), (USER_B, "bravo")):
        for purpose, st, cat, s_off, e_off in specs:
            rows.append(
                await _seed(
                    owner,
                    purpose=purpose.replace("alpha", word).replace("Alpha", word.title()),
                    status=st,
                    purpose_category=cat,
                    start=NOW + timedelta(hours=s_off),
                    end=NOW + timedelta(hours=e_off),
                )
            )
    return rows


def _expected(rows: list[Row], search, statuses, category, window) -> set[str]:
    out = set()
    for r in rows:
        if search:
            prefix = _id_prefix_term(search)
            in_purpose = r.purpose is not None and search.lower() in r.purpose.lower()
            in_id = prefix is not None and r.id.replace("-", "").startswith(prefix)
            if not (in_purpose or in_id):
                continue
        if statuses and r.status not in statuses:
            continue
        if category == "none" and r.purpose_category is not None:
            continue
        if category not in (None, "none") and r.purpose_category != category:
            continue
        sa, sb, ea, eb = window
        if sa and not r.start >= sa:
            continue
        if sb and not r.start < sb:
            continue
        if ea and not r.end >= ea:
            continue
        if eb and not r.end < eb:
            continue
        out.add(r.id)
    return out


WINDOWS = {
    "any": (None, None, None, None),
    "upcoming": (NOW, None, None, None),
    "current": (None, NOW, NOW, None),
    "past": (None, None, None, NOW),
}
STATUS_SETS = [
    (),
    (ReservationStatus.ACTIVE,),
    (ReservationStatus.ACTIVE, ReservationStatus.PENDING),
]
CATEGORIES = [None, "none", "qa_regression"]
SORTS = [("created_at", "desc"), ("status", "asc"), ("start_time", "desc")]


def _params(search, statuses, category, window_name, sort, all_flag):
    p: list[tuple[str, str]] = []
    if search is not None:
        p.append(("search", search))
    p += [("status", s.value) for s in statuses]
    if category is not None:
        p.append(("purpose_category", category))
    names = ("starts_after", "starts_before", "ends_after", "ends_before")
    for name, value in zip(names, WINDOWS[window_name]):
        if value is not None:
            p.append((name, value.isoformat()))
    p += [("sort_by", sort[0]), ("sort_dir", sort[1])]
    if all_flag:
        p.append(("all", "true"))
    return p


async def test_visibility_enumeration_non_admin_never_sees_foreign_rows(make_client):
    """Every combination of search, status, category, and window, for a non-admin:
    the page holds only the caller's own rows, it equals the expected filtered set,
    `total` equals its size, and all=true is the same 403 under every combination."""
    rows = await _two_user_fixture()
    a_rows = [r for r in rows if r.owner == USER_A]
    b_rows = [r for r in rows if r.owner == USER_B]
    searches = [None, "shared", "bravo", "ALPHA", b_rows[0].id[:8], a_rows[0].id[:8], b_rows[1].id]
    a_ids = {r.id for r in a_rows}
    combos = list(itertools.product(searches, STATUS_SETS, CATEGORIES, WINDOWS))
    assert len(combos) == 252
    async with make_client(A_USER) as c:
        for i, (search, statuses, category, window) in enumerate(combos):
            sort = SORTS[i % len(SORTS)]
            resp = await c.get("/", params=_params(search, statuses, category, window, sort, False))
            got = _ids(resp)
            assert got <= a_ids, (search, statuses, category, window)
            assert got == _expected(a_rows, search, statuses, category, WINDOWS[window])
            assert resp.json()["total"] == len(got)
            denied_params = _params(search, statuses, category, window, sort, True)
            denied = await c.get("/", params=denied_params)
            assert denied.status_code == 403
            assert denied.json()["detail"] == "Only admins can list all reservations"


async def test_visibility_enumeration_admin_all_sees_both_users(make_client):
    """The admin all-view applies the same filters across every owner, and without
    all the admin's own view is scoped exactly like a non-admin's."""
    rows = await _two_user_fixture()
    a_rows = [r for r in rows if r.owner == USER_A]
    searches = [None, "shared", "bravo", rows[-1].id[:8]]
    combos = list(itertools.product(searches, STATUS_SETS, CATEGORIES, WINDOWS))
    async with make_client(A_ADMIN) as c:
        for i, (search, statuses, category, window) in enumerate(combos):
            sort = SORTS[i % len(SORTS)]
            every = await c.get("/", params=_params(search, statuses, category, window, sort, True))
            got = _ids(every)
            assert got == _expected(rows, search, statuses, category, WINDOWS[window])
            assert every.json()["total"] == len(got)
            own = await c.get("/", params=_params(search, statuses, category, window, sort, False))
            assert _ids(own) == _expected(a_rows, search, statuses, category, WINDOWS[window])
