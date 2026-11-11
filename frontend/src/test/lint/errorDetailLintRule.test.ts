// @vitest-environment node
//
// Issue #1314: the no-restricted-syntax block in eslint.config.js keeps raw
// error details out of the UI. This runs the repo's real ESLint config over
// fixture snippets, linted as if they lived at src/pages/ (where the block
// applies), and pins both directions: every form the rule must refuse, and
// every legitimate form it must allow. The forms it is documented NOT to
// catch are pinned too, so a change that starts catching one shows up here
// and the CONVENTIONS text gets updated with it.
import { ESLint } from "eslint";
import { beforeAll, describe, expect, it } from "vitest";

// A variable base keeps Vite from rewriting `new URL(..., import.meta.url)`
// into an asset import (the same trick as manualSearch.test.ts).
const HERE = import.meta.url;
const FRONTEND_ROOT = decodeURIComponent(new URL("../../../", HERE).pathname);

let eslint: ESLint;

beforeAll(() => {
  eslint = new ESLint({ cwd: FRONTEND_ROOT });
});

async function restrictedHits(code: string, file = "src/pages/LintFixture.tsx"): Promise<number> {
  const [result] = await eslint.lintText(code, { filePath: FRONTEND_ROOT + file });
  return result.messages.filter((m) => m.ruleId === "no-restricted-syntax").length;
}

const REFUSED: [string, string][] = [
  [
    "the original string cast",
    "const d = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;",
  ],
  ["a cast to string or undefined", "const d = err as { detail?: string | undefined };"],
  [
    "a cast of the detail's own fields",
    "const d = err as { response?: { data?: { detail?: { errors?: string[] } } } };",
  ],
  ["a type argument", "if (isAxiosError<{ detail: string }>(err)) { void err; }"],
  ["a generic helper's type argument", "const d = pick<{ detail: string }>(err);"],
  ["a satisfies clause", "const d = body satisfies { detail: string };"],
  ["a named alias then .data?.detail", "const r = err as ApiError; const d = r.response?.data?.detail;"],
  ["an any cast then .data.detail", "const d = (err as any).response.data.detail;"],
  ["an optional .data?.detail read", "const d = err.response?.data?.detail;"],
  ["a computed .data[\"detail\"] read", 'const d = err.response.data["detail"];'],
  ["destructuring from .data", "const { detail } = err.response.data;"],
  ["destructuring from ?.data", "const { detail } = err.response?.data;"],
  ["destructuring with a default", 'const { detail = "" } = err.response.data;'],
  ["nested destructuring through data", "const { data: { detail } } = err.response;"],
];

const ALLOWED: [string, string][] = [
  ["the helper", 'const t = errorDetail(err, "Failed");'],
  ["the raw accessor", "const d = responseDetail(err);"],
  ["a structured narrower", "const d = structuredDetail(err, 409, (x) => x.error === 'e');"],
  ["an interface field", "interface Problem { detail: string | null }"],
  ["a class field", "class E extends Error { detail?: string; }"],
  ["a different object's .detail", "const d = problem.detail;"],
  ["a query key helper", "const k = deviceKeys.detail(id);"],
  ["a query key string", 'const k = ["devices", id, "detail", versionId];'],
  ["a cast whose detail is unknown", "const d = err as { response?: { data?: { detail?: unknown } } };"],
  ["an object literal cast", 'const o = { detail: "x" } as Payload;'],
  ["destructuring detail from something else", "const { detail } = props;"],
];

// Documented limits: syntax the rule cannot tie back to a response body.
const NOT_CAUGHT: [string, string][] = [
  ["an intermediate variable", "const body = err.response?.data; const d = body.detail;"],
  ["destructured data then .detail", "const { data } = err.response; const d = data?.detail;"],
  ["a fetch body", "const d = (await resp.json())?.detail;"],
  ["an assignment destructure", "let detail; ({ detail } = err.response.data);"],
];

describe("errorDetail lint rule (issue #1314)", () => {
  it.each(REFUSED)("refuses %s", async (_label, code) => {
    expect(await restrictedHits(code)).toBeGreaterThan(0);
  });

  it.each(ALLOWED)("allows %s", async (_label, code) => {
    expect(await restrictedHits(code)).toBe(0);
  });

  it.each(NOT_CAUGHT)("does not catch %s (a documented limit)", async (_label, code) => {
    expect(await restrictedHits(code)).toBe(0);
  });

  it("exempts lib/errors.ts and the tests", async () => {
    const cast = "const d = err.response?.data?.detail;";
    expect(await restrictedHits(cast, "src/lib/errors.ts")).toBe(0);
    expect(await restrictedHits(cast, "src/test/x.test.ts")).toBe(0);
  });
});
