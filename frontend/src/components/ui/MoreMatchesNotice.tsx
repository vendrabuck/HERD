import { cn } from "@/lib/cn";
import { hasRowsPastPage } from "@/lib/pageCap";

/**
 * The line a search-capped result list shows when the server holds more
 * matches than the one page the list asked for (issue #1590): `<total minus
 * shown> more matching <noun> are not listed. Type more of the name to narrow
 * the list.` A list that shows one page of a search and lets the user type to
 * narrow it places this under its rows, so a match past the page is never
 * silently missing: the line says how many exist past the page and how to
 * reach them. It counts against the page the server returned, not the rows the
 * list draws, so a list that hides some of the page on the client (the Add
 * list hides devices already selected) still states a true number. Renders
 * nothing when every match is on the page (`total` at most `shown`) or `total`
 * is not a finite number.
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
  if (!hasRowsPastPage(shown, total)) return null;
  return (
    <p className={cn("text-xs text-gray-500", className)}>
      {total - shown} more matching {noun} are not listed. Type more of the name to narrow the list.
    </p>
  );
}
