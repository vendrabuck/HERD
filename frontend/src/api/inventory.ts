import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import type { Device, DeviceCreate, DeviceFilters } from "@/types/device.types";
import apiClient from "./client";

async function fetchDevices(filters?: DeviceFilters): Promise<Device[]> {
  const params: Record<string, string> = {};
  if (filters?.device_type) params.device_type = filters.device_type;
  if (filters?.topology_type) params.topology_type = filters.topology_type;
  if (filters?.status) params.status = filters.status;
  const resp = await apiClient.get<Device[]>("/inventory/devices", { params });
  return resp.data;
}

async function createDevice(data: DeviceCreate): Promise<Device> {
  const resp = await apiClient.post<Device>("/inventory/devices", data);
  return resp.data;
}

export function useDevices(filters?: DeviceFilters) {
  return useQuery({
    queryKey: ["devices", filters],
    queryFn: () => fetchDevices(filters),
  });
}

export function useCreateDevice() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createDevice,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["devices"] }),
  });
}
