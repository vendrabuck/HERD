import { http, HttpResponse } from "msw";
import { describe, it, expect } from "vitest";

import { server } from "../mocks/server";
import { getPreferences, patchPreferences, resetPreferences } from "@/api/userProfile";

const PREFS = {
  user_id: "00000000-0000-0000-0000-000000000001",
  saved_filters: { devices: { status: "AVAILABLE" } },
  page_sizes: { devices: 25 },
  extras: {},
  updated_at: "2026-04-20T00:00:00+00:00",
};

describe("userProfile api", () => {
  it("getPreferences GETs /user-profile/preferences", async () => {
    server.use(
      http.get("/api/user-profile/preferences", () => HttpResponse.json(PREFS)),
    );
    const result = await getPreferences();
    expect(result.page_sizes.devices).toBe(25);
  });

  it("patchPreferences sends only the patched fields", async () => {
    let received: unknown;
    server.use(
      http.patch("/api/user-profile/preferences", async ({ request }) => {
        received = await request.json();
        return HttpResponse.json({ ...PREFS, page_sizes: { devices: 50 } });
      }),
    );
    const result = await patchPreferences({ page_sizes: { devices: 50 } });
    expect(received).toEqual({ page_sizes: { devices: 50 } });
    expect(result.page_sizes.devices).toBe(50);
  });

  it("resetPreferences DELETEs the resource", async () => {
    let called = false;
    server.use(
      http.delete("/api/user-profile/preferences", () => {
        called = true;
        return HttpResponse.json({
          ...PREFS,
          saved_filters: {},
          page_sizes: {},
          extras: {},
        });
      }),
    );
    const result = await resetPreferences();
    expect(called).toBe(true);
    expect(result.saved_filters).toEqual({});
  });

  it("getPreferences propagates HTTP errors", async () => {
    server.use(
      http.get("/api/user-profile/preferences", () =>
        HttpResponse.json({ detail: "unauthenticated" }, { status: 401 }),
      ),
    );
    await expect(getPreferences()).rejects.toThrow();
  });
});
