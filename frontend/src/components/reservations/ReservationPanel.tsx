import { useState } from "react";
import { useCancelReservation, useReleaseReservation, useReservations } from "@/api/reservations";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { PurposeCategoryTag } from "./PurposeCategoryTag";
import type { Reservation } from "@/types/reservation.types";

function ReservationRow({ reservation }: { reservation: Reservation }) {
  const cancel = useCancelReservation();
  const release = useReleaseReservation();
  const [confirmCancelOpen, setConfirmCancelOpen] = useState(false);
  const shortId = reservation.id.slice(0, 8);

  const start = new Date(reservation.start_time).toLocaleDateString();
  const end = new Date(reservation.end_time).toLocaleDateString();

  return (
    <div className="flex items-center gap-3 px-4 py-2 border-b border-gray-100 hover:bg-gray-50">
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-sm font-mono text-gray-500">
            {reservation.id.slice(0, 8)}
          </span>
          <StatusBadge status={reservation.status} />
          <span className="text-xs px-1.5 py-0.5 rounded bg-gray-100 text-gray-600">
            {reservation.topology_type}
          </span>
          <PurposeCategoryTag category={reservation.purpose_category} />
        </div>
        <p className="text-xs text-gray-500 mt-0.5">
          {reservation.device_ids.length} device{reservation.device_ids.length !== 1 ? "s" : ""},{" "}
          {start} to {end}
          {reservation.purpose && ` - ${reservation.purpose}`}
        </p>
      </div>

      {reservation.status === "ACTIVE" && (
        <div className="flex gap-1">
          <button
            onClick={() => release.mutate(reservation.id)}
            disabled={release.isPending}
            aria-label={`Release reservation ${shortId}`}
            className="text-xs text-green-600 hover:text-green-800 px-2 py-1 rounded hover:bg-green-50 disabled:opacity-50"
          >
            Release
          </button>
          <button
            onClick={() => setConfirmCancelOpen(true)}
            disabled={cancel.isPending}
            aria-label={`Cancel reservation ${shortId}`}
            className="text-xs text-red-600 hover:text-red-800 px-2 py-1 rounded hover:bg-red-50 disabled:opacity-50"
          >
            Cancel
          </button>
        </div>
      )}

      <ConfirmDialog
        open={confirmCancelOpen}
        title="Cancel Reservation"
        description="Cancel this reservation? This releases its devices and cannot be undone."
        confirmLabel="Cancel reservation"
        cancelLabel="Keep reservation"
        destructive
        onConfirm={() => {
          setConfirmCancelOpen(false);
          cancel.mutate(reservation.id);
        }}
        onCancel={() => setConfirmCancelOpen(false)}
      />
    </div>
  );
}

export function ReservationPanel() {
  const { data: reservations, isLoading } = useReservations();

  return (
    <div className="bg-white border-t border-gray-200">
      <div className="flex items-center justify-between px-4 py-2 border-b border-gray-100">
        <h2 className="text-sm font-semibold text-gray-800">
          My Reservations
          {reservations && (
            <span className="ml-2 text-xs text-gray-400 font-normal">
              ({reservations.length})
            </span>
          )}
        </h2>
      </div>

      <div className="overflow-y-auto max-h-40" aria-label="My reservations">
        {isLoading && (
          <p role="status" aria-live="polite" className="text-xs text-gray-400 text-center py-3">Loading...</p>
        )}
        {!isLoading && (!reservations || reservations.length === 0) && (
          <p className="text-xs text-gray-500 text-center py-3">No reservations yet</p>
        )}
        {reservations?.map((res) => (
          <ReservationRow key={res.id} reservation={res} />
        ))}
      </div>
    </div>
  );
}
