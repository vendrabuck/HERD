import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import type { Port, PortCreate, PortUpdate, BulkPortCreate } from "@/types/port.types";
import apiClient from "./client";

export async function fetchPorts(deviceId: string): Promise<Port[]> {
  const resp = await apiClient.get<Port[]>(
    `/inventory/devices/${deviceId}/ports`,
  );
  return resp.data;
}

async function createPort({
  deviceId,
  data,
}: {
  deviceId: string;
  data: PortCreate;
}): Promise<Port> {
  const resp = await apiClient.post<Port>(
    `/inventory/devices/${deviceId}/ports`,
    data,
  );
  return resp.data;
}

async function updatePort({
  portId,
  data,
}: {
  portId: string;
  data: PortUpdate;
}): Promise<Port> {
  const resp = await apiClient.put<Port>(`/inventory/ports/${portId}`, data);
  return resp.data;
}

async function deletePort(portId: string): Promise<void> {
  await apiClient.delete(`/inventory/ports/${portId}`);
}

async function createPortsBulk({
  deviceId,
  data,
}: {
  deviceId: string;
  data: BulkPortCreate;
}): Promise<Port[]> {
  const resp = await apiClient.post<Port[]>(
    `/inventory/devices/${deviceId}/ports/bulk`,
    data,
  );
  return resp.data;
}

export function usePorts(deviceId: string | undefined) {
  return useQuery({
    queryKey: ["ports", deviceId],
    queryFn: () => fetchPorts(deviceId!),
    enabled: !!deviceId,
  });
}

export function useCreatePort() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createPort,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["ports"] }),
  });
}

export function useUpdatePort() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: updatePort,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["ports"] }),
  });
}

export function useCreatePortsBulk() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createPortsBulk,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["ports"] }),
  });
}

export function useDeletePort() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: deletePort,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["ports"] }),
  });
}
