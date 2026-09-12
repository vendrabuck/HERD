"""Pins for seed_devices_public.pick_dut_template (issue #775, #781).

`get_or_create_device` always sends ip, login and password in field_data, and
inventory rejects a create carrying fields the template does not declare
("Unknown fields: ip, login, password"). The picker therefore has to filter on
the declared field keys, not merely on the driver's connection type. Without
that filter the choice is order-dependent: the integration suite seeds
`int-seed-template-*` rows backed by a Management driver whose only field is
`model`, so on any database those tests have touched such a template can sort
first, win the pick, and break the ACL fixture seeding.

Issue #781: the picker's last fallback tier returned items[0] even when no
template declared the seeded fields, i.e. a template guaranteed to fail the
same validation the filter exists to satisfy. The picker now returns None in
that case, and the caller (seed_acl_test_fixtures) already skips on None.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SEED_PATH = _REPO_ROOT / "seed_devices_public.py"


@pytest.fixture(scope="module")
def seed():
    spec = importlib.util.spec_from_file_location("seed_devices_public", _SEED_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["seed_devices_public"] = module
    spec.loader.exec_module(module)
    return module


def _template(name, driver_id, field_keys):
    sections = [{"name": "Network", "fields": [{"key": k} for k in field_keys]}]
    return {"id": f"tpl-{name}", "name": name, "driver_id": driver_id, "sections": sections}


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeClient:
    """Minimal stand-in returning canned driver and template listings."""

    def __init__(self, drivers, templates):
        self._drivers = drivers
        self._templates = templates

    def get(self, url, params=None):
        items = self._drivers if "/drivers" in url else self._templates
        return _FakeResponse({"items": items})


@pytest.fixture
def mgmt():
    return [{"id": "drv-mgmt", "connection_type": "Management"}]


@pytest.fixture
def seed_keys(seed):
    return list(seed.SEED_DEVICE_FIELD_KEYS)


def test_skips_a_management_template_that_lacks_the_seeded_fields(seed, mgmt, seed_keys):
    """The exact issue #775 ordering: a fieldless Management template sorts first."""
    client = _FakeClient(
        mgmt,
        [
            _template("int-seed-template-abc", "drv-mgmt", ["model"]),
            _template("Ubuntu Client", "drv-mgmt", seed_keys),
        ],
    )
    assert seed.pick_dut_template(client) == "tpl-Ubuntu Client"


def test_prefers_a_management_backed_template_among_usable_ones(seed, mgmt, seed_keys):
    client = _FakeClient(
        mgmt,
        [
            _template("L2-Switch-48", "drv-switch", seed_keys),
            _template("Ubuntu Client", "drv-mgmt", seed_keys),
        ],
    )
    assert seed.pick_dut_template(client) == "tpl-Ubuntu Client"


def test_falls_back_to_a_usable_non_management_template(seed, mgmt, seed_keys):
    client = _FakeClient(
        mgmt,
        [
            _template("int-seed-template-abc", "drv-mgmt", ["model"]),
            _template("L2-Switch-48", "drv-switch", seed_keys),
        ],
    )
    assert seed.pick_dut_template(client) == "tpl-L2-Switch-48"


def test_returns_none_when_no_template_declares_the_seeded_fields(seed, mgmt):
    """Issue #781: previously fell back to items[0], a template guaranteed to
    fail get_or_create_device's create (it does not declare ip/login/password).
    The caller, seed_acl_test_fixtures, treats None as "skip", so returning the
    unusable template instead of None is the bug this pins against."""
    client = _FakeClient(mgmt, [_template("odd-one", "drv-mgmt", ["model"])])
    assert seed.pick_dut_template(client) is None


def test_returns_none_with_no_templates(seed, mgmt):
    assert seed.pick_dut_template(_FakeClient(mgmt, [])) is None


def test_template_field_keys_flattens_every_section(seed):
    tpl = {
        "sections": [
            {"name": "Network", "fields": [{"key": "ip"}]},
            {"name": "Auth", "fields": [{"key": "login"}, {"key": "password"}]},
        ]
    }
    assert seed.template_field_keys(tpl) == {"ip", "login", "password"}
    assert seed.template_declares_seed_fields(tpl)


def test_template_with_no_sections_declares_nothing(seed):
    assert seed.template_field_keys({"sections": None}) == set()
    assert not seed.template_declares_seed_fields({})


def test_template_field_keys_skips_a_fieldless_entry(seed):
    """A field dict without a `key` must not surface as a member (it would
    otherwise poison the set with None and break set[str]'s real return type)."""
    tpl = {
        "sections": [
            {"name": "Network", "fields": [{"key": "ip"}, {"label": "no key here"}]},
        ]
    }
    assert seed.template_field_keys(tpl) == {"ip"}


class _FakeDeviceClient:
    """Captures the POST body get_or_create_device sends, no HTTP involved."""

    def __init__(self):
        self.posted_json = None

    def post(self, url, json=None):
        self.posted_json = json
        return _FakeResponse({"id": "dev-1"}, status_code=201)


def test_get_or_create_device_field_data_matches_seed_device_field_keys(seed):
    """Pins the request body's field_data key set against SEED_DEVICE_FIELD_KEYS
    without a live stack, so drift between the two (the #781 assert's subject)
    fails here in CI rather than only during a real seed run."""
    client = _FakeDeviceClient()
    seed.get_or_create_device(client, "dev-1", "tpl-1", ip="10.0.0.1")
    assert client.posted_json is not None
    assert set(client.posted_json["field_data"]) == set(seed.SEED_DEVICE_FIELD_KEYS)
