import { http, HttpResponse } from "msw";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { describe, it, expect, vi } from "vitest";

vi.mock("react-hot-toast", () => ({
  default: { error: vi.fn(), success: vi.fn() },
}));

import { server } from "../mocks/server";
import { AIAssistantTabLegacy } from "@/components/reservations/AIAssistantTabLegacy";
import type { ToolCall } from "@/types/ai.types";

function Harness() {
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState("");
  const [toolCalls, setToolCalls] = useState<ToolCall[]>([]);
  const [toolIterations, setToolIterations] = useState(0);
  return (
    <AIAssistantTabLegacy
      reservationId="res-1"
      question={question}
      setQuestion={setQuestion}
      answer={answer}
      setAnswer={setAnswer}
      toolCalls={toolCalls}
      setToolCalls={setToolCalls}
      toolIterations={toolIterations}
      setToolIterations={setToolIterations}
    />
  );
}

describe("AIAssistantTabLegacy (AI-UI-18, #1040)", () => {
  it("never sends a conversation_id, so each question starts a new conversation", async () => {
    const bodies: Record<string, unknown>[] = [];
    server.use(
      http.post("/api/ai/reservations/res-1/assistant", async ({ request }) => {
        bodies.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json({
          answer: `answer ${bodies.length}`,
          model: "m",
          input_tokens: 1,
          output_tokens: 1,
          stop_reason: "end_turn",
          tool_calls: [],
          tool_iterations: 0,
          // The server returns an id; the legacy tab must not send it back.
          conversation_id: "conv-1",
        });
      }),
    );
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <Harness />
      </QueryClientProvider>,
    );

    for (const [index, question] of ["first question", "second question"].entries()) {
      fireEvent.change(screen.getByPlaceholderText(/What devices are in my reservation/), {
        target: { value: question },
      });
      fireEvent.click(screen.getByRole("button", { name: "Ask" }));
      await waitFor(() => expect(screen.getByText(`answer ${index + 1}`)).toBeInTheDocument());
    }

    expect(bodies).toEqual([{ question: "first question" }, { question: "second question" }]);
  });
});
