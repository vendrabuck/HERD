import { matchPath } from "react-router-dom";
import { describe, it, expect } from "vitest";
import { ROUTE_TITLES, titleForPath } from "@/lib/routeTitles";

describe("titleForPath (issue #1317)", () => {
  it.each([
    ["/inventory", "Inventory - HERD"],
    ["/inventory/abc", "Device - HERD"],
    ["/templates/new", "New template - HERD"],
    ["/templates/t-1", "Template - HERD"],
    ["/topology", "Topology - HERD"],
    ["/topology/abc", "Topology editor - HERD"],
    ["/reservations/calendar", "Reservation calendar - HERD"],
    ["/admin/groups/new", "New user group - HERD"],
    ["/admin/groups/g-1", "User group - HERD"],
    ["/admin/device-groups/new", "New device group - HERD"],
    ["/admin/device-groups/dg-1", "Device group - HERD"],
    ["/topology-templates", "Topology templates - HERD"],
    ["/admin/about", "About - HERD"],
  ])("titles %s as %s", (path, title) => {
    expect(titleForPath(path)).toBe(title);
  });

  it("returns null for a path with no route", () => {
    expect(titleForPath("/nowhere")).toBe(null);
    expect(titleForPath("/inventory/a/b")).toBe(null);
  });
});

describe("ROUTE_TITLES order (issue #1317)", () => {
  // titleForPath takes the FIRST matching pattern, so a static pattern listed
  // after a parameterized one that also matches it would never be reached.
  it("lists every static pattern before any pattern that would shadow it", () => {
    const patterns = ROUTE_TITLES.map(([pattern]) => pattern);
    const shadowed: string[] = [];
    patterns.forEach((pattern, j) => {
      if (pattern.includes(":")) return;
      patterns.slice(0, j).forEach((earlier) => {
        if (matchPath({ path: earlier, end: true }, pattern)) {
          shadowed.push(`${pattern} after ${earlier}`);
        }
      });
    });
    expect(shadowed).toEqual([]);
  });
});
