import { useState } from "react";
import toast from "react-hot-toast";
import {
  usePaginatedConnections,
  useCreateConnection,
  useCreateConnectionsBulk,
  useDeleteConnection,
} from "@/api/connections";
import { usePaginatedDevices, useAllDeviceNames } from "@/api/inventory";
import { usePorts } from "@/api/ports";
import { Modal } from "@/components/ui/Modal";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { Pagination } from "@/components/ui/Pagination";
import { MultiConnectDialog } from "@/components/admin/connections/MultiConnectDialog";
import { cn } from "@/lib/cn";
import type { ConnectionCreate } from "@/types/connection.types";

// The multi-connection dialog is the default create surface; the original
// single-pair modal survives behind this toggle, the same way the topology
// editor keeps QuickConnectPopover alongside its wiring dialog.
type CreateMode = "multi" | "single";

export function ConnectionsPage() {
  const [skip, setSkip] = useState(0);
  const limit = 50;
  const [filterDeviceSearch, setFilterDeviceSearch] = useState("");
  const [filterDeviceId, setFilterDeviceId] = useState<string | undefined>(undefined);

  const { data, isLoading } = usePaginatedConnections(skip, limit, filterDeviceId);
  const connections = data?.items;
  const total = data?.total ?? 0;

  // Fetch all device names for resolution in the table
  const { data: deviceMap } = useAllDeviceNames();

  // Filter device search results
  const [filterSearchInput, setFilterSearchInput] = useState("");
  const { data: filterDevices } = usePaginatedDevices(
    filterSearchInput.trim() ? { search: filterSearchInput.trim() } : undefined,
    0,
    20,
  );

  const createConnection = useCreateConnection();
  const createConnectionsBulk = useCreateConnectionsBulk();
  const deleteConnection = useDeleteConnection();

  // Create modal state
  const [createMode, setCreateMode] = useState<CreateMode>("multi");
  const [showCreate, setShowCreate] = useState(false);
  const [showMulti, setShowMulti] = useState(false);
  const [deleteId, setDeleteId] = useState<string | null>(null);

  // Create form state
  const [deviceASearch, setDeviceASearch] = useState("");
  const [deviceAId, setDeviceAId] = useState("");
  const [deviceAName, setDeviceAName] = useState("");
  const [portA, setPortA] = useState("");
  const [deviceBSearch, setDeviceBSearch] = useState("");
  const [deviceBId, setDeviceBId] = useState("");
  const [deviceBName, setDeviceBName] = useState("");
  const [portB, setPortB] = useState("");
  const [connectionType, setConnectionType] = useState("ethernet");
  const [notes, setNotes] = useState("");

  // Device search for create modal
  const { data: deviceAResults } = usePaginatedDevices(
    deviceASearch.trim().length >= 2 ? { search: deviceASearch.trim() } : undefined,
    0,
    20,
  );
  const { data: deviceBResults } = usePaginatedDevices(
    deviceBSearch.trim().length >= 2 ? { search: deviceBSearch.trim() } : undefined,
    0,
    20,
  );

  // Port fetching for selected devices
  const { data: portsA = [] } = usePorts(deviceAId || undefined);
  const { data: portsB = [] } = usePorts(deviceBId || undefined);

  const handleCreate = async () => {
    if (!deviceAId) {
      toast.error("Device A is required");
      return;
    }
    if (!portA) {
      toast.error("Port A is required");
      return;
    }
    if (!deviceBId) {
      toast.error("Device B is required");
      return;
    }
    if (!portB) {
      toast.error("Port B is required");
      return;
    }
    const body: ConnectionCreate = {
      device_a_id: deviceAId,
      port_a: portA,
      device_b_id: deviceBId,
      port_b: portB,
      connection_type: connectionType.trim() || "ethernet",
      notes: notes.trim() || undefined,
    };
    try {
      await createConnection.mutateAsync(body);
      toast.success("Connection created");
      closeCreateModal();
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Failed to create connection";
      toast.error(msg);
    }
  };

  const openCreate = () => {
    if (createMode === "multi") setShowMulti(true);
    else setShowCreate(true);
  };

  // Fires only when every row of the batch was created; a partially rejected
  // batch keeps the dialog open with its failures flagged, so this must never
  // be reached for one.
  const handleBulkSuccess = (created: number) => {
    toast.success(created === 1 ? "Created 1 connection" : `Created ${created} connections`);
    setShowMulti(false);
  };

  const handleDelete = async () => {
    if (!deleteId) return;
    try {
      await deleteConnection.mutateAsync(deleteId);
      toast.success("Connection deleted");
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Failed to delete connection";
      toast.error(msg);
    }
    setDeleteId(null);
  };

  const closeCreateModal = () => {
    setShowCreate(false);
    setDeviceASearch("");
    setDeviceAId("");
    setDeviceAName("");
    setPortA("");
    setDeviceBSearch("");
    setDeviceBId("");
    setDeviceBName("");
    setPortB("");
    setConnectionType("ethernet");
    setNotes("");
  };

  const selectDeviceA = (id: string, name: string) => {
    setDeviceAId(id);
    setDeviceAName(name);
    setDeviceASearch("");
    setPortA("");
  };

  const selectDeviceB = (id: string, name: string) => {
    setDeviceBId(id);
    setDeviceBName(name);
    setDeviceBSearch("");
    setPortB("");
  };

  const handleFilterDevice = (id: string, name: string) => {
    setFilterDeviceId(id);
    setFilterDeviceSearch(name);
    setFilterSearchInput("");
    setSkip(0);
  };

  const clearFilter = () => {
    setFilterDeviceId(undefined);
    setFilterDeviceSearch("");
    setFilterSearchInput("");
    setSkip(0);
  };

  const deviceName = (id: string) => deviceMap?.get(id) ?? id.slice(0, 8) + "...";

  return (
    <div className="h-full overflow-y-auto">
      <div className="px-6 xl:px-12 2xl:px-16 py-8">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-gray-900">Connections</h2>
          <div className="flex items-center gap-3">
            <div className="flex gap-1" role="group" aria-label="Create mode">
              {(["multi", "single"] as CreateMode[]).map((mode) => (
                <button
                  key={mode}
                  type="button"
                  onClick={() => setCreateMode(mode)}
                  aria-pressed={createMode === mode}
                  title={
                    mode === "multi"
                      ? "Stage many cables between two devices and register them in one batch"
                      : "Register a single cable, one pair of ports at a time"
                  }
                  className={cn(
                    "px-3 py-1.5 text-sm font-medium rounded transition-colors",
                    createMode === mode
                      ? "bg-gray-900 text-white"
                      : "bg-gray-100 text-gray-700 hover:bg-gray-200",
                  )}
                >
                  {mode === "multi" ? "Multi" : "Single"}
                </button>
              ))}
            </div>
            <button
              onClick={openCreate}
              className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition-colors"
            >
              Create Connection
            </button>
          </div>
        </div>

        {/* Device filter */}
        <div className="mb-4 relative">
          <div className="flex items-center gap-2">
            <input
              type="text"
              placeholder="Filter by device name..."
              value={filterDeviceId ? filterDeviceSearch : filterSearchInput}
              onChange={(e) => {
                if (filterDeviceId) {
                  clearFilter();
                }
                setFilterSearchInput(e.target.value);
              }}
              className="w-full max-w-sm px-3 py-2 text-sm border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
            />
            {filterDeviceId && (
              <button
                onClick={clearFilter}
                className="px-3 py-2 text-sm text-gray-600 hover:text-gray-900 bg-gray-100 rounded-lg hover:bg-gray-200 transition-colors"
              >
                Clear
              </button>
            )}
          </div>
          {filterSearchInput.trim() && !filterDeviceId && filterDevices?.items && filterDevices.items.length > 0 && (
            <div className="absolute z-10 mt-1 w-full max-w-sm bg-white border border-gray-200 rounded-lg shadow-lg max-h-48 overflow-y-auto">
              {filterDevices.items.map((d) => (
                <button
                  key={d.id}
                  onClick={() => handleFilterDevice(d.id, d.name)}
                  className="block w-full text-left px-3 py-2 text-sm hover:bg-gray-100"
                >
                  {d.name}
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
          {isLoading ? (
            <p className="text-sm text-gray-500 px-4 py-4">Loading connections...</p>
          ) : !connections || connections.length === 0 ? (
            <p className="text-sm text-gray-500 px-4 py-4">No connections found</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[700px] text-sm text-left">
                <thead className="bg-gray-50 text-gray-600 uppercase text-xs">
                  <tr>
                    <th className="px-4 py-3">Side A</th>
                    <th className="px-4 py-3">Side B</th>
                    <th className="px-4 py-3">Type</th>
                    <th className="px-4 py-3">Notes</th>
                    <th className="px-4 py-3">Created By</th>
                    <th className="px-4 py-3">Date</th>
                    <th className="px-4 py-3">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {connections.map((conn) => (
                    <tr key={conn.id} className="hover:bg-gray-50">
                      <td className="px-4 py-3">
                        <div className="font-medium text-gray-900">{deviceName(conn.device_a_id)}</div>
                        <div className="text-gray-600 font-mono text-xs">{conn.port_a}</div>
                      </td>
                      <td className="px-4 py-3">
                        <div className="font-medium text-gray-900">{deviceName(conn.device_b_id)}</div>
                        <div className="text-gray-600 font-mono text-xs">{conn.port_b}</div>
                      </td>
                      <td className="px-4 py-3 text-gray-600">{conn.connection_type}</td>
                      <td className="px-4 py-3 text-gray-500 truncate max-w-[200px]">
                        {conn.notes || "-"}
                      </td>
                      <td className="px-4 py-3 text-gray-600">{conn.created_by}</td>
                      <td className="px-4 py-3 text-gray-500">
                        {new Date(conn.created_at).toLocaleDateString()}
                      </td>
                      <td className="px-4 py-3">
                        <button
                          onClick={() => setDeleteId(conn.id)}
                          className="text-xs text-red-600 hover:text-red-800"
                        >
                          Delete
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <Pagination total={total} skip={skip} limit={limit} onPageChange={setSkip} />
        </div>
      </div>

      {/* Both create surfaces mount only while open: Modal hardcodes
          id="modal-title", so two mounted Modals would put duplicate ids in the
          document and every aria-labelledby would resolve to the first one. */}
      {showCreate && (
      <Modal
        open
        onClose={closeCreateModal}
        title="Create Connection"
        className="max-w-lg"
      >
        <div className="space-y-4">
          {/* Device A */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Device A</label>
            {deviceAId ? (
              <div className="flex items-center gap-2">
                <span className="text-sm text-gray-900">{deviceAName}</span>
                <button
                  type="button"
                  onClick={() => { setDeviceAId(""); setDeviceAName(""); setPortA(""); }}
                  className="text-xs text-gray-500 hover:text-gray-700"
                >
                  Change
                </button>
              </div>
            ) : (
              <div className="relative">
                <input
                  type="text"
                  placeholder="Search devices..."
                  value={deviceASearch}
                  onChange={(e) => setDeviceASearch(e.target.value)}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
                {deviceASearch.trim().length >= 2 && deviceAResults?.items && deviceAResults.items.length > 0 && (
                  <div className="absolute z-10 mt-1 w-full bg-white border border-gray-200 rounded-lg shadow-lg max-h-40 overflow-y-auto">
                    {deviceAResults.items.map((d) => (
                      <button
                        key={d.id}
                        onClick={() => selectDeviceA(d.id, d.name)}
                        className="block w-full text-left px-3 py-2 text-sm hover:bg-gray-100"
                      >
                        {d.name}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Port A */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Port A</label>
            <select
              value={portA}
              onChange={(e) => setPortA(e.target.value)}
              disabled={!deviceAId}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-gray-100 disabled:text-gray-400"
            >
              <option value="">{deviceAId ? "Select port" : "Select device first"}</option>
              {portsA.map((p) => (
                <option key={p.id} value={p.name}>{p.name}</option>
              ))}
            </select>
            {deviceAId && portsA.length === 0 && (
              <p className="text-xs text-gray-400 mt-1">No ports configured on this device</p>
            )}
          </div>

          {/* Device B */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Device B</label>
            {deviceBId ? (
              <div className="flex items-center gap-2">
                <span className="text-sm text-gray-900">{deviceBName}</span>
                <button
                  type="button"
                  onClick={() => { setDeviceBId(""); setDeviceBName(""); setPortB(""); }}
                  className="text-xs text-gray-500 hover:text-gray-700"
                >
                  Change
                </button>
              </div>
            ) : (
              <div className="relative">
                <input
                  type="text"
                  placeholder="Search devices..."
                  value={deviceBSearch}
                  onChange={(e) => setDeviceBSearch(e.target.value)}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
                {deviceBSearch.trim().length >= 2 && deviceBResults?.items && deviceBResults.items.length > 0 && (
                  <div className="absolute z-10 mt-1 w-full bg-white border border-gray-200 rounded-lg shadow-lg max-h-40 overflow-y-auto">
                    {deviceBResults.items.map((d) => (
                      <button
                        key={d.id}
                        onClick={() => selectDeviceB(d.id, d.name)}
                        className="block w-full text-left px-3 py-2 text-sm hover:bg-gray-100"
                      >
                        {d.name}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Port B */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Port B</label>
            <select
              value={portB}
              onChange={(e) => setPortB(e.target.value)}
              disabled={!deviceBId}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-gray-100 disabled:text-gray-400"
            >
              <option value="">{deviceBId ? "Select port" : "Select device first"}</option>
              {portsB.map((p) => (
                <option key={p.id} value={p.name}>{p.name}</option>
              ))}
            </select>
            {deviceBId && portsB.length === 0 && (
              <p className="text-xs text-gray-400 mt-1">No ports configured on this device</p>
            )}
          </div>

          {/* Connection Type */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Connection Type</label>
            <input
              type="text"
              value={connectionType}
              onChange={(e) => setConnectionType(e.target.value)}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>

          {/* Notes */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Notes</label>
            <textarea
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              rows={2}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>

          <div className="flex justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={closeCreateModal}
              className="px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 rounded-lg hover:bg-gray-200 transition-colors"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleCreate}
              disabled={createConnection.isPending}
              className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors"
            >
              {createConnection.isPending ? "Creating..." : "Create"}
            </button>
          </div>
        </div>
      </Modal>
      )}

      {/* Mounted only while open so its device/port queries never run in the
          background behind the single-pair modal. */}
      {showMulti && (
        <MultiConnectDialog
          open
          onSubmit={(items) => createConnectionsBulk.mutateAsync(items)}
          onSuccess={handleBulkSuccess}
          onCancel={() => setShowMulti(false)}
        />
      )}

      <ConfirmDialog
        open={deleteId !== null}
        title="Delete Connection"
        description="Are you sure you want to delete this connection? This cannot be undone."
        confirmLabel="Delete"
        destructive
        onConfirm={handleDelete}
        onCancel={() => setDeleteId(null)}
      />
    </div>
  );
}
