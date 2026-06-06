import { useMutation, useQuery, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import type { PaginatedResponse } from "@/types/pagination.types";
import apiClient from "./client";

export interface DeviceConfigVersion {
  id: string;
  device_id: string;
  version_number: number;
  connection_type: string;
  description: string | null;
  created_by: string;
  author_name: string;
  created_at: string;
  restored_from_id: string | null;
  last_apply_run_id: string | null;
}

export interface DeviceConfigVersionDetail extends DeviceConfigVersion {
  config: Record<string, unknown>;
}

export interface DeviceConfigDiff {
  version_a: string;
  version_b: string;
  diff: string;
}

export interface DeviceConfigApplyResponse {
  version_id: string;
  run_id: string | null;
  status: string;
  error: string | null;
}

async function fetchVersions(
  deviceId: string,
  skip = 0,
  limit = 50,
): Promise<PaginatedResponse<DeviceConfigVersion>> {
  const resp = await apiClient.get<PaginatedResponse<DeviceConfigVersion>>(
    `/inventory/devices/${deviceId}/config-versions`,
    { params: { skip, limit } },
  );
  return resp.data;
}

async function fetchVersion(
  deviceId: string,
  versionId: string,
): Promise<DeviceConfigVersionDetail> {
  const resp = await apiClient.get<DeviceConfigVersionDetail>(
    `/inventory/devices/${deviceId}/config-versions/${versionId}`,
  );
  return resp.data;
}

async function createVersion(
  deviceId: string,
  body: { config: Record<string, unknown>; description?: string },
): Promise<DeviceConfigVersionDetail> {
  const resp = await apiClient.post<DeviceConfigVersionDetail>(
    `/inventory/devices/${deviceId}/config-versions`,
    body,
  );
  return resp.data;
}

async function fetchDiff(
  deviceId: string,
  from: string,
  to: string,
): Promise<DeviceConfigDiff> {
  const resp = await apiClient.get<DeviceConfigDiff>(
    `/inventory/devices/${deviceId}/config-versions/diff`,
    { params: { from, to } },
  );
  return resp.data;
}

async function restoreVersion(
  deviceId: string,
  versionId: string,
  body: { description?: string },
): Promise<DeviceConfigVersionDetail> {
  const resp = await apiClient.post<DeviceConfigVersionDetail>(
    `/inventory/devices/${deviceId}/config-versions/${versionId}/restore`,
    body,
  );
  return resp.data;
}

async function applyVersion(
  deviceId: string,
  versionId: string,
): Promise<DeviceConfigApplyResponse> {
  const resp = await apiClient.post<DeviceConfigApplyResponse>(
    `/inventory/devices/${deviceId}/config-versions/${versionId}/apply`,
  );
  return resp.data;
}

export function useDeviceConfigVersions(
  deviceId: string | undefined,
  skip = 0,
  limit = 50,
) {
  return useQuery({
    queryKey: ["devices", deviceId, "config-versions", "list", skip, limit],
    queryFn: () => fetchVersions(deviceId!, skip, limit),
    enabled: !!deviceId,
    placeholderData: keepPreviousData,
  });
}

export function useDeviceConfigVersion(
  deviceId: string | undefined,
  versionId: string | undefined,
) {
  return useQuery({
    queryKey: ["devices", deviceId, "config-versions", "detail", versionId],
    queryFn: () => fetchVersion(deviceId!, versionId!),
    enabled: !!deviceId && !!versionId,
  });
}

export function useDeviceConfigDiff(
  deviceId: string | undefined,
  from: string | undefined,
  to: string | undefined,
) {
  return useQuery({
    queryKey: ["devices", deviceId, "config-versions", "diff", from, to],
    queryFn: () => fetchDiff(deviceId!, from!, to!),
    enabled: !!deviceId && !!from && !!to,
  });
}

export function useCreateDeviceConfigVersion(deviceId: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: { config: Record<string, unknown>; description?: string }) =>
      createVersion(deviceId!, body),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["devices", deviceId, "config-versions", "list"],
      });
    },
  });
}

export function useRestoreDeviceConfigVersion(deviceId: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ versionId, body }: { versionId: string; body: { description?: string } }) =>
      restoreVersion(deviceId!, versionId, body),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["devices", deviceId, "config-versions", "list"],
      });
    },
  });
}

export function useApplyDeviceConfigVersion(deviceId: string | undefined) {
  return useMutation({
    mutationFn: (versionId: string) => applyVersion(deviceId!, versionId),
  });
}
