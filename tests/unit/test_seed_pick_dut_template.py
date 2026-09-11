"""Pins for seed_devices_public.pick_dut_template (issue #775).

`get_or_create_device` always sends ip, login and password in field_data, and
inventory rejects a create carrying fields the template does not declare
("Unknown fields: ip, login, password"). The picker therefore has to filter on
the declared field keys, not merely on the driver's connection type. Without
that filter the choice is order-dependent: the integration suite seeds
`int-seed-template-*` rows backed by a Management driver whose only field is
`model`, so on any database those tests have touched such a template can sort
first, win the pick, and break the ACL fixture seeding.
"""

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "seed_devices_public", REPO_ROOT / "seed_devices_public.py"
)
seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed)


def _template(name, driver_id, field_keys, sectioned=True):
    sections = (
        [{"name": "Network", "fields": [{"key": k} for k in field_keys]}] if sectioned else []
    )
    return {"id": f"tpl-{name}", "name": name, "driver_id": driver_id, "sections": sections}


class _FakeClient:
    """Minimal stand-in returning canned driver and template listings."""

    def __init__(self, drivers, templates):
        self._drivers = drivers
        self._templates = templates

    def get(self, url, params=None):
        items = self._drivers if "/drivers" in url else self._templates
        return _FakeResponse({"items": items})


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


MGMT = [{"id": "drv-mgmt", "connection_type": "Management"}]
SEED_KEYS = list(seed.SEED_DEVICE_FIELD_KEYS)


def test_skips_a_management_template_that_lacks_the_seeded_fields():
    """The exact issue #775 ordering: a fieldless Management template sorts first."""
    client = _FakeClient(
        MGMT,
        [
            _template("int-seed-template-abc", "drv-mgmt", ["model"]),
            _template("Ubuntu Client", "drv-mgmt", SEED_KEYS),
        ],
    )
    assert seed.pick_dut_template(client) == "tpl-Ubuntu Client"


def test_prefers_a_management_backed_template_among_usable_ones():
    client = _FakeClient(
        MGMT,
        [
            _template("L2-Switch-48", "drv-switch", SEED_KEYS),
            _template("Ubuntu Client", "drv-mgmt", SEED_KEYS),
        ],
    )
    assert seed.pick_dut_template(client) == "tpl-Ubuntu Client"


def test_falls_back_to_a_usable_non_management_template():
    client = _FakeClient(
        MGMT,
        [
            _template("int-seed-template-abc", "drv-mgmt", ["model"]),
            _template("L2-Switch-48", "drv-switch", SEED_KEYS),
        ],
    )
    assert seed.pick_dut_template(client) == "tpl-L2-Switch-48"


def test_falls_back_to_the_first_template_when_none_declare_the_fields():
    client = _FakeClient(MGMT, [_template("odd-one", "drv-mgmt", ["model"])])
    assert seed.pick_dut_template(client) == "tpl-odd-one"


def test_returns_none_with_no_templates():
    assert seed.pick_dut_template(_FakeClient(MGMT, [])) is None


def test_template_field_keys_flattens_every_section():
    tpl = {
        "sections": [
            {"name": "Network", "fields": [{"key": "ip"}]},
            {"name": "Auth", "fields": [{"key": "login"}, {"key": "password"}]},
        ]
    }
    assert seed.template_field_keys(tpl) == {"ip", "login", "password"}
    assert seed.template_declares_seed_fields(tpl)


def test_template_with_no_sections_declares_nothing():
    assert seed.template_field_keys({"sections": None}) == set()
    assert not seed.template_declares_seed_fields({})
