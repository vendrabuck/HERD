import { useState, useEffect, useMemo } from "react";
import { useParams, useNavigate } from "react-router-dom";
import toast from "react-hot-toast";
import { useAuthStore } from "@/stores/authStore";
import { useGroup, useCreateGroup, useUpdateGroup, useBulkAddMembers, useBulkRemoveMembers } from "@/api/groups";
import { useAllUsers } from "@/api/admin";
import { TransferList } from "@/components/ui/TransferList";
import type { TransferItem } from "@/components/ui/TransferList";

export function GroupDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const user = useAuthStore((s) => s.user);
  const isNew = !id;

  const { data: group, isLoading } = useGroup(id);
  const createGroup = useCreateGroup();
  const updateGroup = useUpdateGroup();
  const bulkAdd = useBulkAddMembers();
  const bulkRemove = useBulkRemoveMembers();
  const isAdmin = user?.role === "admin" || user?.role === "superadmin";
  const { data: allUsers } = useAllUsers(isAdmin);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [trackedGroupId, setTrackedGroupId] = useState<string | undefined>(undefined);

  useEffect(() => {
    if (user && !isAdmin) {
      navigate("/topology");
    }
  }, [user, isAdmin, navigate]);

  // Adjusting state during render: sync form fields when group data arrives
  if (group && group.id !== trackedGroupId) {
    setName(group.name);
    setDescription(group.description ?? "");
    setTrackedGroupId(group.id);
  }

  const memberIds = useMemo(
    () => new Set(group?.members.map((m) => m.user_id) ?? []),
    [group?.members]
  );

  const availableItems: TransferItem[] = useMemo(
    () =>
      (allUsers ?? [])
        .filter((u) => !memberIds.has(u.id))
        .map((u) => ({ id: u.id, label: u.username, sublabel: u.email })),
    [allUsers, memberIds]
  );

  const assignedItems: TransferItem[] = useMemo(
    () =>
      (group?.members ?? []).map((m) => ({
        id: m.user_id,
        label: m.username,
        sublabel: m.email,
      })),
    [group?.members]
  );

  if (!user || !isAdmin) return null;
  if (!isNew && isLoading) {
    return (
      <div className="h-full overflow-y-auto">
        <div className="px-6 xl:px-12 2xl:px-16 py-8">
          <p className="text-sm text-gray-500">Loading group...</p>
        </div>
      </div>
    );
  }

  const handleSave = async () => {
    if (!name.trim()) {
      toast.error("Group name is required");
      return;
    }
    try {
      if (isNew) {
        const created = await createGroup.mutateAsync({
          name: name.trim(),
          description: description.trim() || null,
        });
        toast.success("User group created");
        navigate(`/admin/groups/${created.id}`, { replace: true });
      } else {
        await updateGroup.mutateAsync({
          id: id!,
          data: {
            name: name.trim(),
            description: description.trim() || null,
          },
        });
        toast.success("User group updated");
      }
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Failed to save group";
      toast.error(msg);
    }
  };

  const handleAssign = async (ids: string[]) => {
    try {
      const result = await bulkAdd.mutateAsync({ groupId: id!, userIds: ids });
      toast.success(`${result.added} member(s) added${result.skipped ? `, ${result.skipped} skipped` : ""}`);
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Failed to add members";
      toast.error(msg);
    }
  };

  const handleUnassign = async (ids: string[]) => {
    try {
      const result = await bulkRemove.mutateAsync({ groupId: id!, userIds: ids });
      toast.success(`${result.removed} member(s) removed`);
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Failed to remove members";
      toast.error(msg);
    }
  };

  const saving = createGroup.isPending || updateGroup.isPending;

  return (
    <div className="h-full overflow-y-auto">
      <div className="px-6 xl:px-12 2xl:px-16 py-8">
        <button
          onClick={() => navigate("/admin/groups")}
          className="text-sm text-blue-600 hover:text-blue-800 mb-4 inline-block"
        >
          Back to User Groups
        </button>

        <h2 className="text-lg font-semibold text-gray-900 mb-4">
          {isNew ? "Create User Group" : "Edit User Group"}
        </h2>

        {/* Group form */}
        <div className="bg-white rounded-lg border border-gray-200 p-6 mb-6">
          <div className="space-y-4">
            <div>
              <label htmlFor="group-name" className="block text-sm font-medium text-gray-700 mb-1">
                Name
              </label>
              <input
                id="group-name"
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                maxLength={100}
                className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
              />
            </div>
            <div>
              <label htmlFor="group-desc" className="block text-sm font-medium text-gray-700 mb-1">
                Description
              </label>
              <textarea
                id="group-desc"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                rows={3}
                className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
              />
            </div>
            <div className="flex gap-3">
              <button
                onClick={handleSave}
                disabled={saving}
                className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition-colors disabled:opacity-50"
              >
                {saving ? "Saving..." : isNew ? "Create" : "Save"}
              </button>
            </div>
          </div>
        </div>

        {/* Members section with TransferList (only for existing groups) */}
        {!isNew && group && (
          <div className="bg-white rounded-lg border border-gray-200 p-6">
            <h3 className="text-sm font-semibold text-gray-900 mb-3">
              Members ({group.members.length})
            </h3>
            <TransferList
              availableItems={availableItems}
              assignedItems={assignedItems}
              onAssign={handleAssign}
              onUnassign={handleUnassign}
              availableLabel="Available Users"
              assignedLabel="Members"
              isLoading={bulkAdd.isPending || bulkRemove.isPending}
            />
          </div>
        )}
      </div>
    </div>
  );
}
