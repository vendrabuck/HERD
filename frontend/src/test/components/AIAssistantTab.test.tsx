import { http, HttpResponse } from "msw";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import type { ReactNode } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";

const { toastError } = vi.hoisted(() => ({ toastError: vi.fn() }));
vi.mock("react-hot-toast", () => ({
  default: { error: toastError, success: vi.fn() },
}));

import { server } from "../mocks/server";
import { AIAssistantTab } from "@/components/reservations/AIAssistantTab";
import type { ToolCall } from "@/types/ai.types";

function Harness({
  initialQuestion = "",
  initialAnswer = "",
  initialToolCalls = [],
  initialToolIterations = 0,
  onPendingApply,
}: {
  initialQuestion?: string;
  initialAnswer?: string;
  initialToolCalls?: ToolCall[];
  initialToolIterations?: number;
  onPendingApply?: (p: unknown) => void;
}) {
  const [question, setQuestion] = useState(initialQuestion);
  const [answer, setAnswer] = useState(initialAnswer);
  const [toolCalls, setToolCalls] = useState<ToolCall[]>(initialToolCalls);
  const [toolIterations, setToolIterations] = useState(initialToolIterations);
  return (
    <AIAssistantTab
      reservationId="res-1"
      question={question}
      setQuestion={setQuestion}
      answer={answer}
      setAnswer={setAnswer}
      setPendingApply={onPendingApply}
      toolCalls={toolCalls}
      setToolCalls={setToolCalls}
      toolIterations={toolIterations}
      setToolIterations={setToolIterations}
    />
  );
}

function renderWithProviders(node: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

beforeEach(() => {
  toastError.mockClear();
});

describe("AIAssistantTab", () => {
  it("shows the empty-state placeholder when there is no answer", () => {
    renderWithProviders(<Harness />);
    expect(screen.getByText("No answer yet.")).toBeInTheDocument();
  });

  it("submits the question and renders the answer", async () => {
    server.use(
      http.post("/api/ai/reservations/res-1/assistant", () =>
        HttpResponse.json({
          answer: "You have one firewall reserved.",
          model: "claude-sonnet-4-6",
          input_tokens: 50,
          output_tokens: 10,
          stop_reason: "end_turn",
          tool_calls: [],
          tool_iterations: 0,
          conversation_id: null,
        }),
      ),
    );

    renderWithProviders(<Harness />);

    const textarea = screen.getByPlaceholderText(/What devices are in my reservation/i);
    fireEvent.change(textarea, { target: { value: "what do I have?" } });

    fireEvent.click(screen.getByRole("button", { name: "Ask" }));

    await waitFor(() =>
      expect(screen.getByText("You have one firewall reserved.")).toBeInTheDocument(),
    );
  });

  it("renders the tool-calls panel with name, args, duration, and iterations badge", async () => {
    server.use(
      http.post("/api/ai/reservations/res-1/assistant", () =>
        HttpResponse.json({
          answer: "You have two switches.",
          model: "claude-sonnet-4-6",
          input_tokens: 60,
          output_tokens: 20,
          stop_reason: "end_turn",
          tool_calls: [
            {
              name: "get_reservation_inventory",
              arguments_summary: "reservation_id=res-1",
              duration_ms: 124,
              error: null,
            },
            {
              name: "get_device_connections",
              arguments_summary: "device_id=dev-7",
              duration_ms: 88,
              error: null,
            },
          ],
          tool_iterations: 2,
          conversation_id: null,
        }),
      ),
    );

    renderWithProviders(<Harness />);
    fireEvent.change(screen.getByPlaceholderText(/What devices/i), {
      target: { value: "what's in my reservation?" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));

    await waitFor(() => expect(screen.getByText("You have two switches.")).toBeInTheDocument());

    expect(screen.getByText("Tool calls (2)")).toBeInTheDocument();
    expect(screen.getByText("2 iterations")).toBeInTheDocument();
    expect(screen.getByText("get_reservation_inventory")).toBeInTheDocument();
    expect(screen.getByText("reservation_id=res-1")).toBeInTheDocument();
    expect(screen.getByText("124ms")).toBeInTheDocument();
    expect(screen.getByText("get_device_connections")).toBeInTheDocument();
  });

  it("renders error styling on a tool call that errored", async () => {
    server.use(
      http.post("/api/ai/reservations/res-1/assistant", () =>
        HttpResponse.json({
          answer: "Partial answer.",
          model: "claude-sonnet-4-6",
          input_tokens: 50,
          output_tokens: 10,
          stop_reason: "end_turn",
          tool_calls: [
            {
              name: "get_device_connections",
              arguments_summary: "device_id=missing",
              duration_ms: 12,
              error: "device not found",
            },
          ],
          tool_iterations: 1,
          conversation_id: null,
        }),
      ),
    );

    renderWithProviders(<Harness />);
    fireEvent.change(screen.getByPlaceholderText(/What devices/i), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));

    await waitFor(() => expect(screen.getByText("Partial answer.")).toBeInTheDocument());

    expect(screen.getByText("1 iteration")).toBeInTheDocument();
    expect(screen.getByText("device not found")).toBeInTheDocument();
  });

  it("hides the panel and emits an info log when no tools were called", async () => {
    const infoSpy = vi.spyOn(console, "info").mockImplementation(() => {});

    server.use(
      http.post("/api/ai/reservations/res-1/assistant", () =>
        HttpResponse.json({
          answer: "Direct answer, no tools needed.",
          model: "claude-sonnet-4-6",
          input_tokens: 40,
          output_tokens: 8,
          stop_reason: "end_turn",
          tool_calls: [],
          tool_iterations: 0,
          conversation_id: null,
        }),
      ),
    );

    renderWithProviders(<Harness />);
    fireEvent.change(screen.getByPlaceholderText(/What devices/i), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));

    await waitFor(() =>
      expect(screen.getByText("Direct answer, no tools needed.")).toBeInTheDocument(),
    );

    expect(screen.queryByText(/Tool calls/)).not.toBeInTheDocument();
    expect(infoSpy).toHaveBeenCalledWith("ai_assistant_no_tools_called", {
      reservationId: "res-1",
      iterations: 0,
    });

    infoSpy.mockRestore();
  });

  it("disables submit when question is empty", () => {
    renderWithProviders(<Harness />);
    expect(screen.getByRole("button", { name: "Ask" })).toBeDisabled();
  });

  it("toasts on 503 and clears the prior answer", async () => {
    server.use(
      http.post("/api/ai/reservations/res-1/assistant", () =>
        HttpResponse.json(
          { detail: "AI orchestrator is not configured: ANTHROPIC_API_KEY is blank" },
          { status: 503 },
        ),
      ),
    );

    renderWithProviders(<Harness initialAnswer="previous answer" initialQuestion="x" />);

    fireEvent.click(screen.getByRole("button", { name: "Ask" }));

    await waitFor(() => expect(toastError).toHaveBeenCalled());
    expect(toastError.mock.calls[0][0]).toMatch(/not configured/i);
    expect(screen.queryByText("previous answer")).not.toBeInTheDocument();
  });

  it("enforces the 4000-character question cap", () => {
    renderWithProviders(<Harness />);
    const textarea = screen.getByPlaceholderText(/What devices/i) as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "a".repeat(5000) } });
    expect(textarea.value.length).toBe(4000);
    expect(screen.getByText("4000 / 4000")).toBeInTheDocument();
  });

  it("forwards pending_apply to the parent when the assistant scheduled a dry-run", async () => {
    const onPendingApply = vi.fn();
    const pending = {
      job_id: "job-1",
      version_id: "ver-1",
      device_id: "dev-1",
      dry_run: true,
      scheduled_for: "2026-05-26T12:00:00Z",
    };
    server.use(
      http.post("/api/ai/reservations/res-1/assistant", () =>
        HttpResponse.json({
          answer: "I drafted the change and scheduled a dry-run.",
          model: "claude-sonnet-4-6",
          input_tokens: 50,
          output_tokens: 10,
          stop_reason: "end_turn",
          tool_calls: [],
          tool_iterations: 0,
          conversation_id: null,
          pending_apply: pending,
        }),
      ),
    );

    renderWithProviders(<Harness initialQuestion="set vlan 200" onPendingApply={onPendingApply} />);
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));

    await waitFor(() => expect(onPendingApply).toHaveBeenCalled());
    expect(onPendingApply.mock.calls[onPendingApply.mock.calls.length - 1][0]).toEqual(pending);
  });

  it("clears pending_apply on the parent when the response has none", async () => {
    const onPendingApply = vi.fn();
    server.use(
      http.post("/api/ai/reservations/res-1/assistant", () =>
        HttpResponse.json({
          answer: "Nothing to schedule.",
          model: "claude-sonnet-4-6",
          input_tokens: 5,
          output_tokens: 5,
          stop_reason: "end_turn",
          tool_calls: [],
          tool_iterations: 0,
          conversation_id: null,
        }),
      ),
    );
    renderWithProviders(<Harness initialQuestion="just a question" onPendingApply={onPendingApply} />);
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));

    await waitFor(() => expect(onPendingApply).toHaveBeenCalled());
    expect(onPendingApply.mock.calls[onPendingApply.mock.calls.length - 1][0]).toBeNull();
  });
});


// --- Branch 3: chat-style assistant (now streaming over SSE) ---

import { AIAssistantChat } from "@/components/reservations/AIAssistantTab";

function ChatHarness({ onPendingApply }: { onPendingApply?: (p: unknown) => void } = {}) {
  return <AIAssistantChat reservationId="res-1" setPendingApply={onPendingApply} />;
}

const STREAM_URL = "/api/ai/reservations/res-1/assistant/stream";

/** Build a text/event-stream Response body from a list of (event, data) frames. */
function sseStream(frames: Array<{ event: string; data: unknown }>): Response {
  const body = frames
    .map((f) => `event: ${f.event}\ndata: ${JSON.stringify(f.data)}\n\n`)
    .join("");
  return new HttpResponse(body, {
    headers: { "Content-Type": "text/event-stream" },
  });
}

/** A done frame's AssistantResponse payload with sensible defaults. */
function doneFrame(overrides: Record<string, unknown> = {}) {
  return {
    event: "done",
    data: {
      answer: "answer",
      model: "m",
      input_tokens: 1,
      output_tokens: 1,
      stop_reason: "end_turn",
      tool_calls: [],
      tool_iterations: 1,
      conversation_id: "conv-1",
      pending_apply: null,
      ...overrides,
    },
  };
}

describe("AIAssistantChat (Branch 3, streaming)", () => {
  beforeEach(() => {
    try {
      sessionStorage.clear();
    } catch {
      // ignore
    }
    toastError.mockClear();
  });

  it("streams tokens then appends the final assistant message on send", async () => {
    server.use(
      http.post(STREAM_URL, () =>
        sseStream([
          { event: "status", data: { message: "analyzing", tools: [], interim: false } },
          { event: "token", data: { text: "first " } },
          { event: "token", data: { text: "turn answer" } },
          doneFrame({ answer: "first turn answer" }),
        ]),
      ),
    );

    renderWithProviders(<ChatHarness />);
    fireEvent.change(screen.getByTestId("assistant-input"), {
      target: { value: "what do I have?" },
    });
    fireEvent.click(screen.getByTestId("assistant-send"));

    await waitFor(() => expect(screen.getByText("first turn answer")).toBeInTheDocument());
    expect(screen.getByText("what do I have?")).toBeInTheDocument();
    expect(screen.getByTestId("bubble-user")).toBeInTheDocument();
    expect(screen.getByTestId("bubble-assistant")).toBeInTheDocument();
  });

  it("captures conversation_id and resends it on the second turn", async () => {
    let secondRequestBody: { question?: string; conversation_id?: string } = {};
    server.use(
      http.post(STREAM_URL, async ({ request }) => {
        const body = (await request.json()) as {
          question?: string;
          conversation_id?: string;
        };
        const isFirst = !body.conversation_id;
        if (!isFirst) secondRequestBody = body;
        return sseStream([
          { event: "token", data: { text: isFirst ? "first answer" : "second answer" } },
          doneFrame({
            answer: isFirst ? "first answer" : "second answer",
            conversation_id: "conv-42",
          }),
        ]);
      }),
    );

    renderWithProviders(<ChatHarness />);
    const input = screen.getByTestId("assistant-input");

    fireEvent.change(input, { target: { value: "turn one" } });
    fireEvent.click(screen.getByTestId("assistant-send"));
    await waitFor(() => expect(screen.getByText("first answer")).toBeInTheDocument());

    fireEvent.change(input, { target: { value: "turn two" } });
    fireEvent.click(screen.getByTestId("assistant-send"));
    await waitFor(() => expect(screen.getByText("second answer")).toBeInTheDocument());

    expect(secondRequestBody.conversation_id).toBe("conv-42");
    expect(secondRequestBody.question).toBe("turn two");
  });

  it("Start new conversation resets the thread and the conversation id", async () => {
    server.use(
      http.post(STREAM_URL, () =>
        sseStream([doneFrame({ conversation_id: "conv-99" })]),
      ),
    );

    renderWithProviders(<ChatHarness />);
    fireEvent.change(screen.getByTestId("assistant-input"), {
      target: { value: "first" },
    });
    fireEvent.click(screen.getByTestId("assistant-send"));
    await waitFor(() => expect(screen.getByText("answer")).toBeInTheDocument());
    expect(screen.getByText(/Conversation:/)).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("assistant-reset"));

    expect(screen.queryByText("answer")).not.toBeInTheDocument();
    expect(screen.queryByText(/Conversation:/)).not.toBeInTheDocument();
  });

  it("surfaces pending_apply up to the parent", async () => {
    const onPendingApply = vi.fn();
    server.use(
      http.post(STREAM_URL, () =>
        sseStream([
          doneFrame({
            answer: "scheduled the dry-run",
            pending_apply: {
              job_id: "job-abc",
              version_id: "v-1",
              device_id: "d-1",
              dry_run: true,
              scheduled_for: "2026-05-29T03:00:00Z",
            },
          }),
        ]),
      ),
    );

    renderWithProviders(<ChatHarness onPendingApply={onPendingApply} />);
    fireEvent.change(screen.getByTestId("assistant-input"), {
      target: { value: "apply something" },
    });
    fireEvent.click(screen.getByTestId("assistant-send"));

    await waitFor(() => expect(onPendingApply).toHaveBeenCalled());
    const lastCall = onPendingApply.mock.calls[onPendingApply.mock.calls.length - 1][0];
    expect(lastCall).not.toBeNull();
    expect((lastCall as { job_id: string }).job_id).toBe("job-abc");
  });

  it("shows a tool status while tools run and discards interim tokens", async () => {
    server.use(
      http.post(STREAM_URL, () =>
        sseStream([
          // Pre-tool narration that must be discarded when the tool status fires.
          { event: "token", data: { text: "let me check" } },
          {
            event: "status",
            data: { message: "running tools", tools: ["get_device"], interim: true },
          },
          { event: "token", data: { text: "with tools" } },
          doneFrame({
            answer: "with tools",
            tool_calls: [
              { name: "get_device", arguments_summary: "device_id=abc", duration_ms: 23, error: null },
            ],
            tool_iterations: 2,
          }),
        ]),
      ),
    );

    renderWithProviders(<ChatHarness />);
    fireEvent.change(screen.getByTestId("assistant-input"), {
      target: { value: "use a tool" },
    });
    fireEvent.click(screen.getByTestId("assistant-send"));

    await waitFor(() => expect(screen.getByText("with tools")).toBeInTheDocument());
    // The interim narration must not survive into the final bubble.
    expect(screen.queryByText("let me check")).not.toBeInTheDocument();
    expect(screen.getByText(/Tool calls \(1\)/)).toBeInTheDocument();
    expect(screen.getByText("get_device")).toBeInTheDocument();
    expect(screen.getByText(/2 iterations/)).toBeInTheDocument();
  });

  it("shows an error bubble and toasts on an error event mid-stream", async () => {
    server.use(
      http.post(STREAM_URL, () =>
        sseStream([{ event: "error", data: { message: "boom" } }]),
      ),
    );
    renderWithProviders(<ChatHarness />);
    fireEvent.change(screen.getByTestId("assistant-input"), {
      target: { value: "anything" },
    });
    fireEvent.click(screen.getByTestId("assistant-send"));

    await waitFor(() => expect(toastError).toHaveBeenCalled());
    expect(screen.getByText(/boom/)).toBeInTheDocument();
  });

  it("toasts a mapped message on a pre-stream 503", async () => {
    server.use(
      http.post(STREAM_URL, () =>
        HttpResponse.json({ detail: "not configured" }, { status: 503 }),
      ),
    );
    renderWithProviders(<ChatHarness />);
    fireEvent.change(screen.getByTestId("assistant-input"), {
      target: { value: "anything" },
    });
    fireEvent.click(screen.getByTestId("assistant-send"));

    await waitFor(() => expect(toastError).toHaveBeenCalledWith("AI feature is not configured"));
  });
});
