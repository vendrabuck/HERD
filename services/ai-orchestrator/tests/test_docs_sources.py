"""Unit tests for the documentation source registry and corpus reader (ADR 0015, issue #31).

No network and no stack: every corpus is a tmp_path directory, and the
built-in manual root is pointed at a temporary copy of real manual pages.
"""

import logging
import shutil
from pathlib import Path

import pytest
from app.config import settings
from app.services import docs_sources

REPO_ROOT = Path(__file__).resolve().parents[3]
MANUAL_DIR = REPO_ROOT / "docs" / "manual"


@pytest.fixture(autouse=True)
def _clean_docs_settings(monkeypatch):
    """Every test starts from the shipped defaults with no corpora and the
    manual root pointing nowhere, so a test that wants a source has to set
    one up explicitly."""
    monkeypatch.setattr(settings, "ai_docs_manual_enabled", True)
    monkeypatch.setattr(settings, "ai_docs_corpus_dirs", "")
    monkeypatch.setattr(settings, "ai_docs_index_ttl_seconds", 600)
    monkeypatch.setattr(docs_sources, "MANUAL_ROOT", Path("/nonexistent/herd/manual"))
    docs_sources.reset_caches()
    yield
    docs_sources.reset_caches()


def _write(root: Path, name: str, body: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# --- Registry construction ---------------------------------------------


def test_registry_is_empty_when_nothing_is_configured():
    assert docs_sources.enabled_sources() == {}


def test_registry_carries_the_manual_when_its_root_exists(tmp_path, monkeypatch):
    _write(tmp_path, "index.html", "<html><title>T</title><body>hi</body></html>")
    monkeypatch.setattr(docs_sources, "MANUAL_ROOT", tmp_path)
    docs_sources.reset_caches()

    registry = docs_sources.enabled_sources()

    assert set(registry) == {docs_sources.MANUAL_SOURCE_NAME}
    assert registry[docs_sources.MANUAL_SOURCE_NAME].root == tmp_path


def test_registry_drops_the_manual_when_disabled(tmp_path, monkeypatch):
    _write(tmp_path, "index.html", "<html><body>hi</body></html>")
    monkeypatch.setattr(docs_sources, "MANUAL_ROOT", tmp_path)
    monkeypatch.setattr(settings, "ai_docs_manual_enabled", False)
    docs_sources.reset_caches()

    assert docs_sources.enabled_sources() == {}


def test_registry_parses_operator_corpora(tmp_path, monkeypatch):
    first = tmp_path / "vendor-a"
    second = tmp_path / "vendor-b"
    _write(first, "a.md", "alpha")
    _write(second, "b.md", "beta")
    monkeypatch.setattr(settings, "ai_docs_corpus_dirs", f"vendor-a={first}, vendor_b={second}")
    docs_sources.reset_caches()

    registry = docs_sources.enabled_sources()

    assert set(registry) == {"vendor-a", "vendor_b"}
    assert registry["vendor-a"].root == first


def test_registry_combines_the_manual_and_operator_corpora(tmp_path, monkeypatch):
    manual = tmp_path / "manual"
    corpus = tmp_path / "kb"
    _write(manual, "index.html", "<html><body>hi</body></html>")
    _write(corpus, "note.md", "note")
    monkeypatch.setattr(docs_sources, "MANUAL_ROOT", manual)
    monkeypatch.setattr(settings, "ai_docs_corpus_dirs", f"kb={corpus}")
    docs_sources.reset_caches()

    assert set(docs_sources.enabled_sources()) == {docs_sources.MANUAL_SOURCE_NAME, "kb"}


def test_registry_skips_a_missing_directory_and_logs_it_once(tmp_path, monkeypatch, caplog):
    good = tmp_path / "good"
    _write(good, "a.md", "alpha")
    missing = tmp_path / "gone"
    monkeypatch.setattr(settings, "ai_docs_corpus_dirs", f"good={good},gone={missing}")
    docs_sources.reset_caches()

    with caplog.at_level(logging.WARNING, logger=docs_sources.__name__):
        assert set(docs_sources.enabled_sources()) == {"good"}
        # Force a rebuild with the same settings: the warning must not repeat.
        docs_sources._registry_cache = None
        assert set(docs_sources.enabled_sources()) == {"good"}

    skips = [
        r
        for r in caplog.records
        if "docs_source_skipped" in r.getMessage() and getattr(r, "source", None) == "gone"
    ]
    assert len(skips) == 1


def test_registry_skips_malformed_and_relative_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ai_docs_corpus_dirs", "no-equals-sign, Bad Name=/tmp, rel=docs")
    docs_sources.reset_caches()

    assert docs_sources.enabled_sources() == {}


def test_registry_refuses_an_entry_named_web(tmp_path, monkeypatch):
    corpus = tmp_path / "kb"
    _write(corpus, "a.md", "alpha")
    monkeypatch.setattr(settings, "ai_docs_corpus_dirs", f"web={corpus}")
    docs_sources.reset_caches()

    assert docs_sources.enabled_sources() == {}


# --- Indexing and ranking ----------------------------------------------


def _corpus(tmp_path, monkeypatch, files: dict[str, str], name: str = "kb") -> Path:
    root = tmp_path / name
    for path, body in files.items():
        _write(root, path, body)
    monkeypatch.setattr(settings, "ai_docs_corpus_dirs", f"{name}={root}")
    docs_sources.reset_caches()
    return root


def test_search_ranks_the_more_relevant_document_first(tmp_path, monkeypatch):
    _corpus(
        tmp_path,
        monkeypatch,
        {
            "ldap.md": "# LDAP group sync\nLDAP group sync maps directory groups into HERD groups.",
            "other.md": "# Reservations\nA reservation books devices. LDAP is mentioned once here "
            + ("filler word " * 200),
        },
    )

    hits = docs_sources.search("ldap group sync")

    assert [h["path"] for h in hits] == ["ldap.md", "other.md"]
    assert hits[0]["score"] > hits[1]["score"]
    assert "LDAP" in hits[0]["snippet"]
    assert hits[0]["title"] == "LDAP group sync"


def test_search_is_deterministic_and_breaks_ties_by_path(tmp_path, monkeypatch):
    body = "vlan trunk configuration"
    _corpus(
        tmp_path,
        monkeypatch,
        {"b.md": body, "a.md": body, "c.md": body},
    )

    first = docs_sources.search("vlan trunk")
    second = docs_sources.search("vlan trunk")

    assert [h["path"] for h in first] == ["a.md", "b.md", "c.md"]
    assert first == second


def test_search_caps_results_at_ten(tmp_path, monkeypatch):
    _corpus(tmp_path, monkeypatch, {f"doc{i:02d}.md": "vlan trunking" for i in range(25)})

    hits = docs_sources.search("vlan")

    assert len(hits) == docs_sources.MAX_SEARCH_RESULTS == 10


def test_search_can_be_restricted_to_one_source(tmp_path, monkeypatch):
    kb = tmp_path / "kb"
    other = tmp_path / "other"
    _write(kb, "a.md", "vlan trunking")
    _write(other, "b.md", "vlan trunking")
    monkeypatch.setattr(settings, "ai_docs_corpus_dirs", f"kb={kb},other={other}")
    docs_sources.reset_caches()

    hits = docs_sources.search("vlan", source="other")

    assert [h["source"] for h in hits] == ["other"]


def test_search_ignores_non_text_and_hidden_files(tmp_path, monkeypatch):
    _corpus(
        tmp_path,
        monkeypatch,
        {
            "visible.md": "vlan trunking",
            ".hidden.md": "vlan trunking",
            "image.png": "vlan trunking",
            "nested/.private/secret.md": "vlan trunking",
        },
    )

    assert [h["path"] for h in docs_sources.search("vlan")] == ["visible.md"]


def test_search_returns_nothing_for_a_query_with_no_overlap(tmp_path, monkeypatch):
    _corpus(tmp_path, monkeypatch, {"a.md": "vlan trunking"})

    assert docs_sources.search("kubernetes ingress") == []


def test_index_is_rebuilt_after_the_ttl_expires(tmp_path, monkeypatch):
    root = _corpus(tmp_path, monkeypatch, {"a.md": "vlan trunking"})
    assert len(docs_sources.search("vlan")) == 1

    _write(root, "b.md", "vlan access port")
    # Still cached: the corpus changed but the TTL has not elapsed.
    assert len(docs_sources.search("vlan")) == 1

    monkeypatch.setattr(settings, "ai_docs_index_ttl_seconds", 0)
    assert len(docs_sources.search("vlan")) == 2


# --- HTML to text -------------------------------------------------------


def test_html_to_text_on_a_real_manual_page(tmp_path, monkeypatch):
    source_page = MANUAL_DIR / "index.html"
    assert source_page.is_file(), "the checked-in manual page is the fixture for this test"
    manual = tmp_path / "manual"
    manual.mkdir()
    shutil.copy(source_page, manual / "index.html")
    monkeypatch.setattr(docs_sources, "MANUAL_ROOT", manual)
    docs_sources.reset_caches()

    doc = docs_sources.read_document(
        docs_sources.MANUAL_SOURCE_NAME, "index.html", offset=0, window=100_000
    )

    assert doc["title"] == "HERD Manual: Start here"
    text = doc["text"]
    assert "<" not in text and ">" not in text
    assert "function" not in text.lower() or "{" not in text
    assert "HERD" in text
    # Block elements are separated, so headings do not run into body text.
    assert "\n" in text


def test_html_to_text_drops_script_and_style_and_separates_blocks():
    title, text = docs_sources.html_to_text(
        "<html><head><title>Docs</title>"
        "<style>body{color:red}</style>"
        "<script>var leak = 'do-not-index';</script></head>"
        "<body><h1>Heading</h1><p>First para</p><p>Second para</p>"
        "<ul><li>one</li><li>two</li></ul></body></html>"
    )

    assert title == "Docs"
    assert "do-not-index" not in text
    assert "color:red" not in text
    assert "First para\nSecond para" in text
    assert "one\ntwo" in text


def test_html_title_falls_back_to_the_first_heading():
    title, _text = docs_sources.html_to_text("<html><body><h1>Only Heading</h1></body></html>")

    assert title == "Only Heading"


def test_markdown_title_comes_from_the_first_heading(tmp_path, monkeypatch):
    _corpus(tmp_path, monkeypatch, {"a.md": "# Wiring a fork\n\nbody text about wiring"})

    hits = docs_sources.search("wiring fork")

    assert hits[0]["title"] == "Wiring a fork"


# --- Reading and paging -------------------------------------------------


def test_read_document_pages_until_next_offset_is_null(tmp_path, monkeypatch):
    body = "".join(f"line {i:03d}\n" for i in range(200))
    _corpus(tmp_path, monkeypatch, {"long.txt": body})

    window = 250
    offset = 0
    collected = ""
    windows = 0
    while True:
        doc = docs_sources.read_document("kb", "long.txt", offset=offset, window=window)
        assert doc["offset"] == offset
        assert len(doc["text"]) <= window
        collected += doc["text"]
        windows += 1
        if doc["next_offset"] is None:
            break
        offset = doc["next_offset"]
        assert windows < 100, "paging must terminate"

    assert windows > 1
    assert collected == docs_sources.normalize_plain_text(body)


def test_read_document_past_the_end_returns_empty_text(tmp_path, monkeypatch):
    _corpus(tmp_path, monkeypatch, {"a.md": "short body"})

    doc = docs_sources.read_document("kb", "a.md", offset=10_000, window=500)

    assert doc["text"] == ""
    assert doc["next_offset"] is None


def test_read_document_rejects_an_unknown_source(tmp_path, monkeypatch):
    _corpus(tmp_path, monkeypatch, {"a.md": "body"})

    with pytest.raises(docs_sources.DocsLookupError, match="unknown documentation source"):
        docs_sources.read_document("nope", "a.md", offset=0, window=100)


# --- Path safety --------------------------------------------------------


def test_read_document_refuses_a_traversal_path(tmp_path, monkeypatch):
    _write(tmp_path, "outside.md", "secret material")
    _corpus(tmp_path, monkeypatch, {"a.md": "body"})

    with pytest.raises(docs_sources.DocsLookupError, match="not found"):
        docs_sources.read_document("kb", "../outside.md", offset=0, window=100)


def test_read_document_refuses_an_absolute_path(tmp_path, monkeypatch):
    _corpus(tmp_path, monkeypatch, {"a.md": "body"})

    with pytest.raises(docs_sources.DocsLookupError, match="not found"):
        docs_sources.read_document("kb", "/etc/hostname", offset=0, window=100)


def test_read_document_refuses_a_symlink_escape(tmp_path, monkeypatch):
    outside = _write(tmp_path, "outside.md", "secret material")
    root = _corpus(tmp_path, monkeypatch, {"a.md": "body"})
    (root / "escape.md").symlink_to(outside)

    with pytest.raises(docs_sources.DocsLookupError, match="not found"):
        docs_sources.read_document("kb", "escape.md", offset=0, window=100)


def test_read_document_refuses_a_non_text_extension(tmp_path, monkeypatch):
    root = _corpus(tmp_path, monkeypatch, {"a.md": "body"})
    _write(root, "notes.pdf", "not really a pdf")

    with pytest.raises(docs_sources.DocsLookupError, match="not found"):
        docs_sources.read_document("kb", "notes.pdf", offset=0, window=100)


def test_read_document_follows_a_symlink_that_stays_inside_the_root(tmp_path, monkeypatch):
    root = _corpus(tmp_path, monkeypatch, {"real/a.md": "inside body"})
    (root / "alias.md").symlink_to(root / "real" / "a.md")

    doc = docs_sources.read_document("kb", "alias.md", offset=0, window=100)

    assert doc["text"] == "inside body"
