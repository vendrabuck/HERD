import { describe, it, expect } from "vitest";
import {
  nextTopologySort,
  parseSavedTopologyFilter,
  parseTopologySort,
  serializeTopologyFilter,
} from "@/lib/topologyFilters";
import { topologyListParams } from "@/api/topologies";
import type { SortState } from "@/stores/preferencesStore";

describe("parseSavedTopologyFilter (issue #958)", () => {
  it("reads a valid saved object", () => {
    expect(parseSavedTopologyFilter({ search: "lab", owner: "mine" })).toEqual({
      search: "lab",
      owner: "mine",
    });
  });

  it.each([
    ["undefined", undefined],
    ["null", null],
    ["a string", "mine"],
    ["an array", ["mine"]],
    ["an empty object", {}],
  ])("falls back to the defaults for %s", (_label, raw) => {
    expect(parseSavedTopologyFilter(raw)).toEqual({ search: "", owner: "all" });
  });

  it.each(["theirs", "MINE", "", 1, null, true])(
    "a stale or foreign owner %j falls back to all",
    (owner) => {
      expect(parseSavedTopologyFilter({ owner }).owner).toBe("all");
    },
  );

  it("a non-string search falls back to empty", () => {
    expect(parseSavedTopologyFilter({ search: 42 }).search).toBe("");
  });
});

describe("serializeTopologyFilter", () => {
  it("omits owner at its default", () => {
    expect(serializeTopologyFilter({ search: "", owner: "all" })).toEqual({ search: "" });
  });

  it("writes a non-default owner", () => {
    expect(serializeTopologyFilter({ search: "x", owner: "mine" })).toEqual({
      search: "x",
      owner: "mine",
    });
  });

  it("round-trips through parse", () => {
    for (const state of [
      { search: "", owner: "all" as const },
      { search: "core", owner: "mine" as const },
    ]) {
      expect(parseSavedTopologyFilter(serializeTopologyFilter(state))).toEqual(state);
    }
  });
});

describe("parseTopologySort", () => {
  it.each(["name", "owner_name", "created_at", "updated_at"])("accepts %s", (field) => {
    expect(parseTopologySort({ sortBy: field, sortDir: "desc" })).toEqual({
      sortBy: field,
      sortDir: "desc",
    });
  });

  it.each(["status", "id", "created_by", "NAME", ""])(
    "a field the endpoint does not sort by (%s) means no explicit sort",
    (field) => {
      expect(parseTopologySort({ sortBy: field, sortDir: "asc" })).toBeNull();
    },
  );

  it("a bad direction means no explicit sort", () => {
    expect(parseTopologySort({ sortBy: "name", sortDir: "up" } as unknown as SortState)).toBeNull();
  });

  it("null means no explicit sort", () => {
    expect(parseTopologySort(null)).toBeNull();
  });
});

describe("nextTopologySort: ascending, descending, then back to the default", () => {
  it("a new field starts ascending", () => {
    expect(nextTopologySort(null, "name")).toEqual({ sortBy: "name", sortDir: "asc" });
    expect(nextTopologySort({ sortBy: "created_at", sortDir: "desc" }, "name")).toEqual({
      sortBy: "name",
      sortDir: "asc",
    });
  });

  it("the default order (no explicit sort) is not the same field: Updated starts ascending", () => {
    expect(nextTopologySort(null, "updated_at")).toEqual({
      sortBy: "updated_at",
      sortDir: "asc",
    });
  });

  it("second click descends, third clears", () => {
    expect(nextTopologySort({ sortBy: "name", sortDir: "asc" }, "name")).toEqual({
      sortBy: "name",
      sortDir: "desc",
    });
    expect(nextTopologySort({ sortBy: "name", sortDir: "desc" }, "name")).toBeNull();
  });
});

describe("topologyListParams", () => {
  it("sends only skip and limit with no query", () => {
    expect(topologyListParams(0, 50)).toEqual({ skip: 0, limit: 50 });
    expect(topologyListParams(50, 25, {})).toEqual({ skip: 50, limit: 25 });
  });

  it("maps every set field to its backend name", () => {
    expect(
      topologyListParams(0, 50, {
        search: "lab",
        owner: "mine",
        sortBy: "owner_name",
        sortDir: "asc",
      }),
    ).toEqual({
      skip: 0,
      limit: 50,
      search: "lab",
      owner: "mine",
      sort_by: "owner_name",
      sort_dir: "asc",
    });
  });

  it("an empty search is not sent", () => {
    expect(topologyListParams(0, 50, { search: "" })).toEqual({ skip: 0, limit: 50 });
  });
});
