import { useMutation, useQuery, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import type { PaginatedResponse } from "@/types/pagination.types";
import type {
  LdapGroupMapping,
  LdapGroupMappingCreate,
  LdapGroupMappingCreateResult,
  LdapSyncRun,
  LdapSyncRunStart,
  LdapSyncStatus,
} from "@/types/ldapSync.types";
import apiClient from "./client";

// Pinned 409 detail strings from services/auth/app/routers/ldap_sync.py's
// _RUN_IN_PROGRESS_DETAIL / _RUN_IN_PROGRESS_REPLICA_DETAIL. The route
// overloads 409 for both "a run is already in progress" (informational,
// nothing is wrong) and the auth_method != "ldap" mode refusal (a real
// error); there is no separate error code to switch on, so the caller
// matches the exact backend copy. No shared schema generates these today;
// if the backend strings ever change, update both sides by hand.
export const RUN_IN_PROGRESS_DETAIL = "A sync run is already in progress";
export const RUN_IN_PROGRESS_REPLICA_DETAIL = "A sync run is already in progress on another replica";

// A "running" row older than this almost certainly belongs to a crashed
// process: execute_run's finally always finalizes the row (success/partial/
// aborted/failed) on any clean exit, including cancellation during service
// shutdown (ADR 0011 phase 5). Past this age, stop polling it forever
// instead of hammering the API on a row that will never terminate on its
// own, and flag it in the badge so an admin investigates rather than trusts
// the count fields as live.
export const STALE_RUNNING_THRESHOLD_MS = 30 * 60 * 1000;

export function isRunStale(run: LdapSyncRun, now: number = Date.now()): boolean {
  if (run.status !== "running") return false;
  return now - new Date(run.started_at).getTime() > STALE_RUNNING_THRESHOLD_MS;
}

// Polls while any run on the current page is still actively "running" (and
// not stale), following the per-job refetchInterval pattern in
// deviceConfigJobs.ts's useApplyJob: once every listed run has reached a
// terminal status, or the only running row is stale, polling stops on its
// own rather than running on a flat interval forever. Exported so it can be
// unit-tested directly without standing up the full query machinery.
export function runsRefetchInterval(
  page: PaginatedResponse<LdapSyncRun> | undefined,
): number | false {
  if (!page) return false;
  const anyActivelyRunning = page.items.some((run) => run.status === "running" && !isRunStale(run));
  return anyActivelyRunning ? 2000 : false;
}

async function fetchStatus(): Promise<LdapSyncStatus> {
  const resp = await apiClient.get<LdapSyncStatus>("/auth/admin/ldap-sync/status");
  return resp.data;
}

async function fetchPaginatedMappings(
  skip = 0,
  limit = 50,
): Promise<PaginatedResponse<LdapGroupMapping>> {
  const resp = await apiClient.get<PaginatedResponse<LdapGroupMapping>>(
    "/auth/admin/ldap-sync/mappings",
    { params: { skip, limit } },
  );
  return resp.data;
}

async function createMapping(
  data: LdapGroupMappingCreate,
): Promise<LdapGroupMappingCreateResult> {
  const resp = await apiClient.post<LdapGroupMappingCreateResult>(
    "/auth/admin/ldap-sync/mappings",
    data,
  );
  return resp.data;
}

async function deleteMapping(id: string): Promise<void> {
  await apiClient.delete(`/auth/admin/ldap-sync/mappings/${id}`);
}

async function fetchPaginatedRuns(skip = 0, limit = 50): Promise<PaginatedResponse<LdapSyncRun>> {
  const resp = await apiClient.get<PaginatedResponse<LdapSyncRun>>("/auth/admin/ldap-sync/runs", {
    params: { skip, limit },
  });
  return resp.data;
}

async function startSyncRun(): Promise<LdapSyncRunStart> {
  const resp = await apiClient.post<LdapSyncRunStart>("/auth/admin/ldap-sync/run");
  return resp.data;
}

// NOTE: this hook must not gain `placeholderData` (e.g. keepPreviousData)
// without revisiting every fail-closed gate derived from it. The page's
// actionsEnabled is `status?.auth_method === "ldap"`, deliberately with no
// isLoading branch: while the query has no data yet, that expression is
// already false (undefined !== "ldap"). placeholderData would carry a
// STALE auth_method across a refetch (e.g. right after a config change)
// and could enable create/sync-now against a mode that no longer applies.
export function useLdapSyncStatus() {
  return useQuery({
    queryKey: ["ldap-sync", "status"],
    queryFn: fetchStatus,
  });
}

export function usePaginatedMappings(skip = 0, limit = 50) {
  return useQuery({
    queryKey: ["ldap-sync", "mappings", skip, limit],
    queryFn: () => fetchPaginatedMappings(skip, limit),
    placeholderData: keepPreviousData,
  });
}

export function useCreateMapping() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createMapping,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["ldap-sync", "mappings"] }),
  });
}

export function useDeleteMapping() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: deleteMapping,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["ldap-sync", "mappings"] }),
  });
}

export function usePaginatedSyncRuns(skip = 0, limit = 50) {
  return useQuery({
    queryKey: ["ldap-sync", "runs", skip, limit],
    queryFn: () => fetchPaginatedRuns(skip, limit),
    placeholderData: keepPreviousData,
    refetchInterval: (query) =>
      runsRefetchInterval(query.state.data as PaginatedResponse<LdapSyncRun> | undefined),
  });
}

export function useStartSyncRun() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: startSyncRun,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["ldap-sync", "runs"] }),
  });
}
