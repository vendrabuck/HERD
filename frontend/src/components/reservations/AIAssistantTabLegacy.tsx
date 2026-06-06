// Pre-Branch-3 single-shot assistant UI, kept verbatim as the rollback path
// when VITE_AI_CHAT_ENABLED is off. Delete in a follow-up branch once the
// chat UI is fully on in production.
import { isAxiosError } from "axios";
import toast from "react-hot-toast";
import { useReservationAssistant } from "@/api/ai";
import type { PendingApply, ToolCall } from "@/types/ai.types";

const MAX_QUESTION_LENGTH = 4000;

interface Props {
  reservationId: string;
  question: string;
  setQuestion: (q: string) => void;
  answer: string;
  setAnswer: (a: string) => void;
  setPendingApply?: (p: PendingApply | null) => void;
  toolCalls: ToolCall[];
  setToolCalls: (calls: ToolCall[]) => void;
  toolIterations: number;
  setToolIterations: (n: number) => void;
}

export function AIAssistantTabLegacy({
  reservationId,
  question,
  setQuestion,
  answer,
  setAnswer,
  setPendingApply,
  toolCalls,
  setToolCalls,
  toolIterations,
  setToolIterations,
}: Props) {
  const ask = useReservationAssistant();

  const handleSubmit = async () => {
    const trimmed = question.trim();
    if (!trimmed) {
      toast.error("Enter a question first");
      return;
    }
    try {
      const result = await ask.mutateAsync({ reservationId, question: trimmed });
      setAnswer(result.answer);
      setToolCalls(result.tool_calls);
      setToolIterations(result.tool_iterations);
      if (result.tool_calls.length === 0) {
        console.info("ai_assistant_no_tools_called", {
          reservationId,
          iterations: result.tool_iterations,
        });
      }
      if (setPendingApply) {
        setPendingApply(result.pending_apply ?? null);
      }
    } catch (err) {
      setAnswer("");
      setToolCalls([]);
      setToolIterations(0);
      setPendingApply?.(null);
      if (isAxiosError(err)) {
        const status = err.response?.status;
        const detail = err.response?.data?.detail;
        if (status === 503) {
          toast.error("AI feature is not configured");
        } else if (status === 404) {
          toast.error("Reservation not found");
        } else if (status === 413) {
          toast.error(detail || "Reservation is too large for the assistant");
        } else if (status === 504) {
          toast.error("Assistant timed out; try again");
        } else if (status === 502) {
          toast.error(detail || "Assistant failed");
        } else {
          toast.error("Failed to ask the assistant");
        }
      } else {
        toast.error("Failed to ask the assistant");
      }
    }
  };

  return (
    <div className="space-y-3 text-sm">
      <div>
        <label className="block text-gray-500 mb-1">Question</label>
        <textarea
          value={question}
          onChange={(e) => setQuestion(e.target.value.slice(0, MAX_QUESTION_LENGTH))}
          placeholder="What devices are in my reservation and how are they connected?"
          rows={3}
          className="w-full text-sm px-2 py-1.5 border border-gray-300 rounded focus:outline-none focus:ring-1 focus:ring-gray-900"
          disabled={ask.isPending}
        />
        <div className="flex justify-between text-xs text-gray-400 mt-0.5">
          <span>The assistant answers based on this reservation only.</span>
          <span>
            {question.length} / {MAX_QUESTION_LENGTH}
          </span>
        </div>
      </div>

      <div className="flex justify-end">
        <button
          onClick={handleSubmit}
          disabled={ask.isPending || !question.trim()}
          className="text-sm px-3 py-1.5 rounded bg-gray-900 text-white hover:bg-gray-800 disabled:opacity-50"
        >
          {ask.isPending ? "Asking..." : "Ask"}
        </button>
      </div>

      <div>
        <label className="block text-gray-500 mb-1">Answer</label>
        {ask.isPending ? (
          <div className="text-gray-400 italic">Waiting for the assistant...</div>
        ) : answer ? (
          <div className="whitespace-pre-wrap text-gray-800 bg-gray-50 border border-gray-200 rounded p-2">
            {answer}
          </div>
        ) : (
          <div className="text-gray-400 italic">No answer yet.</div>
        )}
      </div>

      {toolCalls.length > 0 && (
        <details className="text-sm">
          <summary className="cursor-pointer text-gray-500 hover:text-gray-700 select-none">
            Tool calls ({toolCalls.length})
            <span className="ml-2 text-xs bg-gray-200 text-gray-700 px-1.5 py-0.5 rounded">
              {toolIterations} {toolIterations === 1 ? "iteration" : "iterations"}
            </span>
          </summary>
          <ul className="mt-2 space-y-2">
            {toolCalls.map((tc, idx) => (
              <li key={idx} className="border border-gray-200 rounded p-2 bg-gray-50">
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
  );
}
