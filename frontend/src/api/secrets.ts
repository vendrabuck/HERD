import { useQuery } from "@tanstack/react-query";
import type { SecretSummary } from "@/types/secret.types";
import apiClient from "./client";

// Read-only: the secrets service is API-only for authoring (no frontend CRUD
// surface exists). This module exists solely to populate the hypervisor
// registration form's secret_id picker with existing secret names.
async function fetchSecrets(): Promise<SecretSummary[]> {
  const resp = await apiClient.get<SecretSummary[]>("/secrets/secrets");
  return resp.data;
}

export function useSecrets() {
  return useQuery({
    queryKey: ["secrets"],
    queryFn: fetchSecrets,
  });
}
