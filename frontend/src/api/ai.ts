import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuthStore } from "@/stores/authStore";
import apiClient from "./client";
import type {
  AICommitRequest,
  AICommitResponse,
  AIGenerateRequest,
  AIGenerateResponse,
  AIStatusResponse,
  AssistantResponse,
  IdentitySuggestion,
  SuggestIdentityRequest,
} from "@/types/ai.types";

async function generateTopology(req: AIGenerateRequest): Promise<AIGenerateResponse> {
  const formData = new FormData();
  formData.append("prompt", req.prompt);
  for (const file of req.files ?? []) {
    formData.append("files", file);
  }
  const resp = await apiClient.post<AIGenerateResponse>("/ai/generate", formData, {
    headers: { "Content-Type": "multipart/form-data" },
  });
  return resp.data;
}

async function commitProposal(req: AICommitRequest): Promise<AICommitResponse> {
  const resp = await apiClient.post<AICommitResponse>("/ai/commit", req);
  return resp.data;
}

async function fetchAIStatus(): Promise<AIStatusResponse> {
  const resp = await apiClient.get<AIStatusResponse>("/ai/status");
  return resp.data;
}

export function useAIStatus() {
  return useQuery({
    queryKey: ["ai", "status"],
    queryFn: fetchAIStatus,
    staleTime: 5 * 60 * 1000,
  });
}

export function useAIGenerate() {
  return useMutation({
    mutationFn: generateTopology,
  });
}

export function useAICommit() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: commitProposal,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["topologies"] });
      queryClient.invalidateQueries({ queryKey: ["reservations"] });
    },
  });
}

async function askReservationAssistant(
  reservationId: string,
  question: string,
  conversationId?: string | null,
): Promise<AssistantResponse> {
  const body: { question: string; conversation_id?: string } = { question };
  if (conversationId) {
    body.conversation_id = conversationId;
  }
  const resp = await apiClient.post<AssistantResponse>(
    `/ai/reservations/${reservationId}/assistant`,
    body,
  );
  return resp.data;
}

// SSE events emitted by POST /ai/reservations/{id}/assistant/stream. Mirrors the
// backend's status/token/done/error frames (see docs/AI_ASSISTANT.md).
export interface AssistantStatusEvent {
  type: "status";
  message: string;
  tools: string[];
  interim: boolean;
}
export interface AssistantTokenEvent {
  type: "token";
  text: string;
}
export interface AssistantDoneEvent {
  type: "done";
  result: AssistantResponse;
}
export interface AssistantErrorEvent {
  type: "error";
  message: string;
}
export type AssistantStreamEvent =
  | AssistantStatusEvent
  | AssistantTokenEvent
  | AssistantDoneEvent
  | AssistantErrorEvent;

/**
 * Stream the reservation assistant over SSE, invoking onEvent per frame.
 *
 * Uses fetch + a ReadableStream reader (axios buffers, so it cannot stream).
 * Reuses the same access token and /api base as apiClient; it does NOT get
 * apiClient's silent 401-refresh interceptor, so a token that expires mid-call
 * surfaces as an error and the caller can fall back to the buffered endpoint. A
 * pre-stream non-2xx (503/404/422/429) is thrown so the caller maps it like the
 * buffered path. Pass an AbortSignal to cancel an in-flight stream.
 */
async function streamReservationAssistant(
  reservationId: string,
  question: string,
  conversationId: string | null | undefined,
  onEvent: (event: AssistantStreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const body: { question: string; conversation_id?: string } = { question };
  if (conversationId) {
    body.conversation_id = conversationId;
  }
  const token = useAuthStore.getState().accessToken;
  const resp = await fetch(`/api/ai/reservations/${reservationId}/assistant/stream`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(body),
    signal,
  });

  if (!resp.ok || !resp.body) {
    // Setup failure before the stream opened (auth/config/quota/validation).
    // Surface a shaped error the caller maps like the buffered path's axios error.
    let detail: string | undefined;
    try {
      detail = (await resp.json())?.detail;
    } catch {
      // non-JSON body; leave detail undefined
    }
    throw new AssistantStreamError(resp.status, detail);
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  // Parse SSE frames: events are separated by a blank line; each frame has an
  // `event:` line and a `data:` line. Accumulate across chunk boundaries.
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      const parsed = parseSseFrame(frame);
      if (parsed) onEvent(parsed);
    }
  }
}

export class AssistantStreamError extends Error {
  status: number;
  detail?: string;
  constructor(status: number, detail?: string) {
    super(detail ?? `Assistant stream failed (${status})`);
    this.name = "AssistantStreamError";
    this.status = status;
    this.detail = detail;
  }
}

function parseSseFrame(frame: string): AssistantStreamEvent | null {
  let event = "";
  let data = "";
  for (const line of frame.split("\n")) {
    if (line.startsWith("event: ")) event = line.slice(7);
    else if (line.startsWith("data: ")) data = line.slice(6);
  }
  if (!event || !data) return null;
  try {
    const payload = JSON.parse(data);
    if (event === "status")
      return {
        type: "status",
        message: payload.message,
        tools: payload.tools ?? [],
        interim: payload.interim ?? false,
      };
    if (event === "token") return { type: "token", text: payload.text };
    if (event === "done") return { type: "done", result: payload };
    if (event === "error") return { type: "error", message: payload.message };
  } catch {
    return null;
  }
  return null;
}

export { streamReservationAssistant };

export function useReservationAssistant() {
  return useMutation({
    mutationFn: ({
      reservationId,
      question,
      conversationId,
    }: {
      reservationId: string;
      question: string;
      conversationId?: string | null;
    }) => askReservationAssistant(reservationId, question, conversationId),
  });
}

async function suggestTemplateIdentity(
  req: SuggestIdentityRequest,
): Promise<IdentitySuggestion> {
  const resp = await apiClient.post<IdentitySuggestion>(
    "/ai/templates/suggest-identity",
    req,
  );
  return resp.data;
}

export function useSuggestTemplateIdentity() {
  return useMutation({
    mutationFn: suggestTemplateIdentity,
  });
}
