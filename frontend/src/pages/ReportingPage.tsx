import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuthStore } from "@/stores/authStore";
import {
  downloadUtilizationCsv,
  useUtilizationReport,
  type UtilizationCsvSection,
} from "@/api/reporting";
import { useDevices } from "@/api/inventory";
import type { DayBucket, DeviceBucket } from "@/types/reporting.types";
import { Card, CardHeader } from "@/components/ui/Card";
import { StatCard } from "@/components/ui/StatCard";
import { EmptyRow } from "@/components/ui/EmptyState";
import { SkeletonRows } from "@/components/ui/Skeleton";

function triggerCsvDownload(filename: string, body: string): void {
  const blob = new Blob([body], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function escapeCsvCell(v: string | number): string {
  const s = String(v);
  if (/[,"\n\r]/.test(s)) {
    return `"${s.replace(/"/g, '""')}"`;
  }
  return s;
}

type RangePreset = "7d" | "30d" | "custom";

function toIso(d: Date): string {
  return d.toISOString();
}

function toDateInputValue(d: Date): string {
  // YYYY-MM-DD in UTC, suitable for <input type="date">. Matches the UTC
  // anchoring we send to the server so what the user sees in the picker
  // is the same day the backend filters on, regardless of browser timezone.
  return d.toISOString().slice(0, 10);
}

function parseUtcDay(value: string, endOfDay: boolean): Date {
  // Interpret the picker's YYYY-MM-DD as a UTC day. Without this, new Date("2026-04-15")
  // is parsed as local midnight, which the backend then shifts to a different UTC day.
  const suffix = endOfDay ? "T23:59:59.999Z" : "T00:00:00Z";
  return new Date(`${value}${suffix}`);
}

function presetWindow(preset: Exclude<RangePreset, "custom">): { start: Date; end: Date } {
  const end = new Date();
  const days = preset === "7d" ? 7 : 30;
  const start = new Date(end.getTime() - days * 24 * 60 * 60 * 1000);
  return { start, end };
}

function formatHours(h: number): string {
  return h.toFixed(1);
}

export function ReportingPage() {
  const user = useAuthStore((s) => s.user);
  const navigate = useNavigate();
  const isAdmin = user?.role === "admin" || user?.role === "superadmin";

  useEffect(() => {
    if (user && !isAdmin) {
      navigate("/topology");
    }
  }, [user, isAdmin, navigate]);

  const [preset, setPreset] = useState<RangePreset>("30d");
  const defaultWindow = presetWindow("30d");
  const [customStart, setCustomStart] = useState<string>(toDateInputValue(defaultWindow.start));
  const [customEnd, setCustomEnd] = useState<string>(toDateInputValue(defaultWindow.end));

  const window = useMemo(() => {
    if (preset === "custom") {
      return {
        start: parseUtcDay(customStart, false),
        end: parseUtcDay(customEnd, true),
      };
    }
    return presetWindow(preset);
  }, [preset, customStart, customEnd]);

  const windowValid = window.end > window.start;

  const report = useUtilizationReport(
    { start: toIso(window.start), end: toIso(window.end) },
    Boolean(user) && isAdmin && windowValid,
  );
  const devices = useDevices();

  const deviceIndex = useMemo(() => {
    const map = new Map<string, { name: string; template_name: string }>();
    for (const d of devices.data ?? []) {
      map.set(d.id, { name: d.name, template_name: d.template_name ?? "Unknown" });
    }
    return map;
  }, [devices.data]);

  const byTemplate = useMemo(() => {
    if (!report.data) return [];
    const rollup = new Map<string, { template_name: string; hours: number; count: number }>();
    for (const b of report.data.by_device) {
      const meta = deviceIndex.get(b.device_id);
      const name = meta?.template_name ?? "Unknown";
      const entry = rollup.get(name) ?? { template_name: name, hours: 0, count: 0 };
      entry.hours += b.hours;
      entry.count += b.reservation_count;
      rollup.set(name, entry);
    }
    return Array.from(rollup.values()).sort((a, b) => b.hours - a.hours);
  }, [report.data, deviceIndex]);

  const canDownload = Boolean(report.data) && windowValid && !report.isLoading;

  const handleServerCsv = async (section: UtilizationCsvSection) => {
    const { filename, body } = await downloadUtilizationCsv(
      { start: toIso(window.start), end: toIso(window.end) },
      section,
    );
    triggerCsvDownload(filename, body);
  };

  const handleTemplateCsv = () => {
    const header = "template_name,hours,reservation_count";
    const rows = byTemplate.map((t) =>
      [escapeCsvCell(t.template_name), t.hours.toFixed(4), t.count].join(","),
    );
    const body = [header, ...rows].join("\n") + "\n";
    const startDate = toDateInputValue(window.start);
    const endDate = toDateInputValue(window.end);
    triggerCsvDownload(`utilization-template-${startDate}-to-${endDate}.csv`, body);
  };

  if (!user || !isAdmin) {
    return null;
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="px-6 xl:px-12 2xl:px-16 py-6 space-y-6">
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div>
            <h2 className="text-lg font-semibold text-gray-900">Utilization Report</h2>
            <p className="text-sm text-gray-500 mt-1">
              Who used what, for how long, across completed reservations in the selected window.
            </p>
          </div>
          <RangeSelector
            preset={preset}
            setPreset={setPreset}
            customStart={customStart}
            setCustomStart={setCustomStart}
            customEnd={customEnd}
            setCustomEnd={setCustomEnd}
          />
        </div>

        <HeadlineCards
          totalHours={report.data?.total_hours ?? 0}
          totalReservations={report.data?.total_reservations ?? 0}
          executionRuns={report.data?.execution_run_count ?? null}
          loading={report.isLoading}
        />

        {report.isError && (
          <AlertStrip variant="error">
            Failed to load report. {report.error instanceof Error ? report.error.message : ""}
          </AlertStrip>
        )}

        {!windowValid && (
          <AlertStrip variant="warning">End date must be after start date.</AlertStrip>
        )}

        <DailyTrendChart data={report.data?.by_day ?? []} loading={report.isLoading} />

        <div className="grid grid-cols-1 lg:grid-cols-2 2xl:grid-cols-4 gap-6">
          <TableCard
            title="By User"
            action={
              <CsvButton
                onClick={() => handleServerCsv("user")}
                disabled={!canDownload}
                label="Download CSV"
              />
            }
          >
            {report.isLoading ? (
              <SkeletonRows />
            ) : (
              <table className="w-full text-sm text-left">
                <thead className="bg-gray-50 text-gray-500 uppercase text-xs">
                  <tr>
                    <th className="px-4 py-3">User</th>
                    <th className="px-4 py-3 text-right">Hours</th>
                    <th className="px-4 py-3 text-right">Count</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {(report.data?.by_user ?? []).map((u) => (
                    <tr key={u.user_id} className="hover:bg-gray-50">
                      <td className="px-4 py-2 font-medium text-gray-900">
                        {u.owner_name || u.user_id.slice(0, 8)}
                      </td>
                      <td className="px-4 py-2 text-right tabular-nums">{formatHours(u.hours)}</td>
                      <td className="px-4 py-2 text-right tabular-nums">{u.reservation_count}</td>
                    </tr>
                  ))}
                  {report.data && report.data.by_user.length === 0 && <EmptyRow colSpan={3} />}
                </tbody>
              </table>
            )}
          </TableCard>

          <TableCard
            title="By Device"
            action={
              <CsvButton
                onClick={() => handleServerCsv("device")}
                disabled={!canDownload}
                label="Download CSV"
              />
            }
          >
            {report.isLoading ? (
              <SkeletonRows />
            ) : (
              <table className="w-full text-sm text-left">
                <thead className="bg-gray-50 text-gray-500 uppercase text-xs">
                  <tr>
                    <th className="px-4 py-3">Device</th>
                    <th className="px-4 py-3 text-right">Hours</th>
                    <th className="px-4 py-3 text-right">Count</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {(report.data?.by_device ?? []).map((d: DeviceBucket) => {
                    const meta = deviceIndex.get(d.device_id);
                    return (
                      <tr key={d.device_id} className="hover:bg-gray-50">
                        <td className="px-4 py-2">
                          <div className="font-medium text-gray-900">
                            {meta?.name ?? d.device_id.slice(0, 8)}
                          </div>
                          {meta && (
                            <div className="text-xs text-gray-500">{meta.template_name}</div>
                          )}
                        </td>
                        <td className="px-4 py-2 text-right tabular-nums">{formatHours(d.hours)}</td>
                        <td className="px-4 py-2 text-right tabular-nums">
                          {d.reservation_count}
                        </td>
                      </tr>
                    );
                  })}
                  {report.data && report.data.by_device.length === 0 && <EmptyRow colSpan={3} />}
                </tbody>
              </table>
            )}
          </TableCard>

          <TableCard title="By Group (cost center)">
            {report.isLoading ? (
              <SkeletonRows />
            ) : (
              <table className="w-full text-sm text-left">
                <thead className="bg-gray-50 text-gray-500 uppercase text-xs">
                  <tr>
                    <th className="px-4 py-3">Group</th>
                    <th className="px-4 py-3 text-right">Hours</th>
                    <th className="px-4 py-3 text-right">Count</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {(report.data?.by_group ?? []).map((g) => (
                    <tr
                      key={g.group_id ?? "ungrouped"}
                      className="hover:bg-gray-50"
                    >
                      <td className="px-4 py-2 font-medium text-gray-900">{g.group_name}</td>
                      <td className="px-4 py-2 text-right tabular-nums">{formatHours(g.hours)}</td>
                      <td className="px-4 py-2 text-right tabular-nums">{g.reservation_count}</td>
                    </tr>
                  ))}
                  {report.data && report.data.by_group.length === 0 && (
                    <EmptyRow colSpan={3} />
                  )}
                </tbody>
              </table>
            )}
          </TableCard>

          <TableCard
            title="By Topology Type"
          >
            {report.isLoading ? (
              <SkeletonRows />
            ) : (
              <table className="w-full text-sm text-left">
                <thead className="bg-gray-50 text-gray-500 uppercase text-xs">
                  <tr>
                    <th className="px-4 py-3">Type</th>
                    <th className="px-4 py-3 text-right">Hours</th>
                    <th className="px-4 py-3 text-right">Count</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {(report.data?.by_topology_type ?? []).map((t) => (
                    <tr key={t.topology_type} className="hover:bg-gray-50">
                      <td className="px-4 py-2 font-medium text-gray-900">{t.topology_type}</td>
                      <td className="px-4 py-2 text-right tabular-nums">{formatHours(t.hours)}</td>
                      <td className="px-4 py-2 text-right tabular-nums">{t.reservation_count}</td>
                    </tr>
                  ))}
                  {report.data && report.data.by_topology_type.length === 0 && (
                    <EmptyRow colSpan={3} />
                  )}
                </tbody>
              </table>
            )}
          </TableCard>

          <TableCard
            title="By Template"
            action={
              <CsvButton
                onClick={handleTemplateCsv}
                disabled={!canDownload || byTemplate.length === 0}
                label="Download CSV"
              />
            }
          >
            {report.isLoading || devices.isLoading ? (
              <SkeletonRows />
            ) : (
              <table className="w-full text-sm text-left">
                <thead className="bg-gray-50 text-gray-500 uppercase text-xs">
                  <tr>
                    <th className="px-4 py-3">Template</th>
                    <th className="px-4 py-3 text-right">Hours</th>
                    <th className="px-4 py-3 text-right">Count</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {byTemplate.map((t) => (
                    <tr key={t.template_name} className="hover:bg-gray-50">
                      <td className="px-4 py-2 font-medium text-gray-900">{t.template_name}</td>
                      <td className="px-4 py-2 text-right tabular-nums">{formatHours(t.hours)}</td>
                      <td className="px-4 py-2 text-right tabular-nums">{t.count}</td>
                    </tr>
                  ))}
                  {report.data && byTemplate.length === 0 && <EmptyRow colSpan={3} />}
                </tbody>
              </table>
            )}
          </TableCard>
        </div>
      </div>
    </div>
  );
}

function HeadlineCards({
  totalHours,
  totalReservations,
  executionRuns,
  loading,
}: {
  totalHours: number;
  totalReservations: number;
  executionRuns: number | null;
  loading: boolean;
}) {
  return (
    <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
      <StatCard label="Total reservation-hours" value={totalHours.toFixed(1)} loading={loading} />
      <StatCard label="Reservations counted" value={totalReservations} loading={loading} />
      <StatCard
        label="Execution runs"
        value={executionRuns === null ? "n/a" : executionRuns}
        loading={loading}
      />
    </div>
  );
}

function RangeSelector({
  preset,
  setPreset,
  customStart,
  setCustomStart,
  customEnd,
  setCustomEnd,
}: {
  preset: RangePreset;
  setPreset: (p: RangePreset) => void;
  customStart: string;
  setCustomStart: (s: string) => void;
  customEnd: string;
  setCustomEnd: (s: string) => void;
}) {
  const btnClass = (active: boolean) =>
    `px-3 py-1.5 text-sm border transition-colors ${active ? "bg-gray-900 text-white border-gray-900" : "bg-white text-gray-700 border-gray-300 hover:bg-gray-50"}`;
  return (
    <div className="flex items-center gap-3 flex-wrap">
      <div className="inline-flex rounded-md overflow-hidden">
        <button type="button" className={btnClass(preset === "7d")} onClick={() => setPreset("7d")}>
          7 days
        </button>
        <button
          type="button"
          className={btnClass(preset === "30d")}
          onClick={() => setPreset("30d")}
        >
          30 days
        </button>
        <button
          type="button"
          className={btnClass(preset === "custom")}
          onClick={() => setPreset("custom")}
        >
          Custom
        </button>
      </div>
      {preset === "custom" && (
        <div className="flex items-center gap-2">
          <input
            type="date"
            value={customStart}
            onChange={(e) => setCustomStart(e.target.value)}
            className="border border-gray-300 rounded-md px-2 py-1 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
          />
          <span className="text-gray-500 text-sm">to</span>
          <input
            type="date"
            value={customEnd}
            onChange={(e) => setCustomEnd(e.target.value)}
            className="border border-gray-300 rounded-md px-2 py-1 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
          />
        </div>
      )}
    </div>
  );
}

function TableCard({
  title,
  action,
  children,
}: {
  title: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <Card>
      <CardHeader title={title} action={action} />
      <div className="overflow-x-auto">{children}</div>
    </Card>
  );
}

function CsvButton({
  onClick,
  disabled,
  label,
}: {
  onClick: () => void;
  disabled: boolean;
  label: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="text-xs px-2 py-1 border border-gray-300 rounded-md bg-white text-gray-700 hover:bg-gray-50 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
    >
      {label}
    </button>
  );
}

function AlertStrip({
  variant,
  children,
}: {
  variant: "error" | "warning";
  children: React.ReactNode;
}) {
  // Feedback-surface tokens: a -50 wash + -200 border + saturated -700/800 text.
  const styles =
    variant === "error"
      ? "bg-red-50 border-red-200 text-red-700"
      : "bg-amber-50 border-amber-200 text-amber-800";
  return <div className={`border rounded-lg p-4 text-sm ${styles}`}>{children}</div>;
}

function DailyTrendChart({ data, loading }: { data: DayBucket[]; loading: boolean }) {
  const width = 1000;
  const height = 180;
  const padLeft = 40;
  const padRight = 16;
  const padTop = 16;
  const padBottom = 28;
  const chartW = width - padLeft - padRight;
  const chartH = height - padTop - padBottom;

  const maxHours = Math.max(1, ...data.map((d) => d.hours));
  const barGap = 3;
  const barW = data.length > 0 ? Math.max(2, chartW / data.length - barGap) : 0;

  return (
    <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
      <div className="px-4 py-3 border-b border-gray-200">
        <h3 className="text-sm font-semibold text-gray-900">Daily trend (reservation-hours)</h3>
      </div>
      <div className="p-4">
        {loading ? (
          <div className="h-40 bg-gray-100 rounded animate-pulse" />
        ) : data.length === 0 ? (
          <div className="h-40 flex items-center justify-center text-sm text-gray-500">
            No data in this window
          </div>
        ) : (
          <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-40" role="img" aria-label="Daily reservation-hours trend">
            {[0, 0.5, 1].map((frac) => {
              const y = padTop + chartH - frac * chartH;
              const isBaseline = frac === 0;
              return (
                <g key={frac}>
                  <line
                    x1={padLeft}
                    y1={y}
                    x2={padLeft + chartW}
                    y2={y}
                    stroke={isBaseline ? "#e5e7eb" : "#f3f4f6"}
                  />
                  <text
                    x={padLeft - 4}
                    y={y + (isBaseline ? 0 : 3)}
                    textAnchor="end"
                    fontSize="10"
                    fill="#6b7280"
                  >
                    {(maxHours * frac).toFixed(0)}
                  </text>
                </g>
              );
            })}
            {data.map((d, i) => {
              const h = (d.hours / maxHours) * chartH;
              const x = padLeft + i * (barW + barGap);
              const y = padTop + (chartH - h);
              return (
                <g key={d.day}>
                  <rect
                    x={x}
                    y={y}
                    width={barW}
                    height={h}
                    rx="1"
                    className="[fill:#111827] hover:[fill:#374151] transition-colors"
                  >
                    <title>
                      {d.day}: {d.hours.toFixed(1)}h across {d.reservation_count} reservation
                      {d.reservation_count === 1 ? "" : "s"}
                    </title>
                  </rect>
                </g>
              );
            })}
            {data.length > 0 && (
              <>
                <text
                  x={padLeft}
                  y={height - 8}
                  textAnchor="start"
                  fontSize="10"
                  fill="#6b7280"
                >
                  {data[0].day}
                </text>
                <text
                  x={padLeft + chartW}
                  y={height - 8}
                  textAnchor="end"
                  fontSize="10"
                  fill="#6b7280"
                >
                  {data[data.length - 1].day}
                </text>
              </>
            )}
          </svg>
        )}
      </div>
    </div>
  );
}
