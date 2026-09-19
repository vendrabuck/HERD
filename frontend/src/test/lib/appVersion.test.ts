import { APP_VERSION, APP_BUILD, APP_BUILD_DATE, sameRelease, buildsDiffer } from "@/lib/appVersion";
import pkg from "../../../package.json";

describe("appVersion constants", () => {
  it("APP_VERSION is read from package.json's version field, not a hardcoded literal", () => {
    // vite.config.ts's `define` block reads the same file at config-eval
    // time and shares its test block with vitest, so this proves the wiring
    // end to end rather than just re-asserting a copy of the string.
    expect(APP_VERSION).toBe(pkg.version);
  });

  it("APP_BUILD falls back to 'dev' when VITE_HERD_BUILD is unset (default vitest env)", () => {
    // vitest runs with no VITE_HERD_BUILD in the environment by default, so
    // this pins the fallback the issue requires without needing a separate
    // process spawn: vite.config.ts's define computes __APP_BUILD__ the same
    // way for `vite build` and for vitest.
    expect(APP_BUILD).toBe("dev");
  });

  it("APP_BUILD_DATE is null when VITE_HERD_BUILD_DATE is unset (default vitest env)", () => {
    expect(APP_BUILD_DATE).toBeNull();
  });
});

describe("sameRelease", () => {
  it("treats matching PEP 440 and semver dev spellings of the same release as the same", () => {
    expect(sameRelease("0.6.0.dev0", "0.6.0-dev")).toBe(true);
  });

  it("treats two equal final releases as the same", () => {
    expect(sameRelease("0.5.0", "0.5.0")).toBe(true);
  });

  it("treats two dev builds with a different dev counter as the same release", () => {
    expect(sameRelease("0.6.0.dev3", "0.6.0-dev")).toBe(true);
  });

  it("does not match a tagged release against the dev build of the same number", () => {
    expect(sameRelease("0.6.0", "0.6.0.dev0")).toBe(false);
    expect(sameRelease("0.6.0.dev0", "0.6.0")).toBe(false);
  });

  it("does not match different release numbers", () => {
    expect(sameRelease("0.5.0", "0.6.0")).toBe(false);
  });

  it("does not match different release numbers even when both are dev", () => {
    expect(sameRelease("0.5.0.dev0", "0.6.0-dev")).toBe(false);
  });

  it("fails closed on an unparseable version string", () => {
    expect(sameRelease("not-a-version", "0.6.0-dev")).toBe(false);
    expect(sameRelease("not-a-version", "not-a-version")).toBe(false);
  });
});

describe("buildsDiffer", () => {
  it("is false for two identical build strings", () => {
    expect(buildsDiffer("v0.5.0-16-gb29c8812", "v0.5.0-16-gb29c8812")).toBe(false);
  });

  it("is true for two different real build strings", () => {
    expect(buildsDiffer("v0.5.0-16-gb29c8812", "v0.5.0-17-gdeadbeef")).toBe(true);
  });

  it("never flags a 'dev' build on either side, even against a different real build", () => {
    expect(buildsDiffer("dev", "v0.5.0-16-gb29c8812")).toBe(false);
    expect(buildsDiffer("v0.5.0-16-gb29c8812", "dev")).toBe(false);
    expect(buildsDiffer("dev", "dev")).toBe(false);
  });
});
