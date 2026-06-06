import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

/**
 * A single headline metric: an uppercase gray label over a large
 * tabular-nums value. White card with a hover hairline on the border.
 */
export function StatCard({
  label,
  value,
  loading,
  className,
}: {
  label: ReactNode;
  value: ReactNode;
  loading?: boolean;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "bg-white rounded-lg border border-gray-200 p-5 transition-colors hover:border-gray-300",
        className,
      )}
    >
      <div className="text-xs uppercase tracking-wide text-gray-500">{label}</div>
      <div className="mt-2 text-3xl font-semibold text-gray-900 tabular-nums">
        {loading ? "-" : value}
      </div>
    </div>
  );
}
