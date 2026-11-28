/**
 * The guard `MoreMatchesNotice` and `PageCapNotice` share (issue #1590): true
 * when the server holds rows past the one page a list read, that is when its
 * `total` is a finite number above the `shown` rows of the page. A missing,
 * non-numeric, or infinite `total`, or one at most `shown`, is false, so a
 * notice never states a count the answer does not support.
 */
export function hasRowsPastPage(shown: number, total: unknown): total is number {
  return typeof total === "number" && Number.isFinite(total) && total > shown;
}
