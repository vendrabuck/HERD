"""Locust load test definitions for HERD.

Three user classes simulating different usage patterns:
- ReservationUser: creates, lists, queries calendar, releases reservations
- InventoryBrowser: browses devices and templates
- ACLChecker: batch checks permissions
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
