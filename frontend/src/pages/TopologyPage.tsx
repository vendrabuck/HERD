import { useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  usePaginatedTopologies,
  useCreateTopology,
  useDeleteTopology,
  useCloneTopology,
} from "@/api/topologies";
import { useAuthStore } from "@/stores/authStore";
import { Modal } from "@/components/ui/Modal";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { Pagination } from "@/components/ui/Pagination";
import type { Topology } from "@/types/topology.types";

function TopologyRow({
  topology,
  onDelete,
  onClone,
  canDelete,
}: {
  topology: Topology;
  onDelete: (id: string) => void;
  onClone: (topology: Topology) => void;
  canDelete: boolean;
}) {
  const navigate = useNavigate();
  const created = new Date(topology.created_at).toLocaleDateString();
  const updated = new Date(topology.updated_at).toLocaleDateString();

  return (
    <tr
      className="border-b border-gray-100 hover:bg-gray-50 cursor-pointer"
      onClick={() => navigate(`/topology/${topology.id}`)}
    >
      <td className="px-4 py-3 text-sm font-medium text-blue-600 hover:text-blue-800">
        {topology.name}
      </td>
      <td className="px-4 py-3 text-sm text-gray-500">{topology.owner_name || topology.created_by.slice(0, 8)}</td>
      <td className="px-4 py-3 text-sm text-gray-500">{created}</td>
      <td className="px-4 py-3 text-sm text-gray-500">{updated}</td>
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

export function TopologyPage() {
  const user = useAuthStore((s) => s.user);
  const createTopology = useCreateTopology();
  const deleteTopology = useDeleteTopology();
  const cloneTopology = useCloneTopology();
  const navigate = useNavigate();

  const [skip, setSkip] = useState(0);
  const limit = 50;
  const { data, isLoading, isError } = usePaginatedTopologies(skip, limit);
  const topologies = data?.items;
  const total = data?.total ?? 0;

  const [showCreateModal, setShowCreateModal] = useState(false);
  const [newName, setNewName] = useState("");
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [cloneSource, setCloneSource] = useState<Topology | null>(null);
  const [cloneName, setCloneName] = useState("");

  const isAdmin = user?.role === "admin" || user?.role === "superadmin";

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newName.trim()) return;
    const topology = await createTopology.mutateAsync({ name: newName.trim() });
    setNewName("");
    setShowCreateModal(false);
    navigate(`/topology/${topology.id}`);
  };

  const handleDelete = async () => {
    if (!deleteId) return;
    await deleteTopology.mutateAsync(deleteId);
    setDeleteId(null);
  };

  const handleCloneOpen = (topology: Topology) => {
    setCloneSource(topology);
    setCloneName(`${topology.name} (copy)`);
  };

  const handleCloneSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!cloneSource || !cloneName.trim()) return;
    const clone = await cloneTopology.mutateAsync({
      id: cloneSource.id,
      name: cloneName.trim(),
    });
    setCloneSource(null);
    setCloneName("");
    navigate(`/topology/${clone.id}`);
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-6xl mx-auto px-6 py-6">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-gray-900">
            Topologies
            {data && (
              <span className="ml-2 text-sm text-gray-400 font-normal">({total})</span>
            )}
          </h2>
          <button
            onClick={() => setShowCreateModal(true)}
            className="px-4 py-2 bg-blue-600 text-white text-sm rounded hover:bg-blue-700 transition-colors"
          >
            New Topology
          </button>
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
              No topologies yet. Create one to get started.
            </p>
          )}
          {topologies && topologies.length > 0 && (
            <table className="w-full">
              <thead>
                <tr className="border-b border-gray-200 bg-gray-50">
                  <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Name</th>
                  <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Owner</th>
                  <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Created</th>
                  <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Updated</th>
                  <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase"></th>
                </tr>
              </thead>
              <tbody>
                {topologies.map((t) => (
                  <TopologyRow
                    key={t.id}
                    topology={t}
                    onDelete={setDeleteId}
                    onClone={handleCloneOpen}
                    canDelete={isAdmin || t.created_by === user?.id}
                  />
                ))}
              </tbody>
            </table>
          )}
          <Pagination total={total} skip={skip} limit={limit} onPageChange={setSkip} />
        </div>

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
      </div>
    </div>
  );
}
