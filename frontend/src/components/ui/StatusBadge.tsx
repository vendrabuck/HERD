import { cn } from "@/lib/cn";

/**
 * Single source of truth for status-badge color tokens. Keyed by the backend
 * enum string, value is the pale -100 background + saturated -700/800 text pair
 * the design system prescribes. Centralizes maps that were previously duplicated
 * in InventoryPage, ReservationsPage, ReservationPanel, and DeviceHealthBadge.
 *
 * Tokens are preserved verbatim from those call sites so existing tests that
 * assert on the class strings (DeviceHealthBadge.test.tsx) keep passing.
 */
const STATUS_COLORS: Record<string, string> = {
  // Device status (inventory)
  AVAILABLE: "bg-green-100 text-green-800",
  RESERVED: "bg-yellow-100 text-yellow-800",
  OFFLINE: "bg-gray-100 text-gray-600",
  MAINTENANCE: "bg-red-100 text-red-700",

  // Reservation lifecycle
  ACTIVE: "bg-green-100 text-green-800",
  PENDING: "bg-yellow-100 text-yellow-800",
  PENDING_PROVISION: "bg-amber-100 text-amber-800",
  COMPLETED: "bg-gray-100 text-gray-600",
  CANCELLED: "bg-red-100 text-red-700",
  FAILED: "bg-red-200 text-red-900",

  // Device health
  HEALTHY: "bg-green-100 text-green-800",
  DEGRADED: "bg-yellow-100 text-yellow-800",
  UNREACHABLE: "bg-red-100 text-red-800",
  UNKNOWN: "bg-gray-100 text-gray-600",
};

const FALLBACK = "bg-gray-100 text-gray-600";

export interface StatusBadgeProps {
  /** Backend enum string, rendered verbatim unless `label` overrides the text. */
  status: string;
  /** Optional display text; keys still come from `status`. */
  label?: string;
  title?: string;
  className?: string;
}

export function StatusBadge({ status, label, title, className }: StatusBadgeProps) {
  return (
    <span
      title={title}
      className={cn(
        "inline-block text-xs px-1.5 py-0.5 rounded font-medium",
        STATUS_COLORS[status] ?? FALLBACK,
        className,
      )}
    >
      {label ?? status}
    </span>
  );
}
