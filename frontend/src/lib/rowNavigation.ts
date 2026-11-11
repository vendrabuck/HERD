import type { MouseEvent } from "react";

/**
 * Row navigation on list pages (issue #1317). The keyboard and assistive
 * technology path to a row's destination is a real control in the row's
 * primary cell: a `<Link>` when the destination is a route, a `<button>` when
 * it opens a dialog. A click anywhere else on the row is a MOUSE CONVENIENCE
 * only, and it must never run for a click that started on an interactive
 * child: that child (the name link, a checkbox, a row action) already handles
 * its own click, so running the row handler too would navigate twice or
 * swallow the child's action.
 */

const INTERACTIVE_SELECTOR = [
  "a[href]",
  "button",
  "input",
  "select",
  "textarea",
  "label",
  "summary",
  "dialog",
  "[role='button']",
  "[role='link']",
  "[role='checkbox']",
  "[contenteditable='true']",
].join(", ");

/** Classes for the primary-cell link or button: focus ring per the HERD design tokens. */
export const ROW_PRIMARY_CONTROL_CLASS =
  "rounded-sm hover:underline focus:outline-none focus:ring-2 focus:ring-blue-500";

/**
 * True when the click started on an interactive element inside the row (not
 * the row itself). A target that is not an Element (a text node in some
 * synthetic events) is resolved through its parent.
 */
export function isInteractiveChildClick(event: MouseEvent<HTMLElement>): boolean {
  const row = event.currentTarget;
  let target: Node | null = event.target as Node | null;
  if (target && !(target instanceof Element)) target = target.parentElement;
  if (!(target instanceof Element)) return false;
  const hit = target.closest(INTERACTIVE_SELECTOR);
  return hit !== null && hit !== row && row.contains(hit);
}

/**
 * Wraps a row's mouse handler so it ignores clicks on interactive children.
 * Use as `<tr onClick={rowClick(() => navigate(to))}>`.
 */
export function rowClick(open: () => void) {
  return (event: MouseEvent<HTMLElement>) => {
    if (isInteractiveChildClick(event)) return;
    open();
  };
}
