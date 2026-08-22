import { isValidElement } from "react";
import { createRoutesFromElements, type RouteObject } from "react-router-dom";
import { describe, it, expect } from "vitest";
import { appRouteElements } from "@/routes";
import { AdminGuard } from "@/components/guards";

// Issue #551: AdminGuard.test.tsx pins AdminGuard's own redirect/render
// behavior against a hand-built route tree, but it never renders App.tsx, so
// it cannot catch the route table itself placing a route outside the
// AdminGuard-wrapped group (a PR #550 reviewer demonstrated this by moving
// the /reporting Route out of the group and getting a green 757/757 suite).
//
// This test pins route MEMBERSHIP structurally instead of by rendering:
// `createRoutesFromElements` (public react-router-dom API) turns the real
// `appRouteElements` JSX into a plain `RouteObject[]` tree with no rendering
// involved, so we never mount `AppLayout` or the 20+ unmocked pages that a
// real render of `App`'s default export would require. We then walk that
// tree and record, for every route with a `path`, whether an ancestor
// route's `element` is an `AdminGuard` (by React element `type` identity).
//
// AdminGuard's own behavior (redirect a non-admin, render children for an
// admin/superadmin, render nothing while unauthenticated) stays pinned by
// AdminGuard.test.tsx; this file only pins which paths sit under it.

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

interface PathGuardEntry {
  path: string;
  guarded: boolean;
}

function isAdminGuardElement(element: RouteObject["element"]): boolean {
  return isValidElement(element) && element.type === AdminGuard;
}

function collectPathGuards(routes: RouteObject[], inheritedGuarded: boolean): PathGuardEntry[] {
  const entries: PathGuardEntry[] = [];

  for (const route of routes) {
    const guarded = inheritedGuarded || isAdminGuardElement(route.element);

    if (typeof route.path === "string") {
      entries.push({ path: route.path, guarded });
    }

    if (route.children) {
      entries.push(...collectPathGuards(route.children, guarded));
    }
  }

  return entries;
}

const routeObjects = createRoutesFromElements(appRouteElements);
const pathGuards = collectPathGuards(routeObjects, false);

function guardedFor(path: string): boolean | undefined {
  return pathGuards.find((entry) => entry.path === path)?.guarded;
}

describe("App route table: AdminGuard membership (issue #551)", () => {
  it.each(EXPECTED_ADMIN_GUARDED_PATHS)(
    "%s has an AdminGuard ancestor",
    (path) => {
      const guarded = guardedFor(path);
      expect(guarded, `expected route "${path}" to be found in the tree`).toBeDefined();
      expect(guarded).toBe(true);
    },
  );

  it("the set of AdminGuard-guarded paths equals the expected list exactly", () => {
    const actualGuardedPaths = pathGuards
      .filter((entry) => entry.guarded)
      .map((entry) => entry.path)
      .sort();

    expect(actualGuardedPaths).toEqual([...EXPECTED_ADMIN_GUARDED_PATHS].sort());
  });

  it("every /admin/-prefixed path is guarded, and the only unguarded /admin path is the bare redirect", () => {
    const adminPrefixed = pathGuards.filter(
      (entry) => entry.path === "/admin" || entry.path.startsWith("/admin/"),
    );
    const unguarded = adminPrefixed.filter((entry) => !entry.guarded).map((entry) => entry.path);

    expect(unguarded).toEqual(["/admin"]);
  });
});
