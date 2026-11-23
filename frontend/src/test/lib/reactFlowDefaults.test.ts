// The @xyflow/react 12 to 13 migration keeps v12's behavior wherever v13
// changed a default (owner decision). This file pins the pieces that live
// outside a rendered page:
// - the fit-view padding constant gives v12's pixels through v13's own
//   viewport math;
// - every <ReactFlow> and <Controls> under src carries the pins (a structural
//   scan, so a second canvas cannot ship with v13's defaults);
// - main.tsx loads index.css before anything else, so Tailwind's cascade
//   layers are declared before v13's `@layer xyflow`.
// The rendered props of the one canvas are pinned in
// src/test/pages/TopologyEditorWiring.test.tsx.
import { describe, expect, it } from "vitest";
import { getViewportForBounds } from "@xyflow/react";
import { V12_FIT_VIEW_OPTIONS, V12_FIT_VIEW_PADDING } from "@/lib/reactFlowDefaults";

// v12 (@xyflow/system 0.0.84, parsePadding) turned its default padding of 0.1
// legacy units into this many pixels on each side of a viewport side V.
function v12DefaultPaddingPx(side: number): number {
  return Math.floor((side - side / (1 + 0.1)) * 0.5);
}

describe("V12_FIT_VIEW_PADDING", () => {
  it("is the fit-view options' padding", () => {
    expect(V12_FIT_VIEW_OPTIONS).toEqual({ padding: "4.545454545454546%" });
    expect(V12_FIT_VIEW_PADDING).toBe("4.545454545454546%");
  });

  it("fits a viewport exactly as v12's default padding did, for every side from 1 to 10000 px", () => {
    const bounds = { x: -40, y: 25, width: 640, height: 360 };
    const mismatches: number[] = [];
    for (let side = 1; side <= 10000; side++) {
      const width = side;
      const height = side + 7;
      // v13 reads a padding object of numbers as pixels, so this is v12's
      // result computed by v13's own getViewportForBounds.
      const v12 = getViewportForBounds(bounds, width, height, 0, Infinity, {
        top: v12DefaultPaddingPx(height),
        bottom: v12DefaultPaddingPx(height),
        left: v12DefaultPaddingPx(width),
        right: v12DefaultPaddingPx(width),
      });
      const pinned = getViewportForBounds(bounds, width, height, 0, Infinity, V12_FIT_VIEW_PADDING);
      if (pinned.x !== v12.x || pinned.y !== v12.y || pinned.zoom !== v12.zoom) mismatches.push(side);
    }
    expect(mismatches).toEqual([]);
  });

  it("differs from v13's own default, so the pin is doing something", () => {
    const bounds = { x: 0, y: 0, width: 640, height: 360 };
    const v13Default = getViewportForBounds(bounds, 1200, 800, 0, Infinity, "5%");
    const pinned = getViewportForBounds(bounds, 1200, 800, 0, Infinity, V12_FIT_VIEW_PADDING);
    expect(pinned.zoom).not.toBe(v13Default.zoom);
  });
});

const sources = import.meta.glob<string>(["/src/**/*.tsx", "!/src/test/**"], {
  query: "?raw",
  import: "default",
  eager: true,
});

function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");
}

function count(code: string, pattern: RegExp): number {
  return (code.match(pattern) ?? []).length;
}

// Limits: this is a text scan. It counts opening tags and pin attributes per
// file, so it sees a pin written as the exact attribute text below and cannot
// tie an attribute to a particular tag when a file holds several canvases; it
// does not see a ReactFlow rendered under another name (an alias import or a
// wrapper component), which is why the known-file list exists.
const REACT_FLOW_OPEN = /<ReactFlow(?=[\s>])/g;
const CONTROLS_OPEN = /<Controls(?=[\s>/])/g;
const PINS = [/forceColorMode="light"/g, /elevateEdgesOnSelect=\{false\}/g];
const FIT_VIEW_PIN = /fitViewOptions=\{V12_FIT_VIEW_OPTIONS\}/g;
const CONTROLS_PINNED = /<Controls\s[^>]*fitViewOptions=\{V12_FIT_VIEW_OPTIONS\}/g;

describe("React Flow v12 defaults structural scan", () => {
  const canvases = Object.entries(sources)
    .map(([path, raw]) => [path, stripComments(raw)] as const)
    .filter(([, code]) => count(code, REACT_FLOW_OPEN) > 0);

  it("scans a non-empty source tree", () => {
    expect(Object.keys(sources).length).toBeGreaterThan(50);
  });

  it("finds exactly the known React Flow canvases", () => {
    expect(canvases.map(([path]) => path)).toEqual(["/src/pages/TopologyEditorPage.tsx"]);
  });

  it.each(canvases)("every <ReactFlow> and <Controls> in %s carries the v12 pins", (_path, code) => {
    const flows = count(code, REACT_FLOW_OPEN);
    const controls = count(code, CONTROLS_OPEN);
    for (const pin of PINS) expect(count(code, pin)).toBeGreaterThanOrEqual(flows);
    expect(count(code, FIT_VIEW_PIN)).toBeGreaterThanOrEqual(flows + controls);
    expect(count(code, CONTROLS_PINNED)).toBe(controls);
  });

  it("fails a canvas or Controls without the pins (scanner self-check)", () => {
    const bad = '<ReactFlow nodes={n} fitView>\n<Controls />\n</ReactFlow>';
    expect(count(bad, REACT_FLOW_OPEN)).toBe(1);
    for (const pin of PINS) expect(count(bad, pin)).toBe(0);
    expect(count(bad, CONTROLS_OPEN)).toBe(1);
    expect(count(bad, CONTROLS_PINNED)).toBe(0);
  });
});

const mainSource = import.meta.glob<string>("/src/main.tsx", {
  query: "?raw",
  import: "default",
  eager: true,
})["/src/main.tsx"];

describe("main.tsx stylesheet order", () => {
  it('imports "./index.css" before any other module, so Tailwind\'s layers precede `@layer xyflow`', () => {
    const imports = Array.from(stripComments(mainSource).matchAll(/^import\s.*$/gm), (m) => m[0]);
    expect(imports.length).toBeGreaterThan(1);
    expect(imports[0]).toBe('import "./index.css";');
  });
});
