"""Locust load test definitions for HERD.

Four user classes simulating different usage patterns:
- ReservationUser: creates, lists, queries calendar, releases reservations
- InventoryBrowser: browses devices and templates
- ACLChecker: batch checks permissions
- LiveEditUser: edits a running reservation's topology (PUT canvas, PATCH device set)
"""

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import urllib3
from locust import HttpUser, between, task

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


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

    def _auth_patch(self, path: str, **kwargs):
        return self.client.patch(path, headers=self.headers, verify=False, **kwargs)

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
        device_id = self._device_ids[0]
        now = datetime.now(timezone.utc)
        resp = self._auth_post(
            "/api/reservations/",
            json={
                "device_ids": [device_id],
                "purpose": f"load-test-{uuid.uuid4().hex[:8]}",
                "start_time": now.isoformat(),
                "end_time": (now + timedelta(minutes=5)).isoformat(),
            },
        )
        if resp.status_code == 201:
            res_id = resp.json()["id"]
            self._auth_put(f"/api/reservations/{res_id}/release")


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


def _canvas_with_edge(device_a_id: str, device_b_id: str, label: str | None = None) -> dict:
    """Minimal React Flow canvas: two device nodes joined by one L2 edge.

    node.data.device.id is the inventory device id the cabling validator
    resolves against the physical Connection graph. An optional label on the
    first node lets the live-edit task mutate canvas_data so the PUT registers
    as a wiring change and exercises the reservation-scoped lock path.
    """
    node_a_data: dict = {"device": {"id": device_a_id}}
    if label is not None:
        node_a_data["device"]["label"] = label
    return {
        "nodes": [
            {"id": "nA", "data": node_a_data},
            {"id": "nB", "data": {"device": {"id": device_b_id}}},
        ],
        "edges": [
            {
                "id": "e1",
                "source": "nA",
                "target": "nB",
                "data": {"layer": "L2", "isProposal": False},
            }
        ],
        "selectedEdgeLayer": "L2",
    }


class LiveEditUser(HerdUser):
    """Simulates an owner repeatedly editing their running reservation's topology.

    The hot path under test is the live-edit cycle:
    - PUT /api/cabling/topologies/{id} with a canvas_data change (the cabling
      reservation-scoped lock; allowed for the owner).
    - PATCH /api/reservations/{id} with a device_ids change (re-runs the cabling
      connectivity gate via /validate/internal).

    on_start logs in, picks two seeded devices, cables them, then creates a
    topology and an owning reservation so the edit tasks act on the user's own
    live reservation. A running, seeded stack is required; without one on_start
    just leaves the ids unset and the tasks no-op.
    """

    weight = 3
    wait_time = between(1, 3)

    def on_start(self):
        self._login(ADMIN_EMAIL, ADMIN_PASSWORD)
        self._device_a = None
        self._device_b = None
        self._device_c = None
        self._topology_id = None
        self._reservation_id = None
        self._connection_id = None

        # Pick available exclusive devices, mirroring ReservationUser.
        resp = self._auth_get("/api/inventory/devices")
        if resp.status_code != 200:
            return
        available = [
            d["id"]
            for d in resp.json().get("items", [])
            if d.get("status") == "AVAILABLE" and d.get("exclusive", True)
        ]
        if len(available) < 2:
            return
        self._device_a, self._device_b = available[0], available[1]
        if len(available) >= 3:
            self._device_c = available[2]

        # Physically cable the two topology devices so the connectivity gate
        # passes on the subsequent device-set PATCH.
        conn = self._auth_post(
            "/api/cabling/connections",
            json={
                "device_a_id": self._device_a,
                "port_a": "eth1",
                "device_b_id": self._device_b,
                "port_b": "eth1",
                "connection_type": "L1",
            },
        )
        if conn.status_code == 201:
            self._connection_id = conn.json().get("id")

        # Create a topology owned by this user and seed its canvas.
        topo = self._auth_post(
            "/api/cabling/topologies",
            json={"name": f"load-live-edit-{uuid.uuid4().hex[:8]}"},
        )
        if topo.status_code != 201:
            return
        self._topology_id = topo.json()["id"]
        self._auth_put(
            f"/api/cabling/topologies/{self._topology_id}",
            json={"canvas_data": _canvas_with_edge(self._device_a, self._device_b)},
        )

        # Create the owning reservation referencing the topology.
        now = datetime.now(timezone.utc)
        res = self._auth_post(
            "/api/reservations/",
            json={
                "device_ids": [self._device_a, self._device_b],
                "topology_id": self._topology_id,
                "purpose": f"load-live-edit-{uuid.uuid4().hex[:8]}",
                "start_time": now.isoformat(),
                "end_time": (now + timedelta(hours=1)).isoformat(),
            },
        )
        if res.status_code == 201:
            self._reservation_id = res.json()["id"]

    @task(6)
    def live_edit_cycle(self):
        """PUT a canvas change then PATCH the device set, the live-edit hot path."""
        if not self._topology_id or not self._reservation_id:
            return

        # Wiring change: a fresh label each time keeps canvas_data != stored so
        # the PUT registers as a canvas change and hits the reservation lock.
        edited = _canvas_with_edge(
            self._device_a,
            self._device_b,
            label=f"edit-{uuid.uuid4().hex[:6]}",
        )
        self._auth_put(
            f"/api/cabling/topologies/{self._topology_id}",
            json={"canvas_data": edited},
        )

        # Device-set change: toggle the optional third device in and out so the
        # PATCH re-runs the connectivity gate against a changing set.
        device_ids = [self._device_a, self._device_b]
        if self._device_c and uuid.uuid4().int % 2 == 0:
            device_ids.append(self._device_c)
        self._auth_patch(
            f"/api/reservations/{self._reservation_id}",
            json={"device_ids": device_ids},
        )

    @task(2)
    def list_reservations(self):
        """Mix in read load on the reservations list."""
        self._auth_get("/api/reservations/")

    def on_stop(self):
        """Best-effort cleanup of this user's reservation, topology, connection."""
        if self._reservation_id:
            self._auth_delete(f"/api/reservations/{self._reservation_id}")
        if self._topology_id:
            self._auth_delete(f"/api/cabling/topologies/{self._topology_id}")
        if self._connection_id:
            self._auth_delete(f"/api/cabling/connections/{self._connection_id}")
