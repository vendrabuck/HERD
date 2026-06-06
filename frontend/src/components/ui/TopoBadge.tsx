import { cn } from "@/lib/cn";

export type TopologyType = "PHYSICAL" | "CLOUD";

/**
 * Topology-type badge. Fixed pairing: PHYSICAL = blue, CLOUD = purple.
 * The `default` variant is the pale -100/-800 pair used in tables and lists.
 * The `onCanvas` variant uses the -200 tints DeviceNode needs for contrast
 * against the colored node fill on the topology canvas.
 */
const VARIANT_COLORS: Record<"default" | "onCanvas", Record<TopologyType, string>> = {
  default: {
    PHYSICAL: "bg-blue-100 text-blue-800",
    CLOUD: "bg-purple-100 text-purple-800",
  },
  onCanvas: {
    PHYSICAL: "bg-blue-200 text-blue-800",
    CLOUD: "bg-purple-200 text-purple-800",
  },
};

const FALLBACK = "bg-gray-200 text-gray-800";

export interface TopoBadgeProps {
  type: string;
  variant?: "default" | "onCanvas";
  className?: string;
}

export function TopoBadge({ type, variant = "default", className }: TopoBadgeProps) {
  const colors = VARIANT_COLORS[variant][type as TopologyType] ?? FALLBACK;
  return (
    <span className={cn("text-xs px-1.5 py-0.5 rounded font-medium", colors, className)}>
      {type}
    </span>
  );
}
