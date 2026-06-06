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

describe("getWeekRange", () => {
  it("starts on Sunday and spans 7 days", () => {
    // March 14, 2026 is a Saturday
    const date = new Date(2026, 2, 14);
    const { start, end } = getWeekRange(date);
    expect(start.getDay()).toBe(0); // Sunday
    expect(start.getHours()).toBe(0);
    const diffMs = end.getTime() - start.getTime();
    const diffDays = diffMs / (1000 * 60 * 60 * 24);
    expect(diffDays).toBe(7);
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
