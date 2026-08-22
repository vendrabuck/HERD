import { isValidElement, type ReactNode } from "react";
import { createRoutesFromElements, Outlet, type RouteObject } from "react-router-dom";
import { describe, it, expect } from "vitest";
import { appRouteElements } from "@/routes";
import { AuthGuard, AdminGuard } from "@/components/guards";

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
