import { http, HttpResponse } from "msw";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { describe, it, expect } from "vitest";

import { server } from "../mocks/server";
import { ReservationCalendarPage } from "@/pages/ReservationCalendarPage";
import { getDayRange } from "@/utils/dateUtils";

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrap = (node: ReactNode) => (
    <QueryClientProvider client={client}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>
  );
  return render(wrap(<ReservationCalendarPage />));
}

function devicesOk() {
  return http.get("/api/inventory/devices", () =>
    HttpResponse.json({ items: [], total: 0, skip: 0, limit: 500 }),
  );
}

describe("ReservationCalendarPage", () => {
  it("shows a could-not-load state on a 503, never the empty-range message (issue #1000)", async () => {
    const detail =
      "Could not verify device visibility; reservations were not returned. Retry the request.";
    server.use(
      devicesOk(),
      http.get("/api/reservations/calendar", () =>
        HttpResponse.json({ detail }, { status: 503 }),
      ),
    );
    renderPage();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(`Could not load the calendar. ${detail}`);
    expect(screen.queryByText("No reservations in this range")).not.toBeInTheDocument();
  });

  it("falls back to a generic reason when the error has no detail", async () => {
    server.use(
      devicesOk(),
      http.get("/api/reservations/calendar", () => HttpResponse.error()),
    );
    renderPage();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "Could not load the calendar. The reservations service did not answer.",
    );
  });

  it("still shows the empty-range message when the answer is an empty list", async () => {
    server.use(
      devicesOk(),
      http.get("/api/reservations/calendar", () => HttpResponse.json([])),
    );
    renderPage();
    expect(await screen.findByText("No reservations in this range")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("the day button switches the header to today's long date and asks for one day (issue #1148)", async () => {
    const ranges: Array<{ start: string | null; end: string | null }> = [];
    server.use(
      devicesOk(),
      http.get("/api/reservations/calendar", ({ request }) => {
        const params = new URL(request.url).searchParams;
        ranges.push({ start: params.get("range_start"), end: params.get("range_end") });
        return HttpResponse.json([]);
      }),
    );
    renderPage();
    await screen.findByText("No reservations in this range");

    const { start, end } = getDayRange(new Date());
    const dayLabel = start.toLocaleDateString(undefined, {
      weekday: "long",
      year: "numeric",
      month: "long",
      day: "numeric",
    });
    // The default view is the week, whose header is a range, not this label.
    expect(screen.queryByText(dayLabel)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "day" }));

    expect(await screen.findByText(dayLabel)).toBeInTheDocument();
    await waitFor(() =>
      expect(ranges).toContainEqual({ start: start.toISOString(), end: end.toISOString() }),
    );
  });
});
