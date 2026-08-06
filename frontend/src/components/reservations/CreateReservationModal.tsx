import { useState } from "react";
import toast from "react-hot-toast";
import { useCreateReservation } from "@/api/reservations";
import { useTemplates } from "@/api/templates";
import { Modal } from "@/components/ui/Modal";
import type { DynamicRequestSpec } from "@/types/reservation.types";

// Mirrors the backend cap: ReservationCreate.dynamic_requests has max_length=50.
const MAX_DYNAMIC_REQUESTS = 50;

export interface DynamicEntry {
  templateId: string;
  count: number;
}

function clampCount(raw: number): number {
  if (Number.isNaN(raw) || raw < 1) return 1;
  if (raw > MAX_DYNAMIC_REQUESTS) return MAX_DYNAMIC_REQUESTS;
  return Math.floor(raw);
}

interface CreateReservationModalProps {
  open: boolean;
  deviceIds: string[];
  topologyId?: string;
  // Prefills the dynamic-request list (e.g. from the topology canvas's dynamic
  // placeholders). Applied once at mount: callers that keep the modal mounted
  // while toggling `open` must remount it to re-prefill.
  initialDynamicEntries?: DynamicEntry[];
  onClose: () => void;
}

export function CreateReservationModal({
  open,
  deviceIds,
  topologyId,
  initialDynamicEntries,
  onClose,
}: CreateReservationModalProps) {
  const create = useCreateReservation();
  const [startTime, setStartTime] = useState("");
  const [endTime, setEndTime] = useState("");
  const [purpose, setPurpose] = useState("");
  const [dynamicEntries, setDynamicEntries] = useState<DynamicEntry[]>(() =>
    (initialDynamicEntries ?? []).map((entry) => ({
      templateId: entry.templateId,
      count: clampCount(entry.count),
    })),
  );

  // Only fetch dynamic templates while the modal is open; this component stays
  // mounted (closed) on pages like the topology editor.
  const { data: dynamicTemplates } = useTemplates("dynamic", { enabled: open });

  const totalDynamic = dynamicEntries.reduce((sum, e) => sum + e.count, 0);
  const overCap = totalDynamic > MAX_DYNAMIC_REQUESTS;
  const hasResources = deviceIds.length > 0 || totalDynamic > 0;

  const addEntry = () => {
    if (!dynamicTemplates || dynamicTemplates.length === 0) return;
    setDynamicEntries((entries) => [...entries, { templateId: dynamicTemplates[0].id, count: 1 }]);
  };

  const updateEntry = (index: number, patch: Partial<DynamicEntry>) => {
    setDynamicEntries((entries) =>
      entries.map((entry, i) => (i === index ? { ...entry, ...patch } : entry)),
    );
  };

  const removeEntry = (index: number) => {
    setDynamicEntries((entries) => entries.filter((_, i) => i !== index));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    if (new Date(endTime) <= new Date(startTime)) {
      toast.error("End time must be after start time");
      return;
    }

    if (!hasResources) {
      toast.error("Select at least one device or add a dynamic instance");
      return;
    }

    if (overCap) {
      toast.error(`A reservation can include at most ${MAX_DYNAMIC_REQUESTS} dynamic instances`);
      return;
    }

    // Each entry expands into count repeated {template_id} items; the backend
    // treats N repetitions of a template_id as a request for N instances.
    const dynamicRequests: DynamicRequestSpec[] = dynamicEntries.flatMap((entry) =>
      Array.from({ length: entry.count }, () => ({ template_id: entry.templateId })),
    );

    try {
      await create.mutateAsync({
        device_ids: deviceIds,
        topology_id: topologyId,
        start_time: new Date(startTime).toISOString(),
        end_time: new Date(endTime).toISOString(),
        purpose: purpose || undefined,
        // Absent and [] behave the same server-side, but omit when empty so
        // device-only requests keep their pre-dynamic wire shape.
        ...(dynamicRequests.length > 0 ? { dynamic_requests: dynamicRequests } : {}),
      });
      toast.success("Reservation created");
      onClose();
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Failed to create reservation";
      toast.error(msg);
    }
  };

  return (
    <Modal open={open} onClose={onClose} title="Create Reservation">
      <form onSubmit={handleSubmit} className="space-y-4">
        <p className="text-sm text-gray-600">
          {deviceIds.length} device{deviceIds.length !== 1 ? "s" : ""} selected
        </p>

        <div>
          <p className="block text-sm font-medium text-gray-700 mb-1">Dynamic instances</p>
          {dynamicEntries.length === 0 && (
            <p className="text-xs text-gray-500 mb-2">
              Book hypervisor-backed instances from a dynamic template.
            </p>
          )}
          <div className="space-y-2">
            {dynamicEntries.map((entry, i) => (
              <div key={i} className="flex items-center gap-2">
                <select
                  aria-label={`Dynamic template ${i + 1}`}
                  value={entry.templateId}
                  onChange={(e) => updateEntry(i, { templateId: e.target.value })}
                  className="flex-1 border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                >
                  {(dynamicTemplates ?? []).map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.name}
                    </option>
                  ))}
                </select>
                <input
                  aria-label={`Instance count ${i + 1}`}
                  type="number"
                  min={1}
                  max={MAX_DYNAMIC_REQUESTS}
                  value={entry.count}
                  onChange={(e) => updateEntry(i, { count: clampCount(e.target.valueAsNumber) })}
                  className="w-20 border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
                <button
                  type="button"
                  aria-label={`Remove dynamic request ${i + 1}`}
                  onClick={() => removeEntry(i)}
                  className="text-xs text-red-600 hover:text-red-800 px-2 py-1 rounded hover:bg-red-50"
                >
                  Remove
                </button>
              </div>
            ))}
          </div>
          <button
            type="button"
            onClick={addEntry}
            disabled={!dynamicTemplates || dynamicTemplates.length === 0}
            className="mt-2 text-sm font-medium text-blue-600 hover:text-blue-800 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Add dynamic instance
          </button>
          {dynamicTemplates && dynamicTemplates.length === 0 && (
            <p className="text-xs text-gray-400 mt-1">No dynamic templates available</p>
          )}
          {overCap && (
            <p role="alert" className="text-xs text-red-600 mt-1">
              A reservation can include at most {MAX_DYNAMIC_REQUESTS} dynamic instances
            </p>
          )}
        </div>

        <div>
          <label htmlFor="res-start" className="block text-sm font-medium text-gray-700 mb-1">
            Start time
          </label>
          <input
            id="res-start"
            type="datetime-local"
            required
            value={startTime}
            onChange={(e) => setStartTime(e.target.value)}
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>

        <div>
          <label htmlFor="res-end" className="block text-sm font-medium text-gray-700 mb-1">
            End time
          </label>
          <input
            id="res-end"
            type="datetime-local"
            required
            value={endTime}
            onChange={(e) => setEndTime(e.target.value)}
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>

        <div>
          <label htmlFor="res-purpose" className="block text-sm font-medium text-gray-700 mb-1">
            Purpose (optional)
          </label>
          <input
            id="res-purpose"
            type="text"
            value={purpose}
            onChange={(e) => setPurpose(e.target.value)}
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            placeholder="Testing, deployment, etc."
          />
        </div>

        <div className="flex justify-end gap-3 pt-2">
          <button
            type="button"
            onClick={onClose}
            className="px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 rounded-lg hover:bg-gray-200 transition-colors"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={create.isPending || !hasResources || overCap}
            aria-busy={create.isPending}
            className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors"
          >
            {create.isPending ? "Creating..." : "Create"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
