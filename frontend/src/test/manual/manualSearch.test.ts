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

  function boot(idx: Index) {
    document.body.textContent = "";
    const header = document.createElement("header");
    header.className = "manualbar";
    header.appendChild(document.createElement("nav"));
    document.body.appendChild(header);
    win.HERD_MANUAL_SEARCH_INDEX = idx;
    // Run the file as the browser does: no `module` in scope.
    new Function("module", manualJs)(undefined);
    return document.getElementById("msearch-input") as HTMLInputElement;
  }

  function type(input: HTMLInputElement, value: string) {
    input.value = value;
    input.dispatchEvent(new Event("input"));
  }

  afterEach(() => {
    delete win.HERD_MANUAL_SEARCH_INDEX;
    delete win.__pwned;
    document.body.textContent = "";
  });

  it("injects a labelled combobox into the header", () => {
    const input = boot(index([PAGE], []));
    expect(input.closest("header.manualbar")).not.toBeNull();
    expect(input.getAttribute("role")).toBe("combobox");
    expect(input.getAttribute("aria-controls")).toBe("msearch-results");
    expect(input.getAttribute("aria-expanded")).toBe("false");
    const label = document.querySelector(`label[for="${input.id}"]`);
    expect(label?.textContent).toBe("Search the manual");
    expect(document.getElementById("msearch-results")?.getAttribute("role")).toBe("listbox");
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

  it("shows a visible no-results state", () => {
    const input = boot(index([PAGE], [sec("a", "A", "words")]));
    type(input, "zzzz");
    expect(document.getElementById("msearch-status")?.textContent).toBe("No results for \u201czzzz\u201d.");
    expect(document.querySelectorAll('[role="option"]').length).toBe(0);
    expect(input.getAttribute("aria-expanded")).toBe("true");
  });

  it("moves the active option with the arrow keys and closes on Escape", () => {
    const input = boot(index([PAGE], [sec("a", "Fork", "x"), sec("b", "Fork two", "y")]));
    type(input, "fork");
    const key = (k: string) => input.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true }));
    key("ArrowDown");
    expect(input.getAttribute("aria-activedescendant")).toBe("msearch-opt-0");
    key("ArrowDown");
    expect(input.getAttribute("aria-activedescendant")).toBe("msearch-opt-1");
    expect(document.getElementById("msearch-opt-1")?.getAttribute("aria-selected")).toBe("true");
    key("ArrowDown");
    expect(input.getAttribute("aria-activedescendant")).toBe("msearch-opt-0");
    key("ArrowUp");
    expect(input.getAttribute("aria-activedescendant")).toBe("msearch-opt-1");
    key("Escape");
    expect(input.getAttribute("aria-expanded")).toBe("false");
    expect(input.hasAttribute("aria-activedescendant")).toBe(false);
    expect(input.value).toBe("fork");
    key("Escape");
    expect(input.value).toBe("");
  });

  it("says so when the index did not load", () => {
    const input = boot(index([PAGE], []));
    delete win.HERD_MANUAL_SEARCH_INDEX;
    type(input, "fork");
    expect(document.getElementById("msearch-status")?.textContent).toBe("The search index did not load.");
  });
});
