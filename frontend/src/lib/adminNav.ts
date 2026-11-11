import { isAdminRole } from "@/lib/roles";

/**
 * The Administration menu's entries (issue #1317), in display order. The menu
 * in `components/layout/AppLayout.tsx` renders ONLY from this array, and the
 * page titles in `lib/routeTitles.ts` reuse its labels.
 *
 * This array is the "Administration dropdown" place of the admin-route rule
 * in docs/specs/CONVENTIONS.md: a new top-level admin page adds its route to
 * the AdminGuard group in `routes.tsx` AND an entry here. `src/test/routes.test.tsx`
 * fails when the two drift: every path here must be AdminGuard-guarded, and
 * every guarded list path (no `:param`, not `/new`) except `/reporting` (which
 * sits in the main nav) must be here.
 *
 * `requires` names the role predicate a viewer must satisfy to see the link.
 * Every entry requires an admin today; the field exists so a future entry with
 * a different gate is a data change, not a second hand-written block.
 */
export type NavRequirement = "admin";

export interface AdminNavItem {
  to: string;
  label: string;
  requires: NavRequirement;
}

const REQUIREMENT_CHECKS: Record<NavRequirement, (role: string | undefined | null) => boolean> = {
  admin: isAdminRole,
};

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

/** The admin menu entries this role may see, in display order. */
export function visibleAdminNav(role: string | undefined | null): AdminNavItem[] {
  return ADMIN_NAV.filter((item) => REQUIREMENT_CHECKS[item.requires](role));
}
