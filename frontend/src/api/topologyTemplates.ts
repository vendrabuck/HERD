import { useMutation, useQuery, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import type { PaginatedResponse } from "@/types/pagination.types";
import type { Topology } from "@/types/topology.types";
import apiClient from "./client";

export interface TopologyTemplate {
  id: string;
  name: string;
  description: string | null;
  created_by: string;
  owner_name: string;
  created_at: string;
  updated_at: string;
}

export interface TopologyTemplateDetail extends TopologyTemplate {
  canvas_data: { nodes: TemplateNode[]; edges: unknown[] } | null;
}

export interface TemplateNode {
  id: string;
  data: { device?: { role?: string }; label?: string };
  [key: string]: unknown;
}

async function listTemplates(skip = 0, limit = 50): Promise<PaginatedResponse<TopologyTemplate>> {
  const resp = await apiClient.get<PaginatedResponse<TopologyTemplate>>(
    "/cabling/templates",
    { params: { skip, limit } },
  );
  return resp.data;
}

async function getTemplate(id: string): Promise<TopologyTemplateDetail> {
  const resp = await apiClient.get<TopologyTemplateDetail>(`/cabling/templates/${id}`);
  return resp.data;
}

async function createFromTopology(
  topologyId: string,
  body: { name: string; description?: string },
): Promise<TopologyTemplateDetail> {
  const resp = await apiClient.post<TopologyTemplateDetail>(
    `/cabling/templates/from-topology/${topologyId}`,
    body,
  );
  return resp.data;
}

async function deleteTemplate(id: string): Promise<void> {
  await apiClient.delete(`/cabling/templates/${id}`);
}

async function instantiateTemplate(
  id: string,
  body: { name: string; role_assignments: Record<string, string> },
): Promise<Topology> {
  const resp = await apiClient.post<Topology>(`/cabling/templates/${id}/instantiate`, body);
  return resp.data;
}

export function useTopologyTemplates(skip = 0, limit = 50) {
  return useQuery({
    queryKey: ["topology-templates", "paginated", skip, limit],
    queryFn: () => listTemplates(skip, limit),
    placeholderData: keepPreviousData,
  });
}

export function useTopologyTemplate(id: string | undefined) {
  return useQuery({
    queryKey: ["topology-templates", id],
    queryFn: () => getTemplate(id!),
    enabled: !!id,
  });
}

export function useCreateTemplateFromTopology() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      topologyId,
      ...body
    }: {
      topologyId: string;
      name: string;
      description?: string;
    }) => createFromTopology(topologyId, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["topology-templates"] }),
  });
}

export function useDeleteTopologyTemplate() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: deleteTemplate,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["topology-templates"] }),
  });
}

export function useInstantiateTemplate() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      ...body
    }: {
      id: string;
      name: string;
      role_assignments: Record<string, string>;
    }) => instantiateTemplate(id, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["topologies"] }),
  });
}
