/**
 * Single source of truth for the frontend's own version/build/build-date
 * (issue #846), wrapping the compile-time constants injected by
 * vite.config.ts's `define` block (declared as globals in vite-env.d.ts and
 * eslint.config.js). No component should reference __APP_VERSION__,
 * __APP_BUILD__, or __APP_BUILD_DATE__ directly; import from here instead.
 */

/** e.g. "0.6.0-dev" (frontend/package.json's own version field). */
export const APP_VERSION: string = __APP_VERSION__;

/** e.g. "v0.5.0-16-gb29c8812", or "dev" when no build args were passed. */
export const APP_BUILD: string = __APP_BUILD__;

/** ISO 8601 UTC, or null when unset (unbuilt-through-the-Makefile case). */
export const APP_BUILD_DATE: string | null = __APP_BUILD_DATE__ ? __APP_BUILD_DATE__ : null;

interface ParsedVersion {
  release: string;
  dev: boolean;
}

/**
 * Parses a version string in either the backend's PEP 440 spelling
 * ("0.6.0.dev0") or the frontend's semver spelling ("0.6.0-dev") of "still
 * in development on this release number", or a plain final release such as
 * "0.6.0". Returns null for any other shape.
 */
function parseVersion(version: string): ParsedVersion | null {
  const pep440Dev = /^(\d+\.\d+\.\d+)\.dev\d*$/.exec(version);
  if (pep440Dev) {
    return { release: pep440Dev[1], dev: true };
  }
  const semverDev = /^(\d+\.\d+\.\d+)-dev$/.exec(version);
  if (semverDev) {
    return { release: semverDev[1], dev: true };
  }
  const final = /^(\d+\.\d+\.\d+)$/.exec(version);
  if (final) {
    return { release: final[1], dev: false };
  }
  return null;
}

/**
 * True when two version strings name the same release, tolerating the PEP
 * 440 (backend, "0.6.0.dev0") versus semver (frontend, "0.6.0-dev") spelling
 * of the same pre-release. "0.6.0" and "0.6.0.dev0" are NOT the same (one is
 * a tagged release, the other still in development on it); "0.5.0" and
 * "0.6.0" are not (different release numbers). An unparseable string never
 * matches anything, including an identical unparseable string on the other
 * side: the About page fails closed and flags it rather than silently
 * assuming a match.
 */
export function sameRelease(backendVersion: string, frontendVersion: string): boolean {
  const backend = parseVersion(backendVersion);
  const frontend = parseVersion(frontendVersion);
  if (!backend || !frontend) return false;
  return backend.release === frontend.release && backend.dev === frontend.dev;
}

/**
 * True when two build strings are worth flagging as skew. A "dev" build on
 * either side is the normal shape of a dev-mounted stack mixing a real host
 * build string with a service that got no build args, so it is never
 * flagged; two different real build strings are.
 */
export function buildsDiffer(a: string, b: string): boolean {
  if (a === "dev" || b === "dev") return false;
  return a !== b;
}
