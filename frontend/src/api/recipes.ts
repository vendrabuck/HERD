import { useMutation } from "@tanstack/react-query";
import type { RecipeDraftResponse } from "@/types/ai.types";
import apiClient from "./client";

async function draftRecipe(req: {
  prompt: string;
  hypervisor_type?: string;
}): Promise<RecipeDraftResponse> {
  const resp = await apiClient.post<RecipeDraftResponse>("/ai/recipes/draft", req);
  return resp.data;
}

async function refineRecipeDraft(req: {
  draft_id: string;
  feedback: string;
}): Promise<RecipeDraftResponse> {
  const resp = await apiClient.post<RecipeDraftResponse>(
    `/ai/recipes/draft/${req.draft_id}/refine`,
    { feedback: req.feedback },
  );
  return resp.data;
}

export function useDraftRecipe() {
  return useMutation({ mutationFn: draftRecipe });
}

export function useRefineRecipeDraft() {
  return useMutation({ mutationFn: refineRecipeDraft });
}

// The draft's package_b64, decoded into an uploadable File for inventory's
// existing admin upload endpoint. The AI never uploads; this runs only when
// the reviewing admin clicks approve.
export function recipePackageFile(packageB64: string, name: string): File {
  const bytes = Uint8Array.from(atob(packageB64), (c) => c.charCodeAt(0));
  const safe = name.trim().replace(/[^A-Za-z0-9._-]+/g, "-") || "generated-recipe";
  return new File([bytes], `${safe}.zip`, { type: "application/zip" });
}
