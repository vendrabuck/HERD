import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { describe, it, expect } from "vitest";
import postcss from "postcss";
import tailwind from "@tailwindcss/postcss";

/*
 * Build-output regression guard for issue #106.
 *
 * The topology/status color utilities only ever appear inside runtime-composed
 * className strings (object-map values such as TOPOLOGY_COLORS / STATUS_COLORS).
 * If Tailwind ever stops emitting them, device boxes render as plain white
 * boxes with no text. A jsdom render test (DeviceNode.test.tsx) cannot catch
 * that: jsdom never runs the Tailwind build, so it can only assert the
 * className is on the element, not that the corresponding CSS rule exists.
 *
 * This test runs the REAL Tailwind v4 compiler (the same @tailwindcss/postcss
 * plugin the production build uses) over src/index.css with the project root as
 * its base, so it reproduces the production pipeline end to end: the @source
 * inline(...) safelist plus automatic content detection over src/**. It then
 * asserts the color utilities are present in the emitted CSS.
 *
 * Honest scope note: because the plugin auto-scans src/** from the project
 * base, a class that is still written as a plain literal somewhere in the tree
 * would keep this test green even if the safelist were deleted. This guard
 * therefore protects the end-to-end property issue #106 cares about (the rule
 * must exist in the compiled CSS); it does not, on its own, prove the safelist
 * is the thing emitting it. The safelist's independent value (surviving a
 * refactor that hides every literal from the scanner) is validated separately
 * in the PR description with a controlled before/after build.
 */

const here = dirname(fileURLToPath(import.meta.url));
const frontendRoot = resolve(here, "../../..");
const indexCssPath = resolve(frontendRoot, "src/index.css");

async function compileIndexCss(): Promise<string> {
  const css = readFileSync(indexCssPath, "utf8");
  const result = await postcss([tailwind({ base: frontendRoot })]).process(css, {
    from: indexCssPath,
  });
  return result.css;
}

function isClassNameChar(ch: string): boolean {
  return /[A-Za-z0-9-]/.test(ch);
}

function ruleExists(css: string, className: string): boolean {
  // Detect a standalone utility rule like `.bg-blue-100` while avoiding partial
  // matches such as bg-blue-1000. We scan for the literal `.<className>` and
  // require the next character not to continue the class name. This uses plain
  // string search (no regex built from interpolated input), so the class names
  // (which contain only letters, digits, and hyphens) need no escaping.
  const needle = `.${className}`;
  let from = css.indexOf(needle);
  while (from !== -1) {
    const next = css[from + needle.length];
    if (next === undefined || !isClassNameChar(next)) {
      return true;
    }
    from = css.indexOf(needle, from + 1);
  }
  return false;
}

describe("Tailwind color safelist (issue #106 build-output guard)", () => {
  const required = [
    // PHYSICAL (blue) and CLOUD (purple) device-box colors, the direct #106 set.
    "bg-blue-100",
    "border-blue-400",
    "text-blue-900",
    "bg-purple-100",
    "border-purple-400",
    "text-purple-900",
    // Badge and status-map tints composed at runtime elsewhere.
    "bg-blue-200",
    "text-blue-800",
    "bg-purple-200",
    "text-purple-800",
    "bg-green-100",
    "text-green-800",
    "bg-yellow-100",
    "bg-red-100",
    "text-gray-600",
  ];

  it("emits every runtime-composed color utility into the compiled CSS", async () => {
    const css = await compileIndexCss();
    const missing = required.filter((cls) => !ruleExists(css, cls));
    expect(missing).toEqual([]);
  });
});
