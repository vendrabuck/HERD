"""The child process that reads one uploaded PDF under kernel limits (issue #1202).

Usage, as ``app.services.capped_child`` starts it:

    python -I -B _pdf_child.py <budget> <address_space_bytes> <cpu_seconds> <nofile>

The upload arrives on stdin. The child applies its resource limits FIRST, before it
reads a byte of the upload and before pdfplumber (or anything that imports it) is
importable, so every line of the parser runs under them, including code paths nobody
has audited:

- ``RLIMIT_AS`` (address space): an allocation past it fails, which Python raises as
  MemoryError and C code may answer with an abort or a segmentation fault;
- ``RLIMIT_CPU``: soft at ``cpu_seconds`` (the kernel sends SIGXCPU, whose default
  action ends the process) and hard one second later (SIGKILL);
- ``RLIMIT_FSIZE`` 0: the child cannot write a file (a pipe is not a file);
- ``RLIMIT_NOFILE``: a few descriptors, enough for the imports;
- ``RLIMIT_CORE`` 0: no core file.

A limit that cannot be applied, or that reads back different, ends the child before
the upload is read, with the ``limits-failed`` status: the read fails closed.

The child writes the extracted text to stdout and nothing else, then exactly one status
line to stderr, ``HERD-PDF-STATUS <word>`` with the word from a fixed set (a parse error
adds the exception's class name, never its message), and exits with the code that
belongs to the word. The parent parses only that line and checks the code agrees.
Logging and warnings are switched off before pdfplumber is imported, so pdfminer's
messages, which quote the file, never reach the parent.

It is started in isolated mode (``-I``, as the execution service starts its driver
sandbox, issue #1180), so no ``PYTHON*`` variable, user site directory, or current
directory reaches the interpreter, and with ``-B`` so it writes no bytecode. It imports
only the standard library until the limits are in force, and nothing from the service:
it must not depend on settings or on the service's environment, which it does not get.
"""

import os
import re
import resource
import sys

STATUS_PREFIX = "HERD-PDF-STATUS"

# Each status word and the exit code that must come with it.
EXIT_CODES = {
    "ok": 0,
    "parse-error": 2,
    "memory": 3,
    "limits-failed": 4,
    "unavailable": 5,
    "usage": 6,
}

_CLASS_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}")

# Every fixed status line, built at import: the child may reach _finish with no memory
# left (the traceback of a MemoryError keeps the parser's objects alive), and a line
# built then would raise MemoryError again and end the child with no status at all.
_LINES = {word: f"{STATUS_PREFIX} {word}\n".encode("ascii") for word in EXIT_CODES}


def _finish(word: str, class_name: str | None = None) -> None:
    """Write the status line and end the process at once, without cleanup.

    ``os._exit`` skips interpreter shutdown, which needs memory the child may not have
    left after a MemoryError. If even the parse-error line cannot be built, the
    allocation that failed is the outcome: the child ends with the memory status.
    """
    line = _LINES[word]
    if class_name is not None:
        try:
            name = class_name if _CLASS_NAME.fullmatch(class_name) else "Exception"
            line = f"{STATUS_PREFIX} {word} {name}\n".encode("ascii")
        except MemoryError:
            word, line = "memory", _LINES["memory"]
    os.write(2, line)
    os._exit(EXIT_CODES[word])


def caused_by_memory(error: BaseException) -> bool:
    """Whether a MemoryError is `error` or in its cause or context chain.

    pdfplumber re-raises whatever pdfminer raises while it opens the file or lists the
    pages as its own PdfminerException, so an allocation refused by RLIMIT_AS there
    reaches this child wrapped. The chain is followed at most 16 links.
    """
    seen = 0
    current: BaseException | None = error
    while current is not None and seen < 16:
        if isinstance(current, MemoryError):
            return True
        current = current.__cause__ or current.__context__
        seen += 1
    return False


def apply_limits(address_space_bytes: int, cpu_seconds: int, nofile: int) -> None:
    """Set the five limits as both soft and hard (CPU: hard one second later), then read
    them back. Raises ValueError or OSError if one cannot be set or reads back different."""
    wanted = [
        (resource.RLIMIT_AS, (address_space_bytes, address_space_bytes)),
        (resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1)),
        (resource.RLIMIT_FSIZE, (0, 0)),
        (resource.RLIMIT_NOFILE, (nofile, nofile)),
        (resource.RLIMIT_CORE, (0, 0)),
    ]
    for res, limits in wanted:
        resource.setrlimit(res, limits)
    for res, limits in wanted:
        if resource.getrlimit(res) != limits:
            raise OSError("a resource limit reads back different")


def read_pdf_text(data: bytes, budget: int) -> str:
    """The text of the pages with text, joined by a blank line, as the reader before
    issue #1202 returned it, except that the walk stops at the first page that takes
    the joined text past ``budget`` characters: the caller keeps at most ``budget``
    characters, so the pages after that one cannot change what it keeps."""
    import io

    import pdfplumber

    parts: list[str] = []
    used = 0
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            page.close()
            if text:
                used += len(text) + (2 if parts else 0)
                parts.append(text)
                if used > budget:
                    break
    return "\n\n".join(parts)


def main(argv: list[str]) -> None:
    try:
        budget, address_space_bytes, cpu_seconds, nofile = (int(a) for a in argv[1:5])
        if len(argv) != 5 or min(budget, address_space_bytes, cpu_seconds, nofile) < 1:
            raise ValueError
    except ValueError:
        _finish("usage")
    try:
        apply_limits(address_space_bytes, cpu_seconds, nofile)
    except (ValueError, OSError):
        _finish("limits-failed")

    # From here on every instruction runs under the limits. The reserve is a megabyte
    # of address space freed before the status is written after a failure, so the
    # child has room to report even when the parser used everything the limit allows.
    reserve: bytearray | None = bytearray(1024 * 1024)
    try:
        import logging
        import warnings

        logging.disable(logging.CRITICAL)
        warnings.simplefilter("ignore")
        data = sys.stdin.buffer.read()
        try:
            import pdfplumber  # noqa: F401
        except ImportError:
            _finish("unavailable")
        # One character past the budget, so the parent can tell a cut text from one
        # that fits exactly.
        text = read_pdf_text(data, budget)[: budget + 1]
        sys.stdout.buffer.write(text.encode("utf-8", "surrogatepass"))
        sys.stdout.buffer.flush()
    except MemoryError:
        reserve = None
        _finish("memory")
    except Exception as e:
        reserve = None
        try:
            memory = caused_by_memory(e)
        except MemoryError:
            memory = True
        if memory:
            _finish("memory")
        _finish("parse-error", type(e).__name__)
    del reserve
    _finish("ok")


if __name__ == "__main__":
    main(sys.argv)
