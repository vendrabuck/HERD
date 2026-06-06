import { useState } from "react";
import { useUpdateReservation } from "@/api/reservations";
import { useAuthStore } from "@/stores/authStore";
import toast from "react-hot-toast";
import type { Reservation } from "@/types/reservation.types";

interface Props {
  reservation: Reservation;
  onUpdated: () => void;
}

function toLocalInput(iso: string): string {
  const d = new Date(iso);
  const offset = d.getTimezoneOffset();
  const local = new Date(d.getTime() - offset * 60000);
  return local.toISOString().slice(0, 16);
}

export function ReservationStatusTab({ reservation, onUpdated }: Props) {
  const user = useAuthStore((s) => s.user);
  const update = useUpdateReservation();
  const [editing, setEditing] = useState(false);
  const [endTime, setEndTime] = useState(() => toLocalInput(reservation.end_time));
  const [purpose, setPurpose] = useState(reservation.purpose ?? "");

  const isOwner = user?.id === reservation.user_id;
  const canEdit = isOwner && (reservation.status === "ACTIVE" || reservation.status === "PENDING");

  const handleSave = async () => {
    const data: Record<string, string> = {};
    const newEnd = new Date(endTime).toISOString();
    if (newEnd !== reservation.end_time) {
      data.end_time = newEnd;
    }
    if (purpose !== (reservation.purpose ?? "")) {
      data.purpose = purpose;
    }
    if (Object.keys(data).length === 0) {
      setEditing(false);
      return;
    }
    try {
      await update.mutateAsync({ id: reservation.id, data });
      toast.success("Reservation updated");
      setEditing(false);
      onUpdated();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Update failed";
      toast.error(msg);
    }
  };

  return (
    <div className="space-y-4 text-sm">
      <div className="grid grid-cols-2 gap-3">
        <div>
          <span className="text-gray-500 text-xs uppercase">Status</span>
          <p className="font-medium">{reservation.status}</p>
        </div>
        <div>
          <span className="text-gray-500 text-xs uppercase">Created</span>
          <p>{new Date(reservation.created_at).toLocaleString()}</p>
        </div>
      </div>

      <div className="border-t border-gray-200 pt-3">
        <div className="grid grid-cols-2 gap-3">
          <div>
            <span className="text-gray-500 text-xs uppercase">Start Time</span>
            <p>{new Date(reservation.start_time).toLocaleString()}</p>
          </div>
          <div>
            <span className="text-gray-500 text-xs uppercase">End Time</span>
            {editing ? (
              <input
                type="datetime-local"
                value={endTime}
                onChange={(e) => setEndTime(e.target.value)}
                className="mt-0.5 w-full border border-gray-300 rounded px-2 py-1 text-sm"
              />
            ) : (
              <p>{new Date(reservation.end_time).toLocaleString()}</p>
            )}
          </div>
        </div>
      </div>

      <div className="border-t border-gray-200 pt-3">
        <span className="text-gray-500 text-xs uppercase">Purpose</span>
        {editing ? (
          <input
            type="text"
            value={purpose}
            onChange={(e) => setPurpose(e.target.value)}
            placeholder="Purpose"
            className="mt-0.5 w-full border border-gray-300 rounded px-2 py-1 text-sm"
          />
        ) : (
          <p>{reservation.purpose || "-"}</p>
        )}
      </div>

      {canEdit && (
        <div className="border-t border-gray-200 pt-3 flex gap-2">
          {editing ? (
            <>
              <button
                onClick={handleSave}
                disabled={update.isPending}
                className="text-sm px-3 py-1.5 rounded bg-gray-900 text-white hover:bg-gray-800 disabled:opacity-50"
              >
                {update.isPending ? "Saving..." : "Save"}
              </button>
              <button
                onClick={() => {
                  setEditing(false);
                  setEndTime(toLocalInput(reservation.end_time));
                  setPurpose(reservation.purpose ?? "");
                }}
                className="text-sm px-3 py-1.5 rounded border border-gray-300 text-gray-600 hover:bg-gray-50"
              >
                Cancel
              </button>
            </>
          ) : (
            <button
              onClick={() => setEditing(true)}
              className="text-sm px-3 py-1.5 rounded border border-gray-300 text-gray-600 hover:bg-gray-50"
            >
              Edit Schedule
            </button>
          )}
        </div>
      )}
    </div>
  );
}
