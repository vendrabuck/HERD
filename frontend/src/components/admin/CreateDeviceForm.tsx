import { useState } from "react";
import toast from "react-hot-toast";
import { useCreateDevice } from "@/api/inventory";
import { useTemplates } from "@/api/templates";
import { DynamicFieldRenderer } from "@/components/devices/DynamicFieldRenderer";
import type { TopologyType, DeviceStatus } from "@/types/device.types";
import type { DeviceTemplate } from "@/types/template.types";

const TOPOLOGY_TYPES: { label: string; value: TopologyType }[] = [
  { label: "Physical", value: "PHYSICAL" },
  { label: "Cloud", value: "CLOUD" },
];

const STATUS_OPTIONS: { label: string; value: DeviceStatus }[] = [
  { label: "Available", value: "AVAILABLE" },
  { label: "Reserved", value: "RESERVED" },
  { label: "Offline", value: "OFFLINE" },
  { label: "Maintenance", value: "MAINTENANCE" },
];

interface FormState {
  name: string;
  template_id: string;
  topology_type: TopologyType;
  status: DeviceStatus;
  field_data: Record<string, unknown>;
  // Blank = inherit from template; numeric = override the template's value.
  poll_interval_seconds: string;
}

const INITIAL_FORM: FormState = {
  name: "",
  template_id: "",
  topology_type: "PHYSICAL",
  status: "AVAILABLE",
  field_data: {},
  poll_interval_seconds: "",
};

export function CreateDeviceForm() {
  const create = useCreateDevice();
  const { data: templates } = useTemplates("device");
  const [form, setForm] = useState<FormState>(INITIAL_FORM);

  const selectedTemplate: DeviceTemplate | undefined = templates?.find(
    (t) => t.id === form.template_id,
  );

  const handleTemplateChange = (templateId: string) => {
    setForm((prev) => ({
      ...prev,
      template_id: templateId,
      field_data: {},
    }));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!form.template_id) {
      toast.error("Select a template");
      return;
    }
    try {
      const trimmed = form.poll_interval_seconds.trim();
      const pollInterval = trimmed === "" ? null : Number(trimmed);
      if (pollInterval !== null && (!Number.isInteger(pollInterval) || pollInterval < 30)) {
        toast.error("Poll interval must be a whole number >= 30 (or blank to inherit)");
        return;
      }
      await create.mutateAsync({
        name: form.name,
        template_id: form.template_id,
        topology_type: form.topology_type,
        status: form.status,
        field_data: form.field_data,
        poll_interval_seconds: pollInterval,
      });
      toast.success("Device created");
      setForm(INITIAL_FORM);
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Failed to create device";
      toast.error(msg);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-4 max-w-md">
      <div>
        <label
          htmlFor="dev-name"
          className="block text-sm font-medium text-gray-700 mb-1"
        >
          Name
        </label>
        <input
          id="dev-name"
          type="text"
          required
          value={form.name}
          onChange={(e) => setForm((prev) => ({ ...prev, name: e.target.value }))}
          className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
      </div>

      <div>
        <label
          htmlFor="dev-template"
          className="block text-sm font-medium text-gray-700 mb-1"
        >
          Template
        </label>
        <select
          id="dev-template"
          value={form.template_id}
          onChange={(e) => handleTemplateChange(e.target.value)}
          className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        >
          <option value="">Select a template...</option>
          {templates?.map((t) => (
            <option key={t.id} value={t.id}>
              {t.name}
            </option>
          ))}
        </select>
      </div>

      <div className="grid grid-cols-2 gap-4">
        <div>
          <label
            htmlFor="dev-topo"
            className="block text-sm font-medium text-gray-700 mb-1"
          >
            Topology type
          </label>
          <select
            id="dev-topo"
            value={form.topology_type}
            onChange={(e) =>
              setForm((prev) => ({
                ...prev,
                topology_type: e.target.value as TopologyType,
              }))
            }
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            {TOPOLOGY_TYPES.map((t) => (
              <option key={t.value} value={t.value}>
                {t.label}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label
            htmlFor="dev-status"
            className="block text-sm font-medium text-gray-700 mb-1"
          >
            Status
          </label>
          <select
            id="dev-status"
            value={form.status}
            onChange={(e) =>
              setForm((prev) => ({
                ...prev,
                status: e.target.value as DeviceStatus,
              }))
            }
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            {STATUS_OPTIONS.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div>
        <label
          htmlFor="dev-poll-interval"
          className="block text-sm font-medium text-gray-700 mb-1"
        >
          Health-poll interval (seconds)
        </label>
        <input
          id="dev-poll-interval"
          type="number"
          min={30}
          step={1}
          placeholder="leave blank to inherit from template"
          value={form.poll_interval_seconds}
          onChange={(e) =>
            setForm((prev) => ({ ...prev, poll_interval_seconds: e.target.value }))
          }
          className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
        <p className="text-xs text-gray-500 mt-1">
          Minimum 30. Blank means use the template default, or no polling if the template has none.
        </p>
      </div>

      {selectedTemplate && (
        <DynamicFieldRenderer
          sections={selectedTemplate.sections}
          fieldData={form.field_data}
          onChange={(field_data) =>
            setForm((prev) => ({ ...prev, field_data }))
          }
        />
      )}

      <button
        type="submit"
        disabled={create.isPending}
        aria-busy={create.isPending}
        className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors"
      >
        {create.isPending ? "Creating..." : "Add Device"}
      </button>
    </form>
  );
}
