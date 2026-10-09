import { getDayRange, getWeekRange, getMonthRange } from "@/utils/dateUtils";

describe("getDayRange", () => {
  it("returns midnight to next midnight", () => {
    const date = new Date(2026, 2, 14, 15, 30, 45); // March 14, 3:30pm
    const { start, end } = getDayRange(date);
    expect(start.getHours()).toBe(0);
    expect(start.getMinutes()).toBe(0);
    expect(start.getSeconds()).toBe(0);
    expect(start.getDate()).toBe(14);
    expect(end.getDate()).toBe(15);
    expect(end.getHours()).toBe(0);
  });
});

// Issue #1142: these assert LOCAL calendar fields, never elapsed
// milliseconds, so they hold in every time zone. A week that contains a DST
// change is 167 or 169 hours long, so "end minus start equals 7 * 24 hours"
// failed on any US-time-zone machine for the week below, while CI (UTC)
// stayed green.
function localFields(d: Date): [number, number, number, number, number, number] {
  return [
    d.getFullYear(),
    d.getMonth(),
    d.getDate(),
    d.getHours(),
    d.getMinutes(),
    d.getSeconds(),
  ];
}

describe("getWeekRange", () => {
  it("starts at local midnight on the Sunday of the same week and ends 7 calendar days later", () => {
    // March 14, 2026 is a Saturday. Its week (March 8 to 15) also contains the
    // United States DST start (Sunday March 8).
    const date = new Date(2026, 2, 14, 15, 30, 45);
    const { start, end } = getWeekRange(date);
    expect(start.getDay()).toBe(0); // Sunday
    expect(localFields(start)).toEqual([2026, 2, 8, 0, 0, 0]);
    expect(localFields(end)).toEqual([2026, 2, 15, 0, 0, 0]);
    expect(end.getDay()).toBe(0);
  });

  it("a Sunday is the start of its own week", () => {
    const date = new Date(2026, 2, 15, 23, 59, 59); // Sunday March 15, 2026
    const { start, end } = getWeekRange(date);
    expect(localFields(start)).toEqual([2026, 2, 15, 0, 0, 0]);
    expect(localFields(end)).toEqual([2026, 2, 22, 0, 0, 0]);
  });

  it("crosses a month and year boundary", () => {
    const date = new Date(2026, 0, 2); // Friday January 2, 2026
    const { start, end } = getWeekRange(date);
    expect(localFields(start)).toEqual([2025, 11, 28, 0, 0, 0]);
    expect(localFields(end)).toEqual([2026, 0, 4, 0, 0, 0]);
  });
});

describe("getMonthRange", () => {
  it("handles a normal month (January)", () => {
    const date = new Date(2026, 0, 15); // January 15
    const { start, end } = getMonthRange(date);
    expect(start.getMonth()).toBe(0);
    expect(start.getDate()).toBe(1);
    expect(end.getMonth()).toBe(1); // February
    expect(end.getDate()).toBe(1);
  });

  it("handles December (year rollover)", () => {
    const date = new Date(2026, 11, 10); // December 10
    const { start, end } = getMonthRange(date);
    expect(start.getMonth()).toBe(11);
    expect(start.getDate()).toBe(1);
    expect(end.getFullYear()).toBe(2027);
    expect(end.getMonth()).toBe(0); // January
    expect(end.getDate()).toBe(1);
  });

  it("handles February in a leap year", () => {
    const date = new Date(2028, 1, 10); // Feb 2028 (leap year)
    const { start, end } = getMonthRange(date);
    expect(start.getMonth()).toBe(1);
    expect(start.getDate()).toBe(1);
    expect(end.getMonth()).toBe(2); // March
    expect(end.getDate()).toBe(1);
  });
});
