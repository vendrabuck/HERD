import { http, HttpResponse } from "msw";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect } from "vitest";

import { server } from "../mocks/server";
import { useSecrets } from "@/api/secrets";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const SECRET = {
  id: "s1",
  name: "proxmox-lab-cred",
  type: "password",
  description: null,
  key_version: 1,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

describe("secrets api hooks", () => {
  it("useSecrets fetches metadata-only rows", async () => {
    server.use(
      http.get("/api/secrets/secrets", () => HttpResponse.json([SECRET])),
    );
    const { result } = renderHook(() => useSecrets(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual([SECRET]);
  });
});
