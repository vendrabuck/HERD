import { useState, useEffect, useMemo } from "react";
import { useParams, useNavigate } from "react-router-dom";
import toast from "react-hot-toast";
import { useAuthStore } from "@/stores/authStore";
import {
  useDeviceGroup,
  useCreateDeviceGroup,
  useUpdateDeviceGroup,
  useDeleteDeviceGroup,
  useBulkAddDevices,
  useBulkRemoveDevices,
  useBulkAddUserGroups,
  useBulkRemoveUserGroups,
} from "@/api/deviceGroups";
import { useDevices } from "@/api/inventory";
import { useGroups } from "@/api/groups";
import { Modal } from "@/components/ui/Modal";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { TransferList } from "@/components/ui/TransferList";
import type { TransferItem } from "@/components/ui/TransferList";

export function DeviceGroupDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const user = useAuthStore((s) => s.user);
  const isNew = !id;

  const { data: group, isLoading } = useDeviceGroup(id);
  const createGroup = useCreateDeviceGroup();
  const updateGroup = useUpdateDeviceGroup();
  const deleteGroup = useDeleteDeviceGroup();
  const bulkAddDevices = useBulkAddDevices();
  const bulkRemoveDevices = useBulkRemoveDevices();
  const bulkAddUG = useBulkAddUserGroups();
  const bulkRemoveUG = useBulkRemoveUserGroups();
  const { data: allDevices } = useDevices();
  const { data: allUserGroups } = useGroups();

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [trackedGroupId, setTrackedGroupId] = useState<string | undefined>(undefined);
  const [showDeviceModal, setShowDeviceModal] = useState(false);
  const [showPermissionModal, setShowPermissionModal] = useState(false);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);

  const isAdmin = user?.role === "admin" || user?.role === "superadmin";

  useEffect(() => {
    if (user && !isAdmin) {
      navigate("/topology");
    }
  }, [user, isAdmin, navigate]);

  // Adjusting state during render
  if (group && group.id !== trackedGroupId) {
    setName(group.name);
    setDescription(group.description ?? "");
    setTrackedGroupId(group.id);
  }

  const groupDeviceIds = useMemo(
    () => new Set(group?.devices.map((d) => d.device_id) ?? []),
    [group?.devices]
  );

  const groupUGIds = useMemo(
    () => new Set(group?.user_groups.map((ug) => ug.user_group_id) ?? []),
    [group?.user_groups]
  );

  const availableDevices: TransferItem[] = useMemo(
    () =>
      (allDevices ?? [])
        .filter((d) => !groupDeviceIds.has(d.id))
        .map((d) => ({
          id: d.id,
          label: d.name,
          sublabel: d.template_name ?? undefined,
        })),
    [allDevices, groupDeviceIds]
  );

  const assignedDevices: TransferItem[] = useMemo(
    () =>
      (group?.devices ?? []).map((d) => ({
        id: d.device_id,
        label: d.device_name ?? d.device_id,
        sublabel: d.template_name ?? undefined,
      })),
    [group?.devices]
  );

  const availableUGs: TransferItem[] = useMemo(
    () =>
      (allUserGroups ?? [])
        .filter((ug) => !groupUGIds.has(ug.id))
        .map((ug) => ({
          id: ug.id,
          label: ug.name,
          sublabel: ug.description ?? undefined,
        })),
    [allUserGroups, groupUGIds]
  );

  const assignedUGs: TransferItem[] = useMemo(
    () =>
      (group?.user_groups ?? []).map((ug) => {
        const found = allUserGroups?.find((g) => g.id === ug.user_group_id);
        return {
          id: ug.user_group_id,
          label: found?.name ?? ug.user_group_id,
          sublabel: found?.description ?? undefined,
        };
      }),
    [group?.user_groups, allUserGroups]
  );

  if (!user || !isAdmin) return null;
  if (!isNew && isLoading) {
    return (
      <div className="h-full overflow-y-auto">
        <div className="px-6 xl:px-12 2xl:px-16 py-8">
          <p className="text-sm text-gray-500">Loading device group...</p>
        </div>
      </div>
    );
  }

  const handleSave = async () => {
    if (!name.trim()) {
      toast.error("Device group name is required");
      return;
    }
    try {
      if (isNew) {
        const created = await createGroup.mutateAsync({
          name: name.trim(),
          description: description.trim() || null,
        });
        toast.success("Device group created");
        navigate(`/admin/device-groups/${created.id}`, { replace: true });
      } else {
        await updateGroup.mutateAsync({
          id: id!,
          data: {
            name: name.trim(),
            description: description.trim() || null,
          },
        });
        toast.success("Device group updated");
      }
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Failed to save device group";
      toast.error(msg);
    }
  };

  const handleDelete = async () => {
    try {
      await deleteGroup.mutateAsync(id!);
      toast.success("Device group deleted");
      navigate("/admin/device-groups");
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Failed to delete device group";
      toast.error(msg);
    }
    setShowDeleteConfirm(false);
  };

  const handleAddDevices = async (ids: string[]) => {
    try {
      const result = await bulkAddDevices.mutateAsync({ groupId: id!, deviceIds: ids });
      toast.success(`${result.added} device(s) added${result.skipped ? `, ${result.skipped} skipped` : ""}`);
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Failed to add devices";
      toast.error(msg);
    }
  };

  const handleRemoveDevices = async (ids: string[]) => {
    try {
      const result = await bulkRemoveDevices.mutateAsync({ groupId: id!, deviceIds: ids });
      toast.success(`${result.removed} device(s) removed`);
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Failed to remove devices";
      toast.error(msg);
    }
  };

  const handleAddPermissions = async (ids: string[]) => {
    try {
      const result = await bulkAddUG.mutateAsync({ groupId: id!, userGroupIds: ids });
      toast.success(`${result.added} user group(s) assigned${result.skipped ? `, ${result.skipped} skipped` : ""}`);
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Failed to assign user groups";
      toast.error(msg);
    }
  };

  const handleRemovePermissions = async (ids: string[]) => {
    try {
      const result = await bulkRemoveUG.mutateAsync({ groupId: id!, userGroupIds: ids });
      toast.success(`${result.removed} user group(s) removed`);
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Failed to remove user groups";
      toast.error(msg);
    }
  };

  const saving = createGroup.isPending || updateGroup.isPending;

  return (
    <div className="h-full overflow-y-auto">
      <div className="px-6 xl:px-12 2xl:px-16 py-8">
        <button
          onClick={() => navigate("/admin/device-groups")}
          className="text-sm text-blue-600 hover:text-blue-800 mb-4 inline-block"
        >
          Back to Device Groups
        </button>

        <h2 className="text-lg font-semibold text-gray-900 mb-4">
          {isNew ? "Create Device Group" : "Edit Device Group"}
        </h2>

        {/* Group form */}
        <div className="bg-white rounded-lg border border-gray-200 p-6 mb-6">
          <div className="space-y-4">
            <div>
              <label htmlFor="dg-name" className="block text-sm font-medium text-gray-700 mb-1">
                Name
              </label>
              <input
                id="dg-name"
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                maxLength={100}
                className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
              />
            </div>
            <div>
              <label htmlFor="dg-desc" className="block text-sm font-medium text-gray-700 mb-1">
                Description
              </label>
              <textarea
                id="dg-desc"
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
              {!isNew && (
                <button
                  onClick={() => setShowDeleteConfirm(true)}
                  className="px-4 py-2 text-sm font-medium text-red-600 border border-red-300 rounded-lg hover:bg-red-50 transition-colors"
                >
                  Delete Device Group
                </button>
              )}
            </div>
          </div>
        </div>

        {/* Devices section */}
        {!isNew && group && (
          <div className="bg-white rounded-lg border border-gray-200 p-6 mb-6">
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-sm font-semibold text-gray-900">
                Devices ({group.devices.length})
              </h3>
              <button
                onClick={() => setShowDeviceModal(true)}
                className="text-sm text-blue-600 hover:text-blue-800"
              >
                Add or Remove Devices
              </button>
            </div>
            {group.devices.length === 0 ? (
              <p className="text-sm text-gray-500">No devices assigned</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm text-left">
                  <thead className="bg-gray-50 text-gray-600 uppercase text-xs">
                    <tr>
                      <th className="px-4 py-2">Device</th>
                      <th className="px-4 py-2">Template</th>
                      <th className="px-4 py-2">Added</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100">
                    {group.devices.map((d) => (
                      <tr key={d.device_id} className="hover:bg-gray-50">
                        <td className="px-4 py-2 font-medium text-gray-900">
                          {d.device_name ?? d.device_id}
                        </td>
                        <td className="px-4 py-2 text-gray-600">
                          {d.template_name ?? "-"}
                        </td>
                        <td className="px-4 py-2 text-gray-500">
                          {new Date(d.added_at).toLocaleDateString()}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}

        {/* Permissions section */}
        {!isNew && group && (
          <div className="bg-white rounded-lg border border-gray-200 p-6">
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-sm font-semibold text-gray-900">
                User Group Permissions ({group.user_groups.length})
              </h3>
              <button
                onClick={() => setShowPermissionModal(true)}
                className="text-sm text-blue-600 hover:text-blue-800"
              >
                Add or Remove Permissions
              </button>
            </div>
            {group.user_groups.length === 0 ? (
              <p className="text-sm text-gray-500">No user groups assigned</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm text-left">
                  <thead className="bg-gray-50 text-gray-600 uppercase text-xs">
                    <tr>
                      <th className="px-4 py-2">User Group</th>
                      <th className="px-4 py-2">Assigned</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100">
                    {group.user_groups.map((ug) => {
                      const found = allUserGroups?.find((g) => g.id === ug.user_group_id);
                      return (
                        <tr key={ug.user_group_id} className="hover:bg-gray-50">
                          <td className="px-4 py-2 font-medium text-gray-900">
                            {found?.name ?? ug.user_group_id}
                          </td>
                          <td className="px-4 py-2 text-gray-500">
                            {new Date(ug.assigned_at).toLocaleDateString()}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Device transfer modal */}
      <Modal
        open={showDeviceModal}
        onClose={() => setShowDeviceModal(false)}
        title="Add or Remove Devices"
        className="max-w-3xl"
      >
        <TransferList
          availableItems={availableDevices}
          assignedItems={assignedDevices}
          onAssign={handleAddDevices}
          onUnassign={handleRemoveDevices}
          availableLabel="Available Devices"
          assignedLabel="Group Devices"
          isLoading={bulkAddDevices.isPending || bulkRemoveDevices.isPending}
        />
      </Modal>

      {/* Permission transfer modal */}
      <Modal
        open={showPermissionModal}
        onClose={() => setShowPermissionModal(false)}
        title="Add or Remove Permissions"
        className="max-w-3xl"
      >
        <TransferList
          availableItems={availableUGs}
          assignedItems={assignedUGs}
          onAssign={handleAddPermissions}
          onUnassign={handleRemovePermissions}
          availableLabel="Available User Groups"
          assignedLabel="Assigned User Groups"
          isLoading={bulkAddUG.isPending || bulkRemoveUG.isPending}
        />
      </Modal>

      {/* Delete confirmation */}
      <ConfirmDialog
        open={showDeleteConfirm}
        title="Delete Device Group"
        description="Are you sure you want to delete this device group? All device and permission assignments will be removed."
        confirmLabel="Delete"
        destructive
        onConfirm={handleDelete}
        onCancel={() => setShowDeleteConfirm(false)}
      />
    </div>
  );
}
