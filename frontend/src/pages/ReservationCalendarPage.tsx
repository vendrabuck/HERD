import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useCalendarReservations } from "@/api/reservations";
import { useAllDevices } from "@/api/inventory";
import { ReservationDetailModal } from "@/components/reservations/ReservationDetailModal";
import { getDayRange, getWeekRange, getMonthRange } from "@/utils/dateUtils";
import type { Reservation, ReservationStatus } from "@/types/reservation.types";

type ViewMode = "day" | "week" | "month";

const ALL_STATUSES: ReservationStatus[] = [
  "ACTIVE",
  "PENDING",
  "PENDING_PROVISION",
  "COMPLETED",
  "CANCELLED",
  "FAILED",
];

const STATUS_COLORS: Record<string, string> = {
  ACTIVE: "bg-green-500",
  PENDING: "bg-yellow-500",
  PENDING_PROVISION: "bg-amber-500",
  COMPLETED: "bg-gray-400",
  CANCELLED: "bg-red-400",
  FAILED: "bg-red-600",
};

const STATUS_HOVER: Record<string, string> = {
  ACTIVE: "hover:bg-green-600",
  PENDING: "hover:bg-yellow-600",
  PENDING_PROVISION: "hover:bg-amber-600",
  COMPLETED: "hover:bg-gray-500",
  CANCELLED: "hover:bg-red-500",
  FAILED: "hover:bg-red-700",
};

function getRange(mode: ViewMode, date: Date) {
  if (mode === "day") return getDayRange(date);
  if (mode === "month") return getMonthRange(date);
  return getWeekRange(date);
}

function navigate(mode: ViewMode, date: Date, delta: number): Date {
  const d = new Date(date);
  if (mode === "day") d.setDate(d.getDate() + delta);
  else if (mode === "week") d.setDate(d.getDate() + delta * 7);
  else d.setMonth(d.getMonth() + delta);
  return d;
}

function formatColumnHeader(date: Date, mode: ViewMode): string {
  if (mode === "month") return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  return date.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

function getTimeColumns(start: Date, end: Date, mode: ViewMode): Date[] {
  const cols: Date[] = [];
  const cur = new Date(start);
  if (mode === "day") {
    // 24 hourly columns
    for (let h = 0; h < 24; h++) {
      const d = new Date(cur);
      d.setHours(h, 0, 0, 0);
      cols.push(d);
    }
  } else {
    while (cur < end) {
      cols.push(new Date(cur));
      cur.setDate(cur.getDate() + 1);
    }
  }
  return cols;
}

function formatDayHeader(date: Date): string {
  return `${date.getHours().toString().padStart(2, "0")}:00`;
}

export function ReservationCalendarPage() {
  const [viewMode, setViewMode] = useState<ViewMode>("week");
  const [currentDate, setCurrentDate] = useState(new Date());
  const [selectedReservation, setSelectedReservation] = useState<Reservation | null>(null);
  const [statusFilter, setStatusFilter] = useState<ReservationStatus[]>([...ALL_STATUSES]);

  const { start, end } = getRange(viewMode, currentDate);
  const rangeStartIso = start.toISOString();
  const rangeEndIso = end.toISOString();

  const { data: reservations, isLoading } = useCalendarReservations({
    range_start: rangeStartIso,
    range_end: rangeEndIso,
    status: statusFilter.length < ALL_STATUSES.length ? statusFilter : undefined,
  });

  const { data: devices } = useAllDevices();

  const deviceNames = useMemo(() => {
    const map = new Map<string, string>();
    if (devices) {
      for (const d of devices) {
        map.set(d.id, d.name);
      }
    }
    return map;
  }, [devices]);

  // Build device rows from reservations
  const deviceRows = useMemo(() => {
    if (!reservations) return [];
    const deviceIdSet = new Set<string>();
    for (const r of reservations) {
      for (const did of r.device_ids) {
        deviceIdSet.add(did);
      }
    }
    return Array.from(deviceIdSet).sort((a, b) => {
      const na = deviceNames.get(a) || a;
      const nb = deviceNames.get(b) || b;
      return na.localeCompare(nb);
    });
  }, [reservations, deviceNames]);

  const columns = getTimeColumns(start, end, viewMode);
  const rangeMs = end.getTime() - start.getTime();

  const toggleStatus = (s: ReservationStatus) => {
    setStatusFilter((prev) =>
      prev.includes(s) ? prev.filter((x) => x !== s) : [...prev, s]
    );
  };

  const headerLabel = viewMode === "day"
    ? start.toLocaleDateString(undefined, { weekday: "long", year: "numeric", month: "long", day: "numeric" })
    : viewMode === "month"
    ? start.toLocaleDateString(undefined, { year: "numeric", month: "long" })
    : `${start.toLocaleDateString(undefined, { month: "short", day: "numeric" })} - ${new Date(end.getTime() - 86400000).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })}`;

  return (
    <div className="h-full overflow-y-auto">
      <div className="px-6 xl:px-12 2xl:px-16 py-6">
        {/* Tab toggle */}
        <div className="flex items-center gap-4 mb-4">
          <Link
            to="/reservations"
            className="text-sm px-3 py-1.5 rounded text-gray-500 hover:text-gray-900 hover:bg-gray-100"
          >
            My Reservations
          </Link>
          <span className="text-sm px-3 py-1.5 rounded bg-gray-900 text-white">
            Calendar
          </span>
        </div>

        {/* Controls */}
        <div className="flex items-center justify-between mb-4 flex-wrap gap-2">
          <div className="flex items-center gap-2">
            <button
              onClick={() => setCurrentDate(navigate(viewMode, currentDate, -1))}
              className="text-sm px-2 py-1 rounded border border-gray-300 hover:bg-gray-100"
            >
              Prev
            </button>
            <button
              onClick={() => setCurrentDate(new Date())}
              className="text-sm px-2 py-1 rounded border border-gray-300 hover:bg-gray-100"
            >
              Today
            </button>
            <button
              onClick={() => setCurrentDate(navigate(viewMode, currentDate, 1))}
              className="text-sm px-2 py-1 rounded border border-gray-300 hover:bg-gray-100"
            >
              Next
            </button>
            <span className="text-sm font-medium text-gray-700 ml-2">{headerLabel}</span>
          </div>

          <div className="flex items-center gap-3">
            {/* View mode toggle */}
            <div className="flex rounded border border-gray-300 overflow-hidden">
              {(["day", "week", "month"] as ViewMode[]).map((m) => (
                <button
                  key={m}
                  onClick={() => setViewMode(m)}
                  className={`text-xs px-3 py-1 capitalize ${
                    viewMode === m ? "bg-gray-900 text-white" : "text-gray-600 hover:bg-gray-100"
                  }`}
                >
                  {m}
                </button>
              ))}
            </div>

            {/* Status filter */}
            <div className="flex items-center gap-2">
              {ALL_STATUSES.map((s) => (
                <label key={s} className="flex items-center gap-1 text-xs text-gray-600 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={statusFilter.includes(s)}
                    onChange={() => toggleStatus(s)}
                    className="rounded border-gray-300"
                  />
                  {s}
                </label>
              ))}
            </div>
          </div>
        </div>

        {/* Timeline */}
        <div className="bg-white rounded-lg border border-gray-200 overflow-x-auto">
          {isLoading && (
            <p className="text-sm text-gray-400 text-center py-8">Loading calendar...</p>
          )}
          {reservations && reservations.length === 0 && !isLoading && (
            <p className="text-sm text-gray-400 text-center py-8">No reservations in this range</p>
          )}
          {reservations && reservations.length > 0 && (
            <div className="min-w-[800px]">
              {/* Header row */}
              <div className="flex border-b border-gray-200">
                <div className="w-40 shrink-0 px-3 py-2 text-xs font-medium text-gray-500 uppercase bg-gray-50 border-r border-gray-200">
                  Device
                </div>
                <div className="flex-1 flex">
                  {columns.map((col, i) => (
                    <div
                      key={i}
                      className="flex-1 px-1 py-2 text-xs text-center text-gray-500 bg-gray-50 border-r border-gray-100 last:border-r-0"
                    >
                      {viewMode === "day" ? formatDayHeader(col) : formatColumnHeader(col, viewMode)}
                    </div>
                  ))}
                </div>
              </div>

              {/* Device rows */}
              {deviceRows.map((deviceId) => {
                const deviceReservations = reservations.filter((r) =>
                  r.device_ids.includes(deviceId)
                );
                return (
                  <div key={deviceId} className="flex border-b border-gray-100 last:border-b-0">
                    <div className="w-40 shrink-0 px-3 py-3 text-sm text-gray-700 border-r border-gray-200 truncate" title={deviceNames.get(deviceId) || deviceId}>
                      {deviceNames.get(deviceId) || deviceId.slice(0, 8)}
                    </div>
                    <div className="flex-1 relative" style={{ minHeight: "40px" }}>
                      {/* Grid lines */}
                      <div className="absolute inset-0 flex pointer-events-none">
                        {columns.map((_, i) => (
                          <div key={i} className="flex-1 border-r border-gray-50 last:border-r-0" />
                        ))}
                      </div>
                      {/* Reservation blocks */}
                      {deviceReservations.map((r) => {
                        const rStart = new Date(r.start_time).getTime();
                        const rEnd = new Date(r.end_time).getTime();
                        const clampedStart = Math.max(rStart, start.getTime());
                        const clampedEnd = Math.min(rEnd, end.getTime());
                        const leftPct = ((clampedStart - start.getTime()) / rangeMs) * 100;
                        const widthPct = ((clampedEnd - clampedStart) / rangeMs) * 100;
                        return (
                          <button
                            key={`${r.id}-${deviceId}`}
                            onClick={() => setSelectedReservation(r)}
                            title={`${r.owner_name || r.user_id.slice(0, 8)}: ${r.purpose || "No purpose"}`}
                            className={`absolute top-1 h-[calc(100%-8px)] rounded text-[10px] text-white px-1 truncate cursor-pointer ${STATUS_COLORS[r.status]} ${STATUS_HOVER[r.status]}`}
                            style={{
                              left: `${leftPct}%`,
                              width: `${Math.max(widthPct, 0.5)}%`,
                            }}
                          >
                            {r.owner_name || r.user_id.slice(0, 8)}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>

      <ReservationDetailModal
        reservation={selectedReservation}
        deviceNames={deviceNames}
        onClose={() => setSelectedReservation(null)}
      />
    </div>
  );
}
