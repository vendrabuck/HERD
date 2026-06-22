"""Branch coverage for app/services/extractor.py edge paths.

test_extractor.py covers the main happy paths and the top-level rejections;
this module pins the remaining branches: the .tar.gz extension alias, the
latin-1 decode fallback, the pdfplumber-missing ImportError, a parseable
multi-page PDF, and the per-member skips in the tgz reader (non-file member,
oversized member, char-budget break, and an extractfile()-returns-None member).
"""

import builtins
import io
import tarfile

import pytest
from app import config as config_module
from app.services import extractor
from app.services.extractor import (
    ExtractionError,
    _decode_text,
    _extension,
    _extract_pdf,
    _extract_tgz,
    extract_files,
)

# --- _extension: .tar.gz aliases to .tgz (line 37-38) ---


def test_extension_tar_gz_alias():
    assert _extension("bundle.tar.gz") == ".tgz"


def test_extension_no_dot_returns_empty():
    assert _extension("README") == ""


def test_extract_files_accepts_tar_gz_suffix():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="inner.txt")
        content = b"double suffix bundle"
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    result = extract_files([("support.tar.gz", buf.getvalue())])
    assert "double suffix bundle" in result[0].text


# --- _decode_text: latin-1 fallback on invalid utf-8 (lines 46-47) ---


def test_decode_text_falls_back_to_latin1():
    # 0xFF is invalid as a standalone utf-8 byte, valid latin-1 (y-diaeresis).
    decoded = _decode_text(b"caf\xff")
    assert decoded.startswith("caf")
    assert len(decoded) == 4


# --- _extract_pdf: pdfplumber missing -> ExtractionError (lines 53-54) ---


def test_extract_pdf_missing_pdfplumber_raises(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pdfplumber":
            raise ImportError("no pdfplumber")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ExtractionError, match="pdfplumber is not installed"):
        _extract_pdf(b"%PDF-1.4 whatever")


# --- _extract_pdf: a parseable multi-page document collects page text (59-65) ---


def test_extract_pdf_collects_page_text(monkeypatch):
    class _Page:
        def __init__(self, text):
            self._text = text

        def extract_text(self):
            return self._text

    class _PDF:
        # Page 2 returns "" so the `if text:` guard (line 61) skips it.
        pages = [_Page("page one body"), _Page("")]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _FakePlumber:
        @staticmethod
        def open(_fileobj):
            return _PDF()

    import sys

    monkeypatch.setitem(sys.modules, "pdfplumber", _FakePlumber)
    out = _extract_pdf(b"%PDF-fake")
    assert out == "page one body"


# --- _extract_tgz member-skip branches (76, 80, 82, 85) ---


def _make_tgz(members: list[tuple[str, bytes]], *, include_dir: bool = False) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if include_dir:
            dir_info = tarfile.TarInfo(name="subdir")
            dir_info.type = tarfile.DIRTYPE
            tar.addfile(dir_info)
        for name, content in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def test_tgz_skips_directory_member():
    """A non-file (directory) member is skipped (line 76)."""
    data = _make_tgz([("keep.txt", b"real content")], include_dir=True)
    result = extract_files([("b.tgz", data)])
    assert "real content" in result[0].text
    assert "subdir" not in result[0].text


def test_tgz_skips_oversized_member(monkeypatch):
    """A member larger than upload_max_file_bytes is skipped (line 80).

    Calls _extract_tgz directly: the per-member size cap and the whole-file
    pre-check in extract_files share one setting, so a member can only exceed
    the cap if the (compressed) archive does too. Driving the inner reader
    directly isolates the member-skip branch.
    """
    monkeypatch.setattr(config_module.settings, "upload_max_file_bytes", 8)
    data = _make_tgz([("small.txt", b"tiny"), ("big.txt", b"x" * 50)])
    out = _extract_tgz(data, remaining_budget=10_000)
    assert "tiny" in out
    assert "x" * 50 not in out


def test_tgz_stops_at_budget(monkeypatch):
    """When the remaining char budget is exhausted, the member loop breaks (82)."""
    # Budget of 5 chars: the first text member fills it, the second is never read.
    monkeypatch.setattr(config_module.settings, "upload_max_extracted_chars", 5)
    data = _make_tgz([("a.txt", b"first member text"), ("b.txt", b"second member text")])
    result = extract_files([("bundle.tgz", data)])
    # Only the first member contributed; the loop broke before the second.
    assert "second member text" not in result[0].text


def test_tgz_skips_member_when_extractfile_returns_none(monkeypatch):
    """If tar.extractfile() returns None for a member, it is skipped (line 85).

    Build the archive first, THEN patch tarfile.open to return a wrapper whose
    extractfile() yields None, and drive _extract_tgz directly so the write-mode
    open used to build the fixture is not intercepted.
    """
    data = _make_tgz([("a.txt", b"unreadable")])

    real_open = tarfile.open

    class _NoneExtractTar:
        def __init__(self, inner):
            self._inner = inner

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

        def __iter__(self):
            return iter(self._inner)

        def extractfile(self, _member):
            return None

    def fake_open(*args, **kwargs):
        return _NoneExtractTar(real_open(*args, **kwargs))

    monkeypatch.setattr(extractor.tarfile, "open", fake_open)
    out = _extract_tgz(data, remaining_budget=10_000)
    # extractfile returned None for every member, so no text was collected.
    assert out == ""
