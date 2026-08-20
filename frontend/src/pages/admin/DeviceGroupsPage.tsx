import { useState } from "react";
import { useNavigate } from "react-router-dom";
import toast from "react-hot-toast";
import { usePaginatedDeviceGroups, useDeleteDeviceGroup } from "@/api/deviceGroups";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { Pagination } from "@/components/ui/Pagination";

export function DeviceGroupsPage() {
  const navigate = useNavigate();
  const [skip, setSkip] = useState(0);
  const limit = 50;
  const { data, isLoading } = usePaginatedDeviceGroups(skip, limit);
  const groups = data?.items;
  const total = data?.total ?? 0;
  const deleteGroup = useDeleteDeviceGroup();
  const [deleteId, setDeleteId] = useState<string | null>(null);

  const handleDelete = async () => {
    if (!deleteId) return;
    try {
      await deleteGroup.mutateAsync(deleteId);
      toast.success("Device group deleted");
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Failed to delete device group";
      toast.error(msg);
    }
    setDeleteId(null);
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="px-6 xl:px-12 2xl:px-16 py-8">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-gray-900">Device Groups</h2>
          <button
            onClick={() => navigate("/admin/device-groups/new")}
            className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition-colors"
          >
            Create Device Group
          </button>
        </div>

        <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
          {isLoading ? (
            <p className="text-sm text-gray-500 px-4 py-4">Loading device groups...</p>
          ) : !groups || groups.length === 0 ? (
            <p className="text-sm text-gray-500 px-4 py-4">No device groups found</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[600px] text-sm text-left">
                <thead className="bg-gray-50 text-gray-600 uppercase text-xs">
                  <tr>
                    <th className="px-4 py-3">Name</th>
                    <th className="px-4 py-3">Description</th>
                    <th className="px-4 py-3">Devices</th>
                    <th className="px-4 py-3">User Groups</th>
                    <th className="px-4 py-3">Created</th>
                    <th className="px-4 py-3">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {groups.map((group) => (
                    <tr
                      key={group.id}
                      className="hover:bg-gray-50 cursor-pointer"
                      onClick={() => navigate(`/admin/device-groups/${group.id}`)}
                    >
                      <td className="px-4 py-3 font-medium text-gray-900">{group.name}</td>
                      <td className="px-4 py-3 text-gray-600">
                        {group.description || "-"}
                      </td>
                      <td className="px-4 py-3 text-gray-600">{group.device_count}</td>
                      <td className="px-4 py-3 text-gray-600">{group.user_group_count}</td>
                      <td className="px-4 py-3 text-gray-500">
                        {new Date(group.created_at).toLocaleDateString()}
                      </td>
                      <td className="px-4 py-3">
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            setDeleteId(group.id);
                          }}
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

      <ConfirmDialog
        open={deleteId !== null}
        title="Delete Device Group"
        description="Are you sure you want to delete this device group? All device and permission assignments will be removed."
        confirmLabel="Delete"
        destructive
        onConfirm={handleDelete}
        onCancel={() => setDeleteId(null)}
      />
    </div>
  );
}
