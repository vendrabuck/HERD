import { describe, expect, it } from "vitest";
import { COPY_NAME_PREFIX, NAME_MAX_LENGTH, copyName } from "@/lib/copyName";

// A character outside the BMP: two UTF-16 units, one character to the server.
const WIDE = "\u{1D11E}";
const codePoints = (s: string) => Array.from(s).length;
// A unicode-mode regex reads a well-formed surrogate pair as one code point, so
// \p{Cs} (a surrogate) matches only half of a pair left on its own.
const hasLoneSurrogate = (s: string) => /\p{Cs}/u.test(s);

describe("copyName", () => {
  it("prefixes a name that fits whole", () => {
    expect(copyName("Edge Router")).toBe("Copy of Edge Router");
  });

  it.each([1, 246, 247, 248, 254, 255])(
    "a %i-character name gives a copy name within the 255-character column",
    (length) => {
      for (const char of ["t", WIDE, "é"]) {
        const name = char.repeat(length);
        const copy = copyName(name);
        expect(codePoints(copy)).toBeLessThanOrEqual(NAME_MAX_LENGTH);
        expect(copy.startsWith(COPY_NAME_PREFIX)).toBe(true);
        const kept = copy.slice(COPY_NAME_PREFIX.length);
        expect(name.startsWith(kept)).toBe(true);
        if (length + COPY_NAME_PREFIX.length <= NAME_MAX_LENGTH) {
          expect(copy).toBe(COPY_NAME_PREFIX + name);
        } else {
          expect(codePoints(copy)).toBe(NAME_MAX_LENGTH);
        }
      }
    },
  );

  it("detects a lone surrogate, and only a lone one", () => {
    expect(hasLoneSurrogate(WIDE)).toBe(false);
    expect(hasLoneSurrogate(WIDE.slice(0, 1))).toBe(true);
    expect(hasLoneSurrogate("a" + WIDE.slice(1))).toBe(true);
  });

  it("never splits a character outside the BMP", () => {
    const copy = copyName(WIDE.repeat(255));
    // A cut by UTF-16 units would end on the first half of a pair.
    expect(hasLoneSurrogate(copy)).toBe(false);
    expect(copy).toBe(COPY_NAME_PREFIX + WIDE.repeat(NAME_MAX_LENGTH - 8));
  });

  it("is deterministic", () => {
    const name = "x".repeat(300);
    expect(copyName(name)).toBe(copyName(name));
  });
});
