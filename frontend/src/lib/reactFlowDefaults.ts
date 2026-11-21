import type { FitViewOptions } from "@xyflow/react";

// @xyflow/react 13 changed defaults the topology canvas relied on. The owner
// decision for the 12 to 13 migration is to keep the v12 behavior, so the
// canvas (TopologyEditorPage, the app's only <ReactFlow>) pins each one:
//
// - Color mode: v12 defaulted `colorMode` to "light"; v13 removed that prop,
//   follows the system color scheme by default, and takes `forceColorMode` to
//   opt out. HERD has no dark mode, so the canvas passes forceColorMode="light"
//   (the same `light` class v12 put on the wrapper).
// - Selected edges: v12 defaulted `elevateEdgesOnSelect` to false; v13
//   defaults it to true. The canvas passes false.
// - Fit-view padding: see V12_FIT_VIEW_PADDING below. It goes on the canvas's
//   `fitViewOptions` (the initial fit) and on <Controls> (its fit-view button,
//   which calls fitView with the Controls' own options).

/**
 * v12's default fit-view padding, expressed in v13's units.
 *
 * v12 (`@xyflow/system` 0.0.84, `fitViewport`) defaulted `padding` to 0.1 in
 * legacy units and turned a number p into
 * `Math.floor((V - V / (1 + p)) * 0.5)` pixels on each side of a viewport
 * side of V pixels. For p = 0.1 that is floor(V / 22). v13 reads a number as
 * pixels and defaults to "5%", where a percentage q gives
 * `Math.floor(V * q * 0.01)` pixels. q = 100 / 22 reproduces v12 exactly; this
 * literal is the double nearest to it, and `reactFlowDefaults.test.ts` checks
 * that it gives the same pixel padding as the v12 formula for every viewport
 * side from 1 to 10000 pixels (the neighbouring 4.545454545454545 is one pixel
 * short whenever V is a multiple of 22).
 */
export const V12_FIT_VIEW_PADDING = "4.545454545454546%";

/** The fit-view options the canvas and its Controls pass, v12's padding. */
export const V12_FIT_VIEW_OPTIONS: FitViewOptions = { padding: V12_FIT_VIEW_PADDING };
