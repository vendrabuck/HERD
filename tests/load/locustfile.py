"""Locust load test definitions for HERD.

Nine user classes, selected by default:
- ReservationUser: creates, lists, queries calendar, releases reservations
- InventoryBrowser: browses devices, templates, and the topologies list
- BulkExporter: exports devices, templates, and topologies; dry-runs a device import
- ACLChecker: batch checks permissions
- RoutedTopologyValidator: repeatedly validates one topology carrying Layer 3
  routing intent (ADR 0014, issue #34 phase 3), a safe no-op when the seeded
  pool has no Layer 3 Switch device
- NotificationUser: polls notifications, updates channel/event preferences,
  and edits its own profile name (issue #1252), restoring it at the end
- BulkConnectionAdmin: posts small POST /connections/bulk batches, then deletes
  what it created
- DeviceConfigUser: writes and reads config versions, the driver config schema,
  and the apply-job list on one device of its own, deleted at the end

- LoginLimitProbe: exactly one user that, once every other user has signed in,
  sends wrong-password logins from this host until auth's per-source attempt
  limit (IAM-LOGIN-7) answers 429, and fails the run on any other answer

Every user signs in once, at start, and every one does so from this host, which
auth sees as one source address with one attempt allowance (60 at once by
default, refilling one a second). `_login` therefore treats a 429 as the limit
working, not as a failure: it records it under its own name, waits the
`Retry-After`, and tries again, so the login figures under `/api/auth/login`
stay those of real password checks.

A tenth, OidcLoginUser (ADR 0016 phase 5b), signs in through the OIDC test
provider; it is abstract unless HERD_LOAD_OIDC=1 and runs only in the
Makefile's _gate-oidc-stack-tests phase, against a stack in OIDC mode (see its
section at the end of this file).

Note on the outbound notification channels and expiry reminder (ROADMAP #40):
the dispatch side is a NATS consumer with no synchronous request surface, so it
cannot be driven directly by an HTTP load tool. Throughput there is bounded by
NATS JetStream redelivery and the per-message DB writes, and is covered by the
unit dedupe tests rather than load. What this load class exercises is the
user-facing surface the feature added: the notification list/unread-count read
path (polled by the bell on an interval, so the hottest read in the service) and
the preferences read/write proxy that the /settings channel toggles drive.
"""

import logging
import os
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import gevent
import urllib3
from locust import HttpUser, between, constant, events, task
from locust.clients import HttpSession

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)


def _load_repo_env() -> None:
    """Populate os.environ from the repo-root .env, matching docker-compose.

    docker-compose auto-loads .env when bringing up the stack, but locust
    invoked directly does not. Without this, the seeded admin (from .env
    SUPERADMIN_*) and the credentials this locustfile tries to log in
    with can disagree. Existing env vars win so callers can still
    override per-run.
    """
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_repo_env()

# Credentials. Fall back through SEED_* (set by seed_devices.py and the
# Makefile _everything-seed target) to SUPERADMIN_* (what the stack
# actually seeded) to a generic default.
ADMIN_EMAIL = os.getenv("SEED_EMAIL") or os.getenv("SUPERADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.getenv("SEED_PASSWORD") or os.getenv("SUPERADMIN_PASSWORD", "admin123!")
USER_EMAIL = os.getenv("SEED_USER_EMAIL", "user1@herd.dev")
USER_PASSWORD = os.getenv("SEED_USER_PASSWORD", "user1user1xx")


# A login auth refused for the per-source attempt limit (IAM-LOGIN-7) is
# recorded under this name, apart from the logins it judged.
LOGIN_LIMITED_NAME = "/api/auth/login [429 attempt limit]"
LOGIN_ATTEMPTS = 20
LOGIN_RETRY_AFTER_CAP_SECONDS = 30


def _retry_after_seconds(resp) -> int | None:
    """The whole seconds a 429's Retry-After states, or None when it states none."""
    raw = (resp.headers.get("Retry-After") or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


def _login_limited(resp) -> int | None:
    """For a 429 with a whole-second Retry-After: record it under
    LOGIN_LIMITED_NAME as a success (the limit doing its job) and return the
    wait. Anything else: None, and the response is judged as usual."""
    if resp.status_code != 429:
        return None
    wait = _retry_after_seconds(resp)
    if wait is None:
        resp.failure("429 without a whole-second Retry-After")
        return None
    resp.request_meta["name"] = LOGIN_LIMITED_NAME
    resp.success()
    return wait


class HerdUser(HttpUser):
    """Base class with login-on-start and TLS bypass."""

    abstract = True

    def _login(self, email: str, password: str) -> None:
        self.access_token = None
        self.headers = {}
        for _ in range(LOGIN_ATTEMPTS):
            with self.client.post(
                "/api/auth/login",
                json={"email": email, "password": password},
                verify=False,
                catch_response=True,
            ) as resp:
                wait = _login_limited(resp)
                if wait is None:
                    if resp.status_code == 200:
                        tokens = resp.json()
                        self.access_token = tokens["access_token"]
                        self.headers = {"Authorization": f"Bearer {self.access_token}"}
                    return
            gevent.sleep(min(wait, LOGIN_RETRY_AFTER_CAP_SECONDS))

    def _auth_get(self, path: str, **kwargs):
        return self.client.get(path, headers=self.headers, verify=False, **kwargs)

    def _auth_post(self, path: str, **kwargs):
        return self.client.post(path, headers=self.headers, verify=False, **kwargs)

    def _auth_put(self, path: str, **kwargs):
        return self.client.put(path, headers=self.headers, verify=False, **kwargs)

    def _auth_delete(self, path: str, **kwargs):
        return self.client.delete(path, headers=self.headers, verify=False, **kwargs)

    def _auth_patch(self, path: str, **kwargs):
        return self.client.patch(path, headers=self.headers, verify=False, **kwargs)


class ReservationUser(HerdUser):
    """Simulates users creating and managing reservations."""

    weight = 3
    wait_time = between(1, 3)

    def on_start(self):
        self._login(ADMIN_EMAIL, ADMIN_PASSWORD)
        self._device_ids = []
        self._reservation_ids = []
        # Cache available device IDs
        resp = self._auth_get("/api/inventory/devices")
        if resp.status_code == 200:
            devices = resp.json()["items"]
            self._device_ids = [
                d["id"] for d in devices if d["status"] == "AVAILABLE" and d.get("exclusive", True)
            ]

    @task(3)
    def list_reservations(self):
        self._auth_get("/api/reservations/")

    @task(2)
    def query_calendar(self):
        now = datetime.now(timezone.utc)
        self._auth_get(
            "/api/reservations/calendar",
            params={
                "range_start": (now - timedelta(hours=12)).isoformat(),
                "range_end": (now + timedelta(hours=12)).isoformat(),
            },
        )

    @task(1)
    def create_and_release(self):
        if not self._device_ids:
            return
        # Spread bookings across devices rather than hammering the first one, so
        # this measures real throughput instead of self-inflicted contention on a
        # single exclusive device.
        device_id = random.choice(self._device_ids)
        now = datetime.now(timezone.utc)
        with self._auth_post(
            "/api/reservations/",
            name="/api/reservations/ [create]",
            catch_response=True,
            json={
                "device_ids": [device_id],
                "purpose": f"load-test-{uuid.uuid4().hex[:8]}",
                "start_time": now.isoformat(),
                "end_time": (now + timedelta(minutes=5)).isoformat(),
                # Issue #646 phase 1: exercise the classification write path
                # under load too, not just the unclassified default.
                "purpose_category": "qa_regression",
            },
        ) as resp:
            # 409 (time-window conflict) and 422 (availability precheck) are the
            # legitimate, expected outcomes when concurrent virtual users contend
            # for the same exclusive device: the reservations service returns
            # either, depending on which guard wins the race (see
            # tests/integration/test_reservation_lifecycle.py, which asserts the
            # second booking lands in (409, 422)). Count them as load, not
            # failures; only an unexpected status is a real problem.
            if resp.status_code in (201, 409, 422):
                resp.success()
            else:
                resp.failure(f"unexpected {resp.status_code}: {resp.text[:200]}")
                return
            if resp.status_code != 201:
                return
            res_id = resp.json()["id"]
        self._auth_put(
            f"/api/reservations/{res_id}/release",
            name="/api/reservations/[id]/release",
        )


class InventoryBrowser(HerdUser):
    """Simulates users browsing inventory."""

    weight = 5
    wait_time = between(1, 2)

    def on_start(self):
        self._login(ADMIN_EMAIL, ADMIN_PASSWORD)
        self._device_ids = []
        resp = self._auth_get("/api/inventory/devices")
        if resp.status_code == 200:
            self._device_ids = [d["id"] for d in resp.json()["items"]]

    @task(5)
    def list_devices(self):
        self._auth_get("/api/inventory/devices")

    @task(3)
    def get_device_detail(self):
        if self._device_ids:
            device_id = self._device_ids[0]
            self._auth_get(f"/api/inventory/devices/{device_id}")

    @task(2)
    def list_templates(self):
        self._auth_get("/api/inventory/templates")

    @task(2)
    def list_topologies(self):
        # The Topologies page is the landing page after login; its default view.
        self._auth_get("/api/cabling/topologies", params={"limit": 50})

    @task(1)
    def search_topologies(self):
        # The list controls (issue #958): a filtered, counted, text-sorted query.
        self._auth_get(
            "/api/cabling/topologies",
            params={
                "search": random.choice(["lab", "demo", "topo", "x"]),
                "owner": random.choice(["all", "mine"]),
                "sort_by": random.choice(["name", "owner_name", "created_at", "updated_at"]),
                "sort_dir": random.choice(["asc", "desc"]),
                "limit": 50,
            },
            # One stats row for every parameter combination.
            name="/api/cabling/topologies [search, owner, sort]",
        )


class BulkExporter(HerdUser):
    """Simulates operators exporting inventory and topologies.

    Export is the read-heavy half of bulk import/export and the path most
    likely to be hit repeatedly during a migration. Import is intentionally not
    load-tested here: it writes rows and would pollute the stack's data set on
    every iteration. The exports below serialize the full device, template, and
    topology sets, so they are a useful stress signal for the serialization
    path under concurrency.
    """

    weight = 1
    wait_time = between(2, 5)

    def on_start(self):
        self._login(ADMIN_EMAIL, ADMIN_PASSWORD)

    @task(3)
    def export_devices_json(self):
        self._auth_get("/api/inventory/devices/export", params={"format": "json"})

    @task(2)
    def export_devices_csv(self):
        self._auth_get("/api/inventory/devices/export", params={"format": "csv"})

    @task(2)
    def export_templates_json(self):
        self._auth_get("/api/inventory/templates/export", params={"format": "json"})

    @task(2)
    def export_topologies_json(self):
        self._auth_get("/api/cabling/topologies/export", params={"format": "json"})

    @task(1)
    def dry_run_device_import(self):
        # Dry-run writes nothing, so it is safe to repeat under load while still
        # exercising the parse, validate, and reference-resolution path.
        body = (
            '[{"name": "load-dry", "template_name": "no-such-template", '
            '"topology_type": "PHYSICAL", "status": "AVAILABLE", "field_data": {}}]'
        )
        self._auth_post(
            "/api/inventory/devices/import",
            params={"format": "json", "dry_run": "true"},
            files={"file": ("d.json", body, "application/json")},
        )


class RoutedTopologyValidator(HerdUser):
    """Exercises the L3 routing-intent validate pass under load (ADR 0014's
    Testing section promise; issue #34 phase 3 is when a slow validate first has
    a driven consequence, since execution now actually applies what validate
    approved).

    on_start looks for an existing Layer 3 Switch device among the seeded device
    pool and, if found, creates ONE small persistent topology wiring a DUT to it
    with one `data.l3` route (the shape validate parses, batch-fetches inventory
    for, and judges on every call), then every task iteration re-validates that
    same topology. If no Layer 3 Switch device is present (a stack seeded without
    `SEED_FRR=1`), the task is a safe no-op: this class deliberately never
    creates its own driver/template/device, since doing that per simulated user
    would pollute the stack the way BulkExporter's docstring warns import would.
    The route need not resolve `valid: true` to be useful load: an unconfigured
    or unattached switch still exercises the same inventory batch-fetch and
    per-route judgement path, just landing a different `invalid_routes` reason.
    """

    weight = 1
    wait_time = between(2, 5)

    def on_start(self):
        self._login(ADMIN_EMAIL, ADMIN_PASSWORD)
        self._topology_id = None

        resp = self._auth_get("/api/inventory/devices", params={"limit": 200})
        if resp.status_code != 200:
            return
        items = resp.json().get("items", [])
        switch = next((d for d in items if d.get("connection_type") == "Layer 3 Switch"), None)
        if switch is None:
            return
        dut = next((d for d in items if d["id"] != switch["id"]), None)
        if dut is None:
            return

        canvas = {
            "nodes": [
                {"id": "nDut", "data": {"device": {"id": dut["id"]}}},
                {
                    "id": "nSwitch",
                    "data": {
                        "device": {"id": switch["id"]},
                        "l3": {
                            "routes": [
                                {
                                    "destination": "10.250.0.0/24",
                                    "next_hop": "10.250.0.1",
                                    "interface": "eth0",
                                }
                            ]
                        },
                    },
                },
            ],
            "edges": [
                {
                    "id": "e1",
                    "source": "nDut",
                    "target": "nSwitch",
                    "data": {"layer": "L1", "isProposal": False},
                }
            ],
        }
        create = self._auth_post(
            "/api/cabling/topologies", json={"name": f"load-l3-validate-{uuid.uuid4().hex[:8]}"}
        )
        if create.status_code != 201:
            return
        self._topology_id = create.json()["id"]
        self._auth_put(f"/api/cabling/topologies/{self._topology_id}", json={"canvas_data": canvas})

    def on_stop(self):
        if self._topology_id:
            self._auth_delete(f"/api/cabling/topologies/{self._topology_id}")

    @task
    def validate_routed_topology(self):
        if not self._topology_id:
            return
        self._auth_post(f"/api/cabling/topologies/{self._topology_id}/validate")


class NotificationUser(HerdUser):
    """Simulates the notification read path and preference updates.

    The bell polls unread-count and the list on an interval, so those reads are
    the hottest in the notifications service; the preferences proxy is the write
    path the /settings channel toggles drive. Outbound channels default off, so
    flipping them here only round-trips the preference, no transport fires.
    """

    weight = 2
    wait_time = between(1, 3)

    # The profile task (issue #1252) alternates the shared load account's
    # full_name between its original value and this marker. Several instances
    # share one account, so an instance that starts while another holds the
    # marker takes the username (the default name) as the original instead.
    LOAD_PROFILE_NAME = "HERD load test profile"

    def on_start(self):
        self._login(USER_EMAIL, USER_PASSWORD)
        self._toggle = False
        self._profile_flip = False
        self._original_full_name = None
        resp = self._auth_get("/api/auth/me")
        if resp.status_code == 200:
            me = resp.json()
            name = me.get("full_name")
            self._original_full_name = (
                me.get("username") if not name or name == self.LOAD_PROFILE_NAME else name
            )

    def on_stop(self):
        if self._original_full_name:
            self._auth_patch("/api/auth/me/profile", json={"full_name": self._original_full_name})

    @task(5)
    def unread_count(self):
        self._auth_get("/api/notifications/notifications/unread-count")

    @task(3)
    def list_notifications(self):
        self._auth_get(
            "/api/notifications/notifications",
            params={"limit": 50, "offset": 0},
        )

    @task(1)
    def update_preferences(self):
        # Alternate the email channel opt-in so each call is a real write.
        self._toggle = not self._toggle
        self._auth_put(
            "/api/notifications/notifications/preferences",
            json={
                "channels": {
                    "in_app": True,
                    "email": self._toggle,
                    "chat": False,
                    "webhook": False,
                },
                "events": {"reservation.expiring_soon": True},
            },
        )

    @task(1)
    def update_profile(self):
        # The Settings page's profile write: read /me, then PATCH the caller's
        # own profile, alternating between two names so each call is a real
        # write. A single-row update, so this smokes the path, not capacity.
        if not self._original_full_name:
            return
        self._auth_get("/api/auth/me")
        self._profile_flip = not self._profile_flip
        name = self.LOAD_PROFILE_NAME if self._profile_flip else self._original_full_name
        self._auth_patch("/api/auth/me/profile", json={"full_name": name})


class ACLChecker(HerdUser):
    """Simulates ACL permission checks."""

    weight = 2
    wait_time = between(1, 3)

    def on_start(self):
        self._login(USER_EMAIL, USER_PASSWORD)
        self._user_id = None
        self._device_ids = []

        # Get user ID
        resp = self._auth_get("/api/auth/me")
        if resp.status_code == 200:
            self._user_id = resp.json()["id"]

        # Cache device IDs
        resp = self._auth_get("/api/inventory/devices")
        if resp.status_code == 200:
            self._device_ids = [d["id"] for d in resp.json()["items"][:4]]

    @task(3)
    def batch_check(self):
        if not self._user_id or not self._device_ids:
            return
        self._auth_post(
            "/api/acl/check/batch",
            json={
                "user_id": self._user_id,
                "resource_type": "device",
                "resource_ids": self._device_ids,
                "permission": "view",
            },
        )

    @task(2)
    def single_check(self):
        if not self._user_id or not self._device_ids:
            return
        self._auth_post(
            "/api/acl/check",
            json={
                "user_id": self._user_id,
                "resource_type": "device",
                "resource_id": self._device_ids[0],
                "permission": "view",
            },
        )


class BulkConnectionAdmin(HerdUser):
    """Exercises POST /connections/bulk, the admin-only bulk cable-create path.

    Every iteration posts a small batch (2-5 pairs) built from a cached pool
    of seeded device ids, then deletes every row the batch actually created
    (one DELETE /connections/{id} per row) so the stack's connection count
    stays flat instead of growing without bound across a run. Port names are
    generated fresh per call (cabling has no port-existence check and rows
    are not deduplicated, see services/cabling/app/services/connection_service.py),
    so this never collides with real cabling nor with a previous iteration's
    rows still in flight.

    The endpoint validates every row before inserting anything and always
    returns 200 with a per-row report; a rejected row (e.g. the self-loop
    guard, or a 503 batch-wide abort if inventory's device-group check is
    momentarily unreachable) is a legitimate outcome under concurrency, not a
    load-test failure, matching create_and_release's treatment of 409/422
    above. Only a transport error or an unexpected status is a real problem.
    """

    weight = 1
    wait_time = between(2, 5)

    def on_start(self):
        self._login(ADMIN_EMAIL, ADMIN_PASSWORD)
        self._device_ids = []
        resp = self._auth_get("/api/inventory/devices", params={"limit": 50})
        if resp.status_code == 200:
            self._device_ids = [d["id"] for d in resp.json()["items"]]

    @task
    def bulk_create_and_cleanup(self):
        if len(self._device_ids) < 2:
            return
        batch_size = random.randint(2, 5)
        items = []
        for _ in range(batch_size):
            device_a, device_b = random.sample(self._device_ids, 2)
            suffix = uuid.uuid4().hex[:8]
            items.append(
                {
                    "device_a_id": device_a,
                    "port_a": f"load-{suffix}-a",
                    "device_b_id": device_b,
                    "port_b": f"load-{suffix}-b",
                    "connection_type": "ethernet",
                }
            )
        with self._auth_post(
            "/api/cabling/connections/bulk",
            name="/api/cabling/connections/bulk",
            catch_response=True,
            json={"items": items},
        ) as resp:
            # 503 is the documented batch-wide fail-closed outcome when
            # inventory's device-group boundary check is momentarily
            # unreachable (see create_connections_bulk's docstring); count it
            # as load, not failure, same as the 409/422 contention above.
            if resp.status_code not in (200, 503):
                resp.failure(f"unexpected {resp.status_code}: {resp.text[:200]}")
                return
            if resp.status_code != 200:
                resp.success()
                return
            body = resp.json()
            if not {"created", "rejected", "rows"} <= body.keys():
                resp.failure(f"malformed ConnectionBulkReport: {body}")
                return
            if body["created"] + body["rejected"] != len(items):
                resp.failure(
                    f"created+rejected ({body['created']}+{body['rejected']}) "
                    f"!= submitted ({len(items)})"
                )
                return
            resp.success()
            created_ids = [
                row["connection_id"] for row in body["rows"] if row["status"] == "created"
            ]
        for connection_id in created_ids:
            self._auth_delete(
                f"/api/cabling/connections/{connection_id}",
                name="/api/cabling/connections/[id]",
            )


class DeviceConfigUser(HerdUser):
    """Exercises the device-configuration read and write paths (issue #1100).

    on_start picks a `Management` device template (the only connection type whose
    versions are pushed, CFG-GATE-1) whose driver's config schema, read through
    inventory's proxy, comes from the registry (`source` `registry`, CFG-SCHEMA-9):
    the version bodies below use the registry's vocabulary, and a driver that
    publishes its own schema replaces it (CFG-SCHEMA-6; frr_mgmt accepts only
    `commands`), so such a template is skipped. Templates are tried in the order the
    template list answers, newest first. It then creates ONE device of its own from
    that template, filling the template's required fields with inert placeholders (a
    documentation address, 192.0.2.10, for an `ip` field); on_stop deletes it, which
    removes every version this user wrote (CFG-VER-15). Seeded devices are never
    written to. The tasks list and create config versions on that device (each create
    validates the config against the schema, CFG-VER-3), read one back, diff the two
    newest, read the driver's config schema, and list the device's apply jobs. It
    never applies or schedules: a push would reach execution and a driver, which is
    not what this load measures.

    With no such template, nothing is created and every task does nothing. A refused
    device create is recorded as a locust failure on
    `/api/inventory/devices [config load device]` (so the run exits 1), and every task
    then does nothing. The device is AVAILABLE, so a `ReservationUser` can book it; a
    delete refused with 409 `device_in_use` cancels the reservations the answer lists
    and tries once more, and a delete that still fails is a locust failure and a
    logged warning naming the device. A run killed before on_stop leaves its
    `load-cfg-` device behind; delete it by name.
    """

    weight = 1
    wait_time = between(1, 3)

    def on_start(self):
        self._login(ADMIN_EMAIL, ADMIN_PASSWORD)
        self._device_id = None
        self._driver_id = None
        self._version_ids = []

        resp = self._auth_get(
            "/api/inventory/templates",
            params={"limit": 200},
            name="/api/inventory/templates [config load template pick]",
        )
        if resp.status_code != 200:
            return
        template = None
        for candidate in resp.json().get("items", []):
            if not (
                candidate.get("template_type") == "device"
                and candidate.get("connection_type") == "Management"
                and candidate.get("driver_id")
            ):
                continue
            schema = self._auth_get(
                f"/api/inventory/drivers/{candidate['driver_id']}/config-schema",
                name="/api/inventory/drivers/[id]/config-schema [config load template pick]",
            )
            if schema.status_code == 200 and schema.json().get("source") == "registry":
                template = candidate
                break
        if template is None:
            return
        create = self._auth_post(
            "/api/inventory/devices",
            name="/api/inventory/devices [config load device]",
            json={
                "name": f"load-cfg-{uuid.uuid4().hex[:8]}",
                "template_id": template["id"],
                "topology_type": "PHYSICAL",
                "status": "AVAILABLE",
                "field_data": self._placeholder_fields(template),
            },
        )
        if create.status_code != 201:
            return
        self._device_id = create.json()["id"]
        self._driver_id = template["driver_id"]

    @staticmethod
    def _placeholder_fields(template: dict) -> dict:
        """Inert values for the template's required fields; nothing ever connects."""
        values = {}
        for section in template.get("sections") or []:
            for field in section.get("fields") or []:
                if not field.get("required"):
                    continue
                kind = field.get("type")
                if field.get("options"):
                    values[field["key"]] = field["options"][0]
                elif kind == "number":
                    values[field["key"]] = 1
                elif kind == "boolean":
                    values[field["key"]] = False
                elif field["key"] == "ip":
                    values[field["key"]] = "192.0.2.10"
                else:
                    values[field["key"]] = "load"
        return values

    def on_stop(self):
        if not self._device_id:
            return
        path = f"/api/inventory/devices/{self._device_id}"
        name = "/api/inventory/devices/[id] [config load device]"
        with self._auth_delete(path, name=name, catch_response=True) as resp:
            blocking = self._blocking_reservation_ids(resp)
            if resp.status_code == 204:
                return
            if blocking is None:
                resp.failure(f"device delete answered {resp.status_code}: {resp.text[:200]}")
                logger.warning(
                    "DeviceConfigUser could not delete its device %s: HTTP %s",
                    self._device_id,
                    resp.status_code,
                )
                return
            # A ReservationUser booked this AVAILABLE device; not a load failure.
            resp.success()
        for reservation_id in blocking:
            self._auth_delete(
                f"/api/reservations/{reservation_id}",
                name="/api/reservations/[id] [config load device release]",
            )
        with self._auth_delete(path, name=name, catch_response=True) as retry:
            if retry.status_code != 204:
                retry.failure(f"device delete answered {retry.status_code}: {retry.text[:200]}")
                logger.warning(
                    "DeviceConfigUser could not delete its device %s after cancelling "
                    "reservations %s: HTTP %s; delete it by hand",
                    self._device_id,
                    blocking,
                    retry.status_code,
                )

    @staticmethod
    def _blocking_reservation_ids(resp) -> list | None:
        """The reservation ids of a 409 `device_in_use` answer, else None."""
        if resp.status_code != 409:
            return None
        try:
            detail = resp.json().get("detail")
        except ValueError:
            return None
        if not isinstance(detail, dict) or detail.get("error") != "device_in_use":
            return None
        ids = detail.get("reservation_ids")
        if not isinstance(ids, list) or not ids:
            return None
        return [str(i) for i in ids]

    @task(4)
    def list_versions(self):
        if not self._device_id:
            return
        self._auth_get(
            f"/api/inventory/devices/{self._device_id}/config-versions",
            name="/api/inventory/devices/[id]/config-versions",
        )

    @task(2)
    def create_version(self):
        if not self._device_id:
            return
        resp = self._auth_post(
            f"/api/inventory/devices/{self._device_id}/config-versions",
            name="/api/inventory/devices/[id]/config-versions [create]",
            # The registry's Management vocabulary (CFG-SCHEMA-1).
            json={
                "config": {
                    "hostname": f"load-{uuid.uuid4().hex[:8]}",
                    "vlan": random.randint(1, 4094),
                },
                "description": "load test",
            },
        )
        if resp.status_code == 201:
            self._version_ids.append(resp.json()["id"])

    @task(2)
    def read_version(self):
        if not self._device_id or not self._version_ids:
            return
        self._auth_get(
            f"/api/inventory/devices/{self._device_id}/config-versions/"
            f"{random.choice(self._version_ids)}",
            name="/api/inventory/devices/[id]/config-versions/[vid]",
        )

    @task(1)
    def diff_versions(self):
        if not self._device_id or len(self._version_ids) < 2:
            return
        self._auth_get(
            f"/api/inventory/devices/{self._device_id}/config-versions/diff",
            name="/api/inventory/devices/[id]/config-versions/diff",
            params={"from": self._version_ids[-2], "to": self._version_ids[-1]},
        )

    @task(2)
    def read_config_schema(self):
        if not self._driver_id:
            return
        self._auth_get(
            f"/api/inventory/drivers/{self._driver_id}/config-schema",
            name="/api/inventory/drivers/[id]/config-schema",
        )

    @task(2)
    def list_apply_jobs(self):
        if not self._device_id:
            return
        self._auth_get(
            f"/api/inventory/devices/{self._device_id}/apply-jobs",
            name="/api/inventory/devices/[id]/apply-jobs",
        )


class LoginLimitProbe(HerdUser):
    """One user that floods wrong-password logins from this host (IAM-LOGIN-7).

    It waits LOGIN_PROBE_DELAY_SECONDS first, so every other user (spawned at
    `make test-load`'s 5 a second) has signed in, then sends logins for an
    unknown account until auth answers 429, and keeps going for
    LOGIN_PROBE_LIMITED_SECONDS. Every answer must be the 401 a judged attempt
    gets or a 429 with a whole-second Retry-After; anything else, a 5xx or a 503
    from the hashing pool included, is a failure, and locust then exits 1.

    What it shows: a single source flooding the login is cut to its allowance
    with refusals that cost auth nothing, and the other users' requests, in the
    same report, carry on beside it. After it stops, the host's allowance
    refills over the rest of the run (a minute with the defaults), so a test
    suite started right after `make test-load` may still meet a 429 for a short
    while.
    """

    fixed_count = 1
    wait_time = constant(0.05)

    LOGIN_PROBE_DELAY_SECONDS = 15
    LOGIN_PROBE_LIMITED_SECONDS = 5

    def on_start(self):
        gevent.sleep(self.LOGIN_PROBE_DELAY_SECONDS)
        self._limited_since = None
        self._done = False

    @task
    def flood(self):
        if self._done:
            return
        with self.client.post(
            "/api/auth/login",
            json={"email": f"load-probe-{uuid.uuid4().hex[:12]}@herd.dev", "password": "wrong"},
            verify=False,
            catch_response=True,
        ) as resp:
            if _login_limited(resp) is not None:
                if self._limited_since is None:
                    self._limited_since = time.monotonic()
                elif time.monotonic() - self._limited_since > self.LOGIN_PROBE_LIMITED_SECONDS:
                    self._done = True
            elif resp.status_code == 401:
                resp.success()
            else:
                resp.failure(f"HTTP {resp.status_code}, expected 401 or 429")


# --- Single sign-on (ADR 0016 phase 5b, D12 level 4) ---------------------------
#
# OidcLoginUser drives the whole OpenID Connect redirect chain over HTTP: the
# begin route, Dex's own login form (infra/oidc-test), the callback, the
# handoff exchange, then a refresh and `GET /me` with the new token. It needs a
# stack whose auth runs in OIDC mode against the lab, which only the Makefile's
# _gate-oidc-stack-tests phase provides, so the class is abstract (never
# selected) unless HERD_LOAD_OIDC=1, and that phase also names it on the
# command line (`locust -f locustfile.py OidcLoginUser`), so the password-login
# classes above never run against an OIDC-mode stack.
#
# What a run asserts: every request answers exactly what the chain expects
# (any other status, a 5xx included, is a locust failure, and any failure or
# task exception makes locust exit 1), and the flow table stays bounded. Rows
# are pruned only once they expired more than an hour ago (PRUNE_AFTER in
# services/auth/app/services/oidc_flow_service.py), so a short run cannot wait
# for its own rows to qualify; instead, after one sequential warm-up sign-in
# each as alice and carol, the two OIDC_LOAD_USERS (so concurrent first logins
# never race to provision the same account), test_start plants
# EXPIRED_FLOW_ROWS rows that expired two hours ago, and at the end every one
# of them must be gone (each begin prunes up to 500) and the rows created since
# the run started must equal the begins
# this process counted: exactly one row per sign-in, none left over. Accounts
# federated with the lab's issuer, and any planted row left, are deleted when
# locust quits (nothing is deleted before the run).

# The environment this class reads; the gate phase sets exactly these, which
# tests/unit/test_oidc_stack_gate_wiring.py pins by reading these constants.
OIDC_LOAD_SELECTOR_ENV = "HERD_LOAD_OIDC"
OIDC_TEST_HOST_ENV = "HERD_TEST_OIDC_HOST"
OIDC_TEST_PORT_ENV = "HERD_TEST_OIDC_PORT"

OIDC_LOAD = os.getenv(OIDC_LOAD_SELECTOR_ENV) == "1"
OIDC_TEST_HOST = os.getenv(OIDC_TEST_HOST_ENV, "127.0.0.1")
OIDC_TEST_PORT = int(os.getenv(OIDC_TEST_PORT_ENV, "5556"))
OIDC_ISSUER = "http://oidc-test:5556/dex"
OIDC_PASSWORD = "oidc-test-password"
OIDC_LOAD_USERS = ("alice@oidc.example.com", "carol@oidc.example.com")
EXPIRED_FLOW_ROWS = 1000
EXPIRED_STATE_PREFIX = "oidc-load-expired-"
REPO_ROOT = Path(__file__).resolve().parents[2]

_oidc_run = {"begins": 0, "started_at": None, "rows_before": 0, "problem": None}


def _psql(sql: str) -> str:
    """One statement in the stack's Postgres through `docker compose exec`
    (COMPOSE_PROJECT_NAME picks the project); its tuples-only output."""
    import subprocess

    cmd = [
        "docker",
        "compose",
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        os.getenv("POSTGRES_USER", "herd"),
        "-d",
        os.getenv("POSTGRES_DB", "herd"),
        "-tA",
        "-c",
        sql,
    ]
    result = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"psql failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout.strip()


def _dex_url(url: str) -> str:
    """`url` with the issuer's host and port replaced by the lab's published address."""
    prefix = "http://oidc-test:5556"
    if url.startswith(prefix):
        return f"http://{OIDC_TEST_HOST}:{OIDC_TEST_PORT}" + url[len(prefix) :]
    return url


def _expect(client, method, url, name, statuses, check=None, **kwargs):
    """One request; the response when its status is in `statuses` and `check`
    (which returns a problem string or None) finds nothing, else None after
    marking it a locust failure."""
    with client.request(
        method,
        url,
        name=name,
        catch_response=True,
        allow_redirects=False,
        verify=False,
        **kwargs,
    ) as resp:
        if resp.status_code not in statuses:
            resp.failure(f"HTTP {resp.status_code}, expected {sorted(statuses)}")
            return None
        problem = check(resp) if check else None
        if problem:
            resp.failure(problem)
            return None
        resp.success()
        return resp


def _oidc_sign_in(client, email: str) -> bool:
    """The full chain as `email` on `client` (one cookie jar, one browser)."""
    import re
    from html import unescape
    from urllib.parse import parse_qs, urljoin, urlsplit

    def to_issuer(resp):
        location = resp.headers.get("location", "")
        return None if location.startswith(f"{OIDC_ISSUER}/auth?") else f"Location {location!r}"

    begin = _expect(client, "GET", "/api/auth/oidc/login", "/api/auth/oidc/login", {302}, to_issuer)
    if begin is None:
        return False
    _oidc_run["begins"] += 1
    authorization_url = begin.headers["location"]
    redirect_uri = parse_qs(urlsplit(authorization_url).query)["redirect_uri"][0]

    url, resp, posted = _dex_url(authorization_url), None, False
    for _ in range(10):
        resp = _expect(client, "GET", url, "dex: authorization", {200, 302, 303})
        if resp is None:
            return False
        if resp.status_code == 200:
            # A page after the form post (Dex's "invalid credentials" page, say),
            # or a page with no form, is a broken chain: raised, so locust
            # counts it as a task exception and exits 1.
            if posted:
                raise AssertionError(f"Dex answered a page after the login post at {url}")
            form = re.search(r'<form[^>]*action="([^"]*)"', resp.text)
            if form is None:
                raise AssertionError(f"no login form at {url}")
            resp = _expect(
                client,
                "POST",
                urljoin(url, unescape(form.group(1))),
                "dex: login form",
                {302, 303},
                data={"login": email, "password": OIDC_PASSWORD},
            )
            if resp is None:
                return False
            posted = True
        location = urljoin(url, resp.headers["location"])
        if location.startswith(redirect_uri + "?"):
            break
        url = _dex_url(location)
    else:
        raise AssertionError("Dex redirected more than ten times")

    def to_completion(resp):
        location = resp.headers.get("location", "")
        return None if location == "/login/sso" else f"Location {location!r}"

    if _expect(client, "GET", location, "/api/auth/oidc/callback", {302}, to_completion) is None:
        return False

    def with_tokens(resp):
        body = resp.json()
        return None if body.get("access_token") and body.get("refresh_token") else "no tokens"

    exchange = _expect(
        client,
        "POST",
        "/api/auth/oidc/exchange",
        "/api/auth/oidc/exchange",
        {200},
        with_tokens,
        json={},
    )
    if exchange is None:
        return False
    refreshed = _expect(
        client,
        "POST",
        "/api/auth/refresh",
        "/api/auth/refresh",
        {200},
        with_tokens,
        json={"refresh_token": exchange.json()["refresh_token"]},
    )
    if refreshed is None:
        return False

    def is_signer(resp):
        return None if resp.json().get("email") == email else "another account"

    me = _expect(
        client,
        "GET",
        "/api/auth/me",
        "/api/auth/me",
        {200},
        is_signer,
        headers={"Authorization": f"Bearer {refreshed.json()['access_token']}"},
    )
    return me is not None


class OidcLoginUser(HttpUser):
    """Signs in through the OIDC lab, refreshes, and reads `/me`, over and over.

    Abstract unless HERD_LOAD_OIDC=1 (see the section comment above)."""

    abstract = not OIDC_LOAD
    weight = 1
    wait_time = between(0.5, 1.5)

    @task
    def sign_in(self):
        # A fresh cookie jar per sign-in, as a fresh browser.
        self.client.cookies.clear()
        _oidc_sign_in(self.client, random.choice(OIDC_LOAD_USERS))


def _delete_oidc_load_rows() -> None:
    _psql(
        "DELETE FROM auth.users WHERE id IN (SELECT user_id FROM auth.federated_identities "
        f"WHERE issuer = '{OIDC_ISSUER}')"
    )
    _psql(f"DELETE FROM auth.oidc_login_flows WHERE state_hash LIKE '{EXPIRED_STATE_PREFIX}%'")


@events.test_start.add_listener
def _oidc_test_start(environment, **kwargs):
    if not OIDC_LOAD:
        return
    import requests

    try:
        options = requests.get(
            f"{environment.host}/api/auth/login-options", verify=False, timeout=15
        )
        mode = options.json().get("auth_method") if options.status_code == 200 else None
        if mode != "oidc":
            raise RuntimeError(f"the stack's auth_method is {mode!r}, not 'oidc'")
        _oidc_run["started_at"] = _psql("SELECT now()")
        _oidc_run["rows_before"] = int(_psql("SELECT count(*) FROM auth.oidc_login_flows"))
        warm_up = HttpSession(
            base_url=environment.host, request_event=environment.events.request, user=None
        )
        for email in OIDC_LOAD_USERS:
            if not _oidc_sign_in(warm_up, email):
                raise RuntimeError(f"the warm-up sign-in as {email} failed")
        _psql(
            "INSERT INTO auth.oidc_login_flows (id, state_hash, binding_hash, nonce, "
            "code_verifier, next_path, status, created_at, expires_at) SELECT "
            f"gen_random_uuid(), '{EXPIRED_STATE_PREFIX}' || g, 'oidc-load-expired', 'n', NULL, "
            "'/topology', 'failed', now() - interval '3 hours', now() - interval '2 hours' "
            f"FROM generate_series(1, {EXPIRED_FLOW_ROWS}) AS g"
        )
    except Exception as exc:
        _oidc_run["problem"] = f"OIDC load setup failed: {exc}"
        logger.error(_oidc_run["problem"])
        environment.runner.quit()


@events.quitting.add_listener
def _oidc_quitting(environment, **kwargs):
    if not OIDC_LOAD or _oidc_run["started_at"] is None:
        if OIDC_LOAD and _oidc_run["problem"]:
            environment.process_exit_code = 1
        return
    problems = [_oidc_run["problem"]] if _oidc_run["problem"] else []
    try:
        left = int(
            _psql(
                "SELECT count(*) FROM auth.oidc_login_flows "
                f"WHERE state_hash LIKE '{EXPIRED_STATE_PREFIX}%'"
            )
        )
        created = int(
            _psql(
                "SELECT count(*) FROM auth.oidc_login_flows WHERE created_at >= "
                f"'{_oidc_run['started_at']}'::timestamptz"
            )
        )
        total = int(_psql("SELECT count(*) FROM auth.oidc_login_flows"))
        begins = _oidc_run["begins"]
        logger.info(
            "OIDC load: %d begins, %d flow rows created, %d of %d expired rows left, "
            "%d rows before, %d after",
            begins,
            created,
            left,
            EXPIRED_FLOW_ROWS,
            _oidc_run["rows_before"],
            total,
        )
        if left:
            problems.append(f"{left} of {EXPIRED_FLOW_ROWS} expired flow rows were not pruned")
        if created != begins:
            problems.append(f"{created} flow rows were created for {begins} begins")
        if total > _oidc_run["rows_before"] + begins:
            problems.append(
                f"the flow table holds {total} rows, more than {_oidc_run['rows_before']} "
                f"before plus {begins} begins"
            )
    except Exception as exc:
        problems.append(f"the flow-table check failed: {exc}")
    finally:
        try:
            _delete_oidc_load_rows()
        except Exception as exc:
            problems.append(f"cleanup failed: {exc}")
    for problem in problems:
        logger.error(problem)
    if problems:
        environment.process_exit_code = 1
