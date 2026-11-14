import { isValidElement, type ReactNode } from "react";
import { createRoutesFromElements, Outlet, type RouteObject } from "react-router-dom";
import { describe, it, expect } from "vitest";
import { appRouteElements } from "@/routes";
import { AuthGuard, AdminGuard, GuestGuard } from "@/components/guards";
import { AppLayout } from "@/components/layout/AppLayout";
import { ADMIN_NAV } from "@/lib/adminNav";
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

  it("every guarded list route except /reporting (main nav) has an ADMIN_NAV entry", () => {
    const listRoutes = guardedPaths
      .filter((path) => !path.includes(":") && !path.endsWith("/new") && path !== "/reporting")
      .sort();
    expect(ADMIN_NAV.map((item) => item.to).sort()).toEqual(listRoutes);
  });

  it("has no duplicate paths or labels", () => {
    expect(new Set(ADMIN_NAV.map((item) => item.to)).size).toBe(ADMIN_NAV.length);
    expect(new Set(ADMIN_NAV.map((item) => item.label)).size).toBe(ADMIN_NAV.length);
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
