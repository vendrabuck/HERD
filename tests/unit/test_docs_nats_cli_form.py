"""Guard for issue #991: no tracked document runs the `nats` CLI inside the NATS service.

The compose `nats` service runs `nats:2.10-alpine`, which ships only
`nats-server`, so `docker compose exec nats nats ...` fails with "executable
file not found". The operator documents use a one-off `natsio/nats-box`
container on the stack network instead. This fails if the broken form comes
back in any tracked markdown or HTML file, and checks that the image the docs
rely on is still the CLI-less one, so the guard is retired deliberately if the
service image ever changes.
"""

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BROKEN_FORM = re.compile(r"exec\s+(-\S+\s+)*nats\s+nats\b")


def _tracked_docs() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "*.md", "*.html"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [REPO_ROOT / line for line in out.splitlines() if line]


def test_no_tracked_doc_runs_the_nats_cli_inside_the_nats_service():
    docs = _tracked_docs()
    assert docs, "git ls-files found no documents"
    offenders = []
    for path in docs:
        if not path.is_file():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if BROKEN_FORM.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}")
    assert offenders == [], "use a natsio/nats-box container instead:\n" + "\n".join(offenders)


def test_pattern_catches_the_broken_form():
    assert BROKEN_FORM.search("docker compose exec nats nats stream info HERD_DLQ")
    assert BROKEN_FORM.search("docker compose exec -T nats nats pub x y")
    assert not BROKEN_FORM.search("docker run -i --rm natsio/nats-box:0.14.5 nats --server x")


def test_nats_service_image_is_the_cli_less_server_image():
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert re.search(r"^\s+image:\s*nats:2\.10-alpine\s*$", compose, re.MULTILINE), (
        "the nats service image changed; recheck whether it ships the nats CLI "
        "before keeping or retiring this guard (issue #991)"
    )


def test_operations_dlq_section_uses_nats_box():
    text = (REPO_ROOT / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
    assert "natsio/nats-box:0.14.5" in text
