import { useState, useMemo } from "react";
import { Modal } from "@/components/ui/Modal";
import { useUpdateReservation } from "@/api/reservations";
import { usePaginatedDevices, useAllDeviceNames } from "@/api/inventory";
import toast from "react-hot-toast";
import type { Reservation } from "@/types/reservation.types";

interface Props {
  reservation: Reservation;
  open: boolean;
  onClose: () => void;
  onUpdated: () => void;
}

export function EditDevicesModal({ reservation, open, onClose, onUpdated }: Props) {
  const [selectedIds, setSelectedIds] = useState<Set<string>>(
    () => new Set(reservation.device_ids),
  );
  const [search, setSearch] = useState("");
  const update = useUpdateReservation();
  const { data: deviceNames } = useAllDeviceNames();

  // Fetch available devices matching the reservation's topology type
  const { data: availableData, isLoading } = usePaginatedDevices(
    {
      topology_type: reservation.topology_type,
      status: "AVAILABLE",
      dut_only: true,
      search: search || undefined,
    },
    0,
    50,
  );

  const availableDevices = availableData?.items ?? [];

  // Merge available devices with currently selected ones for display
  const currentDeviceNames = useMemo(() => {
    const map = new Map<string, string>();
    for (const id of selectedIds) {
      map.set(id, deviceNames?.get(id) ?? id.slice(0, 8) + "...");
    }
    return map;
  }, [selectedIds, deviceNames]);

  const handleToggle = (id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };

  const handleSave = async () => {
    if (selectedIds.size === 0) {
      toast.error("At least one device must be selected");
      return;
    }
    try {
      await update.mutateAsync({
        id: reservation.id,
        data: { device_ids: Array.from(selectedIds) },
      });
      toast.success("Reservation devices updated");
      onUpdated();
      onClose();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Failed to update devices";
      toast.error(msg);
    }
  };

  const hasChanges = useMemo(() => {
    const original = new Set(reservation.device_ids);
    if (original.size !== selectedIds.size) return true;
    for (const id of selectedIds) {
      if (!original.has(id)) return true;
    }
    return false;
  }, [reservation.device_ids, selectedIds]);

  return (
    <Modal open={open} onClose={onClose} title="Edit Reservation Devices" className="max-w-2xl">
      <div className="space-y-4">
        {/* Current devices */}
        <div>
          <h4 className="text-xs font-medium text-gray-500 uppercase mb-2">
            Selected Devices ({selectedIds.size})
          </h4>
          <div className="border border-gray-200 rounded max-h-40 overflow-y-auto">
            {selectedIds.size === 0 ? (
              <p className="text-sm text-gray-400 text-center py-3">No devices selected</p>
            ) : (
              Array.from(selectedIds).map((id) => (
                <div
                  key={id}
                  className="flex items-center justify-between px-3 py-1.5 border-b border-gray-100 last:border-b-0"
                >
                  <span className="text-sm text-gray-700">
                    {currentDeviceNames.get(id) ?? id.slice(0, 8) + "..."}
                  </span>
                  <button
                    onClick={() => handleToggle(id)}
                    className="text-xs text-red-500 hover:text-red-700 px-1.5 py-0.5"
                  >
                    Remove
                  </button>
                </div>
              ))
            )}
          </div>
        </div>

        {/* Add devices */}
        <div>
          <h4 className="text-xs font-medium text-gray-500 uppercase mb-2">Add Devices</h4>
          <input
            type="text"
            placeholder="Search available devices..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full border border-gray-300 rounded px-3 py-1.5 text-sm mb-2"
          />
          <div className="border border-gray-200 rounded max-h-48 overflow-y-auto">
            {isLoading && (
              <p className="text-sm text-gray-400 text-center py-3">Loading...</p>
            )}
            {!isLoading && availableDevices.length === 0 && (
              <p className="text-sm text-gray-400 text-center py-3">No available devices found</p>
            )}
            {availableDevices
              .filter((d) => !selectedIds.has(d.id))
              .map((d) => (
                <div
                  key={d.id}
                  className="flex items-center justify-between px-3 py-1.5 border-b border-gray-100 last:border-b-0 hover:bg-gray-50 cursor-pointer"
                  onClick={() => handleToggle(d.id)}
                >
                  <div>
                    <span className="text-sm text-gray-700">{d.name}</span>
                    <span className="text-xs text-gray-400 ml-2">{d.template_name}</span>
                  </div>
                  <span className="text-xs text-blue-600">Add</span>
                </div>
              ))}
          </div>
        </div>

        {/* Actions */}
        <div className="flex gap-2 pt-2 border-t border-gray-200">
          <button
            onClick={handleSave}
            disabled={update.isPending || !hasChanges}
            className="text-sm px-3 py-1.5 rounded bg-gray-900 text-white hover:bg-gray-800 disabled:opacity-50"
          >
            {update.isPending ? "Saving..." : "Save Changes"}
          </button>
          <button
            onClick={onClose}
            className="text-sm px-3 py-1.5 rounded border border-gray-300 text-gray-600 hover:bg-gray-50"
          >
            Cancel
          </button>
        </div>
      </div>
    </Modal>
  );
}
