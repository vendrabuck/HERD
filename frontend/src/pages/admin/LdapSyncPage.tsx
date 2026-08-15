import { Fragment, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import toast from "react-hot-toast";
import { useAuthStore } from "@/stores/authStore";
import {
  RUN_IN_PROGRESS_DETAIL,
  RUN_IN_PROGRESS_REPLICA_DETAIL,
  isRunStale,
  useLdapSyncStatus,
  usePaginatedMappings,
  useCreateMapping,
  useDeleteMapping,
  usePaginatedSyncRuns,
  useStartSyncRun,
} from "@/api/ldapSync";
import { useGroups } from "@/api/groups";
import { errorDetail } from "@/lib/errors";
import { Modal } from "@/components/ui/Modal";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { Pagination } from "@/components/ui/Pagination";
import { StatusBadge } from "@/components/ui/StatusBadge";
import type { LdapGroupMapping, LdapSyncRun } from "@/types/ldapSync.types";

function errorStatusCode(err: unknown): number | undefined {
  return (err as { response?: { status?: number } })?.response?.status;
}

function formatInterval(seconds: number): string {
  if (seconds % 3600 === 0) {
    const hours = seconds / 3600;
    return `${hours} hour${hours === 1 ? "" : "s"}`;
  }
  if (seconds % 60 === 0) {
    const minutes = seconds / 60;
    return `${minutes} minute${minutes === 1 ? "" : "s"}`;
  }
  return `${seconds} seconds`;
}

const STALE_RUNNING_TITLE =
  'This run has been "running" for over 30 minutes and may belong to a crashed process; its counts may not be reliable.';

const limit = 50;

export function LdapSyncPage() {
  const user = useAuthStore((s) => s.user);
  const navigate = useNavigate();
  const isAdmin = user?.role === "admin" || user?.role === "superadmin";

  const { data: status, isLoading: statusLoading, isError: statusError, refetch: refetchStatus } =
    useLdapSyncStatus();
  // Fail-closed default: create/sync-now stay disabled unless the status
  // query has actually confirmed ldap mode. Undefined (loading or errored)
  // and "local" both read as false here; see the comment on
  // useLdapSyncStatus in api/ldapSync.ts for why this hook must never gain
  // placeholderData without revisiting this gate.
  const isLdapMode = status?.auth_method === "ldap";
  const actionsEnabled = isLdapMode;

  const { data: groups } = useGroups();
  const groupName = (id: string) => {
    const match = groups?.find((g) => g.id === id);
    return match ? match.name : id.slice(0, 8) + "...";
  };

  // --- Mappings section ---
  const [mappingsSkip, setMappingsSkip] = useState(0);
  const { data: mappingsPage, isLoading: mappingsLoading } = usePaginatedMappings(
    mappingsSkip,
    limit,
  );
  const mappings = mappingsPage?.items;
  const mappingsTotal = mappingsPage?.total ?? 0;

  const createMapping = useCreateMapping();
  const deleteMapping = useDeleteMapping();

  const [showCreate, setShowCreate] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<LdapGroupMapping | null>(null);
  const [groupDn, setGroupDn] = useState("");
  const [herdGroupId, setHerdGroupId] = useState("");
  const [createWarning, setCreateWarning] = useState<{ groupDn: string; text: string } | null>(
    null,
  );

  // --- Runs section ---
  const [runsSkip, setRunsSkip] = useState(0);
  const { data: runsPage, isLoading: runsLoading } = usePaginatedSyncRuns(runsSkip, limit);
  const runs = runsPage?.items;
  const runsTotal = runsPage?.total ?? 0;
  const [expandedRunId, setExpandedRunId] = useState<string | null>(null);
  const startSyncRun = useStartSyncRun();

  useEffect(() => {
    if (user && !isAdmin) {
      navigate("/topology");
    }
  }, [user, isAdmin, navigate]);

  // Pagination clamp: if the page we're viewing just emptied out from under
  // us (a delete removed the last row on this page) while earlier pages
  // still hold rows, land back on the last page that actually has data
  // instead of showing a stranded "no mappings found" on e.g. page 3 of 2.
  //
  // Adjusted during render (React's documented pattern for state derived
  // from a query result: https://react.dev/learn/you-might-not-need-an-effect),
  // not in a useEffect: comparing against a PREVIOUS-STATE snapshot (not a
  // ref; refs cannot be read or written during render either) only reacts
  // when a NEW mappingsPage object has actually landed, so this cannot
  // cascade into a render loop.
  const [lastSeenMappingsPage, setLastSeenMappingsPage] = useState(mappingsPage);
  if (!mappingsLoading && mappingsPage && mappingsPage !== lastSeenMappingsPage) {
    setLastSeenMappingsPage(mappingsPage);
    if (mappingsPage.items.length === 0 && mappingsPage.total > 0 && mappingsSkip > 0) {
      const lastPageSkip = Math.max(0, Math.floor((mappingsPage.total - 1) / limit) * limit);
      setMappingsSkip(lastPageSkip);
    }
  }

  if (!user || !isAdmin) return null;

  const closeCreateModal = () => {
    setShowCreate(false);
    setGroupDn("");
    setHerdGroupId("");
  };

  const handleCreateMapping = async () => {
    if (!groupDn.trim()) {
      toast.error("Directory group DN is required");
      return;
    }
    if (!herdGroupId) {
      toast.error("HERD group is required");
      return;
    }
    try {
      const submittedDn = groupDn.trim();
      const result = await createMapping.mutateAsync({
        group_dn: submittedDn,
        herd_group_id: herdGroupId,
      });
      if (result.warning) {
        // The memberless-mapping accept-with-warning (ADR 0011): surfaced as
        // a persistent inline banner naming the group DN, not a toast, since
        // a toast vanishes before an admin who isn't watching the screen
        // would read it.
        setCreateWarning({ groupDn: result.group_dn, text: result.warning });
        toast.success("Mapping created with a warning, see below");
      } else {
        // A later successful create (with or without its own warning)
        // supersedes any earlier warning banner still on screen.
        setCreateWarning(null);
        toast.success("Mapping created");
      }
      closeCreateModal();
    } catch (err: unknown) {
      toast.error(errorDetail(err, "Failed to create mapping"));
    }
  };

  const handleDeleteMapping = async () => {
    if (!deleteTarget) return;
    try {
      await deleteMapping.mutateAsync(deleteTarget.id);
      // The warning banner refers to a specific mapping; once it's deleted
      // the warning no longer describes anything on screen.
      setCreateWarning((current) =>
        current && current.groupDn === deleteTarget.group_dn ? null : current,
      );
      toast.success("Mapping deleted");
    } catch (err: unknown) {
      toast.error(errorDetail(err, "Failed to delete mapping"));
    }
    setDeleteTarget(null);
  };

  const handleSyncNow = async () => {
    try {
      await startSyncRun.mutateAsync();
      // The new run is newest-first on page 1; jump there so it's visible
      // and the runs query's polling condition can actually see it.
      setRunsSkip(0);
      toast.success("Sync run started");
    } catch (err: unknown) {
      const detail = errorDetail(err, "Failed to start sync run");
      const statusCode = errorStatusCode(err);
      const isLockBusy =
        statusCode === 409 &&
        (detail === RUN_IN_PROGRESS_DETAIL || detail === RUN_IN_PROGRESS_REPLICA_DETAIL);
      if (isLockBusy) {
        // Informational, not an error: another run already owns the lock.
        toast(detail);
      } else {
        // Any other 409 (e.g. the auth_method != ldap mode refusal) is a
        // real error, not routine contention.
        toast.error(detail);
      }
    }
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="px-6 xl:px-12 2xl:px-16 py-8">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-gray-900">LDAP Directory Sync</h2>
        </div>

        {statusError && (
          <div className="mb-4 px-4 py-3 rounded-lg border border-red-200 bg-red-50 text-sm text-red-700 flex items-center justify-between gap-4">
            <span>Could not load directory sync status.</span>
            <button
              onClick={() => refetchStatus()}
              className="px-3 py-1.5 text-sm font-medium text-red-700 bg-white border border-red-300 rounded hover:bg-red-100 transition-colors"
            >
              Retry
            </button>
          </div>
        )}
        {!statusLoading && !statusError && !isLdapMode && (
          <div className="mb-4 px-4 py-3 rounded-lg border border-gray-200 bg-gray-50 text-sm text-gray-700">
            This deployment uses local authentication; directory sync is inactive. Existing
            mappings can still be viewed and cleaned up below.
          </div>
        )}
        {!statusLoading && !statusError && isLdapMode && status && (
          <p className="mb-4 text-sm text-gray-500">
            {status.group_sync_enabled
              ? `Automatic sync runs every ${formatInterval(status.sync_interval_seconds)}.`
              : "Automatic sync is disabled; runs are manual-only."}
          </p>
        )}

        {/* --- Mappings --- */}
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-base font-semibold text-gray-900">Group mappings</h3>
          <button
            onClick={() => setShowCreate(true)}
            disabled={!actionsEnabled}
            className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            Create mapping
          </button>
        </div>

        {createWarning && (
          <div className="mb-4 px-4 py-3 rounded-lg border border-amber-200 bg-amber-50 text-sm text-amber-800 flex items-start justify-between gap-4">
            <span>
              <span className="font-mono">{createWarning.groupDn}</span>: {createWarning.text}
            </span>
            <button
              onClick={() => setCreateWarning(null)}
              aria-label="Dismiss warning"
              className="text-amber-600 hover:text-amber-800 text-lg leading-none"
            >
              &times;
            </button>
          </div>
        )}

        <div className="bg-white rounded-lg border border-gray-200 overflow-hidden mb-8">
          {mappingsLoading ? (
            <p className="text-sm text-gray-500 px-4 py-4">Loading mappings...</p>
          ) : !mappings || mappings.length === 0 ? (
            <p className="text-sm text-gray-500 px-4 py-4">No group mappings found</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[700px] text-sm text-left">
                <thead className="bg-gray-50 text-gray-600 uppercase text-xs">
                  <tr>
                    <th className="px-4 py-3">Directory group DN</th>
                    <th className="px-4 py-3">Directory name</th>
                    <th className="px-4 py-3">HERD group</th>
                    <th className="px-4 py-3">Created</th>
                    <th className="px-4 py-3">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {mappings.map((m) => (
                    <tr key={m.id} className="hover:bg-gray-50">
                      <td className="px-4 py-3 font-mono text-xs text-gray-600">{m.group_dn}</td>
                      <td className="px-4 py-3 text-gray-600">{m.directory_name}</td>
                      <td className="px-4 py-3 font-medium text-gray-900">
                        {groupName(m.herd_group_id)}
                      </td>
                      <td className="px-4 py-3 text-gray-500">
                        {new Date(m.created_at).toLocaleDateString()}
                      </td>
                      <td className="px-4 py-3">
                        <button
                          onClick={() => setDeleteTarget(m)}
                          className="text-xs text-red-600 hover:text-red-800"
                        >
                          Delete
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <Pagination
            total={mappingsTotal}
            skip={mappingsSkip}
            limit={limit}
            onPageChange={setMappingsSkip}
          />
        </div>

        {/* --- Runs --- */}
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-base font-semibold text-gray-900">Sync runs</h3>
          <button
            onClick={handleSyncNow}
            disabled={!actionsEnabled || startSyncRun.isPending}
            className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {startSyncRun.isPending ? "Starting..." : "Sync now"}
          </button>
        </div>

        <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
          {runsLoading ? (
            <p className="text-sm text-gray-500 px-4 py-4">Loading runs...</p>
          ) : !runs || runs.length === 0 ? (
            <p className="text-sm text-gray-500 px-4 py-4">No sync runs found</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[900px] text-sm text-left">
                <thead className="bg-gray-50 text-gray-600 uppercase text-xs">
                  <tr>
                    <th className="px-4 py-3">Started</th>
                    <th className="px-4 py-3">Trigger</th>
                    <th className="px-4 py-3">Status</th>
                    <th className="px-4 py-3">Provisioned</th>
                    <th className="px-4 py-3">Added</th>
                    <th className="px-4 py-3">Removed</th>
                    <th className="px-4 py-3">Skipped</th>
                    <th className="px-4 py-3">Deactivated</th>
                    <th className="px-4 py-3">Reactivated</th>
                    <th className="px-4 py-3">Error</th>
                    <th className="px-4 py-3">Detail</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {runs.map((run: LdapSyncRun) => {
                    const hasDetail = Object.keys(run.detail ?? {}).length > 0;
                    const expanded = expandedRunId === run.id;
                    const stale = isRunStale(run);
                    return (
                      <Fragment key={run.id}>
                        <tr className="hover:bg-gray-50">
                          <td className="px-4 py-3 text-gray-500">
                            {new Date(run.started_at).toLocaleString()}
                          </td>
                          <td className="px-4 py-3 text-gray-600 capitalize">{run.trigger}</td>
                          <td className="px-4 py-3">
                            <StatusBadge
                              status={run.status}
                              label={stale ? "running (stale)" : undefined}
                              title={stale ? STALE_RUNNING_TITLE : undefined}
                              className={run.status === "running" ? "animate-pulse" : undefined}
                            />
                          </td>
                          <td className="px-4 py-3 text-gray-600">{run.users_provisioned}</td>
                          <td className="px-4 py-3 text-gray-600">{run.members_added}</td>
                          <td className="px-4 py-3 text-gray-600">{run.members_removed}</td>
                          <td className="px-4 py-3 text-gray-600">{run.members_skipped}</td>
                          <td className="px-4 py-3 text-gray-600">{run.users_deactivated}</td>
                          <td className="px-4 py-3 text-gray-600">{run.users_reactivated}</td>
                          <td
                            className="px-4 py-3 text-red-700 max-w-xs truncate"
                            title={run.error ?? undefined}
                          >
                            {run.error ?? "-"}
                          </td>
                          <td className="px-4 py-3">
                            {hasDetail ? (
                              <button
                                onClick={() => setExpandedRunId(expanded ? null : run.id)}
                                className="text-xs text-blue-600 hover:text-blue-800"
                              >
                                {expanded ? "Hide" : "Details"}
                              </button>
                            ) : (
                              <span className="text-xs text-gray-400">-</span>
                            )}
                          </td>
                        </tr>
                        {expanded && (
                          <tr>
                            <td colSpan={11} className="px-4 py-3 bg-gray-50">
                              <div className="overflow-x-auto">
                                <pre className="text-xs text-gray-700 whitespace-pre-wrap">
                                  {JSON.stringify(run.detail, null, 2)}
                                </pre>
                              </div>
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          <Pagination total={runsTotal} skip={runsSkip} limit={limit} onPageChange={setRunsSkip} />
        </div>
      </div>

      <Modal
        open={showCreate}
        onClose={closeCreateModal}
        title="Create mapping"
        className="max-w-lg"
      >
        <div className="space-y-4">
          <div>
            <label htmlFor="ldap-group-dn" className="block text-sm font-medium text-gray-700 mb-1">
              Directory group DN
            </label>
            <input
              id="ldap-group-dn"
              type="text"
              placeholder="cn=herd-eng,ou=groups,dc=company,dc=local"
              value={groupDn}
              onChange={(e) => setGroupDn(e.target.value)}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <div>
            <label htmlFor="ldap-herd-group" className="block text-sm font-medium text-gray-700 mb-1">
              HERD group
            </label>
            <select
              id="ldap-herd-group"
              value={herdGroupId}
              onChange={(e) => setHerdGroupId(e.target.value)}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              <option value="">Select a group</option>
              {groups?.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name}
                </option>
              ))}
            </select>
          </div>
          <div className="flex justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={closeCreateModal}
              className="px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 rounded-lg hover:bg-gray-200 transition-colors"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleCreateMapping}
              disabled={createMapping.isPending}
              className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors"
            >
              {createMapping.isPending ? "Creating..." : "Create"}
            </button>
          </div>
        </div>
      </Modal>

      <ConfirmDialog
        open={!!deleteTarget}
        title="Delete mapping"
        description={`Delete the mapping for "${deleteTarget?.group_dn}"? The HERD group "${
          deleteTarget ? groupName(deleteTarget.herd_group_id) : ""
        }" keeps its current members; it simply stops syncing from the directory.`}
        confirmLabel="Delete"
        destructive
        onConfirm={handleDeleteMapping}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}
