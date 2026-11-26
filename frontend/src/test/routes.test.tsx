import { isValidElement, type ReactNode } from "react";
import { createRoutesFromElements, Outlet, type RouteObject } from "react-router-dom";
import { describe, it, expect } from "vitest";
import { appRouteElements } from "@/routes";
import { AuthGuard, AdminGuard, GuestGuard } from "@/components/guards";
import { AppLayout } from "@/components/layout/AppLayout";
import { ADMIN_NAV, MAIN_NAV, visibleAdminNav, visibleMainNav } from "@/lib/adminNav";
import { isAdminRole } from "@/lib/roles";
import { ROUTE_TITLES } from "@/lib/routeTitles";
import { OidcCompletePage } from "@/pages/OidcCompletePage";

// Issue #551: structural walk via createRoutesFromElements (public API), no
// render, so AppLayout and the pages never mount. AdminGuard.test.tsx pins
// the guard's own redirect/render behavior; this file pins route
// membership: which paths sit under AdminGuard, and that AdminGuard itself
// sits under AuthGuard.

const EXPECTED_ADMIN_GUARDED_PATHS = [
  "/reporting",
  "/admin/add-device",
  "/admin/users",
  "/admin/groups",
  "/admin/groups/new",
  "/admin/groups/:id",
  "/admin/device-groups",
  "/admin/device-groups/new",
  "/admin/device-groups/:id",
  "/admin/connections",
  "/admin/drivers",
  "/admin/grants",
  "/admin/hypervisors",
  "/admin/ldap-sync",
  "/admin/purpose-review",
  "/admin/about",
];

interface GuardAncestry {
  adminGuarded: boolean;
  authGuarded: boolean;
}

// Searches the route element's whole subtree for componentType, not just
// its top-level type, so a future wrapper (e.g. an ErrorBoundary placed
// around AdminGuard) does not false-red this test.
function elementTreeContains(node: ReactNode, componentType: unknown): boolean {
  if (Array.isArray(node)) {
    return node.some((child) => elementTreeContains(child, componentType));
  }
  if (!isValidElement(node)) {
    return false;
  }
  if (node.type === componentType) {
    return true;
  }
  const children = (node.props as { children?: ReactNode }).children;
  return elementTreeContains(children, componentType);
}

function walkRoutes(
  routes: RouteObject[],
  inherited: GuardAncestry,
  guardsByPath: Map<string, GuardAncestry>,
  duplicatePaths: string[],
): void {
  for (const route of routes) {
    const guards: GuardAncestry = {
      adminGuarded: inherited.adminGuarded || elementTreeContains(route.element, AdminGuard),
      authGuarded: inherited.authGuarded || elementTreeContains(route.element, AuthGuard),
    };

    if (typeof route.path === "string") {
      if (guardsByPath.has(route.path)) {
        duplicatePaths.push(route.path);
      }
      guardsByPath.set(route.path, guards);
    }

    if (route.children) {
      walkRoutes(route.children, guards, guardsByPath, duplicatePaths);
    }
  }
}

function flattenRoutes(routes: RouteObject[]): RouteObject[] {
  const flat: RouteObject[] = [];
  for (const route of routes) {
    flat.push(route);
    if (route.children) {
      flat.push(...flattenRoutes(route.children));
    }
  }
  return flat;
}

const routeObjects = createRoutesFromElements(appRouteElements);
const guardsByPath = new Map<string, GuardAncestry>();
const duplicatePaths: string[] = [];
walkRoutes(routeObjects, { adminGuarded: false, authGuarded: false }, guardsByPath, duplicatePaths);

describe("App route table: AdminGuard membership (issue #551)", () => {
  it("the set of AdminGuard-guarded paths equals the expected list exactly", () => {
    const actualGuardedPaths = [...guardsByPath.entries()]
      .filter(([, guards]) => guards.adminGuarded)
      .map(([path]) => path)
      .sort();

    expect(actualGuardedPaths).toEqual([...EXPECTED_ADMIN_GUARDED_PATHS].sort());
  });

  it("every AdminGuard-guarded path also sits under AuthGuard", () => {
    // AdminGuard returns null rather than redirecting when there is no
    // user (see components/guards.tsx), so losing the AuthGuard parent
    // would blank-page an anonymous visitor instead of sending them to
    // /login.
    for (const path of EXPECTED_ADMIN_GUARDED_PATHS) {
      expect(guardsByPath.get(path)?.authGuarded, path).toBe(true);
    }
  });

  it("the only unguarded /admin-prefixed path is the bare /admin redirect", () => {
    const adminPrefixed = [...guardsByPath.entries()].filter(
      ([path]) => path === "/admin" || path.startsWith("/admin/"),
    );
    const unguarded = adminPrefixed
      .filter(([, guards]) => !guards.adminGuarded)
      .map(([path]) => path);

    expect(unguarded).toEqual(["/admin"]);
  });

  it("the route table has no duplicate paths", () => {
    expect(duplicatePaths).toEqual([]);
  });

  it("the AdminGuard group renders an Outlet for its children", () => {
    const adminGuardGroups = flattenRoutes(routeObjects).filter(
      (route) => typeof route.path !== "string" && elementTreeContains(route.element, AdminGuard),
    );

    expect(adminGuardGroups.length).toBeGreaterThan(0);
    for (const group of adminGuardGroups) {
      expect(elementTreeContains(group.element, Outlet)).toBe(true);
    }
  });
});

// Issue #1317: the Administration menu renders only from ADMIN_NAV, so this is
// where "the dropdown" place of the four-place admin-route rule is checked.
describe("ADMIN_NAV matches the AdminGuard route group (issue #1317)", () => {
  const guardedPaths = [...guardsByPath.entries()]
    .filter(([, guards]) => guards.adminGuarded)
    .map(([path]) => path);

  it("every ADMIN_NAV entry links to an AdminGuard-guarded route", () => {
    for (const item of ADMIN_NAV) {
      expect(guardedPaths, item.to).toContain(item.to);
    }
  });

  it("every guarded list route has a link in ADMIN_NAV or MAIN_NAV, and only there once", () => {
    const listRoutes = guardedPaths
      .filter((path) => !path.includes(":") && !path.endsWith("/new"))
      .sort();
    const linked = [...ADMIN_NAV, ...MAIN_NAV]
      .map((item) => item.to)
      .filter((to) => guardedPaths.includes(to))
      .sort();
    expect(linked).toEqual(listRoutes);
  });

  it("has no duplicate paths or labels", () => {
    expect(new Set(ADMIN_NAV.map((item) => item.to)).size).toBe(ADMIN_NAV.length);
    expect(new Set(ADMIN_NAV.map((item) => item.label)).size).toBe(ADMIN_NAV.length);
  });
});

// Issue #1402: Reporting sat in the main navigation with no requirement, so a
// plain user saw a link whose route AdminGuard bounced to /topology. Every
// header link now states its requirement, and the requirement must match the
// guard on its target route in both directions: a link to a guarded route
// requires "admin" (else a non-admin sees a link that bounces), and a link
// that requires "admin" targets a guarded route (else an admin-only link hides
// a page every user may open).
describe("header links require exactly what their route's guard requires (issue #1402)", () => {
  const guardedPaths = new Set(
    [...guardsByPath.entries()].filter(([, guards]) => guards.adminGuarded).map(([path]) => path),
  );
  const appLayoutPaths = pathsUnderAppLayout(routeObjects, false, []);
  const headerLinks = [
    ...MAIN_NAV.map((item) => ["MAIN_NAV", item] as const),
    ...ADMIN_NAV.map((item) => ["ADMIN_NAV", item] as const),
  ];

  it.each(headerLinks)("%s %o: requires admin exactly when its route is AdminGuard-guarded", (_list, item) => {
    expect(appLayoutPaths, item.to).toContain(item.to);
    expect(item.requires === "admin", item.to).toBe(guardedPaths.has(item.to));
  });

  it("MAIN_NAV lists the five main pages in display order, Reporting last and admin only", () => {
    expect(MAIN_NAV).toEqual([
      { to: "/inventory", label: "Inventory" },
      { to: "/templates", label: "Templates" },
      { to: "/topology", label: "Topology" },
      { to: "/reservations", label: "Reservations" },
      { to: "/reporting", label: "Reporting", requires: "admin" },
    ]);
  });

  it("no path is linked from both lists, and MAIN_NAV has no duplicate path or label", () => {
    const adminPaths = new Set(ADMIN_NAV.map((item) => item.to));
    expect(MAIN_NAV.filter((item) => adminPaths.has(item.to))).toEqual([]);
    expect(new Set(MAIN_NAV.map((item) => item.to)).size).toBe(MAIN_NAV.length);
    expect(new Set(MAIN_NAV.map((item) => item.label)).size).toBe(MAIN_NAV.length);
  });
});

// The role space times both filters. The guard's own rule (AdminGuard.test.tsx)
// renders an admin page only when isAdminRole(user.role) holds, and renders
// nothing for an unknown account; the filters must show a requirement-gated
// link on exactly the same roles.
describe("visibleMainNav and visibleAdminNav over the role space (issue #1402)", () => {
  const plainMain = MAIN_NAV.filter((item) => item.requires === undefined).map((item) => item.to);
  const allMain = MAIN_NAV.map((item) => item.to);
  const allAdmin = ADMIN_NAV.map((item) => item.to);

  const roles: [description: string, role: string | null | undefined, admin: boolean][] = [
    ["user", "user", false],
    ["admin", "admin", true],
    ["superadmin", "superadmin", true],
    ["unknown (undefined, account not loaded)", undefined, false],
    ["unknown (null)", null, false],
    ["malformed (empty string)", "", false],
    ["malformed (wrong case, Admin)", "Admin", false],
    ["malformed (padded, ' admin')", " admin", false],
    ["malformed (unrecognized, auditor)", "auditor", false],
  ];

  it("the plain main links are every MAIN_NAV entry but Reporting", () => {
    expect(plainMain).toEqual(["/inventory", "/templates", "/topology", "/reservations"]);
  });

  it.each(roles)("role %s: main and admin links match the guard's rule", (_d, role, admin) => {
    expect(visibleMainNav(role).map((item) => item.to)).toEqual(admin ? allMain : plainMain);
    expect(visibleAdminNav(role).map((item) => item.to)).toEqual(admin ? allAdmin : []);
    expect(isAdminRole(role)).toBe(admin);
  });
});

function pathsUnderAppLayout(routes: RouteObject[], inside: boolean, out: string[]): string[] {
  for (const route of routes) {
    const here = inside || elementTreeContains(route.element, AppLayout);
    if (here && typeof route.path === "string") out.push(route.path);
    if (route.children) pathsUnderAppLayout(route.children, here, out);
  }
  return out;
}

describe("ROUTE_TITLES covers the AppLayout routes (issue #1317)", () => {
  // The bare /admin path is a redirect, never a rendered page.
  const titled = pathsUnderAppLayout(routeObjects, false, []).filter((p) => p !== "/admin");
  const patterns = ROUTE_TITLES.map(([pattern]) => pattern);

  it("every routed AppLayout path has a title entry", () => {
    expect(titled.length).toBeGreaterThan(0);
    for (const path of titled) {
      expect(patterns, path).toContain(path);
    }
  });

  it("every title entry names a real AppLayout route, once", () => {
    for (const pattern of patterns) {
      expect(titled, pattern).toContain(pattern);
    }
    expect(new Set(patterns).size).toBe(patterns.length);
  });
});

// ADR 0016 phase 4: the single sign-on completion page is a guest page
// outside the app layout, like /login and /register.
describe("the /login/sso route (ADR 0016)", () => {
  const guestPaths = flattenRoutes(routeObjects)
    .filter((route) => typeof route.path === "string" && elementTreeContains(route.element, GuestGuard))
    .map((route) => route.path)
    .sort();

  it("the GuestGuard paths are exactly /login, /login/sso, and /register", () => {
    expect(guestPaths).toEqual(["/login", "/login/sso", "/register"]);
  });

  it("renders OidcCompletePage inside GuestGuard, outside AuthGuard and AppLayout", () => {
    const route = flattenRoutes(routeObjects).find((r) => r.path === "/login/sso");
    expect(route).toBeDefined();
    expect(elementTreeContains(route!.element, GuestGuard)).toBe(true);
    expect(elementTreeContains(route!.element, OidcCompletePage)).toBe(true);
    expect(guardsByPath.get("/login/sso")).toEqual({ adminGuarded: false, authGuarded: false });
    expect(pathsUnderAppLayout(routeObjects, false, [])).not.toContain("/login/sso");
  });
});
