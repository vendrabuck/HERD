import { cn } from "@/lib/cn";

/**
 * The line a search-capped result list shows when the server holds more
 * matches than the one page the list asked for (issue #1590): `Showing <shown>
 * of <total> matching <noun>. Type more of the name to narrow the list.` A
 * list that shows one page of a search and lets the user type to narrow it
 * places this under its rows, so a match past the page is never silently
 * missing: the line says it exists and how to reach it. Renders nothing when
 * every match is shown (`total` at most `shown`) or `total` is not a number.
 */
export function MoreMatchesNotice({
  shown,
  total,
  noun,
  className,
}: {
  /** Rows the server returned on this page. */
  shown: number;
  /** The server's `total` for the same search. */
  total: unknown;
  /** What the rows are, plural: "devices". */
  noun: string;
  className?: string;
}) {
  if (typeof total !== "number" || !Number.isFinite(total) || total <= shown) return null;
  return (
    <p className={cn("text-xs text-gray-500", className)}>
      Showing {shown} of {total} matching {noun}. Type more of the name to narrow the list.
    </p>
  );
}
