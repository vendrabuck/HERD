"""Issue #911: every durable pull consumer must run the shared in-progress
heartbeat, so no in-flight message reaches ack_wait while its handler runs.

Structural, deliberately coarse: any service module that calls
`pull_subscribe(` must also run its batches through `process_batch_with_heartbeat`
from herd_common.jetstream (execution since issue #944; no module keeps an inline
heartbeat loop, whose hand-rolled cancel handling once swallowed a shutdown cancel)
and (issue #944) take `NATS_ACK_WAIT_SECONDS` from settings and
`NATS_HEARTBEAT_SECONDS` from `herd_common.jetstream.heartbeat_interval`, never a
hardcoded value. A new consumer module that skips the heartbeat fails here. The
behavioral proof lives in each service's own tests.
"""

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONSUMER_MODULES = sorted(
    p for p in (REPO / "services").glob("*/app/**/*.py") if "pull_subscribe(" in p.read_text()
)


def test_consumer_modules_are_discovered():
    names = {p.parts[-4] for p in CONSUMER_MODULES}
    assert {"execution", "integration", "notifications"} <= names


# The shared helper's own name, plus the private alias the #944 inline shape used.
_HEARTBEAT_HELPER_NAMES = {"keep_messages_alive", "_keep_messages_alive"}


def find_inline_heartbeats(source: str) -> list[int]:
    """Line numbers where a module runs its own heartbeat instead of the shared
    `process_batch_with_heartbeat`.

    Two behaviors count, whatever the spelling: a call to `keep_messages_alive`
    under its own name, a private name, an import alias (`import ... as _ka`), or
    as a module attribute (`jetstream.keep_messages_alive(...)`); and any
    `.in_progress(` call, which is what a hand-rolled heartbeat loop must make.
    A mention in a comment or docstring is not a call and is not counted.
    """
    tree = ast.parse(source)
    helper_names = set(_HEARTBEAT_HELPER_NAMES)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "keep_messages_alive":
                    helper_names.add(alias.asname or alias.name)
    hits: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in helper_names:
            hits.add(node.lineno)
        elif isinstance(func, ast.Attribute) and (
            func.attr == "in_progress" or func.attr in _HEARTBEAT_HELPER_NAMES
        ):
            hits.add(node.lineno)
    return sorted(hits)


def test_detector_flags_every_spelling_of_an_own_heartbeat():
    src = (
        "from herd_common.jetstream import keep_messages_alive as _ka\n"
        "import herd_common.jetstream as js\n"
        "async def historic(msg):\n"
        "    hb = asyncio.create_task(_keep_messages_alive([msg]))\n"
        "async def plain(msg):\n"
        "    await keep_messages_alive([msg], 5)\n"
        "async def aliased(msg):\n"
        "    hb = asyncio.create_task(_ka([msg], 5))\n"
        "async def attribute(msg):\n"
        "    hb = asyncio.create_task(js.keep_messages_alive([msg], 5))\n"
        "async def hand_rolled(msg):\n"
        "    while True:\n"
        "        await msg.in_progress()\n"
        "        await asyncio.sleep(5)\n"
    )
    assert find_inline_heartbeats(src) == [4, 6, 8, 10, 13]


def test_detector_ignores_the_shared_helper_and_mentions():
    src = (
        '"""Every message gets `in_progress` through keep_messages_alive."""\n'
        "from herd_common.jetstream import process_batch_with_heartbeat\n"
        "# keep_messages_alive( and msg.in_progress() are mentioned here only\n"
        "async def process_batch(msgs):\n"
        "    await process_batch_with_heartbeat(msgs, handle, 5)\n"
        "outcome = 'in_progress'\n"
    )
    assert find_inline_heartbeats(src) == []


def test_no_pull_consumer_module_keeps_an_inline_heartbeat():
    """Issue #944: the inline `try: await heartbeat / except CancelledError: pass`
    shape cannot tell a cancel aimed at the consumer from the heartbeat's own.
    Any own heartbeat, under any spelling, is refused (issue #1145)."""
    inline = [
        f"{p.relative_to(REPO)}:{lineno}"
        for p in CONSUMER_MODULES
        for lineno in find_inline_heartbeats(p.read_text())
    ]
    assert not inline, f"pull consumer with its own heartbeat task: {inline}"


def test_every_pull_consumer_module_uses_the_shared_heartbeat():
    missing = [
        str(p.relative_to(REPO))
        for p in CONSUMER_MODULES
        if "process_batch_with_heartbeat" not in p.read_text()
    ]
    assert not missing, f"pull consumer without the #911 heartbeat: {missing}"


def test_every_pull_consumer_module_takes_ack_wait_and_heartbeat_from_one_source():
    """Issue #944: ack_wait comes from settings and the heartbeat cadence from the
    shared `heartbeat_interval` helper (half of ack_wait, derived once). A module
    that hardcodes either, or computes its own fraction, fails here."""
    for p in CONSUMER_MODULES:
        text = p.read_text()
        assert re.search(
            r"^NATS_ACK_WAIT_SECONDS = settings\.nats_ack_wait_seconds$", text, re.M
        ), f"{p}: NATS_ACK_WAIT_SECONDS must come from settings.nats_ack_wait_seconds"
        assert re.search(
            r"^NATS_HEARTBEAT_SECONDS = heartbeat_interval\(NATS_ACK_WAIT_SECONDS\)$",
            text,
            re.M,
        ), f"{p}: NATS_HEARTBEAT_SECONDS must be heartbeat_interval(NATS_ACK_WAIT_SECONDS)"
        assert not re.search(r"ack_wait\s*=\s*\d", text), f"{p}: hardcoded ack_wait"
