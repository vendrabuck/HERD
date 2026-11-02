/**
 * Issue #1260: every page rendered under AppLayout owns its scroll container.
 *
 * AppLayout wraps the app in `flex flex-col h-screen overflow-hidden` and puts
 * the `<Outlet />` in `flex-1 overflow-hidden`, so the shell never scrolls: a
 * page scrolls only when its own top-level element is a scroll container with
 * a bounded height. The convention is `h-full overflow-y-auto` on the page's
 * outermost element. SettingsPage lacked it, which was harmless until #1258's
 * Profile section pushed the Notifications Save button below the fold, where
 * no wheel, scrollbar, or key could reach it.
 *
 * Approach: no layout runs in jsdom, so this is a source scan.
 *
 * 1. Which pages render under AppLayout is read from the route table itself
 *    (`appRouteElements` through `createRoutesFromElements`, no render), by
 *    walking each route's element subtree for the AppLayout type and naming
 *    every function component found in it. A page file is matched to its
 *    route by name: `src/pages/<dir>/<Name>.tsx` exports `<Name>`.
 * 2. Each page file is parsed with the TypeScript compiler API (TSX), not a
 *    regular expression, so comments, strings, JSX text, sub-components
 *    declared above the page component, and nested callbacks cannot confuse
 *    it. The page component is the top-level exported function (or exported
 *    const arrow) named after the file.
 * 3. The judged return is the component's LAST statement, which must be a
 *    `return` of JSX: the page's main render. Its outermost element must be an
 *    intrinsic element (a `div`, not a component or a fragment) whose static
 *    `className` carries `h-full` and `overflow-y-auto` (or `overflow-auto`)
 *    as whole tokens, so a responsive variant such as `lg:overflow-y-auto`
 *    does not count. `h-full` is required too: without a bounded height an
 *    `overflow-y-auto` box grows with its content and AppLayout clips it, the
 *    same failure.
 *
 * The allowlists below are exact: an entry whose page now satisfies the rule
 * is stale and fails, and the outside-layout list must equal what the route
 * table says.
 *
 * Limits (what the scan cannot see):
 * - Early returns are not judged (the loading and not-found placeholders);
 *   only the component's last statement is. A page whose main render is not
 *   its last statement fails as "not a return" rather than being guessed at.
 * - A className built at run time is not read: a `cn(...)` or `clsx(...)`
 *   call or a variable fails as not static, and only the static parts of a
 *   template literal count.
 * - A scroll container supplied by CSS, an inline `style`, or a wrapping
 *   component is not seen (and fails, because the wrapper must be intrinsic).
 * - It proves the classes, not the geometry: that `h-full` resolves to a
 *   bounded height depends on AppLayout's Outlet container. The geometry is
 *   pinned in a real browser by tests/e2e/test_settings_page.py, whose round
 *   trips click controls below the fold at a 1920x1080 window.
 * - The stale-entry check judges the exported component only. The exported
 *   TopologyEditorPage returns a `<ReactFlowProvider>`, so its entry can go
 *   stale only by restructuring; its inner component gaining a scroll
 *   container is not seen.
 * - A page rendered from anywhere but the route table, or a route component
 *   whose function name differs from its file name, is not matched; the
 *   "every page file is routed" case fails on the latter.
 */
import { isValidElement, type ReactNode } from "react";
import { createRoutesFromElements, type RouteObject } from "react-router-dom";
import * as ts from "typescript";
import { describe, it, expect } from "vitest";
import { appRouteElements } from "@/routes";
import { AppLayout } from "@/components/layout/AppLayout";

// Pages the route table renders OUTSIDE AppLayout. They are full-screen
// pages that centre a card with `min-h-screen` and the document scrolls them.
// The route walk below must find exactly this set outside the layout.
const OUTSIDE_LAYOUT_PAGES = ["ConfigPage", "LoginPage", "RegisterPage"];

// Pages under AppLayout that deliberately do NOT scroll as a page.
// TopologyEditorPage: `flex h-full overflow-hidden` by design; the React Flow
// canvas and its side panels own their own scrolling and panning.
const NO_PAGE_SCROLL_BY_DESIGN = ["TopologyEditorPage"];

const SCROLL_TOKENS = ["overflow-y-auto", "overflow-auto"];
const HEIGHT_TOKEN = "h-full";

const pageSources = import.meta.glob<string>("/src/pages/**/*.tsx", {
  query: "?raw",
  import: "default",
  eager: true,
});

function pageName(path: string): string {
  return path.replace(/^.*\//, "").replace(/\.tsx$/, "");
}

// --- the route table: which page components render under AppLayout ---

function componentNames(node: ReactNode, into: Set<string>): void {
  if (Array.isArray(node)) {
    node.forEach((child) => componentNames(child, into));
    return;
  }
  if (!isValidElement(node)) return;
  if (typeof node.type === "function" && node.type.name) into.add(node.type.name);
  componentNames((node.props as { children?: ReactNode }).children, into);
}

function containsType(node: ReactNode, type: unknown): boolean {
  if (Array.isArray(node)) return node.some((child) => containsType(child, type));
  if (!isValidElement(node)) return false;
  if (node.type === type) return true;
  return containsType((node.props as { children?: ReactNode }).children, type);
}

function collectRouted(
  routes: RouteObject[],
  underLayout: boolean,
  inside: Set<string>,
  outside: Set<string>,
): void {
  for (const route of routes) {
    const here = underLayout || containsType(route.element, AppLayout);
    const names = new Set<string>();
    componentNames(route.element, names);
    names.forEach((name) => (here ? inside : outside).add(name));
    if (route.children) collectRouted(route.children, here, inside, outside);
  }
}

const routedInside = new Set<string>();
const routedOutside = new Set<string>();
collectRouted(createRoutesFromElements(appRouteElements), false, routedInside, routedOutside);

// --- the source rule ---

type Verdict = { ok: true } | { ok: false; reason: string };

function findPageComponentBody(
  file: ts.SourceFile,
  name: string,
): ts.Block | ts.Expression | undefined {
  for (const stmt of file.statements) {
    const exported = ts
      .getModifiers(stmt as ts.HasModifiers)
      ?.some((m) => m.kind === ts.SyntaxKind.ExportKeyword);
    if (!exported) continue;
    if (ts.isFunctionDeclaration(stmt) && stmt.name?.text === name) return stmt.body;
    if (ts.isVariableStatement(stmt)) {
      for (const decl of stmt.declarationList.declarations) {
        if (!ts.isIdentifier(decl.name) || decl.name.text !== name || !decl.initializer) continue;
        const init = decl.initializer;
        if (ts.isArrowFunction(init) || ts.isFunctionExpression(init)) return init.body;
      }
    }
  }
  return undefined;
}

function unwrap(expr: ts.Expression): ts.Expression {
  let e = expr;
  while (ts.isParenthesizedExpression(e) || ts.isAsExpression(e) || ts.isSatisfiesExpression(e)) {
    e = e.expression;
  }
  return e;
}

function staticClassName(attrs: ts.JsxAttributes): string | undefined {
  for (const prop of attrs.properties) {
    if (!ts.isJsxAttribute(prop) || prop.name.getText() !== "className") continue;
    let init: ts.Node | undefined = prop.initializer;
    if (init && ts.isJsxExpression(init)) init = init.expression;
    if (!init) return undefined;
    if (ts.isStringLiteral(init) || ts.isNoSubstitutionTemplateLiteral(init)) return init.text;
    if (ts.isTemplateExpression(init)) {
      return [init.head.text, ...init.templateSpans.map((s) => s.literal.text)].join(" ");
    }
    return undefined;
  }
  return undefined;
}

function judgePageSource(source: string, name: string): Verdict {
  const file = ts.createSourceFile(
    `${name}.tsx`,
    source,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  );
  const body = findPageComponentBody(file, name);
  if (!body) return { ok: false, reason: `no exported component named ${name}` };

  let returned: ts.Expression | undefined;
  if (ts.isBlock(body)) {
    const last = body.statements[body.statements.length - 1];
    if (!last || !ts.isReturnStatement(last) || !last.expression) {
      return { ok: false, reason: `the last statement of ${name} is not a return of JSX` };
    }
    returned = last.expression;
  } else {
    returned = body;
  }

  const top = unwrap(returned);
  let tag: ts.JsxTagNameExpression;
  let attrs: ts.JsxAttributes;
  if (ts.isJsxElement(top)) {
    tag = top.openingElement.tagName;
    attrs = top.openingElement.attributes;
  } else if (ts.isJsxSelfClosingElement(top)) {
    tag = top.tagName;
    attrs = top.attributes;
  } else if (ts.isJsxFragment(top)) {
    return { ok: false, reason: `${name} returns a fragment, which cannot scroll` };
  } else {
    return { ok: false, reason: `${name}'s last return is not JSX` };
  }

  const tagText = tag.getText(file);
  if (!/^[a-z]/.test(tagText)) {
    return { ok: false, reason: `${name}'s top-level element is <${tagText}>, not an intrinsic element` };
  }
  const className = staticClassName(attrs);
  if (className === undefined) {
    return { ok: false, reason: `${name}'s top-level <${tagText}> has no static className` };
  }
  const tokens = className.split(/\s+/).filter(Boolean);
  const missing = [
    ...(tokens.includes(HEIGHT_TOKEN) ? [] : [HEIGHT_TOKEN]),
    ...(SCROLL_TOKENS.some((t) => tokens.includes(t)) ? [] : [SCROLL_TOKENS.join(" or ")]),
  ];
  if (missing.length > 0) {
    return {
      ok: false,
      reason: `${name}'s top-level className "${className}" lacks ${missing.join(" and ")}`,
    };
  }
  return { ok: true };
}

const pages = Object.entries(pageSources)
  .map(([path, source]) => ({ path, name: pageName(path), source }))
  .sort((a, b) => a.path.localeCompare(b.path));

const judgedPages = pages.filter(
  (p) => !OUTSIDE_LAYOUT_PAGES.includes(p.name) && !NO_PAGE_SCROLL_BY_DESIGN.includes(p.name),
);

describe("page scroll container (issue #1260)", () => {
  it("scans a non-empty page tree", () => {
    expect(pages.length).toBeGreaterThan(20);
  });

  it("every page file is a route component", () => {
    const unrouted = pages
      .map((p) => p.name)
      .filter((name) => !routedInside.has(name) && !routedOutside.has(name));
    expect(unrouted).toEqual([]);
  });

  it("the outside-layout allowlist is exactly the pages the route table renders outside AppLayout", () => {
    const outsideOnly = pages
      .map((p) => p.name)
      .filter((name) => routedOutside.has(name) && !routedInside.has(name))
      .sort();
    expect(outsideOnly).toEqual([...OUTSIDE_LAYOUT_PAGES].sort());
  });

  it.each(NO_PAGE_SCROLL_BY_DESIGN)("by-design entry %s is still routed under AppLayout", (name) => {
    expect(routedInside.has(name)).toBe(true);
  });

  it.each([...OUTSIDE_LAYOUT_PAGES, ...NO_PAGE_SCROLL_BY_DESIGN])(
    "allowlisted %s does not follow the convention (else the entry is stale)",
    (name) => {
      const page = pages.find((p) => p.name === name);
      expect(page, `${name}.tsx not found under src/pages`).toBeDefined();
      expect(judgePageSource(page!.source, name).ok).toBe(false);
    },
  );

  it.each(judgedPages.map((p) => [p.path.replace(/^\/src\//, ""), p] as const))(
    "%s owns its scroll container",
    (_label, page) => {
      expect(routedInside.has(page.name), `${page.name} is not routed under AppLayout`).toBe(true);
      const verdict = judgePageSource(page.source, page.name);
      expect(verdict.ok ? "ok" : verdict.reason).toBe("ok");
    },
  );
});

describe("page scroll container scanner self-checks", () => {
  // Shaped like SettingsPage before the fix: a sub-component above the page
  // component, an early return, and a main render with no scroll container.
  const settingsBefore = `
    function Section() {
      return (<section className="h-full overflow-y-auto">x</section>);
    }
    export function SettingsPage() {
      if (loading) {
        return <div className="h-full overflow-y-auto">Loading</div>;
      }
      const rows = items.map((i) => {
        return (<div className="h-full overflow-y-auto">{i}</div>);
      });
      return (
        // a comment that mentions className="h-full overflow-y-auto"
        <div className="p-6 max-w-2xl">
          <p>Don't {"}"} be fooled</p>
          <Section />
        </div>
      );
    }
  `;

  it("fails a page whose main render lacks the container, despite decoys", () => {
    expect(judgePageSource(settingsBefore, "SettingsPage")).toEqual({
      ok: false,
      reason: `SettingsPage's top-level className "p-6 max-w-2xl" lacks h-full and overflow-y-auto or overflow-auto`,
    });
  });

  it("passes the same page once the main render is a scroll container", () => {
    const fixed = settingsBefore.replace(
      '<div className="p-6 max-w-2xl">',
      '<div className="h-full overflow-y-auto p-6 max-w-2xl">',
    );
    expect(fixed).not.toEqual(settingsBefore);
    expect(judgePageSource(fixed, "SettingsPage")).toEqual({ ok: true });
  });

  it.each([
    ["overflow without a bounded height", `export function P() { return <div className="overflow-y-auto" />; }`],
    ["a responsive variant only", `export function P() { return <div className="h-full lg:overflow-y-auto" />; }`],
    ["a fragment", `export function P() { return (<><div className="h-full overflow-y-auto" /></>); }`],
    ["a component wrapper", `export function P() { return <Shell className="h-full overflow-y-auto" />; }`],
    ["a run-time className", `export function P() { return <div className={cn("h-full", "overflow-y-auto")} />; }`],
    ["a last statement that is not a return", `export function P() { return <div className="h-full overflow-y-auto" />; foo(); }`],
    ["a non-exported component", `function P() { return <div className="h-full overflow-y-auto" />; }`],
  ])("fails %s", (_label, source) => {
    expect(judgePageSource(source, "P").ok).toBe(false);
  });

  it.each([
    ["overflow-auto", `export function P() { return <div className="h-full overflow-auto p-4" />; }`],
    ["a template literal's static part", "export function P() { return <div className={`h-full overflow-y-auto ${x}`} />; }"],
    ["an exported const arrow", `export const P = () => (<div className="h-full overflow-y-auto" />);`],
  ])("passes %s", (_label, source) => {
    expect(judgePageSource(source, "P")).toEqual({ ok: true });
  });
});
