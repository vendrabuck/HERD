import { useQuery, useMutation, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import type {
  DeviceGroup,
  DeviceGroupDetail,
  DeviceGroupCreate,
  DeviceGroupUpdate,
  DeviceGroupMembership,
  BulkResult,
  BulkRemoveResult,
} from "@/types/deviceGroup.types";
import type { PaginatedResponse } from "@/types/pagination.types";
import apiClient from "./client";

async function fetchPaginatedDeviceGroups(skip = 0, limit = 50): Promise<PaginatedResponse<DeviceGroup>> {
  const resp = await apiClient.get<PaginatedResponse<DeviceGroup>>("/inventory/device-groups", {
    params: { skip, limit },
  });
  return resp.data;
}

async function fetchDeviceGroups(): Promise<DeviceGroup[]> {
  const resp = await fetchPaginatedDeviceGroups(0, 500);
  return resp.items;
}

async function fetchDeviceGroup(id: string): Promise<DeviceGroupDetail> {
  const resp = await apiClient.get<DeviceGroupDetail>(`/inventory/device-groups/${id}`);
  return resp.data;
}

async function createDeviceGroup(data: DeviceGroupCreate): Promise<DeviceGroup> {
  const resp = await apiClient.post<DeviceGroup>("/inventory/device-groups", data);
  return resp.data;
}

async function updateDeviceGroup({
  id,
  data,
}: {
  id: string;
  data: DeviceGroupUpdate;
}): Promise<DeviceGroup> {
  const resp = await apiClient.put<DeviceGroup>(`/inventory/device-groups/${id}`, data);
  return resp.data;
}

async function deleteDeviceGroup(id: string): Promise<void> {
  await apiClient.delete(`/inventory/device-groups/${id}`);
}

async function bulkAddDevices({
  groupId,
  deviceIds,
}: {
  groupId: string;
  deviceIds: string[];
}): Promise<BulkResult> {
  const resp = await apiClient.post<BulkResult>(
    `/inventory/device-groups/${groupId}/devices/bulk`,
    { device_ids: deviceIds }
  );
  return resp.data;
}

async function bulkRemoveDevices({
  groupId,
  deviceIds,
}: {
  groupId: string;
  deviceIds: string[];
}): Promise<BulkRemoveResult> {
  const resp = await apiClient.post<BulkRemoveResult>(
    `/inventory/device-groups/${groupId}/devices/bulk-remove`,
    { device_ids: deviceIds }
  );
  return resp.data;
}

async function bulkAddUserGroups({
  groupId,
  userGroupIds,
}: {
  groupId: string;
  userGroupIds: string[];
}): Promise<BulkResult> {
  const resp = await apiClient.post<BulkResult>(
    `/inventory/device-groups/${groupId}/permissions/bulk`,
    { user_group_ids: userGroupIds }
  );
  return resp.data;
}

async function bulkRemoveUserGroups({
  groupId,
  userGroupIds,
}: {
  groupId: string;
  userGroupIds: string[];
}): Promise<BulkRemoveResult> {
  const resp = await apiClient.post<BulkRemoveResult>(
    `/inventory/device-groups/${groupId}/permissions/bulk-remove`,
    { user_group_ids: userGroupIds }
  );
  return resp.data;
}

async function fetchDeviceGroupsForDevice(deviceId: string): Promise<DeviceGroupMembership[]> {
  const resp = await apiClient.get<DeviceGroupMembership[]>(
    `/inventory/device-groups/device/${deviceId}`
  );
  return resp.data;
}

export function useDeviceGroupsForDevice(deviceId: string | undefined) {
  return useQuery({
    queryKey: ["deviceGroups", "device", deviceId],
    queryFn: () => fetchDeviceGroupsForDevice(deviceId!),
    enabled: !!deviceId,
  });
}

export function useDeviceGroups() {
  return useQuery({
    queryKey: ["deviceGroups"],
    queryFn: fetchDeviceGroups,
  });
}

export function usePaginatedDeviceGroups(skip = 0, limit = 50) {
  return useQuery({
    queryKey: ["deviceGroups", "paginated", skip, limit],
    queryFn: () => fetchPaginatedDeviceGroups(skip, limit),
    placeholderData: keepPreviousData,
  });
}

export function useDeviceGroup(id: string | undefined) {
  return useQuery({
    queryKey: ["deviceGroups", id],
    queryFn: () => fetchDeviceGroup(id!),
    enabled: !!id,
  });
}

export function useCreateDeviceGroup() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createDeviceGroup,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["deviceGroups"] }),
  });
}

export function useUpdateDeviceGroup() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: updateDeviceGroup,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["deviceGroups"] }),
  });
}

export function useDeleteDeviceGroup() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: deleteDeviceGroup,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["deviceGroups"] }),
  });
}

export function useBulkAddDevices() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: bulkAddDevices,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["deviceGroups"] }),
  });
}

export function useBulkRemoveDevices() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: bulkRemoveDevices,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["deviceGroups"] }),
  });
}

export function useBulkAddUserGroups() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: bulkAddUserGroups,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["deviceGroups"] }),
  });
}

export function useBulkRemoveUserGroups() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: bulkRemoveUserGroups,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["deviceGroups"] }),
  });
}
