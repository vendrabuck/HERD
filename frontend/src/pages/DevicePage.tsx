import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import toast from "react-hot-toast";
import { useDevice, useUpdateDevice, useDeleteDevice } from "@/api/inventory";
import { useTemplate } from "@/api/templates";
import { useAuthStore } from "@/stores/authStore";
import { DynamicFieldRenderer } from "@/components/devices/DynamicFieldRenderer";
import { PortsSection } from "@/components/devices/PortsSection";
import { DeviceConfigSection } from "@/components/device-config/DeviceConfigSection";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { DeviceInfoPanel } from "@/components/inventory/DeviceInfoPanel";
import type { TopologyType, DeviceStatus } from "@/types/device.types";

const STATUS_COLORS: Record<string, string> = {
  AVAILABLE: "bg-green-100 text-green-800",
  RESERVED: "bg-yellow-100 text-yellow-800",
  OFFLINE: "bg-gray-100 text-gray-600",
  MAINTENANCE: "bg-red-100 text-red-700",
};

const TOPOLOGY_COLORS: Record<string, string> = {
  PHYSICAL: "bg-blue-100 text-blue-800",
  CLOUD: "bg-purple-100 text-purple-800",
};

export function DevicePage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { data: device, isLoading, isError } = useDevice(id);
  const { data: template } = useTemplate(device?.template_id);
  const updateDevice = useUpdateDevice();
  const deleteDevice = useDeleteDevice();
  const user = useAuthStore((s) => s.user);
  const isAdmin = user?.role === "admin" || user?.role === "superadmin";

  const [editing, setEditing] = useState(false);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [name, setName] = useState("");
  const [topologyType, setTopologyType] = useState<TopologyType>("PHYSICAL");
  const [status, setStatus] = useState<DeviceStatus>("AVAILABLE");
  const [fieldData, setFieldData] = useState<Record<string, unknown>>({});
  const [syncedId, setSyncedId] = useState<string | undefined>();

  if (device && device.id !== syncedId) {
    setSyncedId(device.id);
    setName(device.name);
    setTopologyType(device.topology_type);
    setStatus(device.status);
    setFieldData(device.field_data);
  }

  const handleSave = async () => {
    if (!name.trim()) {
      toast.error("Name is required");
      return;
    }
    try {
      await updateDevice.mutateAsync({
        id: id!,
        data: {
          name: name.trim(),
          topology_type: topologyType,
          status,
          field_data: fieldData,
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
    try {
      await deleteDevice.mutateAsync(id!);
      toast.success("Device deleted");
      navigate("/inventory");
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Failed to delete device";
      toast.error(msg);
    }
    setShowDeleteConfirm(false);
  };

  const handleCancel = () => {
    if (device) {
      setName(device.name);
      setTopologyType(device.topology_type);
      setStatus(device.status);
      setFieldData(device.field_data);
    }
    setEditing(false);
  };

  if (isLoading) {
    return (
      <div className="h-full flex items-center justify-center">
        <p className="text-sm text-gray-400">Loading device...</p>
      </div>
    );
  }

  if (isError || !device) {
    return (
      <div className="h-full flex items-center justify-center">
        <p className="text-sm text-red-500">Device not found</p>
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-6xl mx-auto px-6 py-6 flex gap-6">
        <aside className="w-60 shrink-0">
          <div className="bg-white rounded-lg border border-gray-200 p-4 sticky top-6">
            <DeviceInfoPanel device={device} />
          </div>
        </aside>
        <div className="flex-1 min-w-0 space-y-6">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold text-gray-900">
            {editing ? "Edit Device" : "Device Details"}
          </h2>
          <div className="flex gap-2">
            <button
              onClick={() => navigate("/inventory")}
              className="px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 rounded-lg hover:bg-gray-200 transition-colors"
            >
              Back
            </button>
            {editing ? (
              <>
                <button
                  onClick={handleCancel}
                  className="px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 rounded-lg hover:bg-gray-200 transition-colors"
                >
                  Cancel
                </button>
                <button
                  onClick={handleSave}
                  disabled={updateDevice.isPending}
                  className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors"
                >
                  {updateDevice.isPending ? "Saving..." : "Save"}
                </button>
              </>
            ) : (
              isAdmin && (
                <>
                  <button
                    onClick={() => setShowDeleteConfirm(true)}
                    className="px-4 py-2 text-sm font-medium text-red-600 bg-red-50 rounded-lg hover:bg-red-100 transition-colors"
                  >
                    Delete
                  </button>
                  <button
                    onClick={() => setEditing(true)}
                    className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition-colors"
                  >
                    Edit
                  </button>
                </>
              )
            )}
          </div>
        </div>

        <div className="bg-white rounded-lg border border-gray-200 p-4 space-y-4">
          {editing ? (
            <div className="grid grid-cols-3 gap-4">
              <div>
                <label htmlFor="dev-name" className="block text-sm font-medium text-gray-700 mb-1">
                  Name
                </label>
                <input
                  id="dev-name"
                  type="text"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
              </div>
              <div>
                <label htmlFor="dev-topology" className="block text-sm font-medium text-gray-700 mb-1">
                  Topology Type
                </label>
                <select
                  id="dev-topology"
                  value={topologyType}
                  onChange={(e) => setTopologyType(e.target.value as TopologyType)}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                >
                  <option value="PHYSICAL">Physical</option>
                  <option value="CLOUD">Cloud</option>
                </select>
              </div>
              <div>
                <label htmlFor="dev-status" className="block text-sm font-medium text-gray-700 mb-1">
                  Status
                </label>
                <select
                  id="dev-status"
                  value={status}
                  onChange={(e) => setStatus(e.target.value as DeviceStatus)}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                >
                  <option value="AVAILABLE">Available</option>
                  <option value="RESERVED">Reserved</option>
                  <option value="OFFLINE">Offline</option>
                  <option value="MAINTENANCE">Maintenance</option>
                </select>
              </div>
            </div>
          ) : (
            <>
              <div className="flex items-center gap-4">
                {device.template_icon ? (
                  <img
                    src={device.template_icon}
                    alt={device.template_name ?? ""}
                    className="w-12 h-12 object-contain rounded border border-gray-200"
                  />
                ) : (
                  <span className="inline-block w-12 h-12 bg-gray-200 rounded" />
                )}
                <div>
                  <h3 className="text-base font-semibold text-gray-900">{device.name}</h3>
                  <p className="text-sm text-gray-500">{device.template_name ?? "No template"}</p>
                </div>
              </div>
              <div className="grid grid-cols-3 gap-4 pt-2">
                <div>
                  <p className="text-xs font-medium text-gray-500 uppercase mb-1">Topology</p>
                  <span className={`text-xs px-1.5 py-0.5 rounded ${TOPOLOGY_COLORS[device.topology_type]}`}>
                    {device.topology_type}
                  </span>
                </div>
                <div>
                  <p className="text-xs font-medium text-gray-500 uppercase mb-1">Status</p>
                  <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${STATUS_COLORS[device.status]}`}>
                    {device.status}
                  </span>
                </div>
                <div>
                  <p className="text-xs font-medium text-gray-500 uppercase mb-1">ID</p>
                  <p className="text-sm text-gray-600 font-mono">{device.id}</p>
                </div>
              </div>
            </>
          )}

          {editing ? (
            <>
              <div>
                <p className="text-sm font-medium text-gray-700 mb-1">
                  Template: {device.template_name ?? "None"} (not editable)
                </p>
              </div>
              {template && (
                <DynamicFieldRenderer
                  sections={template.sections}
                  fieldData={fieldData}
                  onChange={setFieldData}
                />
              )}
            </>
          ) : (
            template &&
            template.sections.length > 0 && (
              <div className="pt-2 space-y-3">
                {template.sections.map((section) => (
                  <div key={section.name}>
                    <p className="text-sm font-semibold text-gray-700 mb-1">{section.name}</p>
                    <div className="grid grid-cols-2 gap-x-6 gap-y-1">
                      {section.fields.map((field) => {
                        const val = device.field_data[field.key];
                        const display =
                          val === undefined || val === null || val === ""
                            ? "-"
                            : field.type === "password"
                              ? "********"
                              : field.type === "boolean"
                                ? val
                                  ? "Yes"
                                  : "No"
                                : String(val);
                        return (
                          <div key={field.key} className="flex justify-between text-sm py-0.5">
                            <span className="text-gray-500">{field.label}</span>
                            <span className="text-gray-900">{display}</span>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                ))}
              </div>
            )
          )}
        </div>

        <div className="bg-white rounded-lg border border-gray-200 p-4">
          <PortsSection deviceId={device.id} isAdmin={isAdmin} />
        </div>

        <DeviceConfigSection deviceId={device.id} />

        <ConfirmDialog
          open={showDeleteConfirm}
          title="Delete Device"
          description={`Delete device "${device.name}"? This will also delete all ports. This cannot be undone.`}
          confirmLabel="Delete"
          destructive
          onConfirm={handleDelete}
          onCancel={() => setShowDeleteConfirm(false)}
        />
        </div>
      </div>
    </div>
  );
}
