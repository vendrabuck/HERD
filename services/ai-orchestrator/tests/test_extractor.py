"""Unit tests for app.services.extractor."""

import io
import tarfile

import pytest
from app import config as config_module
from app.services.extractor import (
    ExtractionError,
    extract_files,
    render_file_context,
)


def test_extract_plain_text():
    result = extract_files([("notes.txt", b"hello world")])
    assert len(result) == 1
    assert result[0].filename == "notes.txt"
    assert result[0].text == "hello world"
    assert result[0].truncated is False


def test_extract_markdown():
    result = extract_files([("README.md", b"# title\n\nbody")])
    assert result[0].text == "# title\n\nbody"


def test_extract_json_reformats_valid_json():
    result = extract_files([("data.json", b'{"b":2,"a":1}')])
    # Should be pretty-printed and key-sorted
    assert '"a": 1' in result[0].text
    assert '"b": 2' in result[0].text


def test_extract_json_falls_back_to_raw_on_invalid():
    result = extract_files([("data.json", b"not json")])
    assert result[0].text == "not json"


def test_extract_xml_passes_through():
    result = extract_files([("config.xml", b"<root><child/></root>")])
    assert "<child/>" in result[0].text


def test_extract_rejects_unsupported_extension():
    with pytest.raises(ExtractionError, match="Unsupported file type"):
        extract_files([("bad.exe", b"")])


def test_extract_rejects_oversized_file(monkeypatch):
    monkeypatch.setattr(config_module.settings, "upload_max_file_bytes", 10)
    with pytest.raises(ExtractionError, match="exceeds limit"):
        extract_files([("big.txt", b"x" * 100)])


def test_extract_rejects_too_many_files(monkeypatch):
    monkeypatch.setattr(config_module.settings, "upload_max_files", 2)
    with pytest.raises(ExtractionError, match="Too many files"):
        extract_files([(f"f{i}.txt", b"hi") for i in range(3)])


def test_extract_truncates_when_over_char_budget(monkeypatch):
    monkeypatch.setattr(config_module.settings, "upload_max_extracted_chars", 5)
    result = extract_files([("long.txt", b"abcdefghij")])
    assert len(result[0].text) == 5
    assert result[0].truncated is True


def test_extract_second_file_marked_truncated_when_budget_exhausted(monkeypatch):
    monkeypatch.setattr(config_module.settings, "upload_max_extracted_chars", 5)
    result = extract_files(
        [
            ("first.txt", b"hello"),
            ("second.txt", b"world"),
        ]
    )
    assert result[0].text == "hello"
    assert result[1].text == ""
    assert result[1].truncated is True


def test_extract_tgz_pulls_text_members():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in [("inner.txt", b"tgz text"), ("ignored.bin", b"\x00\x01\x02")]:
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    result = extract_files([("tech_support.tgz", buf.getvalue())])
    assert "tgz text" in result[0].text
    assert "--- inner.txt ---" in result[0].text
    assert "\x00\x01\x02" not in result[0].text


def test_extract_tgz_invalid_archive_raises():
    with pytest.raises(ExtractionError, match="tgz"):
        extract_files([("broken.tgz", b"not a real tgz")])


def test_extract_pdf_raises_for_garbage():
    with pytest.raises(ExtractionError, match="PDF"):
        extract_files([("doc.pdf", b"not really a pdf")])


def test_render_file_context_empty():
    assert render_file_context([]) == ""


def test_render_file_context_labels_each_file():
    from app.schemas.generate import ExtractedFile

    block = render_file_context(
        [
            ExtractedFile(filename="a.txt", text="alpha"),
            ExtractedFile(filename="b.txt", text="beta", truncated=True),
        ]
    )
    assert "=== FILE: a.txt ===" in block
    assert "=== FILE: b.txt (truncated) ===" in block
    assert "alpha" in block
    assert "beta" in block
