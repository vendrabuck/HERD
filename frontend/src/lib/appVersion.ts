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

/**
 * A version's pre-release marker: "dev" (still in development on the
 * release, the dev counter itself is never compared), an rc with its number
 * (the number DOES matter: rc1 and rc2 are different pre-releases), or null
 * for a plain final release.
 */
type Prerelease = "dev" | { rc: number } | null;

interface ParsedVersion {
  release: string;
  pre: Prerelease;
}

/**
 * Parses a version string in either the backend's PEP 440 spelling or the
 * frontend's semver spelling of a release, its dev pre-release, or its
 * release-candidate pre-release:
 *
 * - final: "0.6.0" (both spellings share this shape)
 * - dev: PEP 440 "0.6.0.dev0", semver "0.6.0-dev" (the devN counter is
 *   parsed but never compared, so any devN counts as the same pre-release)
 * - rc: PEP 440 "0.6.0rc1", semver "0.6.0-rc.1" (the rc number IS compared)
 *
 * Anything else fails closed and returns null: post-releases such as
 * "0.6.0.post1", a semver rc missing its dot ("0.6.0-rc1"), and plain
 * garbage all count as unparseable rather than being guessed at.
 */
function parseVersion(version: string): ParsedVersion | null {
  const pep440Dev = /^(\d+\.\d+\.\d+)\.dev\d*$/.exec(version);
  if (pep440Dev) {
    return { release: pep440Dev[1], pre: "dev" };
  }
  const semverDev = /^(\d+\.\d+\.\d+)-dev$/.exec(version);
  if (semverDev) {
    return { release: semverDev[1], pre: "dev" };
  }
  const pep440Rc = /^(\d+\.\d+\.\d+)rc(\d+)$/.exec(version);
  if (pep440Rc) {
    return { release: pep440Rc[1], pre: { rc: Number(pep440Rc[2]) } };
  }
  const semverRc = /^(\d+\.\d+\.\d+)-rc\.(\d+)$/.exec(version);
  if (semverRc) {
    return { release: semverRc[1], pre: { rc: Number(semverRc[2]) } };
  }
  const final = /^(\d+\.\d+\.\d+)$/.exec(version);
  if (final) {
    return { release: final[1], pre: null };
  }
  return null;
}

/** True when two Prerelease values name the same pre-release state. */
function samePrerelease(a: Prerelease, b: Prerelease): boolean {
  if (a === "dev" || b === "dev") return a === b;
  if (a === null || b === null) return a === b;
  return a.rc === b.rc;
}

/**
 * True when two version strings name the same release, tolerating the PEP
 * 440 (backend) versus semver (frontend) spelling of the same pre-release,
 * dev or rc. "0.6.0" and "0.6.0.dev0" are NOT the same (one is a tagged
 * release, the other still in development on it); "0.6.0rc1" and
 * "0.6.0-rc.1" ARE the same; "0.6.0rc1" and "0.6.0-rc.2" are NOT (the rc
 * number matters); "0.6.0rc1" and "0.6.0-dev" are NOT (different pre-release
 * kinds); "0.5.0" and "0.6.0" are not (different release numbers). An
 * unparseable string never matches anything, including an identical
 * unparseable string on the other side: the About page fails closed and
 * flags it rather than silently assuming a match.
 */
export function sameRelease(backendVersion: string, frontendVersion: string): boolean {
  const backend = parseVersion(backendVersion);
  const frontend = parseVersion(frontendVersion);
  if (!backend || !frontend) return false;
  return backend.release === frontend.release && samePrerelease(backend.pre, frontend.pre);
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

/**
 * A build date for display: always UTC and labeled as such, e.g.
 * "2026-09-19 22:18 UTC". Not toLocaleString(): that renders in the viewer's
 * own locale and timezone with no zone shown, so the same image reads as a
 * different time in every browser, and none of them matches what
 * `make version` prints on the host (ISO 8601 UTC). Returns "-" for null and
 * the raw string for anything that does not parse as a date.
 */
export function formatBuildDate(iso: string | null): string {
  if (!iso) return "-";
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return `${parsed.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}
