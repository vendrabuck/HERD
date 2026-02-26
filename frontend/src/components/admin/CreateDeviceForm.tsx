import { useState } from "react";
import toast from "react-hot-toast";
import { useCreateDevice } from "@/api/inventory";
import type { DeviceType, TopologyType, DeviceStatus } from "@/types/device.types";

const DEVICE_TYPES: { label: string; value: DeviceType }[] = [
  { label: "Firewall", value: "FIREWALL" },
  { label: "Switch", value: "SWITCH" },
  { label: "Router", value: "ROUTER" },
  { label: "Traffic Shaper", value: "TRAFFIC_SHAPER" },
  { label: "Other", value: "OTHER" },
];

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
  device_type: DeviceType;
  topology_type: TopologyType;
  status: DeviceStatus;
  location: string;
  description: string;
}

const INITIAL_FORM: FormState = {
  name: "",
  device_type: "ROUTER",
  topology_type: "PHYSICAL",
  status: "AVAILABLE",
  location: "",
  description: "",
};

export function CreateDeviceForm() {
  const create = useCreateDevice();
  const [form, setForm] = useState<FormState>(INITIAL_FORM);

  const update = (field: keyof FormState, value: string) =>
    setForm((prev) => ({ ...prev, [field]: value }));

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await create.mutateAsync({
        name: form.name,
        device_type: form.device_type,
        topology_type: form.topology_type,
        status: form.status,
        location: form.location || undefined,
        description: form.description || undefined,
      });
      toast.success("Device created");
      setForm(INITIAL_FORM);
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Failed to create device";
      toast.error(msg);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-4 max-w-md">
      <div>
        <label htmlFor="dev-name" className="block text-sm font-medium text-gray-700 mb-1">
          Name
        </label>
        <input
          id="dev-name"
          type="text"
          required
          value={form.name}
          onChange={(e) => update("name", e.target.value)}
          className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
      </div>

      <div className="grid grid-cols-2 gap-4">
        <div>
          <label htmlFor="dev-type" className="block text-sm font-medium text-gray-700 mb-1">
            Device type
          </label>
          <select
            id="dev-type"
            value={form.device_type}
            onChange={(e) => update("device_type", e.target.value)}
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            {DEVICE_TYPES.map((t) => (
              <option key={t.value} value={t.value}>{t.label}</option>
            ))}
          </select>
        </div>

        <div>
          <label htmlFor="dev-topo" className="block text-sm font-medium text-gray-700 mb-1">
            Topology type
          </label>
          <select
            id="dev-topo"
            value={form.topology_type}
            onChange={(e) => update("topology_type", e.target.value)}
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            {TOPOLOGY_TYPES.map((t) => (
              <option key={t.value} value={t.value}>{t.label}</option>
            ))}
          </select>
        </div>
      </div>

      <div>
        <label htmlFor="dev-status" className="block text-sm font-medium text-gray-700 mb-1">
          Status
        </label>
        <select
          id="dev-status"
          value={form.status}
          onChange={(e) => update("status", e.target.value)}
          className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        >
          {STATUS_OPTIONS.map((s) => (
            <option key={s.value} value={s.value}>{s.label}</option>
          ))}
        </select>
      </div>

      <div>
        <label htmlFor="dev-location" className="block text-sm font-medium text-gray-700 mb-1">
          Location (optional)
        </label>
        <input
          id="dev-location"
          type="text"
          value={form.location}
          onChange={(e) => update("location", e.target.value)}
          className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
      </div>

      <div>
        <label htmlFor="dev-desc" className="block text-sm font-medium text-gray-700 mb-1">
          Description (optional)
        </label>
        <textarea
          id="dev-desc"
          value={form.description}
          onChange={(e) => update("description", e.target.value)}
          rows={3}
          className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
      </div>

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
