import { useState, useMemo } from "react";
import { Link } from "react-router-dom";
import toast from "react-hot-toast";
import { usePorts, useCreatePort, useCreatePortsBulk, useDeletePort } from "@/api/ports";
import { useTemplates } from "@/api/templates";
import { useDeviceConnections } from "@/api/connections";
import { useAllDeviceNames } from "@/api/inventory";
import { DynamicFieldRenderer } from "@/components/devices/DynamicFieldRenderer";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { Modal } from "@/components/ui/Modal";
import type { Port } from "@/types/port.types";
import type { DeviceTemplate } from "@/types/template.types";

interface PortsSectionProps {
  deviceId: string;
  isAdmin?: boolean;
}

export function PortsSection({ deviceId, isAdmin }: PortsSectionProps) {
  const { data: ports, isLoading } = usePorts(deviceId);
  const { data: portTemplates } = useTemplates("port");
  const createPort = useCreatePort();
  const createPortsBulk = useCreatePortsBulk();
  const deletePort = useDeletePort();

  const { data: connections } = useDeviceConnections(deviceId);
  const { data: deviceNameMap } = useAllDeviceNames();

  const connectionMap = useMemo(() => {
    const map = new Map<string, { deviceId: string; deviceName: string; portName: string }>();
    if (!connections) return map;
    for (const conn of connections) {
      const isA = conn.device_a_id === deviceId;
      const thisPort = isA ? conn.port_a : conn.port_b;
      const otherDeviceId = isA ? conn.device_b_id : conn.device_a_id;
      const otherPort = isA ? conn.port_b : conn.port_a;
      const otherName = deviceNameMap?.get(otherDeviceId) ?? otherDeviceId.slice(0, 8) + "...";
      map.set(thisPort, { deviceId: otherDeviceId, deviceName: otherName, portName: otherPort });
    }
    return map;
  }, [connections, deviceId, deviceNameMap]);

  const [showForm, setShowForm] = useState(false);
  const [name, setName] = useState("");
  const [templateId, setTemplateId] = useState("");
  const [fieldData, setFieldData] = useState<Record<string, unknown>>({});
  const [deleteTarget, setDeleteTarget] = useState<Port | null>(null);

  const [showBulkModal, setShowBulkModal] = useState(false);
  const [bulkPrefix, setBulkPrefix] = useState("");
  const [bulkStart, setBulkStart] = useState(0);
  const [bulkInstances, setBulkInstances] = useState(1);
  const [bulkTemplateId, setBulkTemplateId] = useState("");
  const [bulkFieldData, setBulkFieldData] = useState<Record<string, unknown>>({});

  const selectedTemplate: DeviceTemplate | undefined = portTemplates?.find(
    (t) => t.id === templateId,
  );
  const bulkSelectedTemplate: DeviceTemplate | undefined = portTemplates?.find(
    (t) => t.id === bulkTemplateId,
  );

  const handleTemplateChange = (id: string) => {
    setTemplateId(id);
    setFieldData({});
  };

  const resetForm = () => {
    setName("");
    setTemplateId("");
    setFieldData({});
    setShowForm(false);
  };

  const handleCreate = async () => {
    if (!name.trim()) {
      toast.error("Port name is required");
      return;
    }
    if (!templateId) {
      toast.error("Select a port template");
      return;
    }
    try {
      await createPort.mutateAsync({
        deviceId,
        data: {
          name: name.trim(),
          template_id: templateId,
          field_data: fieldData,
        },
      });
      toast.success("Port added");
      resetForm();
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Failed to create port";
      toast.error(msg);
    }
  };

  const resetBulkForm = () => {
    setBulkPrefix("");
    setBulkStart(0);
    setBulkInstances(1);
    setBulkTemplateId("");
    setBulkFieldData({});
    setShowBulkModal(false);
  };

  const handleBulkCreate = async () => {
    if (!bulkPrefix.trim()) {
      toast.error("Name prefix is required");
      return;
    }
    if (!bulkTemplateId) {
      toast.error("Select a port template");
      return;
    }
    try {
      const result = await createPortsBulk.mutateAsync({
        deviceId,
        data: {
          name_prefix: bulkPrefix.trim(),
          starting_index: bulkStart,
          instances: bulkInstances,
          template_id: bulkTemplateId,
          field_data: bulkFieldData,
        },
      });
      toast.success(`${result.length} ports created`);
      resetBulkForm();
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Failed to create ports";
      toast.error(msg);
    }
  };

  const handleDelete = async () => {
    if (!deleteTarget) return;
    try {
      await deletePort.mutateAsync(deleteTarget.id);
      toast.success("Port deleted");
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Failed to delete port";
      toast.error(msg);
    }
    setDeleteTarget(null);
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-700">Ports</h3>
        {isAdmin && !showForm && (
          <div className="flex gap-3">
            <button
              type="button"
              onClick={() => setShowForm(true)}
              className="text-sm text-blue-600 hover:text-blue-800"
            >
              + Add Port
            </button>
            <button
              type="button"
              onClick={() => setShowBulkModal(true)}
              className="text-sm text-blue-600 hover:text-blue-800"
            >
              + Add Multiple Ports
            </button>
          </div>
        )}
      </div>

      {isLoading && (
        <p className="text-sm text-gray-400">Loading ports...</p>
      )}

      {ports && ports.length > 0 && (
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-gray-200 bg-gray-50">
              <th className="px-3 py-1.5 text-left text-xs font-medium text-gray-500">
                Name
              </th>
              <th className="px-3 py-1.5 text-left text-xs font-medium text-gray-500">
                Template
              </th>
              <th className="px-3 py-1.5 text-left text-xs font-medium text-gray-500">
                Fields
              </th>
              <th className="px-3 py-1.5 text-left text-xs font-medium text-gray-500">
                Connected To
              </th>
              {isAdmin && (
                <th className="px-3 py-1.5 text-left text-xs font-medium text-gray-500">
                  Actions
                </th>
              )}
            </tr>
          </thead>
          <tbody>
            {ports.map((p) => (
              <tr key={p.id} className="border-b border-gray-100">
                <td className="px-3 py-1.5 font-medium text-gray-900">
                  {p.name}
                </td>
                <td className="px-3 py-1.5 text-gray-500">
                  {p.template_name ?? "-"}
                </td>
                <td className="px-3 py-1.5 text-gray-500">
                  {Object.entries(p.field_data)
                    .map(([k, v]) => `${k}: ${v}`)
                    .join(", ") || "-"}
                </td>
                <td className="px-3 py-1.5 text-gray-500">
                  {connectionMap.get(p.name) ? (
                    <Link
                      to={`/inventory/${connectionMap.get(p.name)!.deviceId}`}
                      className="text-blue-600 hover:text-blue-800 hover:underline"
                    >
                      {connectionMap.get(p.name)!.deviceName}, {connectionMap.get(p.name)!.portName}
                    </Link>
                  ) : (
                    "-"
                  )}
                </td>
                {isAdmin && (
                  <td className="px-3 py-1.5">
                    <button
                      type="button"
                      onClick={() => setDeleteTarget(p)}
                      className="text-red-500 hover:text-red-700"
                    >
                      Delete
                    </button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {ports && ports.length === 0 && !showForm && (
        <p className="text-sm text-gray-400">No ports configured</p>
      )}

      {showForm && (
        <div className="border border-gray-200 rounded-lg p-3 space-y-3 bg-gray-50">
          <div>
            <label
              htmlFor="port-name"
              className="block text-sm font-medium text-gray-700 mb-1"
            >
              Port name
            </label>
            <input
              id="port-name"
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. GigE0/1"
              className="w-full border border-gray-300 rounded px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <div>
            <label
              htmlFor="port-template"
              className="block text-sm font-medium text-gray-700 mb-1"
            >
              Port template
            </label>
            <select
              id="port-template"
              value={templateId}
              onChange={(e) => handleTemplateChange(e.target.value)}
              className="w-full border border-gray-300 rounded px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              <option value="">Select a port template...</option>
              {portTemplates?.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
          </div>
          {selectedTemplate && (
            <DynamicFieldRenderer
              sections={selectedTemplate.sections}
              fieldData={fieldData}
              onChange={setFieldData}
            />
          )}
          <div className="flex gap-2">
            <button
              type="button"
              onClick={handleCreate}
              disabled={createPort.isPending}
              className="px-3 py-1.5 text-sm font-medium text-white bg-blue-600 rounded hover:bg-blue-700 disabled:opacity-50 transition-colors"
            >
              {createPort.isPending ? "Adding..." : "Add Port"}
            </button>
            <button
              type="button"
              onClick={resetForm}
              className="px-3 py-1.5 text-sm font-medium text-gray-700 bg-gray-200 rounded hover:bg-gray-300 transition-colors"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      <ConfirmDialog
        open={!!deleteTarget}
        title="Delete Port"
        description={`Delete port "${deleteTarget?.name}"? This cannot be undone.`}
        confirmLabel="Delete"
        destructive
        onConfirm={handleDelete}
        onCancel={() => setDeleteTarget(null)}
      />

      <Modal
        open={showBulkModal}
        onClose={resetBulkForm}
        title="Add Multiple Ports"
      >
        <div className="space-y-4">
          <div>
            <label
              htmlFor="bulk-prefix"
              className="block text-sm font-medium text-gray-700 mb-1"
            >
              Name prefix
            </label>
            <input
              id="bulk-prefix"
              type="text"
              value={bulkPrefix}
              onChange={(e) => setBulkPrefix(e.target.value)}
              placeholder="e.g. fastethernet0/1/"
              className="w-full border border-gray-300 rounded px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <div className="flex gap-3">
            <div className="flex-1">
              <label
                htmlFor="bulk-start"
                className="block text-sm font-medium text-gray-700 mb-1"
              >
                Starting index
              </label>
              <input
                id="bulk-start"
                type="number"
                min={0}
                value={bulkStart}
                onChange={(e) => setBulkStart(Number(e.target.value))}
                className="w-full border border-gray-300 rounded px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>
            <div className="flex-1">
              <label
                htmlFor="bulk-instances"
                className="block text-sm font-medium text-gray-700 mb-1"
              >
                Instances
              </label>
              <input
                id="bulk-instances"
                type="number"
                min={1}
                max={200}
                value={bulkInstances}
                onChange={(e) => setBulkInstances(Number(e.target.value))}
                className="w-full border border-gray-300 rounded px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>
          </div>
          <div>
            <label
              htmlFor="bulk-template"
              className="block text-sm font-medium text-gray-700 mb-1"
            >
              Port template
            </label>
            <select
              id="bulk-template"
              value={bulkTemplateId}
              onChange={(e) => {
                setBulkTemplateId(e.target.value);
                setBulkFieldData({});
              }}
              className="w-full border border-gray-300 rounded px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              <option value="">Select a port template...</option>
              {portTemplates?.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
          </div>
          {bulkSelectedTemplate && (
            <DynamicFieldRenderer
              sections={bulkSelectedTemplate.sections}
              fieldData={bulkFieldData}
              onChange={setBulkFieldData}
            />
          )}
          {bulkPrefix && bulkInstances > 0 && (
            <p className="text-sm text-gray-500">
              Will create: {bulkPrefix}{bulkStart} through {bulkPrefix}{bulkStart + bulkInstances - 1}
            </p>
          )}
          <div className="flex gap-2 justify-end">
            <button
              type="button"
              onClick={resetBulkForm}
              className="px-3 py-1.5 text-sm font-medium text-gray-700 bg-gray-200 rounded hover:bg-gray-300 transition-colors"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleBulkCreate}
              disabled={createPortsBulk.isPending}
              className="px-3 py-1.5 text-sm font-medium text-white bg-blue-600 rounded hover:bg-blue-700 disabled:opacity-50 transition-colors"
            >
              {createPortsBulk.isPending ? "Creating..." : "Create"}
            </button>
          </div>
        </div>
      </Modal>
    </div>
  );
}
