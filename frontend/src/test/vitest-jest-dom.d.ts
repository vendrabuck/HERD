// Bridges @testing-library/jest-dom 7.0.1's vitest type augmentation to
// vitest 5's Assertion interface.
//
// jest-dom 7.0.1 (types/vitest.d.ts) augments vitest as
// `interface Assertion<T = any> extends TestingLibraryMatchers<any, T>`,
// a single type parameter. Vitest 5 inlined the `expect` package and its
// own `Assertion` is `Assertion<R = void, T = unknown>`, two type
// parameters. The single-parameter augmentation no longer merges with
// vitest's declaration, so every jest-dom matcher (toBeInTheDocument,
// toHaveTextContent, toBeDisabled, and the rest) disappears from the type
// system, even though the matchers are still registered and pass at
// runtime via expect.extend().
//
// This is tracked upstream at testing-library/jest-dom#738 (open,
// unresolved as of 2026-09-14). Delete this file once jest-dom ships a
// release whose vitest augmentation matches vitest 5's two-parameter
// Assertion shape.
import type { TestingLibraryMatchers } from "@testing-library/jest-dom/matchers";

declare module "vitest" {
  // eslint-disable-next-line @typescript-eslint/no-empty-object-type -- declaration merging requires an empty interface here
  interface Assertion<R = void, T = unknown> extends TestingLibraryMatchers<T, R> {}
  // eslint-disable-next-line @typescript-eslint/no-empty-object-type -- declaration merging requires an empty interface here
  interface AsymmetricMatchersContaining extends TestingLibraryMatchers<unknown, unknown> {}
}
