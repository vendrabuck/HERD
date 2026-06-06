import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

/**
 * The one quiet gray line for empty states: centered, gray-500, no
 * illustration. Use `as="row"` to render a table row spanning `colSpan`.
 */
export function EmptyState({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("text-sm text-gray-500 text-center py-8", className)}>{children}</div>
  );
}

export function EmptyRow({
  colSpan,
  children = "No data in this window",
}: {
  colSpan: number;
  children?: ReactNode;
}) {
  return (
    <tr>
      <td colSpan={colSpan} className="px-4 py-8 text-center text-sm text-gray-500">
        {children}
      </td>
    </tr>
  );
}
