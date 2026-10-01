import { http, HttpResponse } from "msw";
import { renderHook, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect, vi } from "vitest";

import { server } from "../mocks/server";
import { classifyPurposeNow, useClassifyPurpose } from "@/api/reservations";
import { purposeClassifyRefusal } from "@/lib/errors";

const ID = "11111111-1111-1111-1111-111111111111";
const URL = `/api/reservations/admin/purpose-review/${ID}/classify`;

describe("classifyPurposeNow", () => {
  it("POSTs to the classify route and returns the typed body", async () => {
    server.use(
      http.post(URL, () =>
        HttpResponse.json({ reservation_id: ID, outcome: "timeout", purpose_suggestion: null }),
      ),
    );
    await expect(classifyPurposeNow(ID)).resolves.toEqual({
      reservation_id: ID,
      outcome: "timeout",
      purpose_suggestion: null,
    });
  });

  it("rejects with an error the refusal narrower understands on 503 and 409", async () => {
    server.use(
      http.post(URL, () =>
        HttpResponse.json({ detail: { error: "purpose_classification_disabled" } }, { status: 503 }),
      ),
    );
    await expect(classifyPurposeNow(ID)).rejects.toSatisfy(
      (e) => purposeClassifyRefusal(e) === "purpose_classification_disabled",
    );
    server.use(
      http.post(URL, () =>
        HttpResponse.json({ detail: { error: "already_suggested" } }, { status: 409 }),
      ),
    );
    await expect(classifyPurposeNow(ID)).rejects.toSatisfy(
      (e) => purposeClassifyRefusal(e) === "already_suggested",
    );
  });
});

describe("useClassifyPurpose", () => {
  it("invalidates the reservation queries whether the call succeeds or fails", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const spy = vi.spyOn(client, "invalidateQueries");
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useClassifyPurpose(), { wrapper });

    server.use(
      http.post(URL, () =>
        HttpResponse.json({ reservation_id: ID, outcome: "ok", purpose_suggestion: null }),
      ),
    );
    await act(async () => {
      await result.current.mutateAsync(ID);
    });
    await waitFor(() => expect(spy).toHaveBeenCalledWith({ queryKey: ["reservations"] }));

    spy.mockClear();
    server.use(
      http.post(URL, () => HttpResponse.json({ detail: { error: "not_eligible" } }, { status: 409 })),
    );
    await act(async () => {
      await result.current.mutateAsync(ID).catch(() => undefined);
    });
    await waitFor(() => expect(spy).toHaveBeenCalledWith({ queryKey: ["reservations"] }));
  });
});
