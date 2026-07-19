import { useState } from "react";
import toast from "react-hot-toast";
import { useTemplate } from "@/api/templates";
import { useUpdateDevice, useDeleteDevice, deleteDeviceErrorMessage } from "@/api/inventory";
import { DynamicFieldRenderer } from "@/components/devices/DynamicFieldRenderer";
import { PortsSection } from "@/components/devices/PortsSection";
import { Modal } from "@/components/ui/Modal";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import type { Device, TopologyType, DeviceStatus } from "@/types/device.types";

const STATUS_COLORS: Record<string, string> = {
  AVAILABLE: "bg-green-100 text-green-800",
  RESERVED: "bg-yellow-100 text-yellow-800",
  OFFLINE: "bg-gray-100 text-gray-600",
  MAINTENANCE: "bg-red-100 text-red-700",
};

interface DeviceDetailModalProps {
  device: Device | null;
  onClose: () => void;
  isAdmin: boolean;
}

export function DeviceDetailModal({ device, onClose, isAdmin }: DeviceDetailModalProps) {
  const [editing, setEditing] = useState(false);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [editName, setEditName] = useState("");
  const [editTopology, setEditTopology] = useState<TopologyType>("PHYSICAL");
  const [editStatus, setEditStatus] = useState<DeviceStatus>("AVAILABLE");
  const [editFieldData, setEditFieldData] = useState<Record<string, unknown>>({});

  const { data: template } = useTemplate(device?.template_id);
  const updateDevice = useUpdateDevice();
  const deleteDevice = useDeleteDevice();

  const startEditing = () => {
    if (!device) return;
    setEditName(device.name);
    setEditTopology(device.topology_type);
    setEditStatus(device.status);
    setEditFieldData({ ...device.field_data });
    setEditing(true);
  };

  const cancelEditing = () => {
    setEditing(false);
  };

  const handleSave = async () => {
    if (!device) return;
    try {
      await updateDevice.mutateAsync({
        id: device.id,
        data: {
          name: editName,
          topology_type: editTopology,
          status: editStatus,
          field_data: editFieldData,
        },
      });
      toast.success("Device updated");
      setEditing(false);
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Failed to update device";
      toast.error(msg);
    }
  };

  const handleDelete = async () => {
    if (!device) return;
    try {
      await deleteDevice.mutateAsync(device.id);
      toast.success("Device deleted");
      setShowDeleteConfirm(false);
      setEditing(false);
      onClose();
    } catch (err: unknown) {
      toast.error(deleteDeviceErrorMessage(err));
    }
  };

  const handleClose = () => {
    setEditing(false);
    setShowDeleteConfirm(false);
    onClose();
  };

  return (
    <>
      <Modal
        open={!!device}
        onClose={handleClose}
        title={editing ? "Edit Device" : "Device Details"}
        className="max-w-2xl"
      >
        {device && (
          <div className="space-y-5">
            {editing ? (
              <>
                <div>
                  <label htmlFor="edit-name" className="block text-sm font-medium text-gray-700 mb-1">
                    Name
                  </label>
                  <input
                    id="edit-name"
                    type="text"
                    value={editName}
                    onChange={(e) => setEditName(e.target.value)}
                    className="w-full border border-gray-300 rounded px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                  />
                </div>
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label htmlFor="edit-topology" className="block text-sm font-medium text-gray-700 mb-1">
                      Topology Type
                    </label>
                    <select
                      id="edit-topology"
                      value={editTopology}
                      onChange={(e) => setEditTopology(e.target.value as TopologyType)}
                      className="w-full border border-gray-300 rounded px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                    >
                      <option value="PHYSICAL">PHYSICAL</option>
                      <option value="CLOUD">CLOUD</option>
                    </select>
                  </div>
                  <div>
                    <label htmlFor="edit-status" className="block text-sm font-medium text-gray-700 mb-1">
                      Status
                    </label>
                    <select
                      id="edit-status"
                      value={editStatus}
                      onChange={(e) => setEditStatus(e.target.value as DeviceStatus)}
                      className="w-full border border-gray-300 rounded px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                    >
                      <option value="AVAILABLE">AVAILABLE</option>
                      <option value="RESERVED">RESERVED</option>
                      <option value="OFFLINE">OFFLINE</option>
                      <option value="MAINTENANCE">MAINTENANCE</option>
                    </select>
                  </div>
                </div>
                {template && (
                  <DynamicFieldRenderer
                    sections={template.sections}
                    fieldData={editFieldData}
                    onChange={setEditFieldData}
                  />
                )}
                <div className="flex justify-end gap-2 pt-2 border-t border-gray-200">
                  <button
                    type="button"
                    onClick={cancelEditing}
                    className="px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 rounded-lg hover:bg-gray-200 transition-colors"
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    onClick={handleSave}
                    disabled={updateDevice.isPending}
                    className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors"
                  >
                    {updateDevice.isPending ? "Saving..." : "Save"}
                  </button>
                </div>
              </>
            ) : (
              <>
                <h3 className="text-lg font-semibold text-gray-900">{device.name}</h3>
                <div className="grid grid-cols-3 gap-3">
                  <div>
                    <span className="block text-xs text-gray-500">Template</span>
                    <span className="text-sm font-medium text-gray-900">{device.template_name ?? "-"}</span>
                  </div>
                  <div>
                    <span className="block text-xs text-gray-500">Topology</span>
                    <span
                      className={`text-xs px-1.5 py-0.5 rounded ${
                        device.topology_type === "PHYSICAL"
                          ? "bg-blue-100 text-blue-800"
                          : "bg-purple-100 text-purple-800"
                      }`}
                    >
                      {device.topology_type}
                    </span>
                  </div>
                  <div>
                    <span className="block text-xs text-gray-500">Status</span>
                    <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${STATUS_COLORS[device.status]}`}>
                      {device.status}
                    </span>
                  </div>
                </div>

                {template && template.sections.length > 0 && (
                  <DynamicFieldRenderer
                    sections={template.sections}
                    fieldData={device.field_data}
                    readOnly
                  />
                )}

                <div className="border-t border-gray-200 pt-4">
                  <PortsSection deviceId={device.id} isAdmin={isAdmin} />
                </div>

                {device.driver_name && (
                  <div className="border-t border-gray-200 pt-4">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-semibold text-gray-700">Driver:</span>
                      <span className="text-sm text-gray-600">{device.driver_name}</span>
                    </div>
                  </div>
                )}

                <div className="flex justify-end gap-2 pt-2 border-t border-gray-200">
                  {isAdmin && (
                    <>
                      <button
                        type="button"
                        onClick={() => setShowDeleteConfirm(true)}
                        className="px-4 py-2 text-sm font-medium text-red-600 bg-red-50 rounded-lg hover:bg-red-100 transition-colors"
                      >
                        Delete
                      </button>
                      <button
                        type="button"
                        onClick={startEditing}
                        className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition-colors"
                      >
                        Edit
                      </button>
                    </>
                  )}
                  <button
                    type="button"
                    onClick={handleClose}
                    className="px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 rounded-lg hover:bg-gray-200 transition-colors"
                  >
                    Close
                  </button>
                </div>
              </>
            )}
          </div>
        )}
      </Modal>

      <ConfirmDialog
        open={showDeleteConfirm}
        title="Delete Device"
        description={`Delete device "${device?.name}"? This will also delete all ports. This cannot be undone.`}
        confirmLabel="Delete"
        destructive
        onConfirm={handleDelete}
        onCancel={() => setShowDeleteConfirm(false)}
      />
    </>
  );
}
