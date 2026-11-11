import { matchPath } from "react-router-dom";
import { ADMIN_NAV } from "@/lib/adminNav";

/**
 * Per-route document titles for the pages under AppLayout (issue #1317).
 * Keyed on the route patterns in `src/routes.tsx`; `src/test/routes.test.tsx`
 * fails when a routed AppLayout path has no entry here or an entry names no
 * route. More specific patterns come first: the first match wins.
 */
export const ROUTE_TITLES: readonly (readonly [pattern: string, title: string])[] = [
  ["/inventory", "Inventory"],
  ["/inventory/:id", "Device"],
  ["/templates", "Templates"],
  ["/templates/new", "New template"],
  ["/templates/:id", "Template"],
  ["/topology", "Topology"],
  ["/topology-templates", "Topology templates"],
  ["/topology/:id", "Topology editor"],
  ["/reservations/calendar", "Reservation calendar"],
  ["/reservations", "Reservations"],
  ["/settings", "Settings"],
  ["/reporting", "Reporting"],
  ["/admin/groups/new", "New user group"],
  ["/admin/groups/:id", "User group"],
  ["/admin/device-groups/new", "New device group"],
  ["/admin/device-groups/:id", "Device group"],
  ...ADMIN_NAV.map((item) => [item.to, item.label] as const),
];

export const TITLE_SUFFIX = "HERD";

/** The page title for a pathname, or null when no route pattern matches. */
export function titleForPath(pathname: string): string | null {
  for (const [pattern, title] of ROUTE_TITLES) {
    if (matchPath({ path: pattern, end: true }, pathname)) {
      return `${title} - ${TITLE_SUFFIX}`;
    }
  }
  return null;
}
