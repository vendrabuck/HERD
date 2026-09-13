"""Read-only documentation sources for the reservation assistant (ADR 0015, issue #31).

Builds a registry of named corpus sources from settings, indexes each one
lazily, and serves two operations the `search_docs` and `read_doc` tools sit
on: a plain-text ranked search over every enabled corpus, and a paged read of
one document.

Everything here is read-only and local. The web source lives in docs_web.py
because it carries a different threat model; this module never reaches the
network. Path resolution is the security boundary for corpora: a requested
path is resolved under the source root with symlinks followed and must land
inside that root, and anything else answers "not found" so the tool is not a
filesystem oracle.

Indexes are cached per source and rebuilt when older than
AI_DOCS_INDEX_TTL_SECONDS, so an operator can update a mounted corpus without
restarting the service.
"""

from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

# The built-in manual corpus. The image copies docs/manual/ here; the dev
# override bind-mounts the checkout over it. Module-level so tests can point
# it at a temporary directory.
MANUAL_SOURCE_NAME = "herd-manual"
MANUAL_ROOT = Path("/app/docs/manual")

# Reserved: read_doc(source="web", ...) is the web fetch, never a corpus.
WEB_SOURCE_NAME = "web"

# Only these extensions are indexed or served. Everything else (images,
# archives, anything binary) is invisible to both tools.
TEXT_EXTENSIONS = frozenset({".md", ".txt", ".html"})

MAX_SEARCH_RESULTS = 10
SNIPPET_CHARS = 240
SNIPPET_LEAD_CHARS = 60
# Bytes of a single file we are willing to read into the index.
MAX_DOC_BYTES = 2 * 1024 * 1024

_SOURCE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Dropped from the QUERY only. Document token counts keep every token, so the
# length normalization below stays honest.
_QUERY_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do", "does",
        "for", "from", "how", "i", "if", "in", "is", "it", "me", "my", "of", "on",
        "or", "that", "the", "then", "there", "this", "to", "was", "what", "when",
        "where", "which", "why", "with", "you", "your",
    }
)  # fmt: skip


class DocsLookupError(Exception):
    """A documentation lookup that the caller should surface to the model."""


@dataclass(frozen=True)
class CorpusSource:
    name: str
    root: Path


@dataclass
class IndexedDoc:
    source: str
    path: str
    title: str
    text: str
    counts: dict[str, int]
    title_counts: dict[str, int]
    token_count: int


@dataclass
class CorpusIndex:
    source: CorpusSource
    docs: list[IndexedDoc] = field(default_factory=list)
    built_at: float = 0.0


# --- Caches -------------------------------------------------------------

_registry_cache: dict[str, CorpusSource] | None = None
_registry_signature: tuple[object, ...] | None = None
_index_cache: dict[str, CorpusIndex] = {}
# Paths already reported as unusable, so a broken AI_DOCS_CORPUS_DIRS entry
# logs once instead of on every registry build.
_warned_entries: set[str] = set()


def reset_caches() -> None:
    """Drop the registry, the indexes, and the warn-once memory. Tests call
    this after changing settings; production never needs it."""
    global _registry_cache, _registry_signature
    _registry_cache = None
    _registry_signature = None
    _index_cache.clear()
    _warned_entries.clear()


def _warn_once(event: str, key: str, **fields: object) -> None:
    """Log one line per distinct problem, once.

    The details go in the MESSAGE, not only in `extra`: the shared JSON
    formatter emits a fixed set of extra keys and would otherwise drop them.
    The `extra` copy stays so a test can assert on the fields rather than on
    the wording.
    """
    if key in _warned_entries:
        return
    _warned_entries.add(key)
    detail = " ".join(f"{name}={value}" for name, value in fields.items())
    logger.warning("%s %s", event, detail, extra=dict(fields))


# --- Registry -----------------------------------------------------------


def _usable_directory(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _parse_corpus_dirs(raw: str) -> list[tuple[str, str]]:
    """Parse AI_DOCS_CORPUS_DIRS into (name, path) pairs. Malformed entries are
    dropped here and warned about by the caller, which knows the whole entry."""
    entries: list[tuple[str, str]] = []
    for chunk in raw.split(","):
        entry = chunk.strip()
        if not entry:
            continue
        name, sep, path = entry.partition("=")
        entries.append((name.strip() if sep else entry, path.strip() if sep else ""))
    return entries


def build_source_registry() -> dict[str, CorpusSource]:
    """Build the corpus registry from current settings.

    The built-in manual is included when AI_DOCS_MANUAL_ENABLED is on and its
    root exists. Operator corpora come from AI_DOCS_CORPUS_DIRS as
    `name=/abs/path` entries. A malformed, missing, or unreadable entry is
    logged once and skipped: a bad mount must never stop the service or the
    other sources.
    """
    registry: dict[str, CorpusSource] = {}

    if settings.ai_docs_manual_enabled:
        if _usable_directory(MANUAL_ROOT):
            registry[MANUAL_SOURCE_NAME] = CorpusSource(name=MANUAL_SOURCE_NAME, root=MANUAL_ROOT)
        else:
            _warn_once(
                "docs_source_skipped",
                f"manual:{MANUAL_ROOT}",
                source=MANUAL_SOURCE_NAME,
                corpus_path=str(MANUAL_ROOT),
                reason="directory not readable",
            )

    for name, path in _parse_corpus_dirs(settings.ai_docs_corpus_dirs):
        entry_key = f"corpus:{name}={path}"
        if not path or not _SOURCE_NAME_RE.match(name) or name in (WEB_SOURCE_NAME,):
            _warn_once(
                "docs_source_skipped",
                entry_key,
                source=name,
                corpus_path=path,
                reason="entry must be name=/absolute/path with a lowercase name",
            )
            continue
        if name in registry:
            _warn_once(
                "docs_source_skipped", entry_key, source=name, corpus_path=path, reason="duplicate"
            )
            continue
        root = Path(path)
        if not root.is_absolute() or not _usable_directory(root):
            _warn_once(
                "docs_source_skipped",
                entry_key,
                source=name,
                corpus_path=path,
                reason="directory not readable",
            )
            continue
        registry[name] = CorpusSource(name=name, root=root)

    return registry


def enabled_sources() -> dict[str, CorpusSource]:
    """Registry of enabled corpus sources, rebuilt when the settings that feed
    it change (which is what makes it testable without a process restart)."""
    global _registry_cache, _registry_signature
    signature = (
        bool(settings.ai_docs_manual_enabled),
        str(MANUAL_ROOT),
        settings.ai_docs_corpus_dirs,
    )
    if _registry_cache is None or _registry_signature != signature:
        _registry_cache = build_source_registry()
        _registry_signature = signature
        _index_cache.clear()
    return _registry_cache


def log_source_summary() -> None:
    """Log the enabled corpus sources once at startup. Any unusable entry has
    already logged its own skip line by the time this returns."""
    sources = enabled_sources()
    logger.info(
        "docs_sources_ready sources=%s web_enabled=%s",
        ",".join(sorted(sources)) or "none",
        bool(settings.ai_docs_web_enabled),
    )


# --- HTML to text -------------------------------------------------------

_SKIP_ELEMENTS = frozenset({"script", "style", "noscript", "template", "svg"})
_BLOCK_ELEMENTS = frozenset(
    {
        "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
        "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
        "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table",
        "tbody", "td", "th", "thead", "tr", "ul",
    }
)  # fmt: skip


class _TextExtractor(HTMLParser):
    """Collect visible text plus a title. Script and style contents are
    dropped entirely; block elements are separated by a newline so paragraphs
    and list items do not run together."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self._title_parts: list[str] = []
        self._in_h1 = False
        self._h1_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_ELEMENTS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if tag == "h1" and not self._h1_parts:
            self._in_h1 = True
        if tag in _BLOCK_ELEMENTS:
            self._parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_ELEMENTS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_ELEMENTS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
        if tag == "h1":
            self._in_h1 = False
        if tag in _BLOCK_ELEMENTS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self._title_parts.append(data)
        if self._in_h1:
            self._h1_parts.append(data)
        self._parts.append(data)

    def result(self) -> tuple[str, str]:
        title = _collapse_inline("".join(self._title_parts)) or _collapse_inline(
            "".join(self._h1_parts)
        )
        # Block elements open and close with a newline each, so a run of
        # paragraphs would otherwise arrive double spaced. The markup already
        # separates the blocks; blank lines here only burn the read window.
        text = re.sub(r"\n{2,}", "\n", normalize_plain_text("".join(self._parts)))
        return title, text.strip()


def _collapse_inline(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize_plain_text(value: str) -> str:
    """Collapse horizontal runs of whitespace, drop trailing spaces, and cap
    blank runs at one blank line. Keeps the text readable for the model
    without preserving HTML's incidental indentation."""
    lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in value.split("\n")]
    out: list[str] = []
    for line in lines:
        if not line and out and not out[-1]:
            continue
        out.append(line)
    return "\n".join(out).strip()


def html_to_text(html: str) -> tuple[str, str]:
    """Convert an HTML document to (title, plain text). Never raises on
    malformed markup: html.parser is tolerant by design."""
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    return extractor.result()


def _markdown_title(text: str) -> str:
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
        return _collapse_inline(stripped)[:120]
    return ""


def document_text_and_title(path: Path, raw: str) -> tuple[str, str]:
    if path.suffix.lower() == ".html":
        title, text = html_to_text(raw)
    else:
        text = normalize_plain_text(raw)
        title = _markdown_title(text)
    if not title:
        title = path.name
    return title, text


# --- Indexing -----------------------------------------------------------


def _tokenize(value: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(value.lower()) if len(t) > 1]


def _counts(tokens: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for token in tokens:
        counts[token] = counts.get(token, 0) + 1
    return counts


def _is_indexable(path: Path) -> bool:
    if path.suffix.lower() not in TEXT_EXTENSIONS:
        return False
    return not any(part.startswith(".") for part in path.parts)


def _read_file(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_DOC_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _build_index(source: CorpusSource) -> CorpusIndex:
    docs: list[IndexedDoc] = []
    try:
        candidates = sorted(p for p in source.root.rglob("*") if p.is_file())
    except OSError:
        candidates = []
    for absolute in candidates:
        try:
            relative = absolute.relative_to(source.root)
        except ValueError:  # pragma: no cover - rglob cannot leave the root
            continue
        if not _is_indexable(relative):
            continue
        raw = _read_file(absolute)
        if raw is None:
            continue
        title, text = document_text_and_title(absolute, raw)
        tokens = _tokenize(text)
        docs.append(
            IndexedDoc(
                source=source.name,
                path=relative.as_posix(),
                title=title,
                text=text,
                counts=_counts(tokens),
                title_counts=_counts(_tokenize(title)),
                token_count=len(tokens),
            )
        )
    return CorpusIndex(source=source, docs=docs, built_at=time.monotonic())


def get_index(source: CorpusSource) -> CorpusIndex:
    """Return the cached index for a source, rebuilding it when it is missing,
    stale (older than the TTL), or was built against a different root."""
    cached = _index_cache.get(source.name)
    ttl = max(0, int(settings.ai_docs_index_ttl_seconds))
    if (
        cached is not None
        and cached.source.root == source.root
        and (time.monotonic() - cached.built_at) < ttl
    ):
        return cached
    index = _build_index(source)
    _index_cache[source.name] = index
    return index


# --- Search -------------------------------------------------------------


def query_tokens(query: str) -> set[str]:
    tokens = {t for t in _tokenize(query) if t not in _QUERY_STOPWORDS}
    return tokens or set(_tokenize(query))


def score_document(tokens: set[str], doc: IndexedDoc) -> float:
    """Length-normalized token overlap.

    Every distinct query token contributes its term frequency; a hit in the
    title is worth three body hits, since a manual page titled "LDAP group
    sync" is a better answer than one that mentions LDAP in passing. The sum
    is scaled by the fraction of query tokens the document covers and divided
    by the square root of the document length, so a long page cannot win on
    sheer size. Rounded so equal-scoring documents tie exactly and the
    caller's path ordering decides.
    """
    if not tokens or doc.token_count == 0:
        return 0.0
    body_hits = sum(doc.counts.get(t, 0) for t in tokens)
    title_hits = sum(doc.title_counts.get(t, 0) for t in tokens)
    if body_hits == 0 and title_hits == 0:
        return 0.0
    matched = sum(1 for t in tokens if doc.counts.get(t) or doc.title_counts.get(t))
    coverage = matched / len(tokens)
    return round((body_hits + 3 * title_hits) * coverage / math.sqrt(doc.token_count), 6)


def _snippet(text: str, tokens: set[str]) -> str:
    lowered = text.lower()
    best = -1
    for token in tokens:
        found = lowered.find(token)
        if found != -1 and (best == -1 or found < best):
            best = found
    start = 0 if best < 0 else max(0, best - SNIPPET_LEAD_CHARS)
    snippet = _collapse_inline(text[start : start + SNIPPET_CHARS])
    if start > 0:
        snippet = f"...{snippet}"
    if start + SNIPPET_CHARS < len(text):
        snippet = f"{snippet}..."
    return snippet


def search(query: str, *, source: str | None = None, limit: int = MAX_SEARCH_RESULTS) -> list[dict]:
    """Rank documents across the enabled corpora.

    Ordering is deterministic: descending score, then source name, then path,
    so two runs over the same corpus return the same list in the same order.
    """
    registry = enabled_sources()
    if source is not None:
        chosen = registry.get(source)
        sources = [chosen] if chosen is not None else []
    else:
        sources = [registry[name] for name in sorted(registry)]

    tokens = query_tokens(query)
    scored: list[tuple[float, str, str, IndexedDoc]] = []
    for corpus in sources:
        for doc in get_index(corpus).docs:
            score = score_document(tokens, doc)
            if score > 0:
                scored.append((score, doc.source, doc.path, doc))
    scored.sort(key=lambda row: (-row[0], row[1], row[2]))

    return [
        {
            "source": doc.source,
            "path": doc.path,
            "title": doc.title,
            "snippet": _snippet(doc.text, tokens),
            "score": score,
        }
        for score, _source_name, _path, doc in scored[: max(0, limit)]
    ]


# --- Read ---------------------------------------------------------------


def resolve_in_root(root: Path, relative: str) -> Path | None:
    """Resolve a requested path under a source root, or None.

    Symlinks are followed and the result must still sit inside the resolved
    root, so a symlink pointing out of the corpus is refused exactly like a
    `../` traversal. Hidden components and non-text extensions are refused
    here too, so a file that would never be indexed can never be read either.
    None means "not found" to the caller: the tool must not distinguish
    "outside the root" from "does not exist".
    """
    candidate = (relative or "").strip()
    if not candidate or candidate.startswith("/") or "\x00" in candidate:
        return None
    try:
        resolved_root = root.resolve(strict=True)
        resolved = (resolved_root / candidate).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if resolved != resolved_root and resolved_root not in resolved.parents:
        return None
    try:
        relative_resolved = resolved.relative_to(resolved_root)
    except ValueError:  # pragma: no cover - guarded by the parents check above
        return None
    if not _is_indexable(relative_resolved):
        return None
    if not resolved.is_file():
        return None
    return resolved


def read_document(source_name: str, path: str, *, offset: int = 0, window: int) -> dict:
    """Read one window of a corpus document.

    `offset` and `next_offset` are character offsets into the converted text,
    so the model can page a long page in without any server-side cursor.
    Raises DocsLookupError for an unknown source or an unresolvable path.
    """
    registry = enabled_sources()
    corpus = registry.get(source_name)
    if corpus is None:
        raise DocsLookupError(f"unknown documentation source: {source_name!r}")
    resolved = resolve_in_root(corpus.root, path)
    if resolved is None:
        raise DocsLookupError(f"document not found in {source_name}: {path!r}")
    raw = _read_file(resolved)
    if raw is None:
        raise DocsLookupError(f"document not found in {source_name}: {path!r}")
    title, text = document_text_and_title(resolved, raw)

    start = max(0, int(offset))
    window = max(1, int(window))
    chunk = text[start : start + window]
    next_offset = start + len(chunk) if start + len(chunk) < len(text) else None
    return {
        "source": source_name,
        "path": resolve_relative_path(corpus.root, resolved, path),
        "title": title,
        "text": chunk,
        "offset": start,
        "next_offset": next_offset,
    }


def resolve_relative_path(root: Path, resolved: Path, fallback: str) -> str:
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):  # pragma: no cover - resolve_in_root already proved this
        return fallback
