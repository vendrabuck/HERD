import { vi, beforeEach, describe, it, expect } from "vitest";

// fetchServiceVersion only wraps apiClient.get; mock it directly so each
// test can hand back an arbitrary response body, including the malformed
// shapes a proxy or gateway can serve with a 200 status (issue #874).
const get = vi.fn();

vi.mock("@/api/client", () => ({
  default: { get: (...a: unknown[]) => get(...a) },
}));

import { fetchServiceVersion, InvalidVersionResponseError } from "@/api/about";

const SERVICE = { name: "inventory", label: "Inventory", path: "/inventory/version" };

describe("fetchServiceVersion", () => {
  beforeEach(() => {
    get.mockReset();
  });

  it("resolves with a well-formed ServiceVersion body", async () => {
    get.mockResolvedValue({
      data: { service: "inventory", version: "0.6.0", build: "dev", build_date: null },
    });
    await expect(fetchServiceVersion(SERVICE)).resolves.toEqual({
      service: "inventory",
      version: "0.6.0",
      build: "dev",
      build_date: null,
    });
  });

  it("resolves with a well-formed body carrying a string build_date", async () => {
    get.mockResolvedValue({
      data: {
        service: "inventory",
        version: "0.6.0",
        build: "v0.5.0-1-gabc",
        build_date: "2026-09-19T22:18:54Z",
      },
    });
    await expect(fetchServiceVersion(SERVICE)).resolves.toMatchObject({
      build_date: "2026-09-19T22:18:54Z",
    });
  });

  it("throws InvalidVersionResponseError for an HTML string body (proxy error page served with 200)", async () => {
    get.mockResolvedValue({ data: "<html><body>502 Bad Gateway</body></html>" });
    await expect(fetchServiceVersion(SERVICE)).rejects.toBeInstanceOf(InvalidVersionResponseError);
  });

  it("throws InvalidVersionResponseError for a body missing version", async () => {
    get.mockResolvedValue({ data: { service: "inventory", build: "dev", build_date: null } });
    await expect(fetchServiceVersion(SERVICE)).rejects.toBeInstanceOf(InvalidVersionResponseError);
  });

  it("throws InvalidVersionResponseError for a non-string version", async () => {
    get.mockResolvedValue({
      data: { service: "inventory", version: 123, build: "dev", build_date: null },
    });
    await expect(fetchServiceVersion(SERVICE)).rejects.toBeInstanceOf(InvalidVersionResponseError);
  });

  it("throws InvalidVersionResponseError when build_date is a number instead of a string or null", async () => {
    get.mockResolvedValue({
      data: { service: "inventory", version: "0.6.0", build: "dev", build_date: 12345 },
    });
    await expect(fetchServiceVersion(SERVICE)).rejects.toBeInstanceOf(InvalidVersionResponseError);
  });

  it("throws InvalidVersionResponseError for a null body", async () => {
    get.mockResolvedValue({ data: null });
    await expect(fetchServiceVersion(SERVICE)).rejects.toBeInstanceOf(InvalidVersionResponseError);
  });

  it("throws InvalidVersionResponseError for an empty object body", async () => {
    get.mockResolvedValue({ data: {} });
    await expect(fetchServiceVersion(SERVICE)).rejects.toBeInstanceOf(InvalidVersionResponseError);
  });

  it("names the offending service in the thrown error", async () => {
    get.mockResolvedValue({ data: "not json" });
    await expect(fetchServiceVersion(SERVICE)).rejects.toMatchObject({ service: "inventory" });
  });
});
