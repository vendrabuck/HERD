// Functional tests for the published manual's search (issue #1298).
//
// The manual is static HTML under docs/manual/, outside this app; its search
// lives in docs/manual/assets/manual.js, which exports its pure matching and
// ranking object (HERDManualSearch) through module.exports when a `module` is
// in scope and runs its browser part otherwise. These tests read the file's
// exact text from disk and run it both ways: with a `module` object, to
// exercise the pure function directly, and without one inside jsdom, to check
// what the browser part renders. tests/e2e/test_manual_search_playwright.py
// covers a real browser.
//
// The file sits outside this app's Vite root, so a `?raw` import is refused
// (server.fs.allow), and this tsconfig carries no Node types; the read goes
// through Node's process.getBuiltinModule with a local type instead.
interface NodeFs {
  readFileSync(path: URL, encoding: "utf-8"): string;
}
const nodeFs = (
  globalThis as unknown as { process: { getBuiltinModule(id: string): unknown } }
).process.getBuiltinModule("node:fs") as NodeFs;

// A variable base keeps Vite from rewriting `new URL(..., import.meta.url)`
// into an asset import, which its fs.allow check would refuse as well.
const HERE = import.meta.url;

function asset(name: string): string {
  return nodeFs.readFileSync(new URL("../../../../docs/manual/assets/" + name, HERE), "utf-8");
}

const manualJs = asset("manual.js");
const searchIndexJs = asset("search-index.js");

interface Part {
  text: string;
  mark: boolean;
}
interface Result {
  file: string;
  title: string;
  kind: string;
  release: boolean;
  anchor: string;
  href: string;
  heading: string;
  eyebrow: string;
  headingParts: Part[];
  snippetParts: Part[];
}
interface Found {
  terms: { term: string; prefix: boolean }[];
  total: number;
  results: Result[];
}
interface Page {
  file: string;
  title: string;
  kind: string;
}
interface Section {
  page: number;
  anchor: string;
  heading: string;
  eyebrow: string;
  text: string;
}
interface Index {
  format: number;
  pages: Page[];
  sections: Section[];
}
interface ManualSearch {
  parseQuery(query: string): { term: string; prefix: boolean }[];
  search(index: Index, query: string, limit?: number): Found;
}

function load(): ManualSearch {
  const mod: { exports: unknown } = { exports: {} };
  new Function("module", manualJs)(mod);
  return mod.exports as ManualSearch;
}

const S = load();

function index(pages: Page[], sections: Section[]): Index {
  return { format: 1, pages, sections };
}

const PAGE: Page = { file: "guide.html", title: "Guide", kind: "page" };

function sec(anchor: string, heading: string, text: string, page = 0, eyebrow = ""): Section {
  return { page, anchor, heading, eyebrow, text };
}

function anchors(found: Found): string[] {
  return found.results.map((r) => `${r.file}#${r.anchor}`);
}

function joined(parts: Part[]): string {
  return parts.map((p) => p.text).join("");
}

function marked(parts: Part[]): string[] {
  return parts.filter((p) => p.mark).map((p) => p.text);
}

describe("parseQuery", () => {
  it("lower-cases, splits on anything but letters and digits, and makes only the last term a prefix", () => {
    expect(S.parseQuery("  Dry-RUN  eth1/1 ")).toEqual([
      { term: "dry", prefix: false },
      { term: "run", prefix: false },
      { term: "eth1", prefix: false },
      { term: "1", prefix: true },
    ]);
  });

  it("merges a repeated term, keeping the prefix rule when the repeat is last", () => {
    expect(S.parseQuery("fork topology fork")).toEqual([
      { term: "fork", prefix: true },
      { term: "topology", prefix: false },
    ]);
  });

  it("keeps a dotted version as one term and splits a letter run off its front", () => {
    expect(S.parseQuery("Release v0.7.0 notes 3.13.12")).toEqual([
      { term: "release", prefix: false },
      { term: "v", prefix: false },
      { term: "0.7.0", prefix: false },
      { term: "notes", prefix: false },
      { term: "3.13.12", prefix: true },
    ]);
    expect(S.parseQuery("version 0.7.0.")).toEqual([
      { term: "version", prefix: false },
      { term: "0.7.0", prefix: true },
    ]);
    expect(S.parseQuery("eth1/1 ge-0/0")).toEqual([
      { term: "eth1", prefix: false },
      { term: "1", prefix: false },
      { term: "ge", prefix: false },
      { term: "0", prefix: true },
    ]);
  });

  it.each(["", "   ", "--- / ,", "\u2014"])("has no terms for %j", (query) => {
    expect(S.parseQuery(query)).toEqual([]);
    expect(S.search(index([PAGE], [sec("a", "Anything", "words")]), query)).toEqual({
      terms: [],
      total: 0,
      results: [],
    });
  });
});

describe("matching", () => {
  const idx = index(
    [PAGE],
    [
      sec("groups", "Device groups", "A device group controls visibility."),
      sec("health", "Device health", "Polling every device."),
      sec("other", "Unrelated", "Nothing here."),
    ],
  );

  it("requires every term", () => {
    expect(anchors(S.search(idx, "device visibility"))).toEqual(["guide.html#groups"]);
    expect(S.search(idx, "device missingword").total).toBe(0);
  });

  it("is case-insensitive in both query and text", () => {
    expect(anchors(S.search(idx, "VISIBILITY"))).toEqual(["guide.html#groups"]);
  });

  it("matches the last term as a word prefix", () => {
    expect(anchors(S.search(idx, "visib"))).toEqual(["guide.html#groups"]);
    expect(anchors(S.search(idx, "device gro"))).toEqual(["guide.html#groups"]);
  });

  it("matches every other term as a whole word only", () => {
    expect(S.search(idx, "dev polling").total).toBe(0);
    expect(S.search(idx, "isibility").total).toBe(0);
    expect(anchors(S.search(idx, "device polling"))).toEqual(["guide.html#health"]);
  });

  it("lets the page title satisfy a term without giving heading credit", () => {
    const glossary = index(
      [
        { file: "glossary.html", title: "Glossary", kind: "page" },
        { file: "live.html", title: "Live editing", kind: "page" },
      ],
      [sec("fork", "Fork", "A private copy.", 0), sec("x", "Fork history", "glossary fork", 1)],
    );
    const found = S.search(glossary, "glossary fork");
    // Both match; live.html has "glossary" in its body, glossary.html only in its title.
    // Heading hits tie (fork), the exact heading wins.
    expect(anchors(found)).toEqual(["glossary.html#fork", "live.html#x"]);
  });

  it("never counts a page-title match as a heading hit", () => {
    const idx = index(
      [
        { file: "a.html", title: "Fork guide", kind: "page" },
        { file: "b.html", title: "Other", kind: "page" },
      ],
      [sec("a", "Alpha", "fork", 0), sec("b", "Beta", "fork fork", 1)],
    );
    // With title credit a.html#a would lead on heading hits; without it b.html#b
    // leads on body count.
    expect(anchors(S.search(idx, "fork"))).toEqual(["b.html#b", "a.html#a"]);
  });

  it("searches the eyebrow as heading text", () => {
    const eyebrow = index([PAGE], [sec("a", "Writing the prompt", "Click it.", 0, "Asking for what you want")]);
    expect(anchors(S.search(eyebrow, "asking"))).toEqual(["guide.html#a"]);
  });
});

describe("ranking", () => {
  it("puts heading hits above body hits, whatever the body count", () => {
    const idx = index(
      [PAGE],
      [
        sec("body", "Something", "fork fork fork fork fork"),
        sec("head", "The fork panel", "no mention"),
      ],
    );
    expect(anchors(S.search(idx, "fork"))).toEqual(["guide.html#head", "guide.html#body"]);
  });

  it("puts more heading terms above fewer", () => {
    const idx = index(
      [PAGE],
      [sec("one", "Dry", "run run run"), sec("two", "Dry run results", "nothing")],
    );
    expect(anchors(S.search(idx, "dry run"))).toEqual(["guide.html#two", "guide.html#one"]);
  });

  it("puts a heading made only of query words above a longer heading", () => {
    const idx = index(
      [PAGE],
      [sec("long", "Fork history panel", "fork fork fork"), sec("exact", "Fork", "once")],
    );
    expect(anchors(S.search(idx, "fork"))).toEqual(["guide.html#exact", "guide.html#long"]);
  });

  it("then more body matches first, then index order", () => {
    const idx = index(
      [PAGE],
      [
        sec("a", "A", "fork"),
        sec("b", "B", "fork forks forked"),
        sec("c", "C", "fork"),
        sec("d", "D", "fork forks"),
      ],
    );
    expect(anchors(S.search(idx, "fork"))).toEqual([
      "guide.html#b",
      "guide.html#d",
      "guide.html#a",
      "guide.html#c",
    ]);
  });

  it("puts earlier release pages after current pages, even with a heading hit, and labels both release kinds", () => {
    const idx = index(
      [
        { file: "release-0-1-0.html", title: "Release 0.1.0", kind: "earlier-release" },
        { file: "release-0-2-0.html", title: "Release 0.2.0", kind: "release" },
        PAGE,
      ],
      [
        sec("old", "Fork", "fork fork fork", 0),
        sec("new", "Fork", "fork", 1),
        sec("body", "Something", "a fork", 2),
      ],
    );
    const found = S.search(idx, "fork");
    expect(anchors(found)).toEqual([
      "release-0-2-0.html#new",
      "guide.html#body",
      "release-0-1-0.html#old",
    ]);
    expect(found.results.map((r) => [r.kind, r.release])).toEqual([
      ["release", true],
      ["page", false],
      ["earlier-release", true],
    ]);
  });

  it("puts a page whose title holds the queried version first, even an earlier release", () => {
    const idx = index(
      [
        { file: "release-0-3-0.html", title: "Release 0.3.0", kind: "earlier-release" },
        { file: "release-0-4-0.html", title: "Release 0.4.0", kind: "release" },
        PAGE,
      ],
      [
        sec("", "Release 0.3.0", "What 0.3.0 shipped.", 0),
        sec("up", "Upgrading from 0.3.0", "0.3.0 0.3.0", 1),
        sec("x", "Something", "0.3.0 0.3.0 0.3.0", 2),
      ],
    );
    expect(anchors(S.search(idx, "0.3.0"))).toEqual([
      "release-0-3-0.html#",
      "release-0-4-0.html#up",
      "guide.html#x",
    ]);
    expect(marked(S.search(idx, "0.3.0").results[0].headingParts)).toEqual(["0.3.0"]);
  });

  it("is deterministic across runs and does not depend on the limit", () => {
    const sections = Array.from({ length: 30 }, (_, i) => sec(`s${i}`, "Same", "fork"));
    const idx = index([PAGE], sections);
    const all = anchors(S.search(idx, "fork"));
    expect(anchors(S.search(idx, "fork"))).toEqual(all);
    expect(all).toEqual(sections.map((s) => `guide.html#${s.anchor}`));
    const limited = S.search(idx, "fork", 5);
    expect(limited.total).toBe(30);
    expect(anchors(limited)).toEqual(all.slice(0, 5));
  });
});

describe("result shape", () => {
  it("links the anchor, or the page itself for an intro record", () => {
    const idx = index([PAGE], [sec("", "Guide", "intro fork"), sec("x", "X", "fork")]);
    expect(S.search(idx, "fork").results.map((r) => r.href)).toEqual(["guide.html", "guide.html#x"]);
  });

  it("marks exactly the matched words in the heading", () => {
    const idx = index([PAGE], [sec("a", "Device groups and devices", "body")]);
    const [r] = S.search(idx, "device gro").results;
    expect(joined(r.headingParts)).toBe("Device groups and devices");
    expect(marked(r.headingParts)).toEqual(["Device", "groups"]);
  });

  it("cuts the snippet around the first match, at spaces, with ellipses", () => {
    const words = Array.from({ length: 80 }, (_, i) => `w${i}`);
    words[40] = "needle";
    const text = words.join(" ");
    const [r] = S.search(index([PAGE], [sec("a", "A", text)]), "needle").results;
    const snippet = joined(r.snippetParts);
    expect(snippet.startsWith("\u2026")).toBe(true);
    expect(snippet.endsWith("\u2026")).toBe(true);
    const inner = snippet.slice(1, -1);
    expect(text).toContain(inner);
    expect(inner.startsWith("w")).toBe(true);
    expect(inner.length).toBeLessThanOrEqual(200);
    expect(marked(r.snippetParts)).toEqual(["needle"]);
  });

  it("never cuts a snippet between the halves of a surrogate pair", () => {
    const emoji = "\u{1F600}";
    // No spaces, so the cut falls back to fixed offsets that land on even indexes,
    // which are low surrogates after the two-letter prefix.
    const text = "xy" + emoji.repeat(150) + "needle" + emoji.repeat(150);
    const [r] = S.search(index([PAGE], [sec("a", "A", text)]), "needle").results;
    const snippet = joined(r.snippetParts);
    expect(snippet).toContain("needle");
    expect(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(snippet)).toBe(false);
  });

  it("starts the snippet at the body start when only the heading matched", () => {
    const [r] = S.search(index([PAGE], [sec("a", "Fork", "Short body.")]), "fork").results;
    expect(r.snippetParts).toEqual([{ text: "Short body.", mark: false }]);
  });

  it("returns markup-looking page text as plain text parts", () => {
    const evil = 'Type <img src=x onerror="window.__pwned=1"> into the box.';
    const idx = index([PAGE], [sec("a", "<b>Bold</b> heading", evil)]);
    const [r] = S.search(idx, "onerror").results;
    expect(joined(r.snippetParts)).toBe(evil);
    expect(marked(r.snippetParts)).toEqual(["onerror"]);
    expect(r.heading).toBe("<b>Bold</b> heading");
    for (const part of [...r.headingParts, ...r.snippetParts]) {
      expect(Object.keys(part).sort()).toEqual(["mark", "text"]);
      expect(typeof part.text).toBe("string");
    }
  });
});

describe("the committed index", () => {
  const fakeWindow: { HERD_MANUAL_SEARCH_INDEX?: Index } = {};
  new Function("window", searchIndexJs)(fakeWindow);
  const real = fakeWindow.HERD_MANUAL_SEARCH_INDEX as Index;

  it("loads as a script that assigns the global", () => {
    expect(real.format).toBe(1);
    expect(real.pages.length).toBeGreaterThanOrEqual(20);
    expect(real.sections.length).toBeGreaterThan(real.pages.length);
  });

  it.each([
    ["fork", "glossary.html#fork"],
    ["dry run", "glossary.html#dry-run"],
    ["device group", "glossary.html#device-group"],
  ])("ranks the glossary entry first for %j", (query, first) => {
    expect(anchors(S.search(real, query))[0]).toBe(first);
  });

  const releases = real.pages.filter((p) => p.kind === "release" || p.kind === "earlier-release");

  it("has seven release pages", () => {
    expect(releases.map((p) => p.file).sort()).toEqual(
      [1, 2, 3, 4, 5, 6, 7].map((n) => `release-0-${n}-0.html`),
    );
  });

  it.each([1, 2, 3, 4, 5, 6, 7])("ranks release-0-%i-0.html first for its version", (n) => {
    expect(S.search(real, `0.${n}.0`).results[0].file).toBe(`release-0-${n}-0.html`);
  });

  it("ranks no earlier release page above a current page", () => {
    for (const query of ["fork", "reservation", "ldap", "release"]) {
      const kinds = S.search(real, query).results.map((r) => r.kind);
      const firstEarlier = kinds.indexOf("earlier-release");
      if (firstEarlier >= 0) {
        expect(kinds.slice(firstEarlier).every((k) => k === "earlier-release")).toBe(true);
      }
    }
  });
});

describe("browser rendering (jsdom)", () => {
  const win = window as unknown as { HERD_MANUAL_SEARCH_INDEX?: Index; __pwned?: number };

  // Run the file as the browser does (no `module` in scope) under a minimal
  // header. `idx` is preset as the global when given, as if the index had
  // already loaded; without it the box must load the index itself.
  function boot(idx?: Index) {
    document.head.querySelectorAll("script").forEach((s) => s.remove());
    document.body.textContent = "";
    const header = document.createElement("header");
    header.className = "manualbar";
    header.appendChild(document.createElement("nav"));
    document.body.appendChild(header);
    if (idx) win.HERD_MANUAL_SEARCH_INDEX = idx;
    new Function("module", manualJs)(undefined);
    return document.getElementById("msearch-input") as HTMLInputElement;
  }

  function type(input: HTMLInputElement, value: string) {
    input.focus();
    input.value = value;
    input.dispatchEvent(new Event("input"));
  }

  function key(target: EventTarget, k: string, init: KeyboardEventInit = {}) {
    target.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true, ...init }));
  }

  function indexScripts(): HTMLScriptElement[] {
    return Array.from(document.querySelectorAll("script")).filter((s) =>
      s.src.endsWith("assets/search-index.js"),
    );
  }

  const live = () => document.getElementById("msearch-status") as HTMLElement;
  const shown = () => document.querySelector(".msearch-count") as HTMLElement;

  afterEach(() => {
    delete win.HERD_MANUAL_SEARCH_INDEX;
    delete win.__pwned;
    document.body.textContent = "";
  });

  it("injects a labelled combobox, a shortcut hint, and a live region outside the panel", () => {
    const input = boot(index([PAGE], []));
    expect(input.closest("header.manualbar")).not.toBeNull();
    expect(input.getAttribute("role")).toBe("combobox");
    expect(input.getAttribute("aria-controls")).toBe("msearch-results");
    expect(input.getAttribute("aria-expanded")).toBe("false");
    expect(input.getAttribute("aria-keyshortcuts")).toBe("/");
    expect(input.hasAttribute("aria-describedby")).toBe(false);
    expect(document.querySelector(".msearch-key")?.getAttribute("aria-hidden")).toBe("true");
    const label = document.querySelector(`label[for="${input.id}"]`);
    expect(label?.textContent).toBe("Search the manual");
    expect(document.getElementById("msearch-results")?.getAttribute("role")).toBe("listbox");
    // The live region exists, empty, before any query, and is not inside the hidden panel.
    expect(live().getAttribute("role")).toBe("status");
    expect(live().getAttribute("aria-live")).toBe("polite");
    expect(live().textContent).toBe("");
    expect(live().closest(".msearch-panel")).toBeNull();
    expect(live().closest(".msearch")).not.toBeNull();
    expect(shown().getAttribute("aria-hidden")).toBe("true");
  });

  it("renders markup-looking text inert, with the match as a <mark> element", () => {
    const evil = 'Type <img src=x onerror="window.__pwned=1"> and <script>window.__pwned=2</script>';
    const input = boot(index([PAGE], [sec("a", "<em>Heading</em> onerror", evil)]));
    type(input, "onerror");
    const panel = document.querySelector(".msearch-panel") as HTMLElement;
    expect(panel.hidden).toBe(false);
    expect(panel.querySelector("img, script, em")).toBeNull();
    const option = panel.querySelector('[role="option"]') as HTMLElement;
    expect(option.querySelector(".msearch-snippet")?.textContent).toBe(evil);
    expect(option.querySelector(".msearch-heading")?.textContent).toBe("<em>Heading</em> onerror");
    expect(Array.from(option.querySelectorAll("mark")).map((m) => m.textContent)).toEqual([
      "onerror",
      "onerror",
    ]);
    expect(win.__pwned).toBeUndefined();
  });

  it("shows a visible no-results state and announces it", () => {
    const input = boot(index([PAGE], [sec("a", "A", "words")]));
    type(input, "zzzz");
    expect(shown().textContent).toBe("No results for \u201czzzz\u201d.");
    expect(shown().className).toBe("msearch-count empty");
    expect(live().textContent).toBe("No results for \u201czzzz\u201d.");
    expect(document.querySelectorAll('[role="option"]').length).toBe(0);
    expect(input.getAttribute("aria-expanded")).toBe("true");
  });

  it.each([
    [1, "1 result"],
    [3, "3 results"],
    [20, "20 results"],
    [25, "Top 20 of 25 results"],
  ])("with %i matches says %j and lists at most 20", (n, text) => {
    const sections = Array.from({ length: n }, (_, i) => sec(`s${i}`, "Fork", "x"));
    const input = boot(index([PAGE], sections));
    type(input, "fork");
    expect(shown().textContent).toBe(text);
    expect(live().textContent).toBe(text);
    expect(document.querySelectorAll('[role="option"]').length).toBe(Math.min(n, 20));
  });

  it("moves the active option with the arrow keys and closes on Escape", () => {
    const input = boot(index([PAGE], [sec("a", "Fork", "x"), sec("b", "Fork two", "y")]));
    type(input, "fork");
    key(input, "ArrowDown");
    expect(input.getAttribute("aria-activedescendant")).toBe("msearch-opt-0");
    key(input, "ArrowDown");
    expect(input.getAttribute("aria-activedescendant")).toBe("msearch-opt-1");
    expect(document.getElementById("msearch-opt-1")?.getAttribute("aria-selected")).toBe("true");
    key(input, "ArrowDown");
    expect(input.getAttribute("aria-activedescendant")).toBe("msearch-opt-0");
    key(input, "ArrowUp");
    expect(input.getAttribute("aria-activedescendant")).toBe("msearch-opt-1");
    key(input, "Escape");
    expect(input.getAttribute("aria-expanded")).toBe("false");
    expect(input.hasAttribute("aria-activedescendant")).toBe(false);
    expect(input.value).toBe("fork");
    key(input, "Escape");
    expect(input.value).toBe("");
  });

  it("focuses the search on a plain / from the page", () => {
    const input = boot(index([PAGE], []));
    key(document.body, "/");
    expect(document.activeElement).toBe(input);
  });

  it.each([
    ["Ctrl", { ctrlKey: true }],
    ["Meta", { metaKey: true }],
    ["Alt", { altKey: true }],
  ])("ignores %s+/", (_name, init) => {
    const input = boot(index([PAGE], []));
    key(document.body, "/", init);
    expect(document.activeElement).not.toBe(input);
  });

  it("leaves / alone while another field has focus", () => {
    const input = boot(index([PAGE], []));
    const other = document.createElement("textarea");
    document.body.appendChild(other);
    other.focus();
    key(other, "/");
    expect(document.activeElement).toBe(other);
    expect(document.activeElement).not.toBe(input);
  });

  it("loads the index on first use only, and answers a query typed before it arrives", () => {
    const input = boot();
    expect(indexScripts()).toHaveLength(0);
    type(input, "fork");
    expect(indexScripts()).toHaveLength(1);
    expect(shown().textContent).toBe("Loading the search index\u2026");
    expect(live().textContent).toBe("Loading the search index\u2026");
    expect(document.querySelectorAll('[role="option"]').length).toBe(0);
    type(input, "fork");
    expect(indexScripts()).toHaveLength(1);
    win.HERD_MANUAL_SEARCH_INDEX = index([PAGE], [sec("a", "Fork", "x")]);
    indexScripts()[0].dispatchEvent(new Event("load"));
    expect(shown().textContent).toBe("1 result");
    expect(document.querySelectorAll('[role="option"]').length).toBe(1);
  });

  it.each([
    ["an error event", (s: HTMLScriptElement) => s.dispatchEvent(new Event("error"))],
    ["a load that sets no global", (s: HTMLScriptElement) => s.dispatchEvent(new Event("load"))],
  ])("says the index did not load after %s", (_name, fire) => {
    const input = boot();
    type(input, "fork");
    fire(indexScripts()[0]);
    expect(shown().textContent).toBe("The search index did not load.");
    expect(live().textContent).toBe("The search index did not load.");
  });

  it("does not add the index script when the global is already present", () => {
    const input = boot(index([PAGE], [sec("a", "Fork", "x")]));
    type(input, "fork");
    expect(indexScripts()).toHaveLength(0);
    expect(shown().textContent).toBe("1 result");
  });
});
