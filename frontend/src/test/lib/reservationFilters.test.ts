import { describe, expect, it } from "vitest";
import {
  EMPTY_RESERVATION_FILTER,
  PURPOSE_CATEGORY_NONE,
  appendReservationListFilters,
  effectivePurposeCategory,
  parseSavedReservationFilter,
  reservationListFilters,
  serializeReservationFilter,
} from "@/lib/reservationFilters";

const NOW = "2030-06-01T12:00:00.000Z";

describe("parseSavedReservationFilter", () => {
  it("reads a valid saved object", () => {
    expect(
      parseSavedReservationFilter({
        search: "lab",
        status: "ACTIVE",
        purpose_category: "training",
        period: "past",
      }),
    ).toEqual({ search: "lab", status: "ACTIVE", purposeCategory: "training", period: "past" });
  });

  it.each([undefined, null, "x", 3, []])("falls back to defaults for %p", (raw) => {
    expect(parseSavedReservationFilter(raw)).toEqual(EMPTY_RESERVATION_FILTER);
  });

  it("drops a stale status or period and a non-string field", () => {
    expect(
      parseSavedReservationFilter({
        search: 42,
        status: "active",
        purpose_category: ["qa"],
        period: "tomorrow",
      }),
    ).toEqual(EMPTY_RESERVATION_FILTER);
    expect(parseSavedReservationFilter({ status: "BOGUS", period: "current" })).toEqual({
      ...EMPTY_RESERVATION_FILTER,
      period: "current",
    });
  });
});

describe("serializeReservationFilter", () => {
  it("omits every filter at All and always writes search", () => {
    expect(serializeReservationFilter(EMPTY_RESERVATION_FILTER)).toEqual({ search: "" });
  });

  it("round-trips through parse", () => {
    const state = {
      search: "x",
      status: "PENDING_PROVISION" as const,
      purposeCategory: PURPOSE_CATEGORY_NONE,
      period: "upcoming" as const,
    };
    expect(parseSavedReservationFilter(serializeReservationFilter(state))).toEqual(state);
  });
});

describe("effectivePurposeCategory", () => {
  it("keeps none without the list", () => {
    expect(effectivePurposeCategory(PURPOSE_CATEGORY_NONE, undefined)).toBe("none");
  });
  it("keeps a category the list has", () => {
    expect(effectivePurposeCategory("training", ["training"])).toBe("training");
  });
  it("drops a stale category and anything before the list loads", () => {
    expect(effectivePurposeCategory("legacy", ["training"])).toBe("");
    expect(effectivePurposeCategory("training", undefined)).toBe("");
    expect(effectivePurposeCategory("", ["training"])).toBe("");
  });
});

describe("reservationListFilters", () => {
  const base = EMPTY_RESERVATION_FILTER;

  it("sends nothing at All", () => {
    expect(reservationListFilters(base, NOW)).toEqual({});
  });

  it("trims the search and drops a blank one", () => {
    expect(reservationListFilters({ ...base, search: "  lab " }, NOW)).toEqual({ search: "lab" });
    expect(reservationListFilters({ ...base, search: "   " }, NOW)).toEqual({});
  });

  it("maps each period onto the half-open window at the anchor", () => {
    expect(reservationListFilters({ ...base, period: "upcoming" }, NOW)).toEqual({
      starts_after: NOW,
    });
    expect(reservationListFilters({ ...base, period: "current" }, NOW)).toEqual({
      starts_before: NOW,
      ends_after: NOW,
    });
    expect(reservationListFilters({ ...base, period: "past" }, NOW)).toEqual({ ends_before: NOW });
  });

  it("composes status, category, and period", () => {
    expect(
      reservationListFilters(
        { search: "q", status: "FAILED", purposeCategory: "none", period: "past" },
        NOW,
      ),
    ).toEqual({ search: "q", status: ["FAILED"], purpose_category: "none", ends_before: NOW });
  });
});

describe("appendReservationListFilters", () => {
  it("repeats status and sets each other key once", () => {
    const params = new URLSearchParams();
    appendReservationListFilters(params, {
      search: "a b",
      status: ["ACTIVE", "PENDING"],
      purpose_category: "none",
      starts_after: NOW,
      ends_before: NOW,
    });
    expect(params.getAll("status")).toEqual(["ACTIVE", "PENDING"]);
    expect(params.toString()).toBe(
      "search=a+b&status=ACTIVE&status=PENDING&purpose_category=none" +
        "&starts_after=2030-06-01T12%3A00%3A00.000Z&ends_before=2030-06-01T12%3A00%3A00.000Z",
    );
  });
});
