import { csvSafeCell } from "@/lib/csvSafety";

const TRIGGER_CHARS = ["=", "+", "-", "@", "\t", "\r"];

describe("csvSafeCell", () => {
  it.each(TRIGGER_CHARS)("prefixes a single quote onto a value starting with %j", (trigger) => {
    const value = `${trigger}HYPERLINK("http://evil","click")`;
    expect(csvSafeCell(value)).toBe(`'${value}`);
  });

  it.each(TRIGGER_CHARS)(
    "prefixes a single quote when leading spaces precede %j (OWASP leading-whitespace decision)",
    (trigger) => {
      const value = `  ${trigger}1+1`;
      expect(csvSafeCell(value)).toBe(`'${value}`);
    },
  );

  it("leaves plain text unchanged", () => {
    expect(csvSafeCell("FW-3200")).toBe("FW-3200");
  });

  it("leaves an empty string unchanged", () => {
    expect(csvSafeCell("")).toBe("");
  });

  it("leaves an apostrophe-led value unchanged (not a trigger character)", () => {
    expect(csvSafeCell("'quoted")).toBe("'quoted");
  });
});
