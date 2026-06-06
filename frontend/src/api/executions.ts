import { useQuery } from "@tanstack/react-query";
import type { CommandLogEntry } from "@/types/ai.types";
import apiClient from "./client";

async function fetchCommandLog(runId: string): Promise<CommandLogEntry[]> {
  const resp = await apiClient.get<CommandLogEntry[]>(`/execution/runs/${runId}/commands`);
  return resp.data;
}

export function useCommandLog(runId: string | null | undefined, enabled = true) {
  return useQuery({
    queryKey: ["execution", "runs", runId, "commands"],
    queryFn: () => fetchCommandLog(runId!),
    enabled: !!runId && enabled,
    // Command logs are immutable once a run completes; no refetch interval.
    staleTime: 60_000,
  });
}
