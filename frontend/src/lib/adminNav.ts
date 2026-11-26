import { isAdminRole } from "@/lib/roles";

/**
 * The header's two link lists, the main navigation and the Administration
 * menu, both declared here and both filtered by role through the same
 * `REQUIREMENT_CHECKS` table. `components/layout/AppLayout.tsx` renders the
 * main navigation ONLY from `MAIN_NAV` (via `visibleMainNav`) and the menu ONLY
 * from `ADMIN_NAV` (via `visibleAdminNav`); the page titles in
 * `lib/routeTitles.ts` reuse the menu's labels.
 *
 * `requires` names the role predicate a viewer must satisfy to see the link.
 * The "admin" predicate is `isAdminRole`, the same predicate `AdminGuard` in
 * `components/guards.tsx` applies, so a link and its route's guard agree on
 * every role, an unknown one (no account loaded yet) included: neither shows
 * an admin page to it. `src/test/routes.test.tsx` fails when a link's
 * `requires` and its target's guard drift: an entry links to an
 * AdminGuard-guarded route exactly when it requires "admin" (issue #1402,
 * where Reporting sat in the main navigation with no requirement and bounced
 * a plain user to /topology).
 *
 * `ADMIN_NAV` is the "Administration dropdown" place of the admin-route rule
 * in docs/specs/CONVENTIONS.md: a new top-level admin page adds its route to
 * the AdminGuard group in `routes.tsx` AND an entry here, in `ADMIN_NAV`, or,
 * for a page that belongs in the main navigation as Reporting does, in
 * `MAIN_NAV` with `requires: "admin"`. `src/test/routes.test.tsx` also fails
 * when a guarded list path (no `:param`, not `/new`) has no entry in either
 * list.
 */
export type NavRequirement = "admin";

export interface NavItem {
  to: string;
  label: string;
  /** Absent: every signed-in viewer sees the link. */
  requires?: NavRequirement;
}

export interface AdminNavItem extends NavItem {
  requires: NavRequirement;
}

const REQUIREMENT_CHECKS: Record<NavRequirement, (role: string | undefined | null) => boolean> = {
  admin: isAdminRole,
};

function visibleTo(item: NavItem, role: string | undefined | null): boolean {
  return item.requires === undefined || REQUIREMENT_CHECKS[item.requires](role);
}

/** The main navigation's entries, in display order. */
export const MAIN_NAV: readonly NavItem[] = [
  { to: "/inventory", label: "Inventory" },
  { to: "/templates", label: "Templates" },
  { to: "/topology", label: "Topology" },
  { to: "/reservations", label: "Reservations" },
  { to: "/reporting", label: "Reporting", requires: "admin" },
];

export const ADMIN_NAV: readonly AdminNavItem[] = [
  { to: "/admin/add-device", label: "Add Device", requires: "admin" },
  { to: "/admin/groups", label: "User Groups", requires: "admin" },
  { to: "/admin/device-groups", label: "Device Groups", requires: "admin" },
  { to: "/admin/connections", label: "Connections", requires: "admin" },
  { to: "/admin/drivers", label: "Drivers", requires: "admin" },
  { to: "/admin/grants", label: "Grants", requires: "admin" },
  { to: "/admin/hypervisors", label: "Hypervisors", requires: "admin" },
  { to: "/admin/ldap-sync", label: "LDAP Sync", requires: "admin" },
  { to: "/admin/purpose-review", label: "Purpose Review", requires: "admin" },
  { to: "/admin/users", label: "Users", requires: "admin" },
  { to: "/admin/about", label: "About", requires: "admin" },
];

/** The main navigation entries this role may see, in display order. */
export function visibleMainNav(role: string | undefined | null): NavItem[] {
  return MAIN_NAV.filter((item) => visibleTo(item, role));
}

/** The admin menu entries this role may see, in display order. */
export function visibleAdminNav(role: string | undefined | null): AdminNavItem[] {
  return ADMIN_NAV.filter((item) => visibleTo(item, role));
}
