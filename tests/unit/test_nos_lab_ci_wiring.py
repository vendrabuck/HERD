"""Static pins for where the NOS lab suites run (issue #785). No lab, no
stack, no Docker: this only parses the Makefile and the two workflow files,
in the same style as tests/unit/test_nos_lab_compose.py.

The tiering decision these pins protect: the four DIALECT suites (one driver
against one lab node, no HERD stack) run on every PR from ci.yml's
nos-dialect job, and the two VIA-STACK FEATURE suites run in nightly.yml
after the stack is seeded. The failure mode worth catching automatically is
a new file appearing under tests/nos_lab/ and belonging to neither list, in
which case it would be written, reviewed, merged, and then never run by
anything.

The nightly ordering is load-bearing in both directions and is pinned here
as well: the L2 feature suite reuses the nos-lab-* devices `make seed-nos`
creates, so it must come after the seeded e2e pass, and it holds real
reservations over real devices, so it must come before the locust load test
rather than racing it for the same ports.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE_PATH = REPO_ROOT / "Makefile"
NOS_LAB_TESTS_DIR = REPO_ROOT / "tests" / "nos_lab"
CI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
NIGHTLY_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "nightly.yml"


def _makefile_list_variable(name: str) -> list[str]:
    """Return the whitespace-separated values of a `NAME := ...` Makefile
    variable, following backslash continuations."""
    assignment = re.compile(rf"^{re.escape(name)}\s*:?=\s*(.*)$")
    lines = MAKEFILE_PATH.read_text().splitlines()
    for index, line in enumerate(lines):
        match = assignment.match(line)
        if not match:
            continue
        collected = [match.group(1)]
        while collected[-1].rstrip().endswith("\\"):
            collected[-1] = collected[-1].rstrip().removesuffix("\\")
            index += 1
            collected.append(lines[index])
        return " ".join(collected).split()
    raise AssertionError(f"{name} is not defined in the Makefile")


def _workflow_steps(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text())
    jobs = data["jobs"]
    return [step for job in jobs.values() for step in job["steps"]]


def _step_names(steps: list[dict]) -> list[str]:
    return [step.get("name", "") for step in steps]


def _step_index(steps: list[dict], needle: str) -> int:
    for index, step in enumerate(steps):
        if needle in str(step.get("run", "")):
            return index
    raise AssertionError(f"no step runs {needle!r}; steps: {_step_names(steps)}")


def test_every_nos_lab_test_file_is_classified_exactly_once():
    dialect = set(_makefile_list_variable("NOS_DIALECT_TESTS"))
    feature = set(_makefile_list_variable("NOS_FEATURE_TESTS"))

    overlap = dialect & feature
    assert not overlap, f"file(s) in both NOS_DIALECT_TESTS and NOS_FEATURE_TESTS: {overlap}"

    on_disk = {f"tests/nos_lab/{path.name}" for path in sorted(NOS_LAB_TESTS_DIR.glob("test_*.py"))}
    assert on_disk, "no test files found under tests/nos_lab/"

    unclassified = on_disk - dialect - feature
    assert not unclassified, (
        f"{sorted(unclassified)} appear in neither NOS_DIALECT_TESTS nor "
        "NOS_FEATURE_TESTS in the Makefile, so nothing runs them: a dialect "
        "suite (one driver against one lab node, no stack) belongs in the "
        "first list and runs on every PR, a via-stack suite belongs in the "
        "second and runs in nightly. See issue #785 and docs/NOS_LAB.md."
    )

    missing = (dialect | feature) - on_disk
    assert not missing, f"the Makefile lists {sorted(missing)}, which do not exist on disk"


def test_ci_runs_the_dialect_target():
    steps = _workflow_steps(CI_WORKFLOW_PATH)
    _step_index(steps, "make nos-test-dialect")


def test_ci_dialect_job_is_independent_and_uploads_lab_logs_on_failure():
    data = yaml.safe_load(CI_WORKFLOW_PATH.read_text())
    job = data["jobs"]["nos-dialect"]
    assert "needs" not in job, (
        "the nos-dialect job must not depend on another job, so it runs in "
        "parallel instead of adding its runtime to backend or frontend"
    )
    uploads = [
        step
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/upload-artifact")
    ]
    assert [step["with"]["name"] for step in uploads] == ["nos-dialect-logs"]
    assert all(step.get("if") == "failure()" for step in uploads)


def test_nightly_runs_the_feature_target_with_the_right_project_name():
    steps = _workflow_steps(NIGHTLY_WORKFLOW_PATH)
    index = _step_index(steps, "make nos-test-feature")
    run = steps[index]["run"]
    assert "make nos-test-feature COMPOSE_PROJECT_NAME=herd" in run, (
        "nightly's stack is the default-named `herd` compose project, so the "
        "feature target must be called with COMPOSE_PROJECT_NAME=herd or "
        "nos-attach looks for a CURDIR-derived network that does not exist"
    )
    assert "make nos-up" in run, "the feature target does not boot the lab itself"


def test_nightly_runs_the_feature_tests_between_the_seeded_e2e_and_the_load_test():
    steps = _workflow_steps(NIGHTLY_WORKFLOW_PATH)
    seeded_e2e = _step_index(steps, "make test-e2e-seeded")
    feature = _step_index(steps, "make nos-test-feature")
    load = _step_index(steps, "make test-load")

    assert seeded_e2e < feature, (
        "the L2 feature suite reuses the nos-lab-* devices `make seed-nos` "
        "creates, so it must run after the stack is seeded"
    )
    assert feature < load, (
        "the feature suites hold real reservations over real devices; locust "
        "reserves across the whole seeded inventory, so running the two "
        "concurrently would race for the same ports"
    )


def test_nightly_always_stops_the_lab():
    steps = _workflow_steps(NIGHTLY_WORKFLOW_PATH)
    index = _step_index(steps, "make nos-down")
    assert steps[index].get("if") == "always()", (
        "the lab teardown must run even when a phase failed, the same way "
        "`Stop LDAP test server` does"
    )


def test_nightly_collects_stack_diagnostics_after_the_feature_tests():
    """The issue #648 ordering invariant, extended to the new step: an
    `if: failure()` step is evaluated at its own position, so a phase that
    runs after it gets no diagnostics at all."""
    steps = _workflow_steps(NIGHTLY_WORKFLOW_PATH)
    feature = _step_index(steps, "make nos-test-feature")
    diagnostics = _step_index(steps, "make _collect-stack-diagnostics")
    assert feature < diagnostics
    assert steps[diagnostics].get("if") == "failure()"
