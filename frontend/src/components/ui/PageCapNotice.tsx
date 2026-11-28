import { cn } from "@/lib/cn";
import { hasRowsPastPage } from "@/lib/pageCap";

/**
 * The line a capped list with no search to narrow it shows when the server
 * holds more rows than the one page it reads (issue #1590): `Showing <shown>
 * of <total> <noun>, <order>.` The sibling of `MoreMatchesNotice` for a list
 * that reads one page on purpose, such as a polled panel that must not walk
 * every page on each poll: the line says the rest exist and which rows the page
 * holds, with no hint to type. Renders nothing when every row is on the page
 * (`total` at most `shown`) or `total` is not a finite number.
 */
export function PageCapNotice({
  shown,
  total,
  noun,
  order,
  className,
}: {
  /** Rows the server returned on this page. */
  shown: number;
  /** The server's `total` for the same list. */
  total: unknown;
  /** What the rows are, plural: "scheduled applies". */
  noun: string;
  /** Which rows the page holds, as it reads after the comma: "latest scheduled first". */
  order: string;
  className?: string;
}) {
  if (!hasRowsPastPage(shown, total)) return null;
  return (
    <p className={cn("text-xs text-gray-500", className)}>
      Showing {shown} of {total} {noun}, {order}.
    </p>
  );
}
