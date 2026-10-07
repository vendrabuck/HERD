import { http, HttpResponse } from "msw";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { describe, it, expect } from "vitest";

import { server } from "../mocks/server";
import { ReservationCalendarPage } from "@/pages/ReservationCalendarPage";

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
});
