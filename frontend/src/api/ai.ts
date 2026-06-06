import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
