import { useCancelReservation, useReservations } from "@/api/reservations";
import type { Reservation } from "@/types/reservation.types";

const STATUS_COLORS: Record<string, string> = {
  ACTIVE: "bg-green-100 text-green-800",
  PENDING: "bg-yellow-100 text-yellow-800",
  COMPLETED: "bg-gray-100 text-gray-600",
  CANCELLED: "bg-red-100 text-red-700",
};

function ReservationRow({ reservation }: { reservation: Reservation }) {
  const cancel = useCancelReservation();

  const start = new Date(reservation.start_time).toLocaleDateString();
  const end = new Date(reservation.end_time).toLocaleDateString();

  return (
    <div className="flex items-center gap-3 px-4 py-2 border-b border-gray-100 hover:bg-gray-50">
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className="text-sm font-mono text-gray-500">
            {reservation.id.slice(0, 8)}
          </span>
          <span
            className={`text-xs px-1.5 py-0.5 rounded font-medium ${STATUS_COLORS[reservation.status]}`}
          >
            {reservation.status}
          </span>
          <span className="text-xs px-1.5 py-0.5 rounded bg-gray-100 text-gray-600">
            {reservation.topology_type}
          </span>
        </div>
        <p className="text-xs text-gray-500 mt-0.5">
          {reservation.device_ids.length} device{reservation.device_ids.length !== 1 ? "s" : ""},{" "}
          {start} to {end}
          {reservation.purpose && ` - ${reservation.purpose}`}
        </p>
      </div>

      {reservation.status === "ACTIVE" && (
        <button
          onClick={() => cancel.mutate(reservation.id)}
          disabled={cancel.isPending}
          className="text-xs text-red-600 hover:text-red-800 px-2 py-1 rounded hover:bg-red-50 disabled:opacity-50"
        >
          Cancel
        </button>
      )}
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

      <div className="overflow-y-auto max-h-40">
        {isLoading && (
          <p className="text-xs text-gray-400 text-center py-3">Loading...</p>
        )}
        {!isLoading && (!reservations || reservations.length === 0) && (
          <p className="text-xs text-gray-400 text-center py-3">No reservations yet</p>
        )}
        {reservations?.map((res) => (
          <ReservationRow key={res.id} reservation={res} />
        ))}
      </div>
    </div>
  );
}
