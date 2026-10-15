"""Locust load test definitions for HERD.

Eight user classes simulating different usage patterns:
- ReservationUser: creates, lists, queries calendar, releases reservations
- InventoryBrowser: browses devices, templates, and the topologies list
- BulkExporter: exports devices, templates, and topologies; dry-runs a device import
- ACLChecker: batch checks permissions
- RoutedTopologyValidator: repeatedly validates one topology carrying Layer 3
  routing intent (ADR 0014, issue #34 phase 3), a safe no-op when the seeded
  pool has no Layer 3 Switch device
- NotificationUser: polls notifications and updates channel/event preferences
- BulkConnectionAdmin: posts small POST /connections/bulk batches, then deletes
  what it created
- DeviceConfigUser: writes and reads config versions, the driver config schema,
  and the apply-job list on one device of its own, deleted at the end

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
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import urllib3
from locust import HttpUser, between, task

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


class HerdUser(HttpUser):
    """Base class with login-on-start and TLS bypass."""

    abstract = True

    def _login(self, email: str, password: str) -> None:
        resp = self.client.post(
            "/api/auth/login",
            json={"email": email, "password": password},
            verify=False,
        )
        if resp.status_code == 200:
            tokens = resp.json()
            self.access_token = tokens["access_token"]
            self.headers = {"Authorization": f"Bearer {self.access_token}"}
        else:
            self.access_token = None
            self.headers = {}

    def _auth_get(self, path: str, **kwargs):
        return self.client.get(path, headers=self.headers, verify=False, **kwargs)

    def _auth_post(self, path: str, **kwargs):
        return self.client.post(path, headers=self.headers, verify=False, **kwargs)

    def _auth_put(self, path: str, **kwargs):
        return self.client.put(path, headers=self.headers, verify=False, **kwargs)

    def _auth_delete(self, path: str, **kwargs):
        return self.client.delete(path, headers=self.headers, verify=False, **kwargs)


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

    def on_start(self):
        self._login(USER_EMAIL, USER_PASSWORD)
        self._toggle = False

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
