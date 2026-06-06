import { useQuery, useMutation, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import type { DriverPackageInfo, DriverUpdate } from "@/types/driver.types";
import type { PaginatedResponse } from "@/types/pagination.types";
import apiClient from "./client";

async function fetchPaginatedDrivers(skip = 0, limit = 50): Promise<PaginatedResponse<DriverPackageInfo>> {
  const resp = await apiClient.get<PaginatedResponse<DriverPackageInfo>>("/inventory/drivers", {
    params: { skip, limit },
  });
  return resp.data;
}

async function fetchDrivers(): Promise<DriverPackageInfo[]> {
  const resp = await fetchPaginatedDrivers(0, 500);
  return resp.items;
}

async function fetchDriver(id: string): Promise<DriverPackageInfo> {
  const resp = await apiClient.get<DriverPackageInfo>(
    `/inventory/drivers/${id}`,
  );
  return resp.data;
}

async function createDriver({
  name,
  description,
  connection_type,
  file,
}: {
  name: string;
  description?: string;
  connection_type: string;
  file: File;
}): Promise<DriverPackageInfo> {
  const formData = new FormData();
  formData.append("name", name);
  if (description) formData.append("description", description);
  formData.append("connection_type", connection_type);
  formData.append("file", file);
  const resp = await apiClient.post<DriverPackageInfo>(
    "/inventory/drivers",
    formData,
    { headers: { "Content-Type": "multipart/form-data" } },
  );
  return resp.data;
}

async function updateDriver({
  id,
  data,
}: {
  id: string;
  data: DriverUpdate;
}): Promise<DriverPackageInfo> {
  const resp = await apiClient.put<DriverPackageInfo>(
    `/inventory/drivers/${id}`,
    data,
  );
  return resp.data;
}

async function deleteDriver(id: string): Promise<void> {
  await apiClient.delete(`/inventory/drivers/${id}`);
}

async function downloadDriver(id: string): Promise<Blob> {
  const resp = await apiClient.get(`/inventory/drivers/${id}/download`, {
    responseType: "blob",
  });
  return resp.data;
}

export function useDrivers() {
  return useQuery({
    queryKey: ["drivers"],
    queryFn: fetchDrivers,
  });
}

export function usePaginatedDrivers(skip = 0, limit = 50) {
  return useQuery({
    queryKey: ["drivers", "paginated", skip, limit],
    queryFn: () => fetchPaginatedDrivers(skip, limit),
    placeholderData: keepPreviousData,
  });
}

export function useDriver(id: string | undefined) {
  return useQuery({
    queryKey: ["drivers", id],
    queryFn: () => fetchDriver(id!),
    enabled: !!id,
  });
}

export function useCreateDriver() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createDriver,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["drivers"] });
    },
  });
}

export function useUpdateDriver() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: updateDriver,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["drivers"] });
    },
  });
}

export function useDeleteDriver() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: deleteDriver,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["drivers"] });
    },
  });
}

export function useDownloadDriver() {
  return useMutation({
    mutationFn: downloadDriver,
  });
}
