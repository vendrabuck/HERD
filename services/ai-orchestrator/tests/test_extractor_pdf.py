"""The PDF reader in a kernel-capped child process (issue #1202).

Sections, in order: hostile PDFs end with a fixed refusal inside the limits, with the
pytest process's memory unchanged and no child left; the parent-side protections of
``capped_child.run_capped_child``, each against a stand-in child written here; the real
child's limits are in force before pdfplumber is imported or the upload read, and its
failure statuses map to their fixed answers; honest PDFs read exactly as the reader
before issue #1202 read them, the character budget and its truncated flag included; and
the walk stops at the budget.

Every hostile read here is itself safe: the child is capped. Each test also has a
wall-clock timeout, so a broken build fails instead of hanging the suite.
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time

import pytest
from app import config as config_module
from app.services import _pdf_child, extractor
from app.services.capped_child import Outcome, classify, run_capped_child
from app.services.extractor import (
    PDF_MEMORY_DETAIL,
    PDF_READER_BUSY_DETAIL,
    PDF_READER_FAILED_DETAIL,
    PDF_UNAVAILABLE_DETAIL,
    ExtractionBusy,
    ExtractionError,
    ExtractionUnavailable,
    extract_files,
)

from tests._pdf_builders import (
    flate_repeated,
    honest_pdfs,
    hostile_pdfs,
    pages_pdf,
    plain_read,
    stream,
    text_page,
    truetype_cmap_pdf,
)

pytestmark = pytest.mark.timeout(120)

_MIB = 1024 * 1024
# The limits the hostile table runs under: the shipped address space, and CPU and wall
# limits lowered so a CPU-bound shape costs seconds, not the shipped 20 and 30.
_TABLE_CPU_SECONDS = 4
_TABLE_WALL_SECONDS = 8
# How much the pytest process's resident peak may grow across one read: the upload is
# built before the peak is reset, so this covers the copy the pipe write may hold and
# the text kept.
_PARENT_GROWTH_KIB = 32 * 1024

MEMORY = PDF_MEMORY_DETAIL
CPU = (
    f"The PDF needs more than {_TABLE_CPU_SECONDS} seconds of processing to read; "
    "nothing was extracted"
)
DEADLINE = f"The PDF took longer than {_TABLE_WALL_SECONDS} seconds to read; nothing was extracted"


def _parse(name: str) -> str:
    return f"Failed to parse PDF ({name})"


# --- Process and memory probes ---


def live_children() -> set[int]:
    """Every child of this process, zombies included, from every thread's list."""
    pids: set[int] = set()
    for tid in os.listdir("/proc/self/task"):
        try:
            with open(f"/proc/self/task/{tid}/children") as f:
                pids |= {int(p) for p in f.read().split()}
        except FileNotFoundError:
            continue
    return pids


def _vm_hwm_kib() -> int:
    with open("/proc/self/status") as status:
        for line in status:
            if line.startswith("VmHWM:"):
                return int(line.split()[1])
    raise RuntimeError("no VmHWM")


def _reset_peak() -> int:
    """Reset this process's resident peak to its current size (TESTING.md: VmHWM after
    clear_refs 5, never ru_maxrss) and return it."""
    with open("/proc/self/clear_refs", "w") as clear_refs:
        clear_refs.write("5")
    return _vm_hwm_kib()


def _process_gone(pid: int) -> bool:
    """Whether `pid` no longer runs: absent, or a zombie waiting for its reaper."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] == "Z"
    except FileNotFoundError:
        return True


async def _wait_gone(pid: int, seconds: float = 5) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _process_gone(pid):
            return True
        await asyncio.sleep(0.02)
    return _process_gone(pid)


async def _outcome(data: bytes) -> str:
    try:
        await extract_files([("doc.pdf", data)])
    except ExtractionError as e:
        return str(e)
    return "ok"


@pytest.fixture
def table_limits(monkeypatch):
    monkeypatch.setattr(extractor, "PDF_CPU_SECONDS", _TABLE_CPU_SECONDS)
    monkeypatch.setattr(extractor, "PDF_WALL_SECONDS", _TABLE_WALL_SECONDS)


# --- Hostile PDFs ---

# The answer each shape ends with under the shipped address space and the table's CPU
# and wall limits, as measured on the development host. A shape that took more than a
# second there may also end with the CPU refusal on a slower host (its allocation and
# its CPU time race); both are fixed refusals inside the limits. "ok" is a shape the
# reader finishes inside the limits (the issue's 200 MiB stream inflates to 432 MiB
# resident in the child, under the address-space limit, and holds no text).
_EXPECTED = {
    "the-issues-200-mib-flate": {"ok", CPU},
    "flate-inside-flate": {"ok", CPU},
    "lzw-runs": {CPU},
    "ascii85-z-behind-flate": {MEMORY, CPU},
    "runlength-behind-flate": {MEMORY},
    "png-predictor-huge-columns": {MEMORY},
    "png-predictor-one-huge-row-via-colors": {MEMORY, CPU},
    "tiff-predictor-row": {MEMORY, CPU},
    "ccitt-huge-columns": {_parse("PdfminerException"), CPU},
    "truncated-flate-bomb": {CPU},
    "tounicode-range-of-4e9": {MEMORY, CPU},
    "two-million-q": {CPU, MEMORY},
    "200k-rectangles": {CPU, MEMORY},
    "form-drawn-10000-times": {CPU},
    "object-stream-of-15m-numbers": {CPU, MEMORY},
    "450k-fresh-names": {CPU},
    "cid-widths-range-40m": {MEMORY},
    "truetype-cmap-32767-full-segments": {CPU},
    "rc4-stream-of-4.9-mb": {CPU},
    "open-bracket-4.19m-in-content": {MEMORY, CPU},
    "open-dict-2.09m-in-content": {CPU},
    "page-tree-5000-deep": {_parse("PdfminerException")},
    "xref-stream-index-2e9": {CPU},
}


def test_every_hostile_shape_has_an_expected_answer():
    assert set(_EXPECTED) == set(hostile_pdfs())


@pytest.mark.parametrize("shape", sorted(hostile_pdfs()))
async def test_hostile_pdf_ends_inside_the_limits(shape, table_limits):
    """Each shape the first attempt and its review found ends with its fixed answer
    within the wall-clock limit plus a margin, the pytest process's resident peak grows
    by less than the margin, and no child is left, whatever the shape does in the
    child."""
    data = hostile_pdfs()[shape]()
    assert len(data) <= config_module.settings.upload_max_file_bytes
    children = live_children()
    base = _reset_peak()
    start = time.monotonic()

    outcome = await _outcome(data)

    elapsed = time.monotonic() - start
    assert outcome in _EXPECTED[shape], outcome
    assert elapsed < _TABLE_WALL_SECONDS + 2
    assert _vm_hwm_kib() - base < _PARENT_GROWTH_KIB
    assert live_children() == children


@pytest.mark.timeout(90)
async def test_truetype_cmap_loop_ends_at_the_shipped_cpu_limit():
    """At the shipped limits (no lowering), the cmap loop of about 2e9 steps ends with
    the CPU refusal, near the 20 CPU seconds and before the 30-second deadline."""
    start = time.monotonic()
    outcome = await _outcome(truetype_cmap_pdf(32_767))
    elapsed = time.monotonic() - start
    assert outcome == (
        "The PDF needs more than 20 seconds of processing to read; nothing was extracted"
    )
    assert extractor.PDF_CPU_SECONDS <= elapsed < extractor.PDF_WALL_SECONDS


async def test_a_refusal_logs_its_outcome_word_and_no_file_text(caplog, table_limits):
    data = b"%PDF-1.4\n1 0 obj << /SECRETFILETEXT (SECRETFILETEXT) >> endobj\ntrailer << >>\n"
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ExtractionError) as exc:
        await extract_files([("doc.pdf", data)])
    assert "SECRETFILETEXT" not in str(exc.value)
    assert all("SECRETFILETEXT" not in r.getMessage() for r in caplog.records)
    refusals = [r for r in caplog.records if r.getMessage() == "pdf read refused"]
    assert [r.outcome for r in refusals] == ["parse-error"]


# --- Parent-side protections, against stand-in children ---

_FAKE_PREFIX = "FAKE-STATUS"


def _script(tmp_path, body: str) -> str:
    path = tmp_path / "child.py"
    path.write_text("import os, signal, sys, time\n" + body)
    return str(path)


async def _run(script: str, *, wall: float = 10, stdout_cap: int = 1024, stdin: bytes = b""):
    return await run_capped_child(
        script,
        [],
        stdin,
        wall_seconds=wall,
        max_stdout_bytes=stdout_cap,
        status_prefix=_FAKE_PREFIX,
        exit_codes=_pdf_child.EXIT_CODES,
    )


@pytest.mark.parametrize("fd", [1, 2], ids=["stdout", "stderr"])
async def test_a_child_that_writes_without_end_is_killed_at_the_cap(tmp_path, fd):
    """A child writing 64 KiB chunks forever, to stdout or to stderr: the parent keeps
    at most the cap, kills the child's group at the first byte past it, and reads the
    result as malformed, in well under a second, its own peak unchanged."""
    script = _script(tmp_path, f"while True:\n    os.write({fd}, b'x' * 65536)\n")
    children = live_children()
    base = _reset_peak()
    start = time.monotonic()
    result = await _run(script, stdout_cap=256 * 1024)
    assert result.outcome is Outcome.MALFORMED
    assert time.monotonic() - start < 5
    assert _vm_hwm_kib() - base < _PARENT_GROWTH_KIB
    assert live_children() == children


async def test_a_child_that_ignores_sigterm_is_killed_at_the_deadline(tmp_path):
    script = _script(
        tmp_path,
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(600)\n",
    )
    children = live_children()
    start = time.monotonic()
    result = await _run(script, wall=1)
    assert result.outcome is Outcome.DEADLINE
    assert time.monotonic() - start < 3
    assert live_children() == children


async def test_a_grandchild_that_outlives_the_child_is_killed_with_its_group(tmp_path):
    """The child forks a grandchild that keeps the output pipes open and sleeps, then
    exits ok. The group kill after the child's exit ends the grandchild, so the pipes
    close and the read finishes at once instead of at the deadline."""
    script = _script(
        tmp_path,
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    time.sleep(600)\n"
        "    os._exit(0)\n"
        "os.write(1, str(pid).encode())\n"
        "os.write(2, b'FAKE-STATUS ok\\n')\n"
        "os._exit(0)\n",
    )
    start = time.monotonic()
    result = await _run(script, wall=20)
    assert result.outcome is Outcome.OK
    assert time.monotonic() - start < 5
    assert await _wait_gone(int(result.stdout))


async def test_the_child_leads_its_own_group_with_an_empty_environment_and_three_descriptors(
    tmp_path, monkeypatch
):
    """No setting, token, or key of the service reaches the child, and no descriptor
    but its three pipes: the pytest process holds descriptors past 2 and a secret in its
    environment, and the child sees neither."""
    monkeypatch.setenv("SECRET_KEY_PROBE", "do-not-pass")
    held = os.open(os.devnull, os.O_RDONLY)
    try:
        script = _script(
            tmp_path,
            "import json\n"
            "def is_open(fd):\n"
            "    try:\n"
            "        os.fstat(fd)\n"
            "        return True\n"
            "    except OSError:\n"
            "        return False\n"
            "out = {'env': dict(os.environ), 'fds': [fd for fd in range(3, 1024) if is_open(fd)],\n"
            "       'leads_group': os.getpgrp() == os.getpid(), 'cwd': os.getcwd(),\n"
            "       'flags': [sys.flags.isolated, sys.flags.dont_write_bytecode]}\n"
            "os.write(1, json.dumps(out).encode())\n"
            "os.write(2, b'FAKE-STATUS ok\\n')\n",
        )
        result = await _run(script, stdout_cap=64 * 1024)
    finally:
        os.close(held)
    seen = json.loads(result.stdout)
    assert set(seen["env"]) <= {"LC_CTYPE"}
    assert seen["fds"] == []
    assert seen["leads_group"] is True
    assert seen["cwd"] == "/"
    assert seen["flags"] == [1, 1]


@pytest.mark.parametrize(
    ("returncode", "stderr", "expected"),
    [
        (0, b"FAKE-STATUS ok\n", (Outcome.OK, None)),
        (0, b"noise\nFAKE-STATUS ok\n\n", (Outcome.OK, None)),
        (2, b"FAKE-STATUS parse-error KeyError\n", (Outcome.PARSE_ERROR, "KeyError")),
        (3, b"FAKE-STATUS memory\n", (Outcome.MEMORY, None)),
        (4, b"FAKE-STATUS limits-failed\n", (Outcome.LIMITS_FAILED, None)),
        (5, b"FAKE-STATUS unavailable\n", (Outcome.UNAVAILABLE, None)),
        (6, b"FAKE-STATUS usage\n", (Outcome.MALFORMED, None)),
        (0, b"", (Outcome.MALFORMED, None)),
        (0, b"FAKE-STATUS bogus\n", (Outcome.MALFORMED, None)),
        (0, b"FAKE-STATUS ok\ntrailing\n", (Outcome.MALFORMED, None)),
        (0, b"FAKE-STATUS ok extra\n", (Outcome.MALFORMED, None)),
        (3, b"FAKE-STATUS ok\n", (Outcome.MALFORMED, None)),
        (0, b"FAKE-STATUS memory\n", (Outcome.MALFORMED, None)),
        (2, b"FAKE-STATUS parse-error Key Error: file text\n", (Outcome.MALFORMED, None)),
        (2, b"FAKE-STATUS parse-error 9Bad\n", (Outcome.MALFORMED, None)),
        (0, b"OTHER-STATUS ok\n", (Outcome.MALFORMED, None)),
        (-signal.SIGXCPU, b"", (Outcome.CPU, None)),
        (-signal.SIGKILL, b"", (Outcome.MEMORY, None)),
        (-signal.SIGSEGV, b"FAKE-STATUS ok\n", (Outcome.MEMORY, None)),
        (-signal.SIGABRT, b"", (Outcome.MEMORY, None)),
        (-signal.SIGTERM, b"", (Outcome.MALFORMED, None)),
    ],
)
def test_the_status_is_read_strictly(returncode, stderr, expected):
    assert classify(returncode, stderr, _FAKE_PREFIX, _pdf_child.EXIT_CODES) == expected


async def test_a_child_that_exits_zero_with_a_malformed_status_is_a_reader_failure(
    tmp_path, monkeypatch
):
    script = _script(tmp_path, "os.write(1, b'text')\nos.write(2, b'HERD-PDF-STATUS fine\\n')\n")
    monkeypatch.setattr(extractor, "_PDF_CHILD_PATH", script)
    with pytest.raises(ExtractionUnavailable) as exc:
        await extract_files([("doc.pdf", b"%PDF-1.4")])
    assert str(exc.value) == PDF_READER_FAILED_DETAIL


async def test_a_child_that_cannot_start_is_a_reader_failure(monkeypatch):
    monkeypatch.setattr(sys, "executable", "/nonexistent/python")
    with pytest.raises(ExtractionUnavailable) as exc:
        await extract_files([("doc.pdf", b"%PDF-1.4")])
    assert str(exc.value) == PDF_READER_FAILED_DETAIL


async def test_cancelling_the_read_kills_and_reaps_the_child(tmp_path):
    script = _script(tmp_path, "time.sleep(600)\n")
    children = live_children()
    task = asyncio.ensure_future(_run(script, wall=60))
    deadline = time.monotonic() + 10
    while not (live_children() - children) and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    (pid,) = live_children() - children
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert live_children() == children
    with pytest.raises(ProcessLookupError):
        os.killpg(pid, 0)


@pytest.mark.parametrize("wait", [1, 15], ids=["wait-runs-out", "wait-suffices"])
async def test_at_most_the_cap_of_children_run_at_once(tmp_path, monkeypatch, wait):
    """Three reads at once with a cap of two, each child holding its slot two seconds:
    no more than two children ever exist. With a one-second wait the third read gets
    the busy refusal; with a longer one it waits its turn and reads."""
    script = _script(
        tmp_path, "time.sleep(2)\nos.write(1, b'done')\nos.write(2, b'HERD-PDF-STATUS ok\\n')\n"
    )
    monkeypatch.setattr(extractor, "_PDF_CHILD_PATH", script)
    monkeypatch.setattr(extractor, "MAX_CONCURRENT_PDF_READS", 2)
    monkeypatch.setattr(extractor, "PDF_SLOT_WAIT_SECONDS", wait)
    children = live_children()
    reads = [asyncio.ensure_future(extract_files([(f"doc{i}.pdf", b"%PDF-1.4")])) for i in range(3)]
    most = 0
    while not all(r.done() for r in reads):
        most = max(most, len(live_children() - children))
        await asyncio.sleep(0.01)
    results = await asyncio.gather(*reads, return_exceptions=True)
    assert most == 2
    if wait == 1:
        assert sum(isinstance(r, ExtractionBusy) for r in results) == 1
        busy = next(r for r in results if isinstance(r, ExtractionBusy))
        assert str(busy) == PDF_READER_BUSY_DETAIL
    else:
        assert [r[0].text for r in results] == ["done"] * 3


def test_the_reader_runs_under_uvloop():
    """uvicorn runs the service on uvloop when it is installed, as it is in the image:
    an honest read and a deadline kill behave the same on it."""
    uvloop = pytest.importorskip("uvloop")
    data = honest_pdfs()["three-flate-pages"]

    async def both():
        text = (await extract_files([("doc.pdf", data)]))[0].text
        start = time.monotonic()
        children = live_children()
        extractor_wall = extractor.PDF_WALL_SECONDS
        extractor.PDF_WALL_SECONDS = 1
        try:
            outcome = await _outcome(truetype_cmap_pdf(32_767))
        finally:
            extractor.PDF_WALL_SECONDS = extractor_wall
        return text, outcome, time.monotonic() - start, live_children() == children

    with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
        text, outcome, elapsed, clean = runner.run(both())
    assert text == "Page one\n\nPage two\n\nPage three"
    assert outcome == "The PDF took longer than 1 seconds to read; nothing was extracted"
    assert elapsed < 3
    assert clean


# --- The real child: limits first, failure statuses ---


def _wrapper(tmp_path, prelude: str) -> str:
    """A script that runs `prelude`, then the real child as __main__ with the same
    arguments, so a test can observe or break the child from inside."""
    path = tmp_path / "wrapper.py"
    path.write_text(
        "import os, resource, runpy, sys\n"
        + prelude
        + f"\nsys.argv = [{extractor._PDF_CHILD_PATH!r}] + sys.argv[1:]\n"
        + f"runpy.run_path({extractor._PDF_CHILD_PATH!r}, run_name='__main__')\n"
    )
    return str(path)


_LIMITS_GUARD = """
def limits_in_force():
    return (
        resource.getrlimit(resource.RLIMIT_AS) == (%(as_)d, %(as_)d)
        and resource.getrlimit(resource.RLIMIT_CPU) == (%(cpu)d, %(cpu)d + 1)
        and resource.getrlimit(resource.RLIMIT_FSIZE) == (0, 0)
        and resource.getrlimit(resource.RLIMIT_NOFILE) == (%(nofile)d, %(nofile)d)
        and resource.getrlimit(resource.RLIMIT_CORE) == (0, 0)
    )

class RefuseParserBeforeLimits:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("pdfplumber", "pdfminer") and not limits_in_force():
            raise ImportError("the parser was imported before the limits")
        return None

class GuardedStdin:
    def __init__(self, real):
        self._real = real

    @property
    def buffer(self):
        if not limits_in_force():
            raise RuntimeError("the upload was read before the limits")
        return self._real.buffer

assert not limits_in_force()
sys.meta_path.insert(0, RefuseParserBeforeLimits())
sys.stdin = GuardedStdin(sys.stdin)
"""


async def test_the_child_applies_every_limit_before_the_parser_or_the_upload(tmp_path, monkeypatch):
    """The wrapper refuses to import pdfplumber or pdfminer, and to hand over the
    upload, unless all five limits are in force at the values the parent passed: an
    honest read through it succeeds only if the child applies them first."""
    guard = _LIMITS_GUARD % {
        "as_": extractor.PDF_ADDRESS_SPACE_BYTES,
        "cpu": extractor.PDF_CPU_SECONDS,
        "nofile": extractor.PDF_NOFILE,
    }
    monkeypatch.setattr(extractor, "_PDF_CHILD_PATH", _wrapper(tmp_path, guard))
    files = await extract_files([("doc.pdf", honest_pdfs()["three-flate-pages"])])
    assert files[0].text == "Page one\n\nPage two\n\nPage three"


async def test_a_child_without_pdfplumber_answers_the_unavailable_400(tmp_path, monkeypatch):
    monkeypatch.setattr(
        extractor, "_PDF_CHILD_PATH", _wrapper(tmp_path, "sys.modules['pdfplumber'] = None")
    )
    with pytest.raises(ExtractionError) as exc:
        await extract_files([("doc.pdf", honest_pdfs()["three-flate-pages"])])
    assert str(exc.value) == PDF_UNAVAILABLE_DETAIL


@pytest.mark.parametrize(
    "prelude",
    [
        "def refuse(*a):\n    raise OSError('no')\nresource.setrlimit = refuse",
        "real = resource.getrlimit\nresource.getrlimit = lambda r: (1, 1)",
    ],
    ids=["setrlimit-fails", "limit-reads-back-different"],
)
async def test_a_child_that_cannot_apply_its_limits_reads_nothing(tmp_path, monkeypatch, prelude):
    """Fail closed: a limit that cannot be set, or reads back different, ends the child
    before the upload is read; the request is the reader failure, never a read without
    limits."""
    monkeypatch.setattr(extractor, "_PDF_CHILD_PATH", _wrapper(tmp_path, prelude))
    with pytest.raises(ExtractionUnavailable) as exc:
        await extract_files([("doc.pdf", honest_pdfs()["three-flate-pages"])])
    assert str(exc.value) == PDF_READER_FAILED_DETAIL


def test_caused_by_memory_follows_the_chain():
    try:
        try:
            raise MemoryError
        except MemoryError as inner:
            raise ValueError("wrapped") from inner
    except ValueError as outer:
        assert _pdf_child.caused_by_memory(outer)
    assert not _pdf_child.caused_by_memory(KeyError("x"))


# --- Honest PDFs read as before ---


def _main_reader(data: bytes, budget: int) -> tuple[str, bool]:
    """What extract_files returned for one PDF before issue #1202: pdfplumber's walk of
    every page, cut to the budget."""
    text = plain_read(data)
    return text[:budget], len(text) > budget


@pytest.mark.parametrize("name", sorted(honest_pdfs()))
async def test_honest_pdf_reads_as_the_reader_before_did(name, monkeypatch):
    data = honest_pdfs()[name]
    full = plain_read(data)
    assert full
    for budget in (10**9, len(full), len(full) - 1):
        monkeypatch.setattr(config_module.settings, "upload_max_extracted_chars", budget)
        (got,) = await extract_files([("doc.pdf", data)])
        assert (got.text, got.truncated) == _main_reader(data, budget), budget


def test_honest_pdf_texts_are_the_ones_the_builders_wrote():
    """A control on the builders: the filtered streams really decode to their text."""
    pdfs = honest_pdfs()
    assert plain_read(pdfs["three-flate-pages"]) == "Page one\n\nPage two\n\nPage three"
    for name in ("flate-twice", "lzw", "flate-ascii85", "asciihex", "runlength"):
        assert plain_read(pdfs[name]) == "Hello from a filtered stream"
    for name in ("png-predictor", "tiff-predictor"):
        assert plain_read(pdfs[name]) == "Predicted rows"
    assert plain_read(pdfs["rc4-encrypted"]) == "Encrypted words"
    assert plain_read(pdfs["tounicode-map"]) == "XXX"


# --- The walk stops at the budget ---


def _two_pages_then_a_bomb() -> bytes:
    """Two text pages, then a page whose content is a predictor row the child cannot
    allocate: reading the third page ends in the memory refusal."""
    bomb = stream(
        b"/Filter /FlateDecode /DecodeParms << /Predictor 11 /Colors 30000000 /Columns 1 >>",
        flate_repeated(b"\0", 30_000_000, head=b"\x01"),
    )
    return pages_pdf([text_page(b"Page one"), text_page(b"Page two"), bomb])


async def test_the_walk_stops_at_the_page_that_passes_the_budget(monkeypatch):
    """With the budget one character short of the first two pages, the walk stops after
    page two and the third page, which would be refused, is never read. At exactly the
    two pages' length nothing has passed the budget yet, so the walk goes on and the
    third page is read and refused: the stop is at the first character past the
    budget."""
    data = _two_pages_then_a_bomb()
    two_pages = "Page one\n\nPage two"
    monkeypatch.setattr(config_module.settings, "upload_max_extracted_chars", len(two_pages) - 1)
    (got,) = await extract_files([("doc.pdf", data)])
    assert (got.text, got.truncated) == (two_pages[:-1], True)

    monkeypatch.setattr(config_module.settings, "upload_max_extracted_chars", len(two_pages))
    with pytest.raises(ExtractionError) as exc:
        await extract_files([("doc.pdf", data)])
    assert str(exc.value) == PDF_MEMORY_DETAIL


async def test_a_second_file_gets_what_the_first_left(monkeypatch):
    monkeypatch.setattr(config_module.settings, "upload_max_extracted_chars", 12)
    pdf = honest_pdfs()["three-flate-pages"]
    first, second = await extract_files([("a.pdf", pdf), ("b.pdf", pdf)])
    assert (first.text, first.truncated) == ("Page one\n\nPa", True)
    assert (second.text, second.truncated) == ("", True)
