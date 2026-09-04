import { http, HttpResponse } from "msw";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";

const { toastSuccess, toastError } = vi.hoisted(() => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}));
vi.mock("react-hot-toast", () => ({
  default: { success: toastSuccess, error: toastError },
}));

import { server } from "../mocks/server";
import {
  useReservations,
  usePaginatedReservations,
  useCalendarReservations,
  useCreateReservation,
  useCancelReservation,
  useReleaseReservation,
  usePurposeCategories,
  useSetPurposeCategory,
} from "@/api/reservations";
import { purposeCategoryLabel } from "@/lib/purposeCategories";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const RESV = {
  id: "11111111-1111-1111-1111-111111111111",
  user_id: "22222222-2222-2222-2222-222222222222",
  start_time: "2026-05-01T00:00:00+00:00",
  end_time: "2026-05-02T00:00:00+00:00",
  status: "CONFIRMED",
  device_ids: ["33333333-3333-3333-3333-333333333333"],
};

describe("reservations api hooks", () => {
  it("useReservations unwraps paginated items", async () => {
    server.use(
      http.get("/api/reservations/", () =>
        HttpResponse.json({ items: [RESV], total: 1, skip: 0, limit: 500 }),
      ),
    );
    const { result } = renderHook(() => useReservations(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual([RESV]);
  });

  it("usePaginatedReservations forwards skip/limit", async () => {
    let capturedUrl = "";
    server.use(
      http.get("/api/reservations/", ({ request }) => {
        capturedUrl = request.url;
        return HttpResponse.json({ items: [], total: 0, skip: 40, limit: 20 });
      }),
    );
    const { result } = renderHook(() => usePaginatedReservations(40, 20), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(capturedUrl).toMatch(/skip=40/);
    expect(capturedUrl).toMatch(/limit=20/);
  });

  it("useCalendarReservations serializes multi-status params", async () => {
    let capturedUrl = "";
    server.use(
      http.get("/api/reservations/calendar", ({ request }) => {
        capturedUrl = request.url;
        return HttpResponse.json([]);
      }),
    );
    const { result } = renderHook(
      () =>
        useCalendarReservations({
          range_start: "2026-05-01T00:00:00Z",
          range_end: "2026-05-08T00:00:00Z",
          status: ["PENDING", "ACTIVE"],
          device_id: "33333333-3333-3333-3333-333333333333",
        }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(capturedUrl).toMatch(/status=PENDING/);
    expect(capturedUrl).toMatch(/status=ACTIVE/);
    expect(capturedUrl).toMatch(/device_id=33333333-3333-3333-3333-333333333333/);
  });

  it("useCreateReservation POSTs to /reservations/", async () => {
    let captured: unknown;
    server.use(
      http.post("/api/reservations/", async ({ request }) => {
        captured = await request.json();
        return HttpResponse.json(RESV, { status: 201 });
      }),
    );
    const { result } = renderHook(() => useCreateReservation(), { wrapper });
    result.current.mutate({
      start_time: RESV.start_time,
      end_time: RESV.end_time,
      device_ids: RESV.device_ids,
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(captured).toMatchObject({ device_ids: RESV.device_ids });
  });

  it("useCreateReservation surfaces 409 conflict errors", async () => {
    server.use(
      http.post("/api/reservations/", () =>
        HttpResponse.json({ detail: "conflicts with reservation X" }, { status: 409 }),
      ),
    );
    const { result } = renderHook(() => useCreateReservation(), { wrapper });
    result.current.mutate({
      start_time: RESV.start_time,
      end_time: RESV.end_time,
      device_ids: RESV.device_ids,
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
  });
});

describe("cancel / release reservation hooks", () => {
  beforeEach(() => {
    toastSuccess.mockClear();
    toastError.mockClear();
  });

  it("useCancelReservation DELETEs and toasts success", async () => {
    let method = "";
    server.use(
      http.delete("/api/reservations/:id", ({ request }) => {
        method = request.method;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { result } = renderHook(() => useCancelReservation(), { wrapper });
    result.current.mutate(RESV.id);
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(method).toBe("DELETE");
    expect(toastSuccess).toHaveBeenCalledWith("Reservation cancelled");
    expect(toastError).not.toHaveBeenCalled();
  });

  it("useCancelReservation toasts the server detail on error", async () => {
    server.use(
      http.delete("/api/reservations/:id", () =>
        HttpResponse.json({ detail: "not your reservation" }, { status: 403 }),
      ),
    );
    const { result } = renderHook(() => useCancelReservation(), { wrapper });
    result.current.mutate(RESV.id);
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(toastError).toHaveBeenCalledWith("not your reservation");
    expect(toastSuccess).not.toHaveBeenCalled();
  });

  it("useReleaseReservation PUTs and toasts success", async () => {
    server.use(
      http.put("/api/reservations/:id/release", () => HttpResponse.json(RESV)),
    );
    const { result } = renderHook(() => useReleaseReservation(), { wrapper });
    result.current.mutate(RESV.id);
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(toastSuccess).toHaveBeenCalledWith("Reservation released");
  });

  it("useReleaseReservation falls back to a generic error message", async () => {
    server.use(
      http.put("/api/reservations/:id/release", () =>
        HttpResponse.json({}, { status: 500 }),
      ),
    );
    const { result } = renderHook(() => useReleaseReservation(), { wrapper });
    result.current.mutate(RESV.id);
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(toastError).toHaveBeenCalledWith("Failed to release reservation");
  });
});

// --- Lab purpose classification (issue #646 phase 1) ------------------------

describe("purpose category hooks", () => {
  it("usePurposeCategories fetches the server-configured category list", async () => {
    const categories = ["qa_regression", "other"];
    server.use(
      http.get("/api/reservations/purpose-categories", () =>
        HttpResponse.json({ categories }),
      ),
    );
    const { result } = renderHook(() => usePurposeCategories(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual({ categories });
  });

  it("useSetPurposeCategory PATCHes the reservation with the chosen category", async () => {
    let captured: unknown;
    let method = "";
    server.use(
      http.patch("/api/reservations/:id/purpose-category", async ({ request }) => {
        method = request.method;
        captured = await request.json();
        return HttpResponse.json({ ...RESV, purpose_category: "training" });
      }),
    );
    const { result } = renderHook(() => useSetPurposeCategory(), { wrapper });
    result.current.mutate({ id: RESV.id, purposeCategory: "training" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(method).toBe("PATCH");
    expect(captured).toEqual({ purpose_category: "training" });
  });

  it("useSetPurposeCategory sends null to clear the category", async () => {
    let captured: unknown;
    server.use(
      http.patch("/api/reservations/:id/purpose-category", async ({ request }) => {
        captured = await request.json();
        return HttpResponse.json({ ...RESV, purpose_category: null });
      }),
    );
    const { result } = renderHook(() => useSetPurposeCategory(), { wrapper });
    result.current.mutate({ id: RESV.id, purposeCategory: null });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(captured).toEqual({ purpose_category: null });
  });
});

describe("purposeCategoryLabel", () => {
  it("maps each of the seven shipped defaults to its human label", () => {
    expect(purposeCategoryLabel("qa_regression")).toBe("QA and regression");
    expect(purposeCategoryLabel("support_case_replication")).toBe(
      "Support case replication",
    );
    expect(purposeCategoryLabel("feature_development")).toBe("Feature development");
    expect(purposeCategoryLabel("customer_demo_poc")).toBe("Customer demo or POC");
    expect(purposeCategoryLabel("training")).toBe("Training");
    expect(purposeCategoryLabel("performance_benchmark")).toBe("Performance benchmark");
    expect(purposeCategoryLabel("other")).toBe("Other");
  });

  it("renders null and the literal 'unclassified' bucket key the same way", () => {
    expect(purposeCategoryLabel(null)).toBe("Unclassified");
    expect(purposeCategoryLabel(undefined)).toBe("Unclassified");
    expect(purposeCategoryLabel("unclassified")).toBe("Unclassified");
  });

  it("humanizes a category outside the shipped defaults from its snake_case value", () => {
    // Configurable via the backend's PURPOSE_CATEGORIES override; never hardcoded
    // here, so an admin-added category still renders as a readable label.
    expect(purposeCategoryLabel("network_debug")).toBe("Network Debug");
    expect(purposeCategoryLabel("chaos_testing")).toBe("Chaos Testing");
  });
});
