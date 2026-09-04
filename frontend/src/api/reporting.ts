import { useQuery } from "@tanstack/react-query";
import type { UtilizationQuery, UtilizationReport } from "@/types/reporting.types";
import apiClient from "./client";

async function fetchUtilizationReport(query: UtilizationQuery): Promise<UtilizationReport> {
  const params = new URLSearchParams();
  params.set("start", query.start);
  params.set("end", query.end);
  if (query.status) {
    for (const s of query.status) {
      params.append("status", s);
    }
  }
  const resp = await apiClient.get<UtilizationReport>(
    `/reservations/reports/utilization?${params.toString()}`,
  );
  return resp.data;
}

export function useUtilizationReport(query: UtilizationQuery, enabled = true) {
  return useQuery({
    queryKey: ["reports", "utilization", query],
    queryFn: () => fetchUtilizationReport(query),
    enabled,
    staleTime: 5 * 60 * 1000,
  });
}

export type UtilizationCsvSection =
  | "user"
  | "device"
  | "fleet"
  | "purpose"
  | "user_purpose"
  | "device_purpose"
  | "purpose_suggested";

export async function downloadUtilizationCsv(
  query: UtilizationQuery,
  section: UtilizationCsvSection,
): Promise<{ filename: string; body: string }> {
  const params = new URLSearchParams();
  params.set("start", query.start);
  params.set("end", query.end);
  params.set("section", section);
  if (query.status) {
    for (const s of query.status) {
      params.append("status", s);
    }
  }
  const resp = await apiClient.get<string>(
    `/reservations/reports/utilization.csv?${params.toString()}`,
    { responseType: "text", transformResponse: (v) => v },
  );
  const disposition = resp.headers["content-disposition"] ?? "";
  const match = /filename="([^"]+)"/.exec(disposition);
  const filename = match?.[1] ?? `utilization-${section}.csv`;
  return { filename, body: resp.data };
}
