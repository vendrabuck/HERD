import { useQuery, useMutation, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import type { Node } from "@xyflow/react";
import type { Device, DeviceCreate, DeviceUpdate, DeviceFilters } from "@/types/device.types";
import type { PaginatedResponse } from "@/types/pagination.types";
import type { CanvasData, DeviceNodeData } from "@/types/topology.types";
import apiClient from "./client";

// Query-key factory. Splits the "devices" namespace into list queries (paginated,
// all-names, simple filtered) and detail queries (per-id). Mutations invalidate
// lists as a group while patching the detail cache surgically.
const deviceKeys = {
  all: ["devices"] as const,
  lists: () => [...deviceKeys.all, "list"] as const,
  paginated: (filters: DeviceFilters | undefined, skip: number, limit: number) =>
    [...deviceKeys.lists(), "paginated", filters, skip, limit] as const,
  simpleList: (filters: DeviceFilters | undefined) =>
    [...deviceKeys.lists(), "simple", filters] as const,
  allList: (filters: DeviceFilters | undefined) =>
    [...deviceKeys.lists(), "all", filters] as const,
  allNames: () => [...deviceKeys.lists(), "all-names"] as const,
  // Sorted so the key is order-independent: the same id set from two callers
  // shares one cache entry.
  byIds: (ids: string[]) => [...deviceKeys.lists(), "by-ids", [...ids].sort()] as const,
  details: () => [...deviceKeys.all, "detail"] as const,
  detail: (id: string) => [...deviceKeys.details(), id] as const,
};

async function fetchPaginatedDevices(
  filters?: DeviceFilters,
  skip = 0,
  limit = 50,
): Promise<PaginatedResponse<Device>> {
  const params: Record<string, string | number> = { skip, limit };
  if (filters?.template_id) params.template_id = filters.template_id;
  if (filters?.topology_type) params.topology_type = filters.topology_type;
  if (filters?.status) params.status = filters.status;
  if (filters?.dut_only) params.dut_only = "true";
  if (filters?.search) params.search = filters.search;
  const resp = await apiClient.get<PaginatedResponse<Device>>("/inventory/devices", { params });
  return resp.data;
}

// Page 0 of the server's 500-row cap, `total` dropped. This is the repo-wide
// "simple list" convention (admin.ts, groups.ts, templates.ts, and others use
// the same shape) and it is only correct while the collection fits in one
// page. Anything that must see EVERY device goes through fetchAllDevices (the
// page walker below) or fetchDevicesByIds (the batch endpoint); issue #703
// moved the device consumers there and left the other modules to a follow-up.
async function fetchDevices(filters?: DeviceFilters): Promise<Device[]> {
  const resp = await fetchPaginatedDevices(filters, 0, 500);
  return resp.items;
}

export async function fetchDevice(id: string): Promise<Device> {
  const resp = await apiClient.get<Device>(`/inventory/devices/${id}`);
  return resp.data;
}

// The server caps a batch-fetch request at 500 ids (422 beyond); chunk larger
// canvases into multiple requests rather than tripping the cap.
const DEVICE_BATCH_LIMIT = 500;

// Fetch many devices in one round-trip. Ids the caller cannot see, or that no
// longer exist, are omitted from the response (the server never fails the
// whole batch over one id), matching how the old per-id GET's 404 was treated.
async function fetchDevicesBatch(ids: string[]): Promise<Device[]> {
  const resp = await apiClient.post<{ items: Device[] }>("/inventory/devices/batch", {
    device_ids: ids,
  });
  return resp.data.items;
}

// Fetch an arbitrary id set through the batch endpoint, chunked past the
// server cap so the caller never trips it. Ids are deduplicated first; an id
// the server omits (deleted, or not visible to this user) is simply absent
// from the result. Rejects if ANY chunk fails: a partial index is exactly the
// silent-truncation defect this helper exists to prevent (issue #703), so a
// consumer sees an error state rather than a quietly incomplete map.
export async function fetchDevicesByIds(ids: string[]): Promise<Device[]> {
  const unique = Array.from(new Set(ids));
  if (unique.length === 0) return [];
  const chunks: string[][] = [];
  for (let i = 0; i < unique.length; i += DEVICE_BATCH_LIMIT) {
    chunks.push(unique.slice(i, i + DEVICE_BATCH_LIMIT));
  }
  const results = await Promise.all(chunks.map(fetchDevicesBatch));
  return results.flat();
}

// Re-fetch each referenced device and rebuild the canvas nodes' device payload
// from inventory, so a topology loaded from persisted data is resilient to thin
// seeded nodes ({ device: { id } } with no name/topology_type) and to devices
// that were renamed/retyped after the canvas was saved. Defense in depth: the
// seed now writes full payloads, but persisted/stale canvases predate that.
//
// A node whose device was deleted or is not visible (omitted from the batch
// response) is kept as-is rather than dropped, so the editor still shows it
// and validation can flag it as missing. A node with no device id (an
// intentional empty/missing-device slot) is left untouched. All unique ids
// travel in a single POST /inventory/devices/batch (chunked past the server
// cap) instead of one GET per device, and a failed fetch never throws: every
// node just keeps its existing data.
export async function hydrateCanvasNodes(data: CanvasData): Promise<CanvasData> {
  const nodes = data.nodes ?? [];

  const ids = Array.from(
    new Set(
      nodes
        .map((n) => (n.data as DeviceNodeData | undefined)?.device?.id)
        .filter((id): id is string => typeof id === "string" && id.length > 0),
    ),
  );
  if (ids.length === 0) return data;

  // A failed batch request (outage, 5xx) hydrates nothing but never throws,
  // so every node just keeps its existing data, same as the old per-device
  // catch behavior. The chunking itself lives in fetchDevicesByIds.
  const fetched = await fetchDevicesByIds(ids).catch((): Device[] => []);
  const fresh = new Map<string, Device>();
  for (const device of fetched) fresh.set(device.id, device);

  const hydratedNodes = nodes.map((node) => {
    const nodeData = node.data as DeviceNodeData | undefined;
    const id = nodeData?.device?.id;
    if (!id) return node;
    // Any node backed by a device must render through the custom DeviceNode, so
    // it needs type "deviceNode". Topologies persisted before the seed fix (or
    // any thin save) carry type null, which makes React Flow fall back to its
    // blank default node. Set the discriminator here even when the live device
    // fetch fails, so the node still renders as a DeviceNode with its existing
    // data rather than a blank box.
    const device = fresh.get(id);
    if (!device) return { ...node, type: "deviceNode" } as Node<DeviceNodeData>;
    return {
      ...node,
      type: "deviceNode",
      data: {
        ...nodeData,
        device,
        label: device.name,
        topologyType: device.topology_type,
      },
    } as Node<DeviceNodeData>;
  });

  return { ...data, nodes: hydratedNodes };
}

async function updateDevice({ id, data }: { id: string; data: DeviceUpdate }): Promise<Device> {
  const resp = await apiClient.put<Device>(`/inventory/devices/${id}`, data);
  return resp.data;
}

async function createDevice(data: DeviceCreate): Promise<Device> {
  const resp = await apiClient.post<Device>("/inventory/devices", data);
  return resp.data;
}

// Walk every page of /inventory/devices (server-side filters honored) and
// return the full row set, so a consumer that must see the whole collection
// is not silently cut at the 500-row page cap (issue #703). Exit conditions
// (in order): short page from the server, or skip meeting/exceeding the
// reported total. A MAX_PAGES cap prevents an infinite loop if the server
// ever returns overlapping pages (e.g., from a mid-fetch write).
export async function fetchAllDevices(filters?: DeviceFilters): Promise<Device[]> {
  const all: Device[] = [];
  const limit = 500;
  const MAX_PAGES = 200;
  let skip = 0;
  for (let page = 0; page < MAX_PAGES; page++) {
    const resp = await fetchPaginatedDevices(filters, skip, limit);
    all.push(...resp.items);
    if (resp.items.length === 0) break;
    skip += resp.items.length;
    if (resp.items.length < limit) break;
    if (skip >= resp.total) break;
  }
  return all;
}

// Id to name map over the whole (unfiltered) collection.
async function fetchAllDeviceNames(): Promise<Map<string, string>> {
  const map = new Map<string, string>();
  for (const d of await fetchAllDevices()) map.set(d.id, d.name);
  return map;
}

export function useAllDeviceNames() {
  return useQuery({
    queryKey: deviceKeys.allNames(),
    queryFn: fetchAllDeviceNames,
    staleTime: 5 * 60 * 1000,
  });
}

// Every device matching `filters`, across all pages. Use this (not useDevices)
// wherever the list feeds a picker or an index that must be complete.
export function useAllDevices(filters?: DeviceFilters) {
  return useQuery({
    queryKey: deviceKeys.allList(filters),
    queryFn: () => fetchAllDevices(filters),
  });
}

// The devices behind a known id set, via the batch endpoint. Disabled (no
// request, `data` undefined) while `ids` is empty.
export function useDevicesByIds(ids: string[]) {
  return useQuery({
    queryKey: deviceKeys.byIds(ids),
    queryFn: () => fetchDevicesByIds(ids),
    enabled: ids.length > 0,
  });
}

export function useDevice(id: string | undefined) {
  return useQuery({
    queryKey: id ? deviceKeys.detail(id) : deviceKeys.details(),
    queryFn: () => fetchDevice(id!),
    enabled: !!id,
  });
}

export function useUpdateDevice() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: updateDevice,
    onSuccess: (updated) => {
      // Patch the detail cache with the server's response rather than invalidating
      // it, so open detail views update instantly and skip a network round-trip.
      queryClient.setQueryData(deviceKeys.detail(updated.id), updated);
      // Lists (paginated, simple, all-names) may reflect name/status changes.
      queryClient.invalidateQueries({ queryKey: deviceKeys.lists() });
    },
  });
}

export function useDevices(filters?: DeviceFilters) {
  return useQuery({
    queryKey: deviceKeys.simpleList(filters),
    queryFn: () => fetchDevices(filters),
  });
}

export function usePaginatedDevices(filters?: DeviceFilters, skip = 0, limit = 50) {
  return useQuery({
    queryKey: deviceKeys.paginated(filters, skip, limit),
    queryFn: () => fetchPaginatedDevices(filters, skip, limit),
    placeholderData: keepPreviousData,
  });
}

// Issue #391: inventory's DELETE /devices/{id} 409s with a structured
// {error: "device_in_use", reservation_ids: [...]} detail (no `message` key,
// unlike the fork-save conflict shape), so the generic `detail` string
// extraction elsewhere would toast an unreadable object. Callers pass the
// caught error's response detail through this to get a plain string instead.
export function deleteDeviceErrorMessage(err: unknown): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && (detail as { error?: string }).error === "device_in_use") {
    return "Device is held by an active reservation and cannot be deleted";
  }
  return "Failed to delete device";
}

export function useDeleteDevice() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) => {
      await apiClient.delete(`/inventory/devices/${id}`);
      return id;
    },
    onSuccess: (deletedId) => {
      queryClient.removeQueries({ queryKey: deviceKeys.detail(deletedId) });
      queryClient.invalidateQueries({ queryKey: deviceKeys.lists() });
    },
  });
}

export function useCreateDevice() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createDevice,
    onSuccess: (created) => {
      // Seed the detail cache so an immediate navigate-to-detail is instant.
      queryClient.setQueryData(deviceKeys.detail(created.id), created);
      queryClient.invalidateQueries({ queryKey: deviceKeys.lists() });
    },
  });
}
