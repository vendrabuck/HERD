import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import toast from "react-hot-toast";
import { useAuthStore } from "@/stores/authStore";
import { useGrants, useCreateGrant, useDeleteGrant } from "@/api/acl";
import { useGroups } from "@/api/groups";
import { Modal } from "@/components/ui/Modal";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { Pagination } from "@/components/ui/Pagination";
import type { Grant, GrantCreate } from "@/types/acl.types";

const RESOURCE_TYPES: Grant["resource_type"][] = ["device", "topology", "reservation", "secret"];
const PERMISSIONS: Grant["permission"][] = ["view", "manage"];

export function GrantsPage() {
  const user = useAuthStore((s) => s.user);
  const navigate = useNavigate();
  const isAdmin = user?.role === "admin" || user?.role === "superadmin";

  const [skip, setSkip] = useState(0);
  const limit = 50;
  const [filterGroupId, setFilterGroupId] = useState("");
  const [filterResourceType, setFilterResourceType] = useState("");
  const [filterResourceId, setFilterResourceId] = useState("");

  const { data, isLoading } = useGrants(
    {
      group_id: filterGroupId || undefined,
      resource_type: filterResourceType || undefined,
      resource_id: filterResourceId.trim() || undefined,
    },
    skip,
    limit,
  );
  const grants = data?.items;
  const total = data?.total ?? 0;

  const { data: groups } = useGroups();
  const groupName = (id: string) => {
    const match = groups?.find((g) => g.id === id);
    return match ? match.name : id.slice(0, 8) + "...";
  };

  const createGrant = useCreateGrant();
  const deleteGrant = useDeleteGrant();

  const [showCreate, setShowCreate] = useState(false);
  const [deleteId, setDeleteId] = useState<string | null>(null);

  const [newGroupId, setNewGroupId] = useState("");
  const [newResourceType, setNewResourceType] = useState<Grant["resource_type"]>("device");
  const [newResourceId, setNewResourceId] = useState("");
  const [newPermission, setNewPermission] = useState<Grant["permission"]>("view");

  useEffect(() => {
    if (user && !isAdmin) {
      navigate("/topology");
    }
  }, [user, isAdmin, navigate]);

  if (!user || !isAdmin) return null;

  const closeCreateModal = () => {
    setShowCreate(false);
    setNewGroupId("");
    setNewResourceType("device");
    setNewResourceId("");
    setNewPermission("view");
  };

  const handleCreate = async () => {
    if (!newGroupId) {
      toast.error("Group is required");
      return;
    }
    if (!newResourceId.trim()) {
      toast.error("Resource ID is required");
      return;
    }
    const body: GrantCreate = {
      group_id: newGroupId,
      resource_type: newResourceType,
      resource_id: newResourceId.trim(),
      permission: newPermission,
    };
    try {
      await createGrant.mutateAsync(body);
      toast.success("Grant created");
      closeCreateModal();
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Failed to create grant";
      toast.error(msg);
    }
  };

  const handleDelete = async () => {
    if (!deleteId) return;
    try {
      await deleteGrant.mutateAsync(deleteId);
      toast.success("Grant deleted");
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Failed to delete grant";
      toast.error(msg);
    }
    setDeleteId(null);
  };

  const clearFilters = () => {
    setFilterGroupId("");
    setFilterResourceType("");
    setFilterResourceId("");
    setSkip(0);
  };

  const hasFilters = filterGroupId || filterResourceType || filterResourceId.trim();

  return (
    <div className="h-full overflow-y-auto">
      <div className="px-6 xl:px-12 2xl:px-16 py-8">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-gray-900">ACL Grants</h2>
          <button
            onClick={() => setShowCreate(true)}
            className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition-colors"
          >
            Create Grant
          </button>
        </div>

        <p className="text-sm text-gray-600 mb-4">
          Grants tie a user group to a resource (device, topology, reservation, or
          secret) with a view or manage permission level. Manage implies view.
        </p>

        {/* Filters */}
        <div className="mb-4 flex flex-wrap items-end gap-3">
          <div>
            <label htmlFor="grant-filter-group" className="block text-xs font-medium text-gray-500 mb-1">
              Group
            </label>
            <select
              id="grant-filter-group"
              value={filterGroupId}
              onChange={(e) => {
                setFilterGroupId(e.target.value);
                setSkip(0);
              }}
              className="border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              <option value="">All groups</option>
              {groups?.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label
              htmlFor="grant-filter-resource-type"
              className="block text-xs font-medium text-gray-500 mb-1"
            >
              Resource type
            </label>
            <select
              id="grant-filter-resource-type"
              value={filterResourceType}
              onChange={(e) => {
                setFilterResourceType(e.target.value);
                setSkip(0);
              }}
              className="border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              <option value="">All types</option>
              {RESOURCE_TYPES.map((rt) => (
                <option key={rt} value={rt}>
                  {rt}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label
              htmlFor="grant-filter-resource-id"
              className="block text-xs font-medium text-gray-500 mb-1"
            >
              Resource ID
            </label>
            <input
              id="grant-filter-resource-id"
              type="text"
              placeholder="uuid..."
              value={filterResourceId}
              onChange={(e) => {
                setFilterResourceId(e.target.value);
                setSkip(0);
              }}
              className="border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          {hasFilters && (
            <button
              onClick={clearFilters}
              className="px-3 py-2 text-sm text-gray-600 hover:text-gray-900 bg-gray-100 rounded-lg hover:bg-gray-200 transition-colors"
            >
              Clear filters
            </button>
          )}
        </div>

        <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
          {isLoading ? (
            <p className="text-sm text-gray-500 px-4 py-4">Loading grants...</p>
          ) : !grants || grants.length === 0 ? (
            <p className="text-sm text-gray-500 px-4 py-4">No grants found</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[700px] text-sm text-left">
                <thead className="bg-gray-50 text-gray-600 uppercase text-xs">
                  <tr>
                    <th className="px-4 py-3">Group</th>
                    <th className="px-4 py-3">Resource Type</th>
                    <th className="px-4 py-3">Resource ID</th>
                    <th className="px-4 py-3">Permission</th>
                    <th className="px-4 py-3">Granted At</th>
                    <th className="px-4 py-3">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {grants.map((grant) => (
                    <tr key={grant.id} className="hover:bg-gray-50">
                      <td className="px-4 py-3 font-medium text-gray-900">
                        {groupName(grant.group_id)}
                      </td>
                      <td className="px-4 py-3 text-gray-600">{grant.resource_type}</td>
                      <td className="px-4 py-3 text-gray-600 font-mono text-xs">
                        {grant.resource_id}
                      </td>
                      <td className="px-4 py-3 text-gray-600">{grant.permission}</td>
                      <td className="px-4 py-3 text-gray-500">
                        {new Date(grant.granted_at).toLocaleDateString()}
                      </td>
                      <td className="px-4 py-3">
                        <button
                          onClick={() => setDeleteId(grant.id)}
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
          <Pagination total={total} skip={skip} limit={limit} onPageChange={setSkip} />
        </div>
      </div>

      <Modal open={showCreate} onClose={closeCreateModal} title="Create Grant" className="max-w-lg">
        <div className="space-y-4">
          <div>
            <label htmlFor="grant-group" className="block text-sm font-medium text-gray-700 mb-1">
              Group
            </label>
            <select
              id="grant-group"
              value={newGroupId}
              onChange={(e) => setNewGroupId(e.target.value)}
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

          <div>
            <label
              htmlFor="grant-resource-type"
              className="block text-sm font-medium text-gray-700 mb-1"
            >
              Resource Type
            </label>
            <select
              id="grant-resource-type"
              value={newResourceType}
              onChange={(e) => setNewResourceType(e.target.value as Grant["resource_type"])}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              {RESOURCE_TYPES.map((rt) => (
                <option key={rt} value={rt}>
                  {rt}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label
              htmlFor="grant-resource-id"
              className="block text-sm font-medium text-gray-700 mb-1"
            >
              Resource ID
            </label>
            <input
              id="grant-resource-id"
              type="text"
              placeholder="uuid of the device, topology, reservation, or secret"
              value={newResourceId}
              onChange={(e) => setNewResourceId(e.target.value)}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>

          <div>
            <label
              htmlFor="grant-permission"
              className="block text-sm font-medium text-gray-700 mb-1"
            >
              Permission
            </label>
            <select
              id="grant-permission"
              value={newPermission}
              onChange={(e) => setNewPermission(e.target.value as Grant["permission"])}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              {PERMISSIONS.map((p) => (
                <option key={p} value={p}>
                  {p}
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
              onClick={handleCreate}
              disabled={createGrant.isPending}
              className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors"
            >
              {createGrant.isPending ? "Creating..." : "Create"}
            </button>
          </div>
        </div>
      </Modal>

      <ConfirmDialog
        open={deleteId !== null}
        title="Delete Grant"
        description="Are you sure you want to delete this grant? The group will lose this access immediately."
        confirmLabel="Delete"
        destructive
        onConfirm={handleDelete}
        onCancel={() => setDeleteId(null)}
      />
    </div>
  );
}
