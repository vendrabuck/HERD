import { useMutation, useQuery, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import type { PaginatedResponse } from "@/types/pagination.types";
import apiClient from "./client";

export interface ApplyJob {
  id: string;
  device_id: string;
  version_id: string;
  scheduled_for: string;
  reservation_id: string | null;
  dry_run: boolean;
  status: string;
  run_id: string | null;
  error: string | null;
  created_by: string;
  author_name: string;
  created_at: string;
  fired_at: string | null;
}

const TERMINAL_STATUSES = new Set(["success", "failed", "skipped", "cancelled"]);

async function fetchApplyJob(jobId: string): Promise<ApplyJob> {
  const resp = await apiClient.get<ApplyJob>(`/inventory/apply-jobs/${jobId}`);
  return resp.data;
}

async function confirmDryRunApply(jobId: string): Promise<ApplyJob> {
  const resp = await apiClient.post<ApplyJob>(`/inventory/apply-jobs/${jobId}/confirm`);
  return resp.data;
}

async function fetchJobs(
  deviceId: string,
  skip = 0,
  limit = 50,
): Promise<PaginatedResponse<ApplyJob>> {
  const resp = await apiClient.get<PaginatedResponse<ApplyJob>>(
    `/inventory/devices/${deviceId}/apply-jobs`,
    { params: { skip, limit } },
  );
  return resp.data;
}

async function scheduleJob(
  deviceId: string,
  versionId: string,
  body: { scheduled_for: string; reservation_id?: string },
): Promise<ApplyJob> {
  const resp = await apiClient.post<ApplyJob>(
    `/inventory/devices/${deviceId}/config-versions/${versionId}/schedule`,
    body,
  );
  return resp.data;
}

async function cancelJob(jobId: string): Promise<void> {
  await apiClient.delete(`/inventory/apply-jobs/${jobId}`);
}

export function useApplyJobs(deviceId: string | undefined, skip = 0, limit = 50) {
  return useQuery({
    queryKey: ["devices", deviceId, "apply-jobs", skip, limit],
    queryFn: () => fetchJobs(deviceId!, skip, limit),
    enabled: !!deviceId,
    placeholderData: keepPreviousData,
    refetchInterval: 10_000,
  });
}

export function useScheduleApplyJob(deviceId: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      versionId,
      ...body
    }: {
      versionId: string;
      scheduled_for: string;
      reservation_id?: string;
    }) => scheduleJob(deviceId!, versionId, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["devices", deviceId, "apply-jobs"] }),
  });
}

export function useCancelApplyJob(deviceId: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (jobId: string) => cancelJob(jobId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["devices", deviceId, "apply-jobs"] }),
  });
}

export function useApplyJob(jobId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: ["apply-jobs", jobId],
    queryFn: () => fetchApplyJob(jobId!),
    enabled: !!jobId && enabled,
    refetchInterval: (query) => {
      const job = query.state.data as ApplyJob | undefined;
      if (!job) return 2000;
      return TERMINAL_STATUSES.has(job.status) ? false : 2000;
    },
  });
}

export function useConfirmDryRunApply() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (jobId: string) => confirmDryRunApply(jobId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["apply-jobs"] });
      queryClient.invalidateQueries({ queryKey: ["devices"] });
    },
  });
}

async function cancelApplyJob(jobId: string): Promise<void> {
  await cancelJob(jobId);
}

export function useCancelApplyJobById() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (jobId: string) => cancelApplyJob(jobId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["apply-jobs"] });
      queryClient.invalidateQueries({ queryKey: ["devices"] });
    },
  });
}
