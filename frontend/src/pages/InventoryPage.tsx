import { useState, useRef, useEffect, useMemo, Fragment } from "react";
import { useNavigate, Link } from "react-router-dom";
import toast from "react-hot-toast";
import { useQueryClient } from "@tanstack/react-query";
import { usePaginatedDevices, useCreateDevice, useDeleteDevice, useAllDeviceNames } from "@/api/inventory";
import { BulkImportExport } from "@/components/ui/BulkImportExport";
import { exportDevices, importDevices } from "@/api/bulk";
import { fetchPorts, useCreatePort, usePorts } from "@/api/ports";
import { useDeviceConnections } from "@/api/connections";
import { useAuthStore } from "@/stores/authStore";
import { isAdminRole } from "@/lib/roles";
import { usePreferencesStore } from "@/stores/preferencesStore";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { Pagination } from "@/components/ui/Pagination";
import { DeviceInfoPanel } from "@/components/inventory/DeviceInfoPanel";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { TopoBadge } from "@/components/ui/TopoBadge";
import { EmptyRow } from "@/components/ui/EmptyState";
import { SkeletonRows } from "@/components/ui/Skeleton";
import type { Device } from "@/types/device.types";

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

export function InventoryPage() {
  const navigate = useNavigate();
  const createDevice = useCreateDevice();
  const createPort = useCreatePort();
  const deleteDevice = useDeleteDevice();
  const user = useAuthStore((s) => s.user);
  const isAdmin = isAdminRole(user?.role);
  const queryClient = useQueryClient();

  const storedSearch = usePreferencesStore((s) =>
    (s.savedFilters.inventory as { search?: string } | undefined)?.search ?? "",
  );
  const limit = usePreferencesStore((s) => s.getPageSize("inventory", 50));
  const setSavedFilter = usePreferencesStore((s) => s.setSavedFilter);
  const setPageSize = usePreferencesStore((s) => s.setPageSize);

  const [skip, setSkip] = useState(0);
  const [userSearch, setUserSearch] = useState<string | null>(null);
  const searchInput = userSearch ?? storedSearch;
  const [debouncedSearch, setDebouncedSearch] = useState(storedSearch);

  const handlePageSizeChange = (size: number) => {
    setPageSize("inventory", size);
    setSkip(0);
  };

  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedSearch(searchInput);
      setSkip(0);
      if (userSearch !== null) {
        setSavedFilter("inventory", { search: userSearch });
      }
    }, 300);
    return () => clearTimeout(timer);
  }, [searchInput, userSearch, setSavedFilter]);

  const filters = debouncedSearch ? { search: debouncedSearch } : undefined;
  const { data, isLoading, isError } = usePaginatedDevices(filters, skip, limit);
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
    if (expandedIds.size > 0) setExpandedIds(new Set());
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
    if (failed === 0) {
      toast.success(`Deleted ${succeeded} device(s)`);
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
          <div className="mb-3">
            <input
              type="text"
              placeholder="Search devices by name..."
              value={searchInput}
              onChange={(e) => setUserSearch(e.target.value)}
              className="w-full max-w-sm px-3 py-2 text-sm border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
            />
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
            {isLoading && (
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
                    <EmptyRow colSpan={isAdmin ? 8 : 6}>No devices found</EmptyRow>
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
