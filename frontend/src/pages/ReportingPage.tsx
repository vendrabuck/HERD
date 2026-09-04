import { useMemo, useState } from "react";
import {
  downloadUtilizationCsv,
  useUtilizationReport,
  type UtilizationCsvSection,
} from "@/api/reporting";
import { useDevices } from "@/api/inventory";
import type { DayBucket, DeviceBucket, FleetSection, PurposeBucket } from "@/types/reporting.types";
import { Card, CardHeader } from "@/components/ui/Card";
import { StatCard } from "@/components/ui/StatCard";
import { EmptyRow } from "@/components/ui/EmptyState";
import { SkeletonRows } from "@/components/ui/Skeleton";
import { PurposeCategoryTag } from "@/components/reservations/PurposeCategoryTag";
import { isUnclassifiedCategory, purposeCategoryLabel } from "@/lib/purposeCategories";

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
    windowValid,
  );
  const devices = useDevices();

  const deviceIndex = useMemo(() => {
    const map = new Map<string, { name: string; template_name: string }>();
    for (const d of devices.data ?? []) {
      map.set(d.id, { name: d.name, template_name: d.template_name ?? "Unknown" });
    }
    return map;
  }, [devices.data]);

  // Purpose classification (issue #646 phase 1) reports users/devices by id
  // only; resolve names from the report's own by_user bucket and the device
  // index above rather than a second fetch.
  const userNameIndex = useMemo(() => {
    const map = new Map<string, string>();
    for (const u of report.data?.by_user ?? []) {
      map.set(u.user_id, u.owner_name);
    }
    return map;
  }, [report.data]);

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

        <FleetUtilizationCard
          fleet={report.data?.fleet ?? null}
          loaded={Boolean(report.data)}
          loading={report.isLoading}
          canDownload={canDownload && Boolean(report.data?.fleet)}
          onDownload={() => handleServerCsv("fleet")}
        />

        {/* Purpose classification (issue #646 phase 1). Gated on by_purpose
            alone so an older backend build that predates the feature hides
            the whole section cleanly rather than rendering empty tables. */}
        {report.data?.by_purpose !== undefined && (
          <div className="space-y-6">
            <PurposeBarChart
              data={report.data.by_purpose}
              suggested={report.data.by_purpose_suggested ?? []}
            />
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
              <PurposeMixTable
                title="Purpose Mix - By User"
                entityHeader="User"
                rows={(report.data.by_user_purpose ?? []).map((r) => ({
                  key: `${r.user_id}:${r.purpose_category}`,
                  entityLabel: userNameIndex.get(r.user_id) ?? r.user_id.slice(0, 8),
                  category: r.purpose_category,
                  hours: r.device_hours,
                  count: r.reservations,
                }))}
              />
              <PurposeMixTable
                title="Purpose Mix - By Device"
                entityHeader="Device"
                rows={(report.data.by_device_purpose ?? []).map((r) => ({
                  key: `${r.device_id}:${r.purpose_category}`,
                  entityLabel: deviceIndex.get(r.device_id)?.name ?? r.device_id.slice(0, 8),
                  category: r.purpose_category,
                  hours: r.device_hours,
                  count: r.reservations,
                }))}
              />
            </div>
          </div>
        )}

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

function FleetUtilizationCard({
  fleet,
  loaded,
  loading,
  canDownload,
  onDownload,
}: {
  fleet: FleetSection | null;
  loaded: boolean;
  loading: boolean;
  canDownload: boolean;
  onDownload: () => void;
}) {
  const [idleOnly, setIdleOnly] = useState(false);
  const rows = useMemo(() => {
    if (!fleet) return [];
    return idleOnly ? fleet.devices.filter((d) => d.reservation_count === 0) : fleet.devices;
  }, [fleet, idleOnly]);

  return (
    <Card>
      <CardHeader
        title="Fleet Utilization"
        action={
          <div className="flex items-center gap-3">
            <label className="flex items-center gap-1.5 text-xs text-gray-600 cursor-pointer">
              <input
                type="checkbox"
                checked={idleOnly}
                onChange={(e) => setIdleOnly(e.target.checked)}
                className="rounded border-gray-300"
              />
              Idle only
            </label>
            <CsvButton onClick={onDownload} disabled={!canDownload} label="Download CSV" />
          </div>
        }
      />
      <div className="px-4 pt-3 pb-1 text-xs text-gray-500">
        Every inventory device against the full selected window. Counts active and completed
        reservations by default; device status is as of now (status history is not tracked).
      </div>
      {loading ? (
        <div className="p-4">
          <SkeletonRows />
        </div>
      ) : loaded && !fleet ? (
        <div className="p-4">
          <AlertStrip variant="warning">
            Inventory service was unreachable; fleet utilization is unavailable for this window.
          </AlertStrip>
        </div>
      ) : (
        <>
          <div className="grid grid-cols-3 gap-4 px-4 py-3 border-b border-gray-100">
            <FleetStat
              label="Fleet utilization"
              value={fleet ? `${fleet.utilization_pct.toFixed(1)}%` : "n/a"}
            />
            <FleetStat label="Devices" value={fleet ? String(fleet.device_count) : "n/a"} />
            <FleetStat
              label="Idle in window"
              value={fleet ? String(fleet.idle_device_count) : "n/a"}
            />
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm text-left">
              <thead className="bg-gray-50 text-gray-500 uppercase text-xs">
                <tr>
                  <th className="px-4 py-3">Device</th>
                  <th className="px-4 py-3">Status</th>
                  <th className="px-4 py-3 text-right">Utilization</th>
                  <th className="px-4 py-3 text-right">Hours</th>
                  <th className="px-4 py-3 text-right">Count</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {rows.map((d) => (
                  <tr key={d.device_id} className="hover:bg-gray-50">
                    <td className="px-4 py-2 font-medium text-gray-900">
                      {d.name || d.device_id.slice(0, 8)}
                    </td>
                    <td className="px-4 py-2 text-xs text-gray-500">{d.status}</td>
                    <td className="px-4 py-2 text-right">
                      <div className="flex items-center justify-end gap-2">
                        <div className="w-24 h-1.5 bg-gray-100 rounded overflow-hidden">
                          <div
                            className="h-full bg-gray-900"
                            style={{ width: `${Math.min(d.utilization_pct, 100)}%` }}
                          />
                        </div>
                        <span className="tabular-nums w-14">{d.utilization_pct.toFixed(1)}%</span>
                      </div>
                    </td>
                    <td className="px-4 py-2 text-right tabular-nums">{formatHours(d.hours)}</td>
                    <td className="px-4 py-2 text-right tabular-nums">{d.reservation_count}</td>
                  </tr>
                ))}
                {fleet && rows.length === 0 && <EmptyRow colSpan={5} />}
              </tbody>
            </table>
          </div>
        </>
      )}
    </Card>
  );
}

function FleetStat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-xs text-gray-500 uppercase">{label}</div>
      <div className="text-lg font-semibold text-gray-900 tabular-nums">{value}</div>
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

// A muted, tone-on-tone 45-degree hatch on the blue ramp (dataviz skill's
// "Lines" texture treatment): the accessibility-safe way to add a third
// visual class to a chart that otherwise reserves color for exactly two
// meanings (see the comment below), without inventing a new hue for it.
const SUGGESTED_HATCH =
  "repeating-linear-gradient(45deg, #93c5fd, #93c5fd 4px, #dbeafe 4px, #dbeafe 8px)";

type PurposeBarKind = "confirmed" | "unclassified" | "suggested";

interface PurposeBarRow {
  key: string;
  label: string;
  kind: PurposeBarKind;
  device_hours: number;
  reservations: number;
}

// Ranked device-hours per purpose category (issue #646 phases 1-2). Color is
// binary for the confirmed data (blue-600 vs the muted gray-400 unclassified
// bucket) - a bar's own row label already carries category identity, so
// hue only needs to mark the one distinction that matters there: classified
// (a real signal) versus unclassified (an absence of one). Phase 2 adds a
// third class, ai_suggested rows with no confirmed category yet: a hatch on
// the same blue rather than a fourth hue, since these are still "leaning
// classified," just unconfirmed, and a legend makes the three classes
// unambiguous without relying on color alone.
function PurposeBarChart({
  data,
  suggested,
}: {
  data: PurposeBucket[];
  suggested: PurposeBucket[];
}) {
  const rows = useMemo<PurposeBarRow[]>(() => {
    const confirmedRows: PurposeBarRow[] = data.map((r) => ({
      key: `confirmed:${r.purpose_category}`,
      label: purposeCategoryLabel(r.purpose_category),
      kind: isUnclassifiedCategory(r.purpose_category) ? "unclassified" : "confirmed",
      device_hours: r.device_hours,
      reservations: r.reservations,
    }));
    const suggestedRows: PurposeBarRow[] = suggested.map((r) => ({
      key: `suggested:${r.purpose_category}`,
      label: `${purposeCategoryLabel(r.purpose_category)} (suggested)`,
      kind: "suggested",
      device_hours: r.device_hours,
      reservations: r.reservations,
    }));
    return [...confirmedRows, ...suggestedRows].sort((a, b) => b.device_hours - a.device_hours);
  }, [data, suggested]);
  const maxHours = Math.max(1, ...rows.map((r) => r.device_hours));
  const hasSuggested = suggested.length > 0;

  return (
    <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
      <div className="px-4 py-3 border-b border-gray-200 flex items-center justify-between flex-wrap gap-2">
        <h3 className="text-sm font-semibold text-gray-900">Device-hours by purpose</h3>
        <PurposeBarLegend showSuggested={hasSuggested} />
      </div>
      <div className="p-4 space-y-2">
        {rows.length === 0 ? (
          <div className="h-16 flex items-center justify-center text-sm text-gray-500">
            No data in this window
          </div>
        ) : (
          rows.map((r) => {
            const pct = Math.max(2, (r.device_hours / maxHours) * 100);
            return (
              <div key={r.key} className="flex items-center gap-3">
                <div className="w-48 shrink-0 text-xs text-gray-600 truncate" title={r.label}>
                  {r.label}
                </div>
                <div className="flex-1 h-3 bg-gray-100 rounded-full overflow-hidden">
                  <div
                    className={`h-full rounded-full ${
                      r.kind === "unclassified"
                        ? "bg-gray-400"
                        : r.kind === "confirmed"
                          ? "bg-blue-600"
                          : ""
                    }`}
                    style={
                      r.kind === "suggested" ? { width: `${pct}%`, backgroundImage: SUGGESTED_HATCH } : { width: `${pct}%` }
                    }
                    title={`${formatHours(r.device_hours)}h across ${r.reservations} reservation${
                      r.reservations === 1 ? "" : "s"
                    }`}
                  />
                </div>
                <div className="w-16 shrink-0 text-right text-xs tabular-nums text-gray-700">
                  {formatHours(r.device_hours)}h
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

// Text labels beside every swatch: identity never rests on color alone
// (dataviz skill's accessibility pass). Suggested only appears once there is
// suggested data to explain.
function PurposeBarLegend({ showSuggested }: { showSuggested: boolean }) {
  return (
    <div className="flex items-center gap-3 text-xs text-gray-600">
      <span className="flex items-center gap-1.5">
        <span className="w-2.5 h-2.5 rounded-sm bg-blue-600" />
        Confirmed
      </span>
      {showSuggested && (
        <span className="flex items-center gap-1.5">
          <span
            className="w-2.5 h-2.5 rounded-sm border border-blue-300"
            style={{ backgroundImage: SUGGESTED_HATCH }}
          />
          AI-suggested
        </span>
      )}
      <span className="flex items-center gap-1.5">
        <span className="w-2.5 h-2.5 rounded-sm bg-gray-400" />
        Unclassified
      </span>
    </div>
  );
}

interface PurposeMixRow {
  key: string;
  entityLabel: string;
  category: string;
  hours: number;
  count: number;
}

// Flat (entity, category) rows sorted by device-hours descending, top 10 by
// default with a "show all" toggle (issue #646 phase 1). Used for both the
// per-user and per-device purpose mix; the two differ only in title, column
// header, and which id-to-name lookup the caller resolved the rows with.
function PurposeMixTable({
  title,
  entityHeader,
  rows,
}: {
  title: string;
  entityHeader: string;
  rows: PurposeMixRow[];
}) {
  const [showAll, setShowAll] = useState(false);
  const sorted = useMemo(() => [...rows].sort((a, b) => b.hours - a.hours), [rows]);
  const visible = showAll ? sorted : sorted.slice(0, 10);

  return (
    <TableCard
      title={title}
      action={
        sorted.length > 10 ? (
          <button
            type="button"
            onClick={() => setShowAll((v) => !v)}
            className="text-xs text-blue-600 hover:text-blue-800"
          >
            {showAll ? "Show top 10" : `Show all (${sorted.length})`}
          </button>
        ) : undefined
      }
    >
      <table className="w-full text-sm text-left">
        <thead className="bg-gray-50 text-gray-500 uppercase text-xs">
          <tr>
            <th className="px-4 py-3">{entityHeader}</th>
            <th className="px-4 py-3">Category</th>
            <th className="px-4 py-3 text-right">Hours</th>
            <th className="px-4 py-3 text-right">Count</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {visible.map((r) => (
            <tr key={r.key} className="hover:bg-gray-50">
              <td className="px-4 py-2 font-medium text-gray-900">{r.entityLabel}</td>
              <td className="px-4 py-2">
                <PurposeCategoryTag category={r.category} />
              </td>
              <td className="px-4 py-2 text-right tabular-nums">{formatHours(r.hours)}</td>
              <td className="px-4 py-2 text-right tabular-nums">{r.count}</td>
            </tr>
          ))}
          {sorted.length === 0 && <EmptyRow colSpan={4} />}
        </tbody>
      </table>
    </TableCard>
  );
}
