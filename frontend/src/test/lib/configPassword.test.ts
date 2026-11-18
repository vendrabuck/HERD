import { describe, expect, it } from "vitest";
import {
  CONFIG_PASSWORD_MAX_CHARS,
  CONFIG_PASSWORD_MIN_CHARS,
  codePointLength,
  configPasswordLengthOk,
} from "@/lib/configPassword";
import {
  CONFIG_CHANGE_PASSWORD_FALLBACK,
  configChangePasswordErrorText,
} from "@/lib/errors";

// A key emoji, outside the Basic Multilingual Plane: two UTF-16 units, one code point.
const KEY = "\u{1F511}";

describe("codePointLength (issue #1452)", () => {
  it("counts code points as Python's len does", () => {
    expect(codePointLength("")).toBe(0);
    expect(codePointLength("a")).toBe(1);
    expect(codePointLength(KEY)).toBe(1);
    expect(KEY.length).toBe(2);
    // A combining mark is its own code point; a lone surrogate is one.
    expect(codePointLength("é")).toBe(2);
    expect(codePointLength("\uD800")).toBe(1);
    expect(codePointLength("ab" + KEY + "\uDC00")).toBe(4);
  });
});

describe("configPasswordLengthOk (issue #1452, OPS-CONFIG-8)", () => {
  it("is the server's 8 to 32 character bound", () => {
    expect([CONFIG_PASSWORD_MIN_CHARS, CONFIG_PASSWORD_MAX_CHARS]).toEqual([8, 32]);
  });

  it.each([
    ["7 ASCII", "a".repeat(7), false],
    ["8 ASCII", "a".repeat(8), true],
    ["32 ASCII", "a".repeat(32), true],
    ["33 ASCII", "a".repeat(33), false],
    ["7 emoji", KEY.repeat(7), false],
    ["8 emoji", KEY.repeat(8), true],
    ["32 emoji", KEY.repeat(32), true],
    ["33 emoji", KEY.repeat(33), false],
    ["5 ASCII and 2 emoji (7, 9 units)", "a".repeat(5) + KEY.repeat(2), false],
    ["16 ASCII and 16 emoji (32, 48 units)", "a".repeat(16) + KEY.repeat(16), true],
    ["17 ASCII and 16 emoji (33)", "a".repeat(17) + KEY.repeat(16), false],
    ["4 letters with combining marks (8)", "é".repeat(4), true],
  ])("%s", (_name, text, ok) => {
    expect(configPasswordLengthOk(text)).toBe(ok);
  });
});

describe("configChangePasswordErrorText (issue #1452)", () => {
  function axiosLike(status: number, detail: unknown) {
    return { response: { status, data: { detail } } };
  }

  it.each([
    "Password must be at least 8 characters",
    "Password must be at most 32 characters",
    "Password must not contain a NUL character",
    "Password must be valid UTF-8 text",
  ])("shows the server's refusal %s", (detail) => {
    expect(configChangePasswordErrorText(axiosLike(422, detail))).toBe(detail);
  });

  it.each([
    [
      "a 422 validation list",
      axiosLike(422, [{ type: "string_type", loc: ["body", "new_password"], msg: "x" }]),
    ],
    ["a blank 422 string", axiosLike(422, "  ")],
    [
      "the 503 for an unusable store",
      axiosLike(503, "The config password store is unusable; the config service log names the cause"),
    ],
    ["a 401", axiosLike(401, "Config session expired")],
    ["a 500", axiosLike(500, "Internal Server Error")],
    ["no answer", new Error("Network Error")],
    ["null", null],
  ])("falls back for %s", (_name, err) => {
    expect(configChangePasswordErrorText(err)).toBe(CONFIG_CHANGE_PASSWORD_FALLBACK);
    expect(CONFIG_CHANGE_PASSWORD_FALLBACK).toBe("Failed to change password");
  });
});
