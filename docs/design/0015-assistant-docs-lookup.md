# Decision: Assistant documentation lookup tools

Status: Accepted 2026-09-13 (three decisions made by Lane on 2026-09-13: sources are a
local corpus first with allowlisted web fetch behind a separate flag; HERD's own manual is
a built-in corpus source; the tool surface is `search_docs` plus `read_doc`). Issue #31.

## Context

The reservation assistant (`docs/AI_ASSISTANT.md`) grounds answers in live HERD data
through seven read-only tools dispatched by `ToolDispatcher`
(`services/ai-orchestrator/app/services/tools.py`): each tool's result is JSON, truncated
to `ASSISTANT_TOOL_RESULT_CHAR_CAP` (8000 chars), and sent to the model wrapped in tagged
blocks that declare it untrusted data. The assistant has no reference material: a
question about what a configuration knob means, or how HERD itself works, is answered
from the model's training data. Issue #31 asks for read-only lookup tools with three
guardrails: strictly read-only, fetched text framed as untrusted, and operator control
over what may be fetched.

Two facts from the tree shape the design. The dispatcher already enforces a per-tool
result cap and a per-hop HTTP timeout (`HTTP_TIMEOUT_SECONDS`, 15 s), so the tools inherit
both without new knobs. The ai-orchestrator image does not carry `docs/manual` today, so a
built-in manual source means the Dockerfile copies it and the dev override bind-mounts it.

## Decision

**1. Sources: a registry of named read-only sources, dark unless configured, except the
manual.** `app/services/docs_sources.py` builds the registry from settings:

- `herd-manual`, built in: the manual pages under `/app/docs/manual` (copied into the
  image from `docs/manual/`; bind-mounted from the checkout under `make up`). Enabled by
  default; `AI_DOCS_MANUAL_ENABLED=false` removes it.
- Operator corpora: `AI_DOCS_CORPUS_DIRS`, a comma-separated list of `name=/abs/path`
  entries, each a directory of `.md`, `.txt`, or `.html` files mounted into the
  container. Missing or unreadable directories are logged once at startup and skipped,
  never fatal.
- Web: `AI_DOCS_WEB_ALLOWED_PREFIXES`, a comma-separated list of `https://` URL prefixes,
  honored only when `AI_DOCS_WEB_ENABLED=true` (default false). Each prefix is normalized
  (lowercase host, no userinfo, no query) before matching, and a candidate URL matches
  only as a plain string prefix of its normalized form.

The two docs tools are advertised only when at least one source is enabled, and they sit
under the assistant's default read-only posture: no `AI_WRITE_TOOLS_ENABLED`, the usual
`ai_is_configured()` 503 when no provider is configured. As with the write tools, the
gate is enforced at the dispatch boundary: a `read_doc` naming a disabled or unknown
source, or a web URL outside the allowlist, is refused there even if the model emits it.

**2. Tools.** `search_docs(query, source?)` returns up to 10 ranked hits, each
`{source, path, title, snippet, score}`, over every enabled CORPUS source (web is never
searched; the model must name a URL). Ranking is plain text: lowercase token overlap with
a length-normalized score, no embeddings and no new dependency. `read_doc(source, path,
offset=0)` returns `{source, path, title, text, offset, next_offset}` where `text` is one
window of at most the tool result cap; `next_offset` is null at the end, so the model can
page. HTML is converted to text with the stdlib parser (tags dropped, scripts and styles
removed, block elements separated by newlines); markdown and plain text pass through.
Corpus indexes are built lazily on first use and rebuilt when older than
`AI_DOCS_INDEX_TTL_SECONDS` (default 600), so an operator can update a mounted corpus
without a restart.

**3. Web fetch safety (only when enabled).** `read_doc(source="web", path=<url>)`:
scheme must be `https`; the normalized URL must match an allowed prefix; the host is
resolved and EVERY address must be public (loopback, private, link-local, multicast,
unspecified, and IPv4-mapped forms refused); at most 3 redirects, each re-validated the
same way; response `Content-Type` must be text/html, text/plain, or text/markdown;
the body is read to at most `AI_DOCS_WEB_MAX_BYTES` (default 524288) and then cut; the
request carries no HERD credentials and no caller JWT, only a fixed User-Agent; the
existing per-hop timeout applies. Fetched text goes through the same HTML-to-text
conversion, then the same tool result cap and untrusted framing as every other tool. The
IP validation lives in one helper with a resolver seam so unit tests cover every refused
class without network.

**4. Path safety for corpora.** A `read_doc` path is resolved under the source root with
symlinks followed and the result required to stay inside the root; anything else is
refused as not found (no distinction between outside and missing, so the tool is not a
filesystem oracle). Hidden files and non-text extensions are never indexed or served.

**5. What does not change.** The seven existing tools, the write tools and their flag,
the conversation persistence model, the per-tool cap and timeout, the privacy rules (tool
result bodies are never logged; the question is never logged). No frontend change is
required: the tool-call panel already renders tool names and argument summaries. The
`/api/ai/status` payload is unchanged (pinned by an integration test).

## Contract summary

- `AI_DOCS_MANUAL_ENABLED` (default true), `AI_DOCS_CORPUS_DIRS` (default empty),
  `AI_DOCS_WEB_ENABLED` (default false), `AI_DOCS_WEB_ALLOWED_PREFIXES` (default empty),
  `AI_DOCS_WEB_MAX_BYTES` (default 524288), `AI_DOCS_INDEX_TTL_SECONDS` (default 600), all
  on the ai-orchestrator Settings, documented in `docs/ENV_VARS.md`.
- Tool definitions and dispatch in `tools.py`, source registry and indexing in
  `docs_sources.py`, web safety in `docs_web.py`, all under `app/services/`.
- `docs/AI_ASSISTANT.md` tool table gains two rows and a "Reference material" section;
  `FEATURES.md` gains one line; `CHANGELOG.md` one bullet.

## Testing

Unit (ai-orchestrator, no network): registry construction from every settings shape,
including missing directories; ranking determinism and the top-10 cap; HTML-to-text on
a manual page; paging with `offset` and `next_offset` against the cap; path traversal and
symlink escape refused as not found; web URL normalization and prefix matching; every
refused IP class through the resolver seam; redirect re-validation; byte cap; content-type
refusal; dispatch-boundary refusal of a web read while `AI_DOCS_WEB_ENABLED` is false and
of an unknown source; tools not advertised when no source is enabled. Integration
(`tests/integration/test_ai_assistant_tools.py`, AI-gated like its siblings): a question
about HERD that the seed cannot answer produces a `search_docs` call over `herd-manual`
followed by a `read_doc`, and the answer cites the manual page title. Live: the same
question against the local provider from the dev stack, with the tool-call panel showing
both tools. The `/api/ai/status` key-set pin stays green.

## Out of scope

Cross-reservation conversation memory (the issue's related idea), embeddings or a vector
store, crawling or indexing web sources, and a UI for managing sources.
