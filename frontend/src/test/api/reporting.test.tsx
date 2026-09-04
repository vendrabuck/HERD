import { http, HttpResponse } from "msw";
import { describe, it, expect } from "vitest";

import { server } from "../mocks/server";
import { downloadUtilizationCsv } from "@/api/reporting";

describe("downloadUtilizationCsv", () => {
  it("fetches the CSV text and parses the filename from content-disposition", async () => {
    server.use(
      http.get("/api/reservations/reports/utilization.csv", ({ request }) => {
        const url = new URL(request.url);
        expect(url.searchParams.get("section")).toBe("user");
        return new HttpResponse(
          "user_id,owner_name,hours,reservation_count\nabc,alice,5.0000,2\n",
          {
            headers: {
              "Content-Type": "text/csv; charset=utf-8",
              "Content-Disposition":
                'attachment; filename="utilization-user-2026-04-01-to-2026-04-19.csv"',
            },
          },
        );
      }),
    );

    const { filename, body } = await downloadUtilizationCsv(
      { start: "2026-04-01T00:00:00Z", end: "2026-04-19T00:00:00Z" },
      "user",
    );
    expect(filename).toBe("utilization-user-2026-04-01-to-2026-04-19.csv");
    expect(body).toContain("user_id,owner_name,hours,reservation_count");
    expect(body).toContain("alice");
  });

  it("falls back to a default filename when content-disposition is missing", async () => {
    server.use(
      http.get("/api/reservations/reports/utilization.csv", () =>
        new HttpResponse("device_id,hours,reservation_count\n", {
          headers: { "Content-Type": "text/csv; charset=utf-8" },
        }),
      ),
    );

    const { filename } = await downloadUtilizationCsv(
      { start: "2026-04-01T00:00:00Z", end: "2026-04-19T00:00:00Z" },
      "device",
    );
    expect(filename).toBe("utilization-device.csv");
  });

  // The purpose sections (issue #646 phases 1-2, wired to the frontend by
  // issue #696): standalone CSV sections, not a column on user/device, so
  // each gets its own request carrying its own section value on the wire.
  it.each([
    ["purpose", "purpose_category,reservations,device_hours\n"],
    ["user_purpose", "user_id,purpose_category,reservations,device_hours\n"],
    ["device_purpose", "device_id,purpose_category,reservations,device_hours\n"],
    ["purpose_suggested", "purpose_category,reservations,device_hours\n"],
  ] as const)("passes section=%s through to the request", async (section, body) => {
    server.use(
      http.get("/api/reservations/reports/utilization.csv", ({ request }) => {
        const url = new URL(request.url);
        expect(url.searchParams.get("section")).toBe(section);
        return new HttpResponse(body, {
          headers: {
            "Content-Type": "text/csv; charset=utf-8",
            "Content-Disposition": `attachment; filename="utilization-${section}-2026-04-01-to-2026-04-19.csv"`,
          },
        });
      }),
    );

    const result = await downloadUtilizationCsv(
      { start: "2026-04-01T00:00:00Z", end: "2026-04-19T00:00:00Z" },
      section,
    );
    expect(result.filename).toBe(`utilization-${section}-2026-04-01-to-2026-04-19.csv`);
    expect(result.body).toBe(body);
  });
});
