import { http, HttpResponse } from "msw";
import { renderHook, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect } from "vitest";

import { server } from "../mocks/server";
import { useAIStatus, useReservationAssistant } from "@/api/ai";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("useAIStatus", () => {
  it("returns enabled=true when the API reports the key is set", async () => {
    server.use(
      http.get("/api/ai/status", () => HttpResponse.json({ enabled: true })),
    );

    const { result } = renderHook(() => useAIStatus(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual({ enabled: true });
  });

  it("returns enabled=false when the API reports the key is blank", async () => {
    server.use(
      http.get("/api/ai/status", () => HttpResponse.json({ enabled: false })),
    );

    const { result } = renderHook(() => useAIStatus(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual({ enabled: false });
  });
});

describe("useReservationAssistant", () => {
  it("posts the question to the per-reservation endpoint and returns the answer", async () => {
    let receivedBody: { question?: string } | undefined;
    server.use(
      http.post(
        "/api/ai/reservations/:id/assistant",
        async ({ request }) => {
          receivedBody = (await request.json()) as { question?: string };
          return HttpResponse.json({
            answer: "You have 2 firewalls reserved.",
            model: "claude-sonnet-4-6",
            input_tokens: 100,
            output_tokens: 20,
            stop_reason: "end_turn",
            conversation_id: null,
          });
        },
      ),
    );

    const { result } = renderHook(() => useReservationAssistant(), { wrapper });
    let data;
    await act(async () => {
      data = await result.current.mutateAsync({
        reservationId: "res-1",
        question: "what's in my reservation?",
      });
    });
    expect(receivedBody).toEqual({ question: "what's in my reservation?" });
    expect(data).toMatchObject({
      answer: "You have 2 firewalls reserved.",
      conversation_id: null,
      stop_reason: "end_turn",
    });
  });
});
