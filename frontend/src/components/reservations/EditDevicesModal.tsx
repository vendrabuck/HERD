import { useState, useMemo, useRef, useEffect, useId } from "react";
import type { MouseEvent } from "react";
import { Modal } from "@/components/ui/Modal";
import { useUpdateReservation } from "@/api/reservations";
import { usePaginatedDevices, useAllDeviceNames } from "@/api/inventory";
import toast from "react-hot-toast";
import { errorDetail } from "@/lib/errors";
import { LoadError, LoadErrorNotice } from "@/components/ui/LoadError";
import { MoreMatchesNotice } from "@/components/ui/MoreMatchesNotice";
import { loadState, noItems } from "@/lib/loadState";
import type { Reservation } from "@/types/reservation.types";

interface Props {
  reservation: Reservation;
  open: boolean;
  onClose: () => void;
  onUpdated: () => void;
}

/** Candidates the Add Devices list asks for in one request. */
export const EDIT_DEVICES_PAGE_SIZE = 50;

export function EditDevicesModal({ reservation, open, onClose, onUpdated }: Props) {
  const [selectedIds, setSelectedIds] = useState<Set<string>>(
    () => new Set(reservation.device_ids),
  );
  const [search, setSearch] = useState("");
  const update = useUpdateReservation();
  const { data: deviceNames } = useAllDeviceNames();

  // Candidates match the reservation's topology type. Only an ACTIVE row holds its
  // devices, so only there must an added device be AVAILABLE now. A PENDING row
  // holds nothing until its window starts, so, like a new booking for a future
  // window, the server's window conflict check decides and the list is not
  // filtered by current status (issue #999).
  // One page of candidates: past it, MoreMatchesNotice says how many match and
  // the search narrows the list (issue #1590). A failed read shows LoadError,
  // never "No available devices found". While a new search is on its way the
  // previous rows stay (keepPreviousData) and the notice is hidden, since its
  // count belongs to the previous search.
  const availableQuery = usePaginatedDevices(
    {
      topology_type: reservation.topology_type,
      status: reservation.status === "PENDING" ? undefined : "AVAILABLE",
      dut_only: true,
      search: search || undefined,
    },
    0,
    EDIT_DEVICES_PAGE_SIZE,
  );
  const availableState = loadState(availableQuery, noItems);
  const availableData = availableState === "loaded" ? availableQuery.data : undefined;
  const availableDevices = availableData?.items ?? [];

  // Merge available devices with currently selected ones for display
  const currentDeviceNames = useMemo(() => {
    const map = new Map<string, string>();
    for (const id of selectedIds) {
      map.set(id, deviceNames?.get(id) ?? id.slice(0, 8) + "...");
    }
    return map;
  }, [selectedIds, deviceNames]);

  const handleToggle = (id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };

  // Focus after an Add or Remove (issue #1405, WCAG 2.4.3). The activated
  // button leaves its list, so focus would fall back to the dialog and the next
  // Tab would start at the top again. Instead it moves to the control that
  // takes its place in the same list: the next one, else the previous one,
  // else the search box. Only a button that held focus hands it on, so a
  // mouse click where the browser does not focus buttons moves nothing.
  const addListRef = useRef<HTMLDivElement>(null);
  const selectedListRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  // The request is state, not a ref, so the click handler touches no ref (the
  // react-hooks/refs rule, see WiringDialog); each activation sets a new
  // object, so the effect runs once per request, after the toggle committed.
  const [focusRequest, setFocusRequest] = useState<{
    list: "add" | "selected";
    index: number;
  } | null>(null);
  const rowIdBase = useId();

  const activate = (
    e: MouseEvent<HTMLButtonElement>,
    id: string,
    list: "add" | "selected",
    index: number,
  ) => {
    if (document.activeElement === e.currentTarget) setFocusRequest({ list, index });
    handleToggle(id);
  };

  useEffect(() => {
    if (!focusRequest) return;
    const container = focusRequest.list === "add" ? addListRef.current : selectedListRef.current;
    const buttons = container?.querySelectorAll<HTMLButtonElement>("button") ?? [];
    const target = buttons[Math.min(focusRequest.index, buttons.length - 1)] ?? searchRef.current;
    target?.focus();
  }, [focusRequest]);

  const handleSave = async () => {
    if (selectedIds.size === 0) {
      toast.error("At least one device must be selected");
      return;
    }
    try {
      await update.mutateAsync({
        id: reservation.id,
        data: { device_ids: Array.from(selectedIds) },
      });
      toast.success("Reservation devices updated");
      onUpdated();
      onClose();
    } catch (err: unknown) {
      toast.error(errorDetail(err, "Failed to update devices"));
    }
  };

  const hasChanges = useMemo(() => {
    const original = new Set(reservation.device_ids);
    if (original.size !== selectedIds.size) return true;
    for (const id of selectedIds) {
      if (!original.has(id)) return true;
    }
    return false;
  }, [reservation.device_ids, selectedIds]);

  return (
    <Modal open={open} onClose={onClose} title="Edit Reservation Devices" className="max-w-2xl">
      <div className="space-y-4">
        {/* Current devices */}
        <div>
          <h4 className="text-xs font-medium text-gray-500 uppercase mb-2">
            {/* A polite status region, the list pages' selection-count pattern:
                an add or remove is announced by the new count. */}
            <span role="status" aria-live="polite">
              Selected Devices ({selectedIds.size})
            </span>
          </h4>
          <div ref={selectedListRef} className="border border-gray-200 rounded max-h-40 overflow-y-auto">
            {selectedIds.size === 0 ? (
              <p className="text-sm text-gray-400 text-center py-3">No devices selected</p>
            ) : (
              Array.from(selectedIds).map((id, index) => (
                <div
                  key={id}
                  className="flex items-center justify-between px-3 py-1.5 border-b border-gray-100 last:border-b-0"
                >
                  <span className="text-sm text-gray-700">
                    {currentDeviceNames.get(id) ?? id.slice(0, 8) + "..."}
                  </span>
                  <button
                    type="button"
                    onClick={(e) => activate(e, id, "selected", index)}
                    className="text-xs text-red-500 hover:text-red-700 px-1.5 py-0.5"
                  >
                    Remove
                  </button>
                </div>
              ))
            )}
          </div>
        </div>

        {/* Add devices */}
        <div>
          <h4 className="text-xs font-medium text-gray-500 uppercase mb-2">Add Devices</h4>
          <input
            ref={searchRef}
            type="text"
            placeholder="Search available devices..."
            aria-label="Search available devices"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full border border-gray-300 rounded px-3 py-1.5 text-sm mb-2"
          />
          <LoadErrorNotice
            what="available devices"
            query={availableQuery}
            isEmpty={noItems}
            className="mb-2"
          />
          <div ref={addListRef} className="border border-gray-200 rounded max-h-48 overflow-y-auto">
            {availableState === "loading" && (
              <p className="text-sm text-gray-400 text-center py-3">Loading...</p>
            )}
            {availableState === "error" && (
              <LoadError
                what="available devices"
                error={availableQuery.error}
                onRetry={() => void availableQuery.refetch()}
                retrying={availableQuery.isFetching}
                className="m-2"
              />
            )}
            {availableState === "empty" && (
              <p className="text-sm text-gray-400 text-center py-3">No available devices found</p>
            )}
            {availableDevices
              .filter((d) => !selectedIds.has(d.id))
              .map((d, index) => (
                // The whole row is one native button (issue #1405), so Tab
                // reaches it and Enter or Space adds the device; there is no
                // second click handler on a wrapper to fire twice. The name
                // replaces the visible content, so the template, which sighted
                // users see, is carried as the description.
                <button
                  key={d.id}
                  type="button"
                  aria-label={`Add ${d.name}`}
                  aria-describedby={d.template_name ? `${rowIdBase}-template-${d.id}` : undefined}
                  className="w-full flex items-center justify-between px-3 py-1.5 text-left border-b border-gray-100 last:border-b-0 hover:bg-gray-50 cursor-pointer focus:outline-none focus:ring-2 focus:ring-inset focus:ring-blue-500"
                  onClick={(e) => activate(e, d.id, "add", index)}
                >
                  <span>
                    <span className="text-sm text-gray-700">{d.name}</span>
                    <span id={`${rowIdBase}-template-${d.id}`} className="text-xs text-gray-400 ml-2">
                      {d.template_name}
                    </span>
                  </span>
                  <span className="text-xs text-blue-600">Add</span>
                </button>
              ))}
          </div>
          {availableData && !availableQuery.isPlaceholderData && (
            <MoreMatchesNotice
              shown={availableData.items.length}
              total={availableData.total}
              noun="devices"
              className="mt-1"
            />
          )}
        </div>

        {/* Actions */}
        <div className="flex gap-2 pt-2 border-t border-gray-200">
          <button
            onClick={handleSave}
            disabled={update.isPending || !hasChanges}
            className="text-sm px-3 py-1.5 rounded bg-gray-900 text-white hover:bg-gray-800 disabled:opacity-50"
          >
            {update.isPending ? "Saving..." : "Save Changes"}
          </button>
          <button
            onClick={onClose}
            className="text-sm px-3 py-1.5 rounded border border-gray-300 text-gray-600 hover:bg-gray-50"
          >
            Cancel
          </button>
        </div>
      </div>
    </Modal>
  );
}
