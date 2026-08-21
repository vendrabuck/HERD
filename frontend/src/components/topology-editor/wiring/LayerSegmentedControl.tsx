import { cn } from "@/lib/cn";
import { LAYER_OPTIONS, LAYER_SEGMENT_CLASSES } from "../edges/layerStyles";
import type { EdgeLayerType } from "@/types/topology.types";

export type LayerSegmentedControlSize = "md" | "sm" | "xs";

const SIZE_CLASSES: Record<LayerSegmentedControlSize, string> = {
  md: "px-4 py-1.5 text-sm",
  sm: "px-2.5 py-0.5 text-xs",
  xs: "px-2 py-0.5 text-[11px]",
};

export interface LayerSegmentedControlProps {
  value: EdgeLayerType;
  onChange: (layer: EdgeLayerType) => void;
  size?: LayerSegmentedControlSize;
  testIdPrefix?: string;
  // Optional hover tooltip on the whole control (issue #531 layer half): the
  // caller decides whether this instance needs the canvas-annotation-only
  // caveat spelled out. Undefined omits the title attribute entirely, so
  // existing callers are unaffected.
  title?: string;
}

/**
 * The shared L1/L2/L3 segmented picker (issue #517 review item 11), used by
 * QuickConnectPopover's single layer selector, WiringDialog's "New lines"
 * default selector, and WiringDialog's per-line expanded selector: same
 * behavior and color rules at three different densities.
 */
export function LayerSegmentedControl({
  value,
  onChange,
  size = "sm",
  testIdPrefix,
  title,
}: LayerSegmentedControlProps) {
  return (
    <div className="flex gap-1" title={title}>
      {LAYER_OPTIONS.map((l) => (
        <button
          key={l}
          type="button"
          data-testid={testIdPrefix ? `${testIdPrefix}-${l}` : undefined}
          onClick={() => onChange(l)}
          aria-pressed={value === l}
          className={cn(
            "rounded font-medium transition-colors",
            SIZE_CLASSES[size],
            value === l ? LAYER_SEGMENT_CLASSES[l] : "bg-gray-100 text-gray-700 hover:bg-gray-200",
          )}
        >
          {l}
        </button>
      ))}
    </div>
  );
}
