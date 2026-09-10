"""Pins for the Makefile's stack-lifecycle recipes (2026-09-10 gate incident).

A compose service behind a profile that is not enabled is neither an active
service nor an orphan, so a plain ``docker compose down`` (even with
``--remove-orphans``) leaves it running. The e2e Selenium container left behind
by the dev project then holds host port 4444 and the gate's e2e phase fails at
Selenium recreate before any test runs. Every ``down`` must therefore enable the
e2e profile, and the e2e recreate must wait for the Grid healthcheck so the
first Playwright page load does not race the new container's network setup.
"""

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"
DEV_OVERRIDE = REPO_ROOT / "docker-compose.override.yml"

# A recipe line invoking `docker compose [flags] down` or `$(GATE_COMPOSE) [flags] down`.
DOWN_LINE = re.compile(r"^\t-?\s*(?:docker compose|\$\(GATE_COMPOSE\))(?P<flags>[^\n]*?)\bdown\b")


def _recipe_lines() -> list[str]:
    return [line for line in MAKEFILE.read_text().splitlines() if line.startswith("\t")]


def test_every_compose_down_enables_the_e2e_profile():
    downs = [line for line in _recipe_lines() if DOWN_LINE.match(line)]
    assert downs, "no compose down recipe lines found; the pattern is stale"
    missing = [line for line in downs if "--profile e2e" not in DOWN_LINE.match(line)["flags"]]
    assert not missing, "compose down without --profile e2e:\n" + "\n".join(missing)


def test_ldap_compose_down_is_not_in_scope():
    # LDAP_COMPOSE targets its own single-service file with no profiles; the pin
    # above must not silently start matching it if that variable is ever renamed.
    assert not any("$(LDAP_COMPOSE)" in line and DOWN_LINE.match(line) for line in _recipe_lines())


def test_e2e_selenium_recreate_waits_for_health():
    recreates = [
        line
        for line in _recipe_lines()
        if "--profile e2e up" in line and "selenium" in line and "--force-recreate" in line
    ]
    assert len(recreates) == 1, recreates
    assert "--wait" in recreates[0]


def test_dev_override_selenium_has_grid_healthcheck():
    data = yaml.safe_load(DEV_OVERRIDE.read_text())
    selenium = data["services"]["selenium"]
    assert "e2e" in selenium.get("profiles", [])
    test = selenium["healthcheck"]["test"]
    assert test[0] == "CMD" and test[1] == "/opt/bin/check-grid.sh", test


def test_stack_wait_initializes_elapsed_before_the_loop():
    text = MAKEFILE.read_text()
    recipe = text[text.index("_master-wait-healthy:") :].split("\n\n", 1)[0]
    assert "START=$$(date +%s); ELAPSED=0;" in recipe
