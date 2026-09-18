"""Static pin: every active AI_* knob in .env.example reaches the ai-orchestrator
container (issue #849).

No stack, no Docker: this only parses docker-compose.yml and .env.example. Compose
passes an env var to a service only through explicit ${VAR} interpolation in its
`environment:` block; no service uses `env_file`. AI_GENERATE_MAX_REPAIRS,
AI_RESOLVER_CANDIDATES_PER_TEMPLATE, and AI_RESOLVER_MAX_SEARCH_STEPS were listed in
.env.example and documented in docs/ENV_VARS.md as working, but had no line in the
ai-orchestrator environment block, so setting them in .env silently did nothing. This
test exists so the next knob cannot go inert the same way.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"

_ACTIVE_ENV_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")


def _active_env_example_keys(prefix: str) -> set[str]:
    """Keys with an uncommented `KEY=` assignment at line start in .env.example,
    restricted to the given prefix. A commented-out line (e.g. `# AI_FOO=bar`) is
    not active."""
    keys = set()
    for line in ENV_EXAMPLE_PATH.read_text().splitlines():
        match = _ACTIVE_ENV_KEY_RE.match(line)
        if match and match.group(1).startswith(prefix):
            keys.add(match.group(1))
    return keys


def _ai_orchestrator_environment() -> dict:
    data = yaml.safe_load(COMPOSE_PATH.read_text())
    service = data["services"]["ai-orchestrator"]
    environment = service["environment"]
    assert isinstance(environment, dict), (
        "ai-orchestrator's environment block is not the dict (KEY: value) form this "
        "test expects; if it changed to the list (KEY=value) form, update this test"
    )
    return environment


def test_every_active_ai_env_example_key_is_wired_to_ai_orchestrator():
    active_keys = _active_env_example_keys("AI_")
    wired_keys = set(_ai_orchestrator_environment())

    missing = sorted(active_keys - wired_keys)
    assert not missing, (
        "these AI_* keys are active in .env.example but missing from the "
        "ai-orchestrator environment block in docker-compose.yml, so setting them "
        "in .env has no effect in the container: " + ", ".join(missing)
    )


def test_every_ai_orchestrator_env_default_matches_the_dot_env_example_default():
    """Where docker-compose.yml carries a ${VAR:-default} fallback for a key that
    also has a default in .env.example, the two must agree, or a fresh clone (no
    .env value set) and an operator who copied .env.example diverge silently."""
    active_defaults = {}
    for line in ENV_EXAMPLE_PATH.read_text().splitlines():
        match = _ACTIVE_ENV_KEY_RE.match(line)
        if match and match.group(1).startswith("AI_"):
            active_defaults[match.group(1)] = line.split("=", 1)[1].strip()

    environment = _ai_orchestrator_environment()
    mismatches = []
    for key, env_example_default in active_defaults.items():
        if not env_example_default:
            continue
        raw_value = str(environment.get(key, ""))
        compose_match = re.match(rf"^\$\{{{re.escape(key)}:-(.*)\}}$", raw_value)
        if compose_match is None:
            continue
        compose_default = compose_match.group(1)
        if compose_default != env_example_default:
            mismatches.append(
                f"{key}: .env.example={env_example_default!r} compose={compose_default!r}"
            )
    assert not mismatches, "default mismatch between .env.example and compose: " + "; ".join(
        mismatches
    )
