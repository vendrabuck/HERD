// Branch 3: multi-turn chat UI for the reservation assistant. Replaces the
// pre-Branch-3 single-shot form (which is preserved as AIAssistantTabLegacy
// behind VITE_AI_CHAT_ENABLED for rollback). The tab owns the conversation
// state internally; the parent modal only consumes setPendingApply so the
// AIApplyConfirmModal can mount when the model schedules a dry-run.
import { useCallback, useEffect, useRef, useState } from "react";
import toast from "react-hot-toast";
import {
  AssistantStreamError,
  streamReservationAssistant,
  type AssistantStreamEvent,
} from "@/api/ai";
import { AI_CHAT_ENABLED } from "@/config/featureFlags";
import type { ChatMessage, PendingApply, ToolCall } from "@/types/ai.types";
import { AIAssistantTabLegacy } from "./AIAssistantTabLegacy";

const MAX_QUESTION_LENGTH = 4000;

interface Props {
  reservationId: string;
  setPendingApply?: (p: PendingApply | null) => void;
  // Carried for the legacy fallback only; the chat UI owns its own state.
  question?: string;
  setQuestion?: (q: string) => void;
  answer?: string;
  setAnswer?: (a: string) => void;
  toolCalls?: ToolCall[];
  setToolCalls?: (calls: ToolCall[]) => void;
  toolIterations?: number;
  setToolIterations?: (n: number) => void;
}

function sessionStorageKey(reservationId: string): string {
  return `herd.assistant.conversationId.${reservationId}`;
}

function makeLocalId(): string {
  // Local-only id for React keys on chat bubbles; not the backend message id.
  return `m_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

function ChatAssistant({
  reservationId,
  setPendingApply,
  setAnswer,
  setToolCalls,
  setToolIterations,
}: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  // The assistant answer streams in: isStreaming gates the input, statusText
  // shows the current "analyzing" / "running tools" phase, and streamingText
  // is the partial answer growing token by token in a live bubble.
  const [isStreaming, setIsStreaming] = useState(false);
  const [statusText, setStatusText] = useState<string>("");
  const [streamingText, setStreamingText] = useState<string>("");
  const [conversationId, setConversationId] = useState<string | null>(() => {
    try {
      return sessionStorage.getItem(sessionStorageKey(reservationId));
    } catch {
      return null;
    }
  });
  const threadRef = useRef<HTMLDivElement>(null);

  // Auto-scroll the thread to the bottom as messages and streamed text grow.
  useEffect(() => {
    const el = threadRef.current;
    if (el) {
      el.scrollTop = el.scrollHeight;
    }
  }, [messages.length, isStreaming, streamingText, statusText]);

  const persistConversationId = useCallback(
    (id: string | null) => {
      try {
        if (id) {
          sessionStorage.setItem(sessionStorageKey(reservationId), id);
        } else {
          sessionStorage.removeItem(sessionStorageKey(reservationId));
        }
      } catch {
        // sessionStorage may be unavailable (e.g. private browsing); the
        // conversation still works in-memory for the lifetime of the modal.
      }
    },
    [reservationId],
  );

  const resetConversation = useCallback(() => {
    setMessages([]);
    setConversationId(null);
    setInput("");
    persistConversationId(null);
    setPendingApply?.(null);
  }, [persistConversationId, setPendingApply]);

  const pushErrorBubble = useCallback((errorText: string) => {
    toast.error(errorText);
    setMessages((prev) => [
      ...prev,
      { id: makeLocalId(), role: "assistant", text: "", errorText },
    ]);
  }, []);

  const handleSubmit = async () => {
    const trimmed = input.trim();
    if (!trimmed) {
      toast.error("Enter a question first");
      return;
    }
    const userMsg: ChatMessage = {
      id: makeLocalId(),
      role: "user",
      text: trimmed,
    };
    setMessages((prev) => [...prev, userMsg]);
    setInput("");
    setIsStreaming(true);
    setStatusText("");
    setStreamingText("");

    // Accumulate streamed tokens here; an interim status (a tool turn's
    // pre-tool narration) resets it so only the final answer's text remains.
    let accumulated = "";

    const onEvent = (ev: AssistantStreamEvent) => {
      if (ev.type === "status") {
        if (ev.interim) {
          // Discard provisional tokens streamed before a tool turn.
          accumulated = "";
          setStreamingText("");
        }
        setStatusText(
          ev.tools.length > 0 ? `Running ${ev.tools.join(", ")}` : "Analyzing",
        );
      } else if (ev.type === "token") {
        accumulated += ev.text;
        setStreamingText(accumulated);
      } else if (ev.type === "done") {
        const result = ev.result;
        const newConvId = result.conversation_id ?? null;
        if (newConvId && newConvId !== conversationId) {
          setConversationId(newConvId);
          persistConversationId(newConvId);
        }
        setMessages((prev) => [
          ...prev,
          {
            id: makeLocalId(),
            role: "assistant",
            text: result.answer,
            toolCalls: result.tool_calls,
            toolIterations: result.tool_iterations,
            pendingApply: result.pending_apply ?? null,
          },
        ]);
        // Bridge the lifted state the parent modal reads for AIApplyConfirmModal
        // and the legacy tool-call panel; the chat owns its own message list.
        setAnswer?.(result.answer);
        setToolCalls?.(result.tool_calls);
        setToolIterations?.(result.tool_iterations);
        setPendingApply?.(result.pending_apply ?? null);
      } else if (ev.type === "error") {
        // A failure after the stream opened (timeout or LLM failure).
        pushErrorBubble(ev.message || "Assistant failed");
      }
    };

    try {
      await streamReservationAssistant(reservationId, trimmed, conversationId, onEvent);
    } catch (err) {
      // Pre-stream failure (the request never opened a stream): map the status
      // the same way the buffered path does.
      let errorText = "Failed to ask the assistant";
      if (err instanceof AssistantStreamError) {
        const { status, detail } = err;
        if (status === 503) {
          errorText = "AI feature is not configured";
        } else if (status === 404 && detail?.toLowerCase().includes("conversation")) {
          // Conversation expired or never existed; reset and start fresh.
          errorText = "Conversation expired; starting a new one";
          resetConversation();
        } else if (status === 404) {
          errorText = "Reservation not found";
        } else if (status === 429) {
          errorText = detail || "Daily AI limit reached";
        } else if (status === 422) {
          errorText = detail || "Question was rejected";
        } else if (status === 504) {
          errorText = "Assistant timed out; try again";
        } else if (status === 502) {
          errorText = detail || "Assistant failed";
        }
      }
      pushErrorBubble(errorText);
    } finally {
      setIsStreaming(false);
      setStatusText("");
      setStreamingText("");
    }
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter sends, Shift+Enter inserts a newline. Standard chat convention.
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void handleSubmit();
    }
  };

  return (
    <div className="flex flex-col h-[28rem] text-sm">
      <div
        ref={threadRef}
        className="flex-1 min-h-0 overflow-y-auto space-y-3 pr-1"
        data-testid="assistant-thread"
      >
        {messages.length === 0 && !isStreaming && (
          <div className="text-gray-400 italic">
            Ask anything about this reservation. The assistant will use only
            this reservation's data.
          </div>
        )}
        {messages.map((m) => (
          <ChatBubble key={m.id} message={m} />
        ))}
        {isStreaming && (
          <div className="flex justify-start" data-testid="assistant-streaming">
            <div className="space-y-1">
              {statusText && (
                <div
                  className="text-xs text-gray-400 italic ml-1"
                  data-testid="assistant-status"
                >
                  {statusText}...
                </div>
              )}
              {streamingText ? (
                <div className="max-w-[80%] bg-gray-50 border border-gray-200 text-gray-800 px-3 py-2 rounded-lg whitespace-pre-wrap">
                  {streamingText}
                  <span className="inline-block w-1.5 h-3.5 ml-0.5 bg-gray-400 animate-pulse align-middle" />
                </div>
              ) : (
                !statusText && (
                  <div className="text-gray-400 italic" data-testid="assistant-pending">
                    Waiting for the assistant...
                  </div>
                )
              )}
            </div>
          </div>
        )}
      </div>

      <div className="mt-3 border-t border-gray-200 pt-2 space-y-2">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value.slice(0, MAX_QUESTION_LENGTH))}
          onKeyDown={onKeyDown}
          placeholder="Type your question. Enter to send, Shift+Enter for a new line."
          rows={2}
          className="w-full text-sm px-2 py-1.5 border border-gray-300 rounded focus:outline-none focus:ring-1 focus:ring-gray-900"
          disabled={isStreaming}
          data-testid="assistant-input"
        />
        <div className="flex items-center justify-between text-xs">
          <div className="text-gray-400">
            {conversationId ? (
              <span>
                Conversation:{" "}
                <span className="font-mono">{conversationId.slice(0, 8)}</span>
              </span>
            ) : (
              <span>New conversation will start on send.</span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <span className="text-gray-400">
              {input.length} / {MAX_QUESTION_LENGTH}
            </span>
            <button
              type="button"
              onClick={resetConversation}
              disabled={isStreaming || (!conversationId && messages.length === 0)}
              className="text-xs px-2 py-1 rounded border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50"
              data-testid="assistant-reset"
            >
              Start new conversation
            </button>
            <button
              type="button"
              onClick={handleSubmit}
              disabled={isStreaming || !input.trim()}
              className="text-sm px-3 py-1.5 rounded bg-gray-900 text-white hover:bg-gray-800 disabled:opacity-50"
              data-testid="assistant-send"
            >
              {isStreaming ? "Asking..." : "Send"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function ChatBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user";
  const containerClass = isUser ? "flex justify-end" : "flex justify-start";
  const bubbleClass = isUser
    ? "max-w-[80%] bg-gray-900 text-white px-3 py-2 rounded-lg whitespace-pre-wrap"
    : "max-w-[80%] bg-gray-50 border border-gray-200 text-gray-800 px-3 py-2 rounded-lg whitespace-pre-wrap";

  return (
    <div className={containerClass} data-testid={`bubble-${message.role}`}>
      <div className="space-y-2">
        <div className={bubbleClass}>
          {message.errorText ? (
            <span className="italic text-red-700">{message.errorText}</span>
          ) : (
            message.text
          )}
        </div>
        {message.toolCalls && message.toolCalls.length > 0 && (
          <details className="text-xs ml-1">
            <summary className="cursor-pointer text-gray-500 hover:text-gray-700 select-none">
              Tool calls ({message.toolCalls.length})
              {message.toolIterations !== undefined && (
                <span className="ml-2 bg-gray-200 text-gray-700 px-1.5 py-0.5 rounded">
                  {message.toolIterations}{" "}
                  {message.toolIterations === 1 ? "iteration" : "iterations"}
                </span>
              )}
            </summary>
            <ul className="mt-2 space-y-2">
              {message.toolCalls.map((tc, idx) => (
                <li
                  key={idx}
                  className="border border-gray-200 rounded p-2 bg-gray-50"
                >
                  <div className="flex justify-between items-baseline">
                    <span className="font-mono text-xs text-gray-800">{tc.name}</span>
                    <span className="text-xs text-gray-500">{tc.duration_ms}ms</span>
                  </div>
                  <pre className="mt-1 text-xs font-mono text-gray-600 whitespace-pre-wrap break-all">
                    {tc.arguments_summary}
                  </pre>
                  {tc.error && (
                    <div className="mt-1 text-xs text-red-800 bg-red-50 border border-red-200 rounded p-1.5">
                      {tc.error}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          </details>
        )}
      </div>
    </div>
  );
}

export function AIAssistantTab(props: Props) {
  // Branch 3: chat UI behind a feature flag so production can roll forward
  // with a one-line flag flip and roll back to the legacy single-shot in
  // the same way. The flag is build-time (VITE_AI_CHAT_ENABLED); to flip
  // without a rebuild, restart the frontend container with the new value.
  if (!AI_CHAT_ENABLED) {
    return (
      <AIAssistantTabLegacy
        reservationId={props.reservationId}
        question={props.question ?? ""}
        setQuestion={props.setQuestion ?? (() => undefined)}
        answer={props.answer ?? ""}
        setAnswer={props.setAnswer ?? (() => undefined)}
        setPendingApply={props.setPendingApply}
        toolCalls={props.toolCalls ?? []}
        setToolCalls={props.setToolCalls ?? (() => undefined)}
        toolIterations={props.toolIterations ?? 0}
        setToolIterations={props.setToolIterations ?? (() => undefined)}
      />
    );
  }
  return <ChatAssistant {...props} />;
}

// Re-export so tests can target the chat path directly without going through
// the feature-flag fork.
export { ChatAssistant as AIAssistantChat };
