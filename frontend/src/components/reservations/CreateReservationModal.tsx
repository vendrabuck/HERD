import { useState } from "react";
import toast from "react-hot-toast";
import { useCreateReservation } from "@/api/reservations";
import { Modal } from "@/components/ui/Modal";

interface CreateReservationModalProps {
  open: boolean;
  deviceIds: string[];
  onClose: () => void;
}

export function CreateReservationModal({ open, deviceIds, onClose }: CreateReservationModalProps) {
  const create = useCreateReservation();
  const [startTime, setStartTime] = useState("");
  const [endTime, setEndTime] = useState("");
  const [purpose, setPurpose] = useState("");

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    if (new Date(endTime) <= new Date(startTime)) {
      toast.error("End time must be after start time");
      return;
    }

    try {
      await create.mutateAsync({
        device_ids: deviceIds,
        start_time: new Date(startTime).toISOString(),
        end_time: new Date(endTime).toISOString(),
        purpose: purpose || undefined,
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
            disabled={create.isPending}
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
