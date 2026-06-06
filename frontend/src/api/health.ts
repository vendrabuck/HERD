import { useQuery } from "@tanstack/react-query";
import apiClient from "./client";

export type HealthStatus = "UNKNOWN" | "HEALTHY" | "DEGRADED" | "UNREACHABLE";

export interface DeviceHealthSnapshot {
  device_id: string;
  last_polled_at: string | null;
  last_status: HealthStatus;
  last_run_id: string | null;
  consecutive_failures: number;
  next_poll_at: string | null;
}

export interface PaginatedDeviceHealth {
  items: DeviceHealthSnapshot[];
  total: number;
  skip: number;
  limit: number;
}

async function fetchDeviceHealth(deviceId: string): Promise<DeviceHealthSnapshot> {
  const resp = await apiClient.get<DeviceHealthSnapshot>(
    `/execution/device-health/${deviceId}`,
  );
  return resp.data;
}

export function useDeviceHealth(deviceId: string | null | undefined, enabled = true) {
  return useQuery({
    queryKey: ["execution", "device-health", deviceId],
    queryFn: () => fetchDeviceHealth(deviceId!),
    enabled: !!deviceId && enabled,
    // Snapshot updates land via the next poll tick (default 30s);
    // keeping data fresh for a minute is a reasonable compromise
    // between staleness and request volume.
    staleTime: 60_000,
  });
}

interface ListParams {
  skip?: number;
  limit?: number;
  last_status?: HealthStatus;
}

async function fetchDeviceHealthList(params: ListParams): Promise<PaginatedDeviceHealth> {
  const resp = await apiClient.get<PaginatedDeviceHealth>("/execution/device-health", {
    params,
  });
  return resp.data;
}

export function useDeviceHealthList(params: ListParams = {}) {
  return useQuery({
    queryKey: ["execution", "device-health", "list", params],
    queryFn: () => fetchDeviceHealthList(params),
    staleTime: 60_000,
  });
}
