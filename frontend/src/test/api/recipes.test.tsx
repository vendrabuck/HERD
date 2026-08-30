import { http, HttpResponse } from "msw";
import { renderHook, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect } from "vitest";

import { server } from "../mocks/server";
import { useDraftRecipe, useRefineRecipeDraft, recipePackageFile } from "@/api/recipes";
import type { RecipeDraftResponse } from "@/types/ai.types";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function draftResponse(): RecipeDraftResponse {
  return {
    draft_id: "11111111-2222-3333-4444-555555555555",
    valid: true,
    attempts: 1,
    model: "test-model",
    prompt: "clone a proxmox vm",
    hypervisor_type: "proxmox",
    explanation: "explanation",
    driver_py: "class Driver:\n    pass\n",
    driver_metadata: {
      name: "proxmox-clone",
      version: "0.1.0",
      connection_type: "Hypervisor",
      supports_dry_run: true,
      draft_id: "11111111-2222-3333-4444-555555555555",
    },
    validation: {
      valid: true,
      structural: { passed: true, errors: [] },
      policy: { passed: true, errors: [] },
      schema: { present: false, schema: null, error: null },
      dry_run: { passed: true, methods: [], error: null },
    },
    package_b64: btoa("fake zip bytes"),
    created_at: "2026-07-07T00:00:00Z",
    updated_at: "2026-07-07T00:00:00Z",
  };
}

describe("recipes api hooks", () => {
  it("useDraftRecipe POSTs the prompt and optional hypervisor type", async () => {
    let captured: unknown = null;
    server.use(
      http.post("/api/ai/recipes/draft", async ({ request }) => {
        captured = await request.json();
        return HttpResponse.json(draftResponse());
      }),
    );
    const { result } = renderHook(() => useDraftRecipe(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({
        prompt: "clone a proxmox vm",
        hypervisor_type: "proxmox",
      });
    });
    expect(captured).toEqual({ prompt: "clone a proxmox vm", hypervisor_type: "proxmox" });
  });

  it("useDraftRecipe omits hypervisor_type when not provided", async () => {
    let captured: unknown = null;
    server.use(
      http.post("/api/ai/recipes/draft", async ({ request }) => {
        captured = await request.json();
        return HttpResponse.json(draftResponse());
      }),
    );
    const { result } = renderHook(() => useDraftRecipe(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({ prompt: "clone a vm" });
    });
    expect(captured).toEqual({ prompt: "clone a vm" });
  });

  it("useDraftRecipe resolves with the draft response from the server", async () => {
    const response = draftResponse();
    server.use(
      http.post("/api/ai/recipes/draft", () => HttpResponse.json(response)),
    );
    const { result } = renderHook(() => useDraftRecipe(), { wrapper });
    await act(async () => {
      const data = await result.current.mutateAsync({ prompt: "clone a vm" });
      expect(data).toEqual(response);
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
  });

  it("useRefineRecipeDraft POSTs feedback to the draft's refine endpoint", async () => {
    let capturedUrl = "";
    let capturedBody: unknown = null;
    server.use(
      http.post(
        "/api/ai/recipes/draft/11111111-2222-3333-4444-555555555555/refine",
        async ({ request }) => {
          capturedUrl = request.url;
          capturedBody = await request.json();
          return HttpResponse.json(draftResponse());
        },
      ),
    );
    const { result } = renderHook(() => useRefineRecipeDraft(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({
        draft_id: "11111111-2222-3333-4444-555555555555",
        feedback: "add TLS verification",
      });
    });
    expect(capturedUrl).toMatch(/\/recipes\/draft\/11111111-2222-3333-4444-555555555555\/refine$/);
    expect(capturedBody).toEqual({ feedback: "add TLS verification" });
  });

  describe("recipePackageFile", () => {
    it("decodes base64 package bytes into a File with a slugified zip name", () => {
      const b64 = btoa("package contents");
      const file = recipePackageFile(b64, "My Cool Driver!!");
      expect(file).toBeInstanceOf(File);
      expect(file.name).toBe("My-Cool-Driver-.zip");
      expect(file.type).toBe("application/zip");
    });

    it("falls back to generated-recipe when the name is empty (not merely unsafe)", () => {
      // An all-unsafe-char name collapses to a single "-" (one run of
      // unsafe characters replaced), which is truthy, so it is kept rather
      // than triggering the fallback; only a genuinely empty/whitespace name
      // does.
      expect(recipePackageFile(btoa("x"), "!!!").name).toBe("-.zip");
      expect(recipePackageFile(btoa("x"), "").name).toBe("generated-recipe.zip");
      expect(recipePackageFile(btoa("x"), "   ").name).toBe("generated-recipe.zip");
    });

    it("trims surrounding whitespace before sanitizing", () => {
      const file = recipePackageFile(btoa("x"), "  proxmox-clone  ");
      expect(file.name).toBe("proxmox-clone.zip");
    });

    it("decodes the exact byte content, not just the length", async () => {
      const original = "the quick brown fox";
      const file = recipePackageFile(btoa(original), "test");
      const text = await file.text();
      expect(text).toBe(original);
    });
  });
});
