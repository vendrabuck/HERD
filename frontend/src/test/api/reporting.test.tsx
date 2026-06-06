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
});
