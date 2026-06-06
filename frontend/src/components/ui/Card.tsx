import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

/**
 * The canonical card: white surface, gray-200 border, rounded-lg, shadow-sm.
 * The border does the separating work; elevation stays minimal.
 */
export function Card({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "bg-white rounded-lg border border-gray-200 shadow-sm overflow-hidden",
        className,
      )}
    >
      {children}
    </div>
  );
}

/**
 * Section-card header strip: a semibold title on the left and an optional
 * action (button, link) on the right, over a bottom border.
 */
export function CardHeader({
  title,
  action,
  className,
}: {
  title: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "px-4 py-3 border-b border-gray-200 flex items-center justify-between gap-2",
        className,
      )}
    >
      <h3 className="text-sm font-semibold text-gray-900">{title}</h3>
      {action}
    </div>
  );
}
