import { hydrateCanvasNodes } from "@/api/inventory";
import type { CanvasData } from "@/types/topology.types";

/**
 * Hydrate a canvas's node device data fresh from inventory, then hand it to
 * loadCanvas -- factored out of TopologyEditorPage's own canvas-load effect
 * (issue #622 review) so every canvas the fork-history preview/diff/restore
 * flow loads goes through the identical path, not a raw loadCanvas. A thin
 * persisted/fetched node (`{ device: { id } }` with no name or
 * topology_type, e.g. straight off a fork version snapshot) would otherwise
 * render with stale or missing device data.
 *
 * Mirrors the page's own `hydrateCanvasNodes(canvas).then(loadCanvas).catch(()
 * => loadCanvas(canvas))` chain exactly: hydrateCanvasNodes itself never
 * throws for a per-device fetch failure (it falls back to the node's
 * existing data internally), so the catch here is belt-and-braces for a
 * rejection above that, kept only so this helper stays provably equivalent
 * to the load path it replaces.
 */
export async function hydrateAndLoadCanvas(
  canvas: CanvasData,
  loadCanvas: (data: CanvasData) => void,
): Promise<void> {
  try {
    const hydrated = await hydrateCanvasNodes(canvas);
    loadCanvas(hydrated);
  } catch {
    loadCanvas(canvas);
  }
}
