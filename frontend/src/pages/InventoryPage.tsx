import { useState, useRef, useEffect, useMemo, useId, Fragment } from "react";
import { useNavigate, Link } from "react-router-dom";
import toast from "react-hot-toast";
import { useQueryClient } from "@tanstack/react-query";
import {
  usePaginatedDevices,
  useCreateDevice,
  useDeleteDevice,
  useAllDeviceNames,
  deviceCabledCount,
} from "@/api/inventory";
import { BulkImportExport } from "@/components/ui/BulkImportExport";
import { exportDevices, importDevices } from "@/api/bulk";
import { fetchPorts, useCreatePort, usePorts } from "@/api/ports";
import { useDeviceConnections } from "@/api/connections";
import { useTemplates } from "@/api/templates";
import { useAuthStore } from "@/stores/authStore";
import { isAdminRole } from "@/lib/roles";
import {
  DEVICE_STATUSES,
  TOPOLOGY_TYPES,
  parseSavedInventoryFilter,
  serializeInventoryFilter,
  type InventoryFilterState,
} from "@/lib/inventoryFilters";
import { usePreferencesStore } from "@/stores/preferencesStore";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { Pagination } from "@/components/ui/Pagination";
import { DeviceInfoPanel } from "@/components/inventory/DeviceInfoPanel";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { TopoBadge } from "@/components/ui/TopoBadge";
import { EmptyRow } from "@/components/ui/EmptyState";
import { SkeletonRows } from "@/components/ui/Skeleton";
import type { Device, DeviceFilters, DeviceStatus, TopologyType } from "@/types/device.types";

const PAGE_SIZE_OPTIONS = [25, 50, 100, 200];

function SelectAllCheckbox({ checked, indeterminate, onChange }: { checked: boolean; indeterminate: boolean; onChange: () => void }) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = indeterminate;
  }, [indeterminate]);
  return (
    <input
      ref={ref}
      type="checkbox"
      checked={checked}
      onChange={onChange}
      className="rounded border-gray-300"
    />
  );
}

function CopyIcon() {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
      <path d="M7 3.5A1.5 1.5 0 0 1 8.5 2h3.879a1.5 1.5 0 0 1 1.06.44l3.122 3.12A1.5 1.5 0 0 1 17 6.622V12.5a1.5 1.5 0 0 1-1.5 1.5h-1v-3.379a3 3 0 0 0-.879-2.121L10.5 5.379A3 3 0 0 0 8.379 4.5H7v-1Z" />
      <path d="M4.5 6A1.5 1.5 0 0 0 3 7.5v9A1.5 1.5 0 0 0 4.5 18h7a1.5 1.5 0 0 0 1.5-1.5v-5.879a1.5 1.5 0 0 0-.44-1.06L9.44 6.439A1.5 1.5 0 0 0 8.378 6H4.5Z" />
    </svg>
  );
}

function ChevronIcon({ expanded }: { expanded: boolean }) {
  return (
    <svg
      className={`w-4 h-4 transition-transform ${expanded ? "rotate-90" : ""}`}
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 20 20"
      fill="currentColor"
    >
      <path
        fillRule="evenodd"
        d="M7.21 14.77a.75.75 0 01.02-1.06L11.168 10 7.23 6.29a.75.75 0 111.04-1.08l4.5 4.25a.75.75 0 010 1.08l-4.5 4.25a.75.75 0 01-1.06-.02z"
        clipRule="evenodd"
      />
    </svg>
  );
}

function ExpandedPortsRow({ device, deviceNameMap, colSpan }: {
  device: Device;
  deviceNameMap: Map<string, string> | undefined;
  colSpan: number;
}) {
  const { data: ports, isLoading: portsLoading } = usePorts(device.id);
  const { data: connections, isLoading: connsLoading } = useDeviceConnections(device.id);

  const connectionMap = useMemo(() => {
    const map = new Map<string, { deviceId: string; deviceName: string; portName: string }>();
    if (!connections) return map;
    for (const conn of connections) {
      const isA = conn.device_a_id === device.id;
      const thisPort = isA ? conn.port_a : conn.port_b;
      const otherDeviceId = isA ? conn.device_b_id : conn.device_a_id;
      const otherPort = isA ? conn.port_b : conn.port_a;
      const otherName = deviceNameMap?.get(otherDeviceId) ?? otherDeviceId.slice(0, 8) + "...";
      map.set(thisPort, { deviceId: otherDeviceId, deviceName: otherName, portName: otherPort });
    }
    return map;
  }, [connections, device.id, deviceNameMap]);

  const isLoading = portsLoading || connsLoading;

  return (
    <tr className="bg-gray-50/50">
      <td colSpan={colSpan} className="px-8 py-3">
        <div className="flex gap-6">
          <DeviceInfoPanel device={device} />
          <div className="flex-1 min-w-0">
            {isLoading ? (
              <p className="text-sm text-gray-400">Loading ports...</p>
            ) : !ports || ports.length === 0 ? (
              <p className="text-sm text-gray-400">No ports configured</p>
            ) : (
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-xs text-gray-500 uppercase">
                    <th className="text-left py-1 pr-4 font-medium">Port</th>
                    <th className="text-left py-1 font-medium">Connected To</th>
                  </tr>
                </thead>
                <tbody>
                  {ports.map((port) => {
                    const conn = connectionMap.get(port.name);
                    return (
                      <tr key={port.id} className="border-t border-gray-100">
                        <td className="py-1 pr-4 font-mono text-gray-900">{port.name}</td>
                        <td className="py-1 text-gray-600">
                          {conn ? (
                            <Link
                              to={`/inventory/${conn.deviceId}`}
                              className="text-blue-600 hover:text-blue-800 hover:underline"
                            >
                              {conn.deviceName}, {conn.portName}
                            </Link>
                          ) : (
                            <span className="text-gray-400">Not connected</span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>
        </div>
      </td>
    </tr>
  );
}

function DeviceRow({ device, isAdmin, onCopy, onClick, selected, onToggle, showCheckbox, expanded, onToggleExpand }: { device: Device; isAdmin: boolean; onCopy: (device: Device) => void; onClick: () => void; selected: boolean; onToggle: () => void; showCheckbox: boolean; expanded: boolean; onToggleExpand: () => void }) {
  return (
    <tr className="border-b border-gray-100 hover:bg-gray-50 cursor-pointer" onClick={onClick}>
      <td className="w-10 px-2 py-2">
        <button
          onClick={(e) => { e.stopPropagation(); onToggleExpand(); }}
          className="text-gray-400 hover:text-gray-600"
          aria-label={expanded ? "Collapse ports" : "Expand ports"}
        >
          <ChevronIcon expanded={expanded} />
        </button>
      </td>
      {showCheckbox && (
        <td className="px-4 py-2">
          <input
            type="checkbox"
            checked={selected}
            onChange={onToggle}
            onClick={(e) => e.stopPropagation()}
            className="rounded border-gray-300"
          />
        </td>
      )}
      <td className="px-4 py-2 text-sm font-medium text-gray-900">{device.name}</td>
      <td className="px-4 py-2 text-sm text-gray-600">
        <span className="inline-flex items-center gap-1.5">
          {device.template_icon ? (
            <img
              src={device.template_icon}
              alt={device.template_name ?? ""}
              className="w-4 h-4 object-contain"
            />
          ) : (
            <span className="inline-block w-4 h-4 bg-gray-200 rounded" />
          )}
          {device.template_name ?? "-"}
        </span>
      </td>
      <td className="px-4 py-2 text-sm">
        <TopoBadge type={device.topology_type} />
      </td>
      <td className="px-4 py-2 text-sm">
        <StatusBadge status={device.status} />
      </td>
      <td className="px-4 py-2 text-sm text-gray-400 font-mono tabular-nums">{device.id.slice(0, 8)}</td>
      {isAdmin && (
        <td className="px-4 py-2">
          <button
            onClick={(e) => { e.stopPropagation(); onCopy(device); }}
            title="Duplicate device"
            className="text-gray-400 hover:text-blue-600 transition-colors"
          >
            <CopyIcon />
          </button>
        </td>
      )}
    </tr>
  );
}

function templateOptionLabel(t: { name: string; vendor?: string | null; model?: string | null }) {
  const detail = [t.vendor, t.model].filter(Boolean).join(" ");
  return detail ? `${t.name} (${detail})` : t.name;
}

function FilterSelect({
  label,
  value,
  onChange,
  children,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  children: React.ReactNode;
}) {
  // An explicit htmlFor/id pair, not a wrapping label: a wrapping label's
  // accessible name would include the selected option's text.
  const id = useId();
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-xs font-medium text-gray-500">
        {label}
      </label>
      <select
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="max-w-[16rem] text-sm font-normal text-gray-900 border border-gray-300 rounded-lg px-2 py-2 bg-white focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
      >
        {children}
      </select>
    </div>
  );
}

export function InventoryPage() {
  const navigate = useNavigate();
  const createDevice = useCreateDevice();
  const createPort = useCreatePort();
  const deleteDevice = useDeleteDevice();
  const user = useAuthStore((s) => s.user);
  const isAdmin = isAdminRole(user?.role);
  const queryClient = useQueryClient();

  const savedRaw = usePreferencesStore((s) => s.savedFilters.inventory);
  const stored = useMemo(() => parseSavedInventoryFilter(savedRaw), [savedRaw]);
  const storedSearch = stored.search;
  const limit = usePreferencesStore((s) => s.getPageSize("inventory", 50));
  const setSavedFilter = usePreferencesStore((s) => s.setSavedFilter);
  const setPageSize = usePreferencesStore((s) => s.setPageSize);

  const [skip, setSkip] = useState(0);
  const [userSearch, setUserSearch] = useState<string | null>(null);
  const searchInput = userSearch ?? storedSearch;
  const [debouncedSearch, setDebouncedSearch] = useState(storedSearch);

  // Column filters: null means the user has not touched the control, so the
  // saved value shows; "" is an explicit All. Saved values are validated on read
  // (parseSavedInventoryFilter drops an unknown status or topology; a template id
  // is checked against the loaded template list below), so a stale preference
  // never reaches the API.
  const [userStatus, setUserStatus] = useState<DeviceStatus | "" | null>(null);
  const [userTemplate, setUserTemplate] = useState<string | null>(null);
  const [userTopology, setUserTopology] = useState<TopologyType | "" | null>(null);
  const status = userStatus ?? stored.status;
  const topologyType = userTopology ?? stored.topologyType;
  const rawTemplate = userTemplate ?? stored.templateId;

  // Device templates only: vendor and model are template fields, so filtering by
  // template covers them. The list is the server's first page of up to 500.
  const { data: templates, isLoading: templatesLoading } = useTemplates("device");
  const templateId = templates?.some((t) => t.id === rawTemplate) ? rawTemplate : "";
  // Hold the device query while a saved template id awaits validation, so a
  // stale id is never sent and a valid one does not flash an unfiltered page.
  const templatePending = rawTemplate !== "" && templatesLoading;

  // The last value of every persisted field. Both the search debounce and the
  // filter handlers write the WHOLE object through persistFilters, so whichever
  // write lands last carries all fields.
  const latestRef = useRef<InventoryFilterState>({
    search: debouncedSearch,
    status,
    templateId,
    topologyType,
  });
  useEffect(() => {
    latestRef.current = { search: debouncedSearch, status, templateId, topologyType };
  }, [debouncedSearch, status, templateId, topologyType]);
  const persistFilters = (patch: Partial<InventoryFilterState>) => {
    latestRef.current = { ...latestRef.current, ...patch };
    setSavedFilter("inventory", serializeInventoryFilter(latestRef.current));
  };

  const handlePageSizeChange = (size: number) => {
    setPageSize("inventory", size);
    setSkip(0);
  };

  useEffect(() => {
    // Skip entirely when the input already matches what is applied: this is
    // true on every mount (debouncedSearch is seeded from storedSearch, and
    // searchInput starts equal to it too), so without this guard the effect
    // would still arm a 300ms timer that calls setSkip(0) unconditionally.
    // That stray timer raced a same-page Next click in e2e (a click just
    // after mount landed setSkip(50), then the leftover mount-timer fired
    // setSkip(0) a moment later and silently reverted it); see nightly run
    // 33300868733, test_inventory_pagination_next_advances_page.
    if (searchInput === debouncedSearch) return;
    const timer = setTimeout(() => {
      setDebouncedSearch(searchInput);
      setSkip(0);
      if (userSearch !== null) {
        persistFilters({ search: userSearch });
      }
    }, 300);
    return () => clearTimeout(timer);
    // debouncedSearch is intentionally excluded below: including it would
    // re-run this effect (and re-arm the timer) every time the timer itself
    // fires, since the timer's own setDebouncedSearch call changes it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchInput, userSearch, setSavedFilter]);

  const activeFilters: DeviceFilters = {};
  if (debouncedSearch) activeFilters.search = debouncedSearch;
  if (status) activeFilters.status = status;
  if (templateId) activeFilters.template_id = templateId;
  if (topologyType) activeFilters.topology_type = topologyType;
  const filtersApplied = Object.keys(activeFilters).length > 0;
  const filters = filtersApplied ? activeFilters : undefined;
  const { data, isLoading, isError } = usePaginatedDevices(filters, skip, limit, {
    enabled: !templatePending,
  });

  const changeStatus = (value: string) => {
    const next = DEVICE_STATUSES.find((v) => v === value) ?? "";
    setUserStatus(next);
    setSkip(0);
    persistFilters({ status: next });
  };
  const changeTemplate = (value: string) => {
    setUserTemplate(value);
    setSkip(0);
    persistFilters({ templateId: value });
  };
  const changeTopology = (value: string) => {
    const next = TOPOLOGY_TYPES.find((v) => v === value) ?? "";
    setUserTopology(next);
    setSkip(0);
    persistFilters({ topologyType: next });
  };
  const clearFilters = () => {
    setUserSearch("");
    setDebouncedSearch("");
    setUserStatus("");
    setUserTemplate("");
    setUserTopology("");
    setSkip(0);
    persistFilters({ search: "", status: "", templateId: "", topologyType: "" });
  };
  const showClear = filtersApplied || searchInput !== "";
  const listLoading = isLoading || (templatePending && !data);
  const devices = data?.items;
  const total = data?.total ?? 0;
  const { data: deviceNameMap } = useAllDeviceNames();

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);

  const prevDeviceIdsRef = useRef<string>("");
  const deviceIds = devices?.map((d) => d.id).join(",") ?? "";
  if (deviceIds !== prevDeviceIdsRef.current) {
    prevDeviceIdsRef.current = deviceIds;
    if (selected.size > 0) setSelected(new Set());
    // Expanded rows are pruned, not cleared: a row still on the page keeps its
    // panel open (the debounced search refetch lands while a user may already
    // have expanded a row), only ids that left the list are dropped. The set
    // is replaced only when something was actually dropped, so this render-time
    // update cannot loop.
    if (expandedIds.size > 0) {
      const listed = new Set(devices?.map((d) => d.id));
      const kept = new Set([...expandedIds].filter((id) => listed.has(id)));
      if (kept.size !== expandedIds.size) setExpandedIds(kept);
    }
  }

  const toggleOne = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleAll = () => {
    if (!devices) return;
    const allSelected = devices.every((d) => selected.has(d.id));
    if (allSelected) {
      setSelected(new Set());
    } else {
      setSelected(new Set(devices.map((d) => d.id)));
    }
  };

  const toggleExpand = (id: string) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const handleBulkDelete = async () => {
    setShowDeleteConfirm(false);
    const ids = Array.from(selected);
    const results = await Promise.allSettled(
      ids.map((id) => deleteDevice.mutateAsync(id))
    );
    const failed = results.filter((r) => r.status === "rejected").length;
    const succeeded = results.length - failed;
    // Issue #940: a device that cabling still names is refused with device_cabled;
    // say so, since "failed 2" gives the admin nothing to act on.
    const cabled = results.filter(
      (r) => r.status === "rejected" && deviceCabledCount(r.reason) !== null,
    ).length;
    if (failed === 0) {
      toast.success(`Deleted ${succeeded} device(s)`);
    } else if (cabled > 0) {
      toast.error(
        `Deleted ${succeeded}, failed ${failed}. ${cabled} still cabled: remove their cables first`,
      );
    } else {
      toast.error(`Deleted ${succeeded}, failed ${failed}`);
    }
    setSelected(new Set());
  };

  const handleCopy = async (device: Device) => {
    try {
      const newDevice = await createDevice.mutateAsync({
        name: `Copy of ${device.name}`,
        template_id: device.template_id,
        topology_type: device.topology_type,
        field_data: device.field_data,
      });

      const ports = await fetchPorts(device.id);
      let portsFailed = 0;
      for (const port of ports) {
        try {
          await createPort.mutateAsync({
            deviceId: newDevice.id,
            data: {
              name: port.name,
              template_id: port.template_id,
              field_data: port.field_data,
            },
          });
        } catch {
          portsFailed++;
        }
      }

      if (portsFailed > 0) {
        toast.success(`Device duplicated, but ${portsFailed} port(s) failed to copy`);
      } else if (ports.length > 0) {
        toast.success(`Device duplicated with ${ports.length} port(s)`);
      } else {
        toast.success("Device duplicated");
      }
    } catch {
      toast.error("Failed to duplicate device");
    }
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="px-6 xl:px-12 2xl:px-16 py-6 space-y-6">
        <section>
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-lg font-semibold text-gray-900">
              All Devices
              {data && (
                <span className="ml-2 text-sm text-gray-400 font-normal">({total})</span>
              )}
            </h2>
            {isAdmin && (
              <BulkImportExport
                resourceLabel="devices"
                onExport={exportDevices}
                onImport={importDevices}
                onImported={() => queryClient.invalidateQueries({ queryKey: ["devices"] })}
              />
            )}
          </div>
          <div className="mb-3 flex flex-wrap items-end gap-3">
            <input
              type="text"
              aria-label="Search devices"
              placeholder="Search devices by name..."
              value={searchInput}
              onChange={(e) => setUserSearch(e.target.value)}
              className="w-full max-w-sm px-3 py-2 text-sm border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
            />
            <FilterSelect label="Status" value={status} onChange={changeStatus}>
              <option value="">All</option>
              {DEVICE_STATUSES.map((v) => (
                <option key={v} value={v}>
                  {v}
                </option>
              ))}
            </FilterSelect>
            <FilterSelect label="Template" value={templateId} onChange={changeTemplate}>
              <option value="">All</option>
              {templates?.map((t) => (
                <option key={t.id} value={t.id}>
                  {templateOptionLabel(t)}
                </option>
              ))}
            </FilterSelect>
            <FilterSelect label="Topology" value={topologyType} onChange={changeTopology}>
              <option value="">All</option>
              {TOPOLOGY_TYPES.map((v) => (
                <option key={v} value={v}>
                  {v}
                </option>
              ))}
            </FilterSelect>
            {showClear && (
              <button
                type="button"
                onClick={clearFilters}
                className="px-3 py-2 text-sm font-medium text-gray-700 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors"
              >
                Clear filters
              </button>
            )}
          </div>
          {isAdmin && selected.size > 0 && (
            <div className="flex items-center gap-3 mb-3 px-1">
              <span className="text-sm text-gray-700 font-medium">{selected.size} selected</span>
              <button
                onClick={() => setShowDeleteConfirm(true)}
                className="px-3 py-1.5 text-sm font-medium text-white bg-red-600 hover:bg-red-700 rounded-lg transition-colors"
              >
                Delete Selected
              </button>
              <button
                onClick={() => setSelected(new Set())}
                className="px-3 py-1.5 text-sm font-medium text-gray-700 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors"
              >
                Clear
              </button>
            </div>
          )}
          <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
            {listLoading && (
              <div role="status" aria-live="polite">
                <SkeletonRows rows={5} />
              </div>
            )}
            {isError && (
              <p className="text-sm text-red-500 text-center py-8">Failed to load devices</p>
            )}
            {!isLoading && !isError && devices && (
              <div className="overflow-x-auto">
              {/* Vertical scroll lives on the outer page wrapper (h-full overflow-y-auto);
                  sticky header cells resolve against that scroll parent. */}
              <table className="w-full min-w-[800px]">
                <thead>
                  <tr>
                    <th className="sticky top-0 z-10 bg-gray-50 border-b border-gray-200 w-10 px-2 py-2" />
                    {isAdmin && (
                      <th className="sticky top-0 z-10 bg-gray-50 border-b border-gray-200 px-4 py-2">
                        <SelectAllCheckbox
                          checked={devices.length > 0 && devices.every((d) => selected.has(d.id))}
                          indeterminate={selected.size > 0 && !devices.every((d) => selected.has(d.id))}
                          onChange={toggleAll}
                        />
                      </th>
                    )}
                    <th className="sticky top-0 z-10 bg-gray-50 border-b border-gray-200 px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase tracking-wide">Name</th>
                    <th className="sticky top-0 z-10 bg-gray-50 border-b border-gray-200 px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase tracking-wide">Template</th>
                    <th className="sticky top-0 z-10 bg-gray-50 border-b border-gray-200 px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase tracking-wide">Topology</th>
                    <th className="sticky top-0 z-10 bg-gray-50 border-b border-gray-200 px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase tracking-wide">Status</th>
                    <th className="sticky top-0 z-10 bg-gray-50 border-b border-gray-200 px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase tracking-wide">ID</th>
                    {isAdmin && (
                      <th className="sticky top-0 z-10 bg-gray-50 border-b border-gray-200 px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase tracking-wide">Actions</th>
                    )}
                  </tr>
                </thead>
                <tbody>
                  {devices.length === 0 ? (
                    <EmptyRow colSpan={isAdmin ? 8 : 6}>
                      {filtersApplied ? (
                        <span>
                          No devices match the current filters.{" "}
                          <button
                            type="button"
                            onClick={clearFilters}
                            className="text-blue-600 hover:underline"
                          >
                            Clear filters
                          </button>
                        </span>
                      ) : (
                        "No devices found"
                      )}
                    </EmptyRow>
                  ) : (
                    devices.map((device) => {
                      const expanded = expandedIds.has(device.id);
                      return (
                        <Fragment key={device.id}>
                          <DeviceRow device={device} isAdmin={isAdmin} onCopy={handleCopy} onClick={() => navigate(`/inventory/${device.id}`)} selected={selected.has(device.id)} onToggle={() => toggleOne(device.id)} showCheckbox={isAdmin} expanded={expanded} onToggleExpand={() => toggleExpand(device.id)} />
                          {expanded && (
                            <ExpandedPortsRow
                              device={device}
                              deviceNameMap={deviceNameMap}
                              colSpan={isAdmin ? 8 : 6}
                            />
                          )}
                        </Fragment>
                      );
                    })
                  )}
                </tbody>
              </table>
              </div>
            )}
            <Pagination
              total={total}
              skip={skip}
              limit={limit}
              onPageChange={setSkip}
              pageSizeOptions={PAGE_SIZE_OPTIONS}
              onPageSizeChange={handlePageSizeChange}
            />
          </div>
        </section>
      </div>
      <ConfirmDialog
        open={showDeleteConfirm}
        title="Delete devices"
        description={`Are you sure you want to delete ${selected.size} device(s)? This action cannot be undone.`}
        confirmLabel="Delete"
        destructive
        onConfirm={handleBulkDelete}
        onCancel={() => setShowDeleteConfirm(false)}
      />
    </div>
  );
}
