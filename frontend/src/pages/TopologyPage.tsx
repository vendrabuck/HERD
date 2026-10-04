import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import toast from "react-hot-toast";
import { ChevronDown, ChevronUp } from "lucide-react";
import {
  usePaginatedTopologies,
  useBulkDeleteTopologies,
  useCreateTopology,
  useDeleteTopology,
  useCloneTopology,
} from "@/api/topologies";
import type { TopologyListQuery } from "@/api/topologies";
import { useAuthStore } from "@/stores/authStore";
import { usePreferencesStore } from "@/stores/preferencesStore";
import { errorDetail } from "@/lib/errors";
import { applySettled } from "@/lib/reservationBulk";
import {
  BULK_DELETE_KEEP_LABEL,
  BULK_DELETE_TITLE,
  bulkDeleteConfirmLabel,
  bulkDeleteDescription,
  canDeleteTopologyAs,
  noneDeletableMessage,
  partitionTopologies,
  summarizeDelete,
} from "@/lib/topologyBulk";
import {
  DEFAULT_TOPOLOGY_SORT,
  TOPOLOGY_SORT_PAGE_KEY,
  nextTopologySort,
  parseSavedTopologyFilter,
  parseTopologySort,
  serializeTopologyFilter,
  type TopologyFilterState,
  type TopologyOwnerFilter,
  type TopologySortField,
} from "@/lib/topologyFilters";
import { FilterSelect, ListFilterLayout, ListFilterPanel } from "@/components/ui/ListFilterPanel";
import { Modal } from "@/components/ui/Modal";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { Pagination } from "@/components/ui/Pagination";
import { BulkImportExport } from "@/components/ui/BulkImportExport";
import { exportTopologies, importTopologies } from "@/api/bulk";
import type { Topology } from "@/types/topology.types";

const TH_CLASS = "px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase tracking-wide";

function SortableHeader({
  label,
  field,
  active,
  direction,
  onSort,
}: {
  label: string;
  field: TopologySortField;
  active: boolean;
  direction: "asc" | "desc";
  onSort: (field: TopologySortField) => void;
}) {
  return (
    <th
      scope="col"
      aria-sort={active ? (direction === "asc" ? "ascending" : "descending") : "none"}
      className={TH_CLASS}
    >
      <button
        type="button"
        onClick={() => onSort(field)}
        className="flex items-center gap-1 uppercase hover:text-gray-900"
      >
        {label}
        {active &&
          (direction === "asc" ? (
            <ChevronUp className="w-3 h-3" aria-hidden="true" />
          ) : (
            <ChevronDown className="w-3 h-3" aria-hidden="true" />
          ))}
      </button>
    </th>
  );
}

function TopologyRow({
  topology,
  onDelete,
  onClone,
  canDelete,
  selected,
  onToggleSelected,
  failure,
}: {
  topology: Topology;
  onDelete: (id: string) => void;
  onClone: (topology: Topology) => void;
  canDelete: boolean;
  selected: boolean;
  onToggleSelected: () => void;
  /** Set after a bulk delete refused this row: the server's reason, or null when unknown. */
  failure: string | null | undefined;
}) {
  const navigate = useNavigate();
  const created = new Date(topology.created_at).toLocaleDateString();
  const updated = new Date(topology.updated_at).toLocaleDateString();

  return (
    <tr
      className={`border-b border-gray-100 hover:bg-gray-50 cursor-pointer ${selected ? "bg-blue-50" : ""}`}
      onClick={() => navigate(`/topology/${topology.id}`)}
    >
      <td className="px-4 py-3 w-8" onClick={(e) => e.stopPropagation()}>
        <input
          type="checkbox"
          checked={selected}
          onChange={onToggleSelected}
          aria-label={`Select topology ${topology.name}`}
          className="h-4 w-4 rounded border-gray-300"
        />
      </td>
      <td className="px-4 py-3 text-sm font-medium text-blue-600 hover:text-blue-800">
        {topology.name}
        {failure !== undefined && (
          <span className="block text-xs font-normal text-red-600">
            Not deleted{failure ? `: ${failure}` : ""}
          </span>
        )}
      </td>
      <td className="px-4 py-3 text-sm text-gray-500">{topology.owner_name || topology.created_by.slice(0, 8)}</td>
      <td className="px-4 py-3 text-sm text-gray-500 tabular-nums">{created}</td>
      <td className="px-4 py-3 text-sm text-gray-500 tabular-nums">{updated}</td>
      <td className="px-4 py-3 text-sm">
        <div className="flex gap-1 justify-end">
          <button
            onClick={(e) => {
              e.stopPropagation();
              onClone(topology);
            }}
            className="text-xs text-blue-600 hover:text-blue-800 px-2 py-1 rounded hover:bg-blue-50"
          >
            Clone
          </button>
          {canDelete && (
            <button
              onClick={(e) => {
                e.stopPropagation();
                onDelete(topology.id);
              }}
              aria-label={`Delete topology ${topology.name}`}
              className="text-xs text-red-600 hover:text-red-800 px-2 py-1 rounded hover:bg-red-50"
            >
              Delete
            </button>
          )}
        </div>
      </td>
    </tr>
  );
}

const NO_SELECTION: ReadonlySet<string> = new Set();
const NO_FAILURES: ReadonlyMap<string, string | null> = new Map();

export function TopologyPage() {
  const user = useAuthStore((s) => s.user);
  const queryClient = useQueryClient();
  const createTopology = useCreateTopology();
  const deleteTopology = useDeleteTopology();
  const cloneTopology = useCloneTopology();
  const navigate = useNavigate();

  const [skip, setSkip] = useState(0);
  const limit = 50;

  // Filters (issue #958), persisted in savedFilters.topologies and read only
  // through parseSavedTopologyFilter, so a stale saved owner falls back to All.
  // null means the user has not touched the control yet, so the saved value
  // shows; the InventoryPage pattern.
  const savedRaw = usePreferencesStore((s) => s.savedFilters.topologies);
  const stored = useMemo(() => parseSavedTopologyFilter(savedRaw), [savedRaw]);
  const setSavedFilter = usePreferencesStore((s) => s.setSavedFilter);
  const [userSearch, setUserSearch] = useState<string | null>(null);
  const searchInput = userSearch ?? stored.search;
  const [debouncedSearch, setDebouncedSearch] = useState<string | null>(null);
  const appliedSearch = debouncedSearch ?? stored.search;
  const [userOwner, setUserOwner] = useState<TopologyOwnerFilter | null>(null);
  const owner = userOwner ?? stored.owner;

  // Every persisted write carries the whole object, so whichever lands last
  // holds every field.
  const latestRef = useRef<TopologyFilterState>({ search: appliedSearch, owner });
  useEffect(() => {
    latestRef.current = { search: appliedSearch, owner };
  }, [appliedSearch, owner]);
  const persistFilters = (patch: Partial<TopologyFilterState>) => {
    latestRef.current = { ...latestRef.current, ...patch };
    setSavedFilter("topologies", serializeTopologyFilter(latestRef.current));
  };

  useEffect(() => {
    // Nothing to debounce until the user types something different from what
    // is applied (the InventoryPage guard against a stray mount timer).
    if (userSearch === null || userSearch === appliedSearch) return;
    const timer = setTimeout(() => {
      setDebouncedSearch(userSearch);
      setSkip(0);
      persistFilters({ search: userSearch });
    }, 300);
    return () => clearTimeout(timer);
    // appliedSearch is excluded: the timer's own setDebouncedSearch changes it,
    // which would re-arm the timer.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userSearch, setSavedFilter]);

  const changeOwner = (value: string) => {
    const next: TopologyOwnerFilter = value === "mine" ? "mine" : "all";
    setUserOwner(next);
    setSkip(0);
    persistFilters({ owner: next });
  };
  const clearFilters = () => {
    setUserSearch("");
    setDebouncedSearch("");
    setUserOwner("all");
    setSkip(0);
    persistFilters({ search: "", owner: "all" });
  };
  const trimmedSearch = appliedSearch.trim();
  const filtersApplied = trimmedSearch !== "" || owner !== "all";
  const showClear = filtersApplied || searchInput !== "";

  // Persisted sort (extras["sort:topologies"]), validated by parseTopologySort:
  // a stale or foreign stored field means no explicit sort, and no explicit sort
  // sends no sort parameters at all, so the backend's default order applies.
  const rawSort = usePreferencesStore((s) => s.getSortState(TOPOLOGY_SORT_PAGE_KEY));
  const setSortState = usePreferencesStore((s) => s.setSortState);
  const explicitSort = parseTopologySort(rawSort);
  const shownSort = explicitSort ?? DEFAULT_TOPOLOGY_SORT;
  const handleSort = (field: TopologySortField) => {
    setSkip(0);
    setSortState(TOPOLOGY_SORT_PAGE_KEY, nextTopologySort(explicitSort, field));
  };

  const query: TopologyListQuery = {};
  if (trimmedSearch) query.search = trimmedSearch;
  if (owner !== "all") query.owner = owner;
  if (explicitSort) {
    query.sortBy = explicitSort.sortBy;
    query.sortDir = explicitSort.sortDir;
  }
  const { data, isLoading, isError } = usePaginatedTopologies(
    skip,
    limit,
    Object.keys(query).length > 0 ? query : undefined,
  );
  const topologies = data?.items;
  const total = data?.total ?? 0;

  // Multi-select. The selection (and any per-row failure reasons) is tagged
  // with the view it was made in: page, applied search, owner filter, and sort.
  // Under any other view it is dropped during render, so it can never hold a
  // row the user cannot currently see (the ReservationsPage #843 pattern).
  const viewKey = [
    skip,
    trimmedSearch,
    owner,
    explicitSort?.sortBy ?? "",
    explicitSort?.sortDir ?? "",
  ].join("|");
  const [selection, setSelection] = useState<{
    key: string;
    ids: ReadonlySet<string>;
    failures: ReadonlyMap<string, string | null>;
  }>({ key: viewKey, ids: NO_SELECTION, failures: NO_FAILURES });
  if (selection.key !== viewKey) {
    setSelection({ key: viewKey, ids: NO_SELECTION, failures: NO_FAILURES });
  }
  const current = selection.key === viewKey ? selection : null;
  const selectedIds = current?.ids ?? NO_SELECTION;
  const failures = current?.failures ?? NO_FAILURES;
  const selectedRows = (topologies ?? []).filter((t) => selectedIds.has(t.id));
  const pageCount = topologies?.length ?? 0;
  const allSelected = pageCount > 0 && selectedRows.length === pageCount;
  const setSelectedIds = (ids: ReadonlySet<string>) => {
    // A row that leaves the selection drops its failure note with it.
    const kept = new Map([...failures].filter(([id]) => ids.has(id)));
    setSelection({ key: viewKey, ids, failures: kept });
  };
  const toggleRow = (id: string) => {
    const next = new Set(selectedIds);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setSelectedIds(next);
  };
  const toggleAll = () =>
    setSelectedIds(allSelected ? NO_SELECTION : new Set((topologies ?? []).map((t) => t.id)));

  const bulkDelete = useBulkDeleteTopologies();
  const [confirmBulk, setConfirmBulk] = useState(false);
  const partition = partitionTopologies(selectedRows, user);
  const noneEligible = partition.eligible.length === 0;

  const runBulkDelete = async () => {
    setConfirmBulk(false);
    const ids = partition.eligible.map((t) => t.id);
    if (ids.length === 0) return;
    let results: PromiseSettledResult<unknown>[];
    try {
      results = await bulkDelete.mutateAsync(ids);
    } catch {
      results = [];
    }
    const outcome = applySettled(selectedIds, ids, results, (err) => errorDetail(err, "") || null);
    setSelection({
      key: viewKey,
      ids: outcome.remaining,
      failures: new Map(outcome.failures.map((f) => [f.id, f.reason])),
    });
    const message = summarizeDelete(outcome);
    if (outcome.failures.length === 0) toast.success(message);
    else toast.error(message);
  };

  const [showCreateModal, setShowCreateModal] = useState(false);
  const [newName, setNewName] = useState("");
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [cloneSource, setCloneSource] = useState<Topology | null>(null);
  const [cloneName, setCloneName] = useState("");

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newName.trim()) return;
    let topology;
    try {
      topology = await createTopology.mutateAsync({ name: newName.trim() });
    } catch {
      // onError already toasted; keep the modal open so the user can retry.
      return;
    }
    setNewName("");
    setShowCreateModal(false);
    navigate(`/topology/${topology.id}`);
  };

  const handleDelete = async () => {
    if (!deleteId) return;
    try {
      await deleteTopology.mutateAsync(deleteId);
    } catch {
      // onError already toasted; leave the confirm dialog state as-is.
      return;
    }
    setDeleteId(null);
  };

  const handleCloneOpen = (topology: Topology) => {
    setCloneSource(topology);
    setCloneName(`${topology.name} (copy)`);
  };

  const handleCloneSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!cloneSource || !cloneName.trim()) return;
    let clone;
    try {
      clone = await cloneTopology.mutateAsync({
        id: cloneSource.id,
        name: cloneName.trim(),
      });
    } catch {
      // onError already toasted; keep the clone modal open for retry.
      return;
    }
    setCloneSource(null);
    setCloneName("");
    navigate(`/topology/${clone.id}`);
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="px-6 xl:px-12 2xl:px-16 py-6">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-gray-900">
            Topologies
            {data && (
              <span className="ml-2 text-sm text-gray-400 font-normal">({total})</span>
            )}
          </h2>
          <div className="flex items-center gap-3">
            <BulkImportExport
              resourceLabel="topologies"
              onExport={exportTopologies}
              onImport={importTopologies}
              onImported={() => queryClient.invalidateQueries({ queryKey: ["topologies"] })}
            />
            <button
              onClick={() => setShowCreateModal(true)}
              className="px-4 py-2 bg-blue-600 text-white text-sm rounded hover:bg-blue-700 transition-colors"
            >
              New Topology
            </button>
          </div>
        </div>

        <ListFilterLayout
          panel={
            <ListFilterPanel
              searchLabel="Search topologies"
              searchPlaceholder="Search by name..."
              searchValue={searchInput}
              onSearchChange={setUserSearch}
              showClear={showClear}
              onClear={clearFilters}
            >
              <FilterSelect label="Owner" value={owner} onChange={changeOwner}>
                <option value="all">All</option>
                <option value="mine">Mine</option>
              </FilterSelect>
            </ListFilterPanel>
          }
        >
          <div role="status" aria-label="Selection" aria-live="polite">
            {selectedRows.length > 0 && (
              <div className="mb-3 flex flex-wrap items-center gap-3 rounded-lg border border-blue-200 bg-blue-50 px-4 py-2 text-sm">
                <span className="font-medium text-gray-900">{selectedRows.length} selected</span>
                <button
                  type="button"
                  onClick={() => setConfirmBulk(true)}
                  disabled={bulkDelete.isPending || noneEligible}
                  aria-describedby={noneEligible ? "bulk-delete-note" : undefined}
                  className="text-xs px-2.5 py-1.5 rounded border border-red-300 text-red-700 hover:bg-red-50 disabled:opacity-50"
                >
                  Delete selected
                </button>
                {noneEligible && (
                  <span id="bulk-delete-note" className="text-xs text-gray-500">
                    {noneDeletableMessage(partition)}
                  </span>
                )}
                <button
                  type="button"
                  onClick={() => setSelectedIds(NO_SELECTION)}
                  disabled={bulkDelete.isPending}
                  className="ml-auto text-xs text-gray-600 hover:text-gray-900 disabled:opacity-50"
                >
                  Clear selection
                </button>
              </div>
            )}
          </div>

          <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
            {isLoading && (
              <p role="status" aria-live="polite" className="text-sm text-gray-400 text-center py-8">
                Loading topologies...
              </p>
            )}
            {isError && (
              <p className="text-sm text-red-500 text-center py-8">Failed to load topologies</p>
            )}
            {topologies && topologies.length === 0 && (
              <p className="text-sm text-gray-400 text-center py-8">
                {filtersApplied
                  ? "No topologies match these filters."
                  : "No topologies yet. Create one to get started."}
              </p>
            )}
            {topologies && topologies.length > 0 && (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[640px]">
                  <thead>
                    <tr className="border-b border-gray-200 bg-gray-50">
                      <th scope="col" className="px-4 py-2 w-8">
                        <input
                          type="checkbox"
                          checked={allSelected}
                          ref={(el) => {
                            if (el) el.indeterminate = selectedRows.length > 0 && !allSelected;
                          }}
                          onChange={toggleAll}
                          aria-label="Select all topologies on this page"
                          className="h-4 w-4 rounded border-gray-300"
                        />
                      </th>
                      <SortableHeader
                        label="Name"
                        field="name"
                        active={shownSort.sortBy === "name"}
                        direction={shownSort.sortDir}
                        onSort={handleSort}
                      />
                      <SortableHeader
                        label="Owner"
                        field="owner_name"
                        active={shownSort.sortBy === "owner_name"}
                        direction={shownSort.sortDir}
                        onSort={handleSort}
                      />
                      <SortableHeader
                        label="Created"
                        field="created_at"
                        active={shownSort.sortBy === "created_at"}
                        direction={shownSort.sortDir}
                        onSort={handleSort}
                      />
                      <SortableHeader
                        label="Updated"
                        field="updated_at"
                        active={shownSort.sortBy === "updated_at"}
                        direction={shownSort.sortDir}
                        onSort={handleSort}
                      />
                      <th scope="col" className={TH_CLASS}>
                        <span className="sr-only">Actions</span>
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {topologies.map((t) => (
                      <TopologyRow
                        key={t.id}
                        topology={t}
                        onDelete={setDeleteId}
                        onClone={handleCloneOpen}
                        canDelete={canDeleteTopologyAs(t, user)}
                        selected={selectedIds.has(t.id)}
                        onToggleSelected={() => toggleRow(t.id)}
                        failure={failures.has(t.id) ? (failures.get(t.id) ?? null) : undefined}
                      />
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <Pagination total={total} skip={skip} limit={limit} onPageChange={setSkip} />
          </div>
        </ListFilterLayout>

        <Modal
          open={showCreateModal}
          onClose={() => setShowCreateModal(false)}
          title="New Topology"
        >
          <form onSubmit={handleCreate}>
            <label htmlFor="topology-name" className="block text-sm font-medium text-gray-700 mb-1">
              Name
            </label>
            <input
              id="topology-name"
              type="text"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="My Lab Topology"
              autoFocus
              className="w-full px-3 py-2 border border-gray-300 rounded text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
            <div className="flex justify-end gap-2 mt-4">
              <button
                type="button"
                onClick={() => setShowCreateModal(false)}
                className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={!newName.trim() || createTopology.isPending}
                className="px-4 py-2 bg-blue-600 text-white text-sm rounded hover:bg-blue-700 disabled:opacity-50"
              >
                {createTopology.isPending ? "Creating..." : "Create"}
              </button>
            </div>
          </form>
        </Modal>

        <Modal
          open={!!cloneSource}
          onClose={() => {
            setCloneSource(null);
            setCloneName("");
          }}
          title="Clone Topology"
        >
          <form onSubmit={handleCloneSubmit}>
            <label htmlFor="clone-topology-name" className="block text-sm font-medium text-gray-700 mb-1">
              Name for the clone
            </label>
            <input
              id="clone-topology-name"
              type="text"
              value={cloneName}
              onChange={(e) => setCloneName(e.target.value)}
              autoFocus
              className="w-full px-3 py-2 border border-gray-300 rounded text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
            <div className="flex justify-end gap-2 mt-4">
              <button
                type="button"
                onClick={() => {
                  setCloneSource(null);
                  setCloneName("");
                }}
                className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={!cloneName.trim() || cloneTopology.isPending}
                className="px-4 py-2 bg-blue-600 text-white text-sm rounded hover:bg-blue-700 disabled:opacity-50"
              >
                {cloneTopology.isPending ? "Cloning..." : "Clone"}
              </button>
            </div>
          </form>
        </Modal>

        <ConfirmDialog
          open={!!deleteId}
          title="Delete topology?"
          description="This will permanently delete this topology and its canvas data."
          confirmLabel="Delete"
          destructive
          onConfirm={handleDelete}
          onCancel={() => setDeleteId(null)}
        />

        <ConfirmDialog
          open={confirmBulk}
          title={BULK_DELETE_TITLE}
          description={confirmBulk ? bulkDeleteDescription(partition) : ""}
          confirmLabel={bulkDeleteConfirmLabel(partition)}
          cancelLabel={BULK_DELETE_KEEP_LABEL}
          destructive
          onConfirm={runBulkDelete}
          onCancel={() => setConfirmBulk(false)}
        />
      </div>
    </div>
  );
}
