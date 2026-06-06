import { useQuery, useMutation, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import type {
  DeviceTemplate,
  TemplateCreate,
  TemplateUpdate,
} from "@/types/template.types";
import type { PaginatedResponse } from "@/types/pagination.types";
import apiClient from "./client";

async function fetchPaginatedTemplates(
  templateType?: string,
  skip = 0,
  limit = 50,
): Promise<PaginatedResponse<DeviceTemplate>> {
  const params: Record<string, string | number> = { skip, limit };
  if (templateType) params.template_type = templateType;
  const resp = await apiClient.get<PaginatedResponse<DeviceTemplate>>("/inventory/templates", {
    params,
  });
  return resp.data;
}

async function fetchTemplates(
  templateType?: string,
): Promise<DeviceTemplate[]> {
  const resp = await fetchPaginatedTemplates(templateType, 0, 500);
  return resp.items;
}

async function fetchTemplate(id: string): Promise<DeviceTemplate> {
  const resp = await apiClient.get<DeviceTemplate>(`/inventory/templates/${id}`);
  return resp.data;
}

async function createTemplate(data: TemplateCreate): Promise<DeviceTemplate> {
  const resp = await apiClient.post<DeviceTemplate>("/inventory/templates", data);
  return resp.data;
}

async function updateTemplate({
  id,
  data,
}: {
  id: string;
  data: TemplateUpdate;
}): Promise<DeviceTemplate> {
  const resp = await apiClient.put<DeviceTemplate>(
    `/inventory/templates/${id}`,
    data,
  );
  return resp.data;
}

async function deleteTemplate(id: string): Promise<void> {
  await apiClient.delete(`/inventory/templates/${id}`);
}

export function useTemplates(templateType?: string) {
  return useQuery({
    queryKey: ["templates", templateType ?? "all"],
    queryFn: () => fetchTemplates(templateType),
  });
}

export function usePaginatedTemplates(templateType?: string, skip = 0, limit = 50) {
  return useQuery({
    queryKey: ["templates", "paginated", templateType ?? "all", skip, limit],
    queryFn: () => fetchPaginatedTemplates(templateType, skip, limit),
    placeholderData: keepPreviousData,
  });
}

export function useTemplate(id: string | undefined) {
  return useQuery({
    queryKey: ["templates", id],
    queryFn: () => fetchTemplate(id!),
    enabled: !!id,
  });
}

export function useCreateTemplate() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createTemplate,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["templates"] }),
  });
}

export function useUpdateTemplate() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: updateTemplate,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["templates"] }),
  });
}

export function useDeleteTemplate() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: deleteTemplate,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["templates"] }),
  });
}
