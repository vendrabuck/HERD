import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Modal } from "@/components/ui/Modal";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { useCancelReservation, useReleaseReservation } from "@/api/reservations";
import { useAIStatus } from "@/api/ai";
import { useAuthStore } from "@/stores/authStore";
import { ReservationInventoryTab } from "./ReservationInventoryTab";
import { ReservationRoutesTab } from "./ReservationRoutesTab";
import { ReservationStatusTab } from "./ReservationStatusTab";
import { AIApplyConfirmModal } from "./AIApplyConfirmModal";
import { AIAssistantTab } from "./AIAssistantTab";
import { EditDevicesModal } from "./EditDevicesModal";
import type { PendingApply, ToolCall } from "@/types/ai.types";
import type { Reservation } from "@/types/reservation.types";

const STATUS_COLORS: Record<string, string> = {
  ACTIVE: "bg-green-100 text-green-800",
  PENDING: "bg-yellow-100 text-yellow-800",
  PENDING_PROVISION: "bg-amber-100 text-amber-800",
  COMPLETED: "bg-gray-100 text-gray-600",
  CANCELLED: "bg-red-100 text-red-700",
  FAILED: "bg-red-200 text-red-900",
};

type Tab = "details" | "inventory" | "routes" | "status" | "assistant";

interface Props {
  reservation: Reservation | null;
  deviceNames: Map<string, string>;
  onClose: () => void;
}

export function ReservationDetailModal({ reservation, deviceNames, onClose }: Props) {
  const [activeTab, setActiveTab] = useState<Tab>("details");
  const [editDevicesOpen, setEditDevicesOpen] = useState(false);
  const [confirmCancelOpen, setConfirmCancelOpen] = useState(false);
  const [assistantQuestion, setAssistantQuestion] = useState("");
  const [assistantAnswer, setAssistantAnswer] = useState("");
  const [pendingApply, setPendingApply] = useState<PendingApply | null>(null);
  const [assistantToolCalls, setAssistantToolCalls] = useState<ToolCall[]>([]);
  const [assistantToolIterations, setAssistantToolIterations] = useState(0);
  const cancel = useCancelReservation();
  const release = useReleaseReservation();
  const user = useAuthStore((s) => s.user);
  const { data: aiStatus } = useAIStatus();
  const navigate = useNavigate();

  if (!reservation) return null;

  const isOwner = user?.id === reservation.user_id;
  const canAct = isOwner && reservation.status === "ACTIVE";
  const canEdit = isOwner && (reservation.status === "ACTIVE" || reservation.status === "PENDING");
  // The fork is editable only while the reservation is ACTIVE (ADR 0006); after
  // it ends the fork is the frozen, read-only as-built record. A fork exists only
  // once the reservation has activated, so a still-PENDING reservation has no fork
  // view to open yet.
  const isEnded =
    reservation.status === "COMPLETED" ||
    reservation.status === "CANCELLED" ||
    reservation.status === "FAILED";
  const canEditFork = isOwner && reservation.status === "ACTIVE" && !!reservation.topology_id;
  const canViewAsBuilt = isOwner && isEnded && !!reservation.topology_id;

  // Both open the reservation's fork view (ADR 0006 Decision 6): the editor loads
  // the fork through GET /reservations/{id}/fork, not the parent topology_id, so
  // the parent's history stays clean. The editor renders read-only for an ended
  // (archived) fork.
  const openForkView = () => {
    onClose();
    navigate(`/topology/${reservation.topology_id}?reservationId=${reservation.id}`);
  };

  const start = new Date(reservation.start_time).toLocaleString();
  const end = new Date(reservation.end_time).toLocaleString();

  const tabs: { key: Tab; label: string }[] = [
    { key: "details", label: "Details" },
    { key: "inventory", label: "Inventory" },
    { key: "routes", label: "Routes" },
    { key: "status", label: "Schedule" },
    ...(aiStatus?.enabled ? [{ key: "assistant" as Tab, label: "AI Assistant" }] : []),
  ];

  return (
    <>
      <Modal open={!!reservation} onClose={onClose} title="Reservation" className="max-w-3xl">
        {/* Tab bar + Edit Resources button */}
        <div className="flex items-center gap-1 mb-4 border-b border-gray-200 pb-2">
          {tabs.map((t) => (
            <button
              key={t.key}
              onClick={() => setActiveTab(t.key)}
              className={`text-sm px-3 py-1.5 rounded ${
                activeTab === t.key
                  ? "bg-gray-900 text-white"
                  : "text-gray-500 hover:text-gray-900 hover:bg-gray-100"
              }`}
            >
              {t.label}
            </button>
          ))}
          {(canEdit || canViewAsBuilt) && activeTab !== "assistant" && (
            <div className="ml-auto flex items-center gap-2">
              {canEditFork && (
                <button
                  onClick={openForkView}
                  className="text-xs px-2.5 py-1.5 rounded border border-blue-300 text-blue-700 hover:bg-blue-50"
                >
                  Edit topology
                </button>
              )}
              {canViewAsBuilt && (
                <button
                  onClick={openForkView}
                  className="text-xs px-2.5 py-1.5 rounded border border-gray-300 text-gray-700 hover:bg-gray-50"
                >
                  View as-built
                </button>
              )}
              {canEdit && (
                <button
                  onClick={() => setEditDevicesOpen(true)}
                  className="text-xs px-2.5 py-1.5 rounded border border-gray-300 text-gray-600 hover:bg-gray-50"
                >
                  Edit Resources
                </button>
              )}
            </div>
          )}
        </div>

        {/* Details tab */}
        {activeTab === "details" && (
          <div className="space-y-3 text-sm">
            <div className="flex justify-between">
              <span className="text-gray-500">Owner</span>
              <span className="font-medium">{reservation.owner_name || reservation.user_id.slice(0, 8)}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-gray-500">Status</span>
              <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${STATUS_COLORS[reservation.status]}`}>
                {reservation.status}
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-gray-500">Start</span>
              <span>{start}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-gray-500">End</span>
              <span>{end}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-gray-500">Purpose</span>
              <span>{reservation.purpose || "-"}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-gray-500">Topology</span>
              <span className="text-xs px-1.5 py-0.5 rounded bg-gray-100 text-gray-600">
                {reservation.topology_type}
              </span>
            </div>
            <div>
              <span className="text-gray-500">Devices ({reservation.device_ids.length})</span>
              <ul className="mt-1 space-y-0.5">
                {reservation.device_ids.map((id) => (
                  <li key={id} className="text-gray-700 font-mono text-xs">
                    {deviceNames.get(id) || id.slice(0, 8)}
                  </li>
                ))}
              </ul>
            </div>
            {canAct && (
              <div className="flex gap-2 pt-2 border-t border-gray-200">
                <button
                  onClick={() => release.mutate(reservation.id, { onSuccess: onClose })}
                  disabled={release.isPending}
                  className="text-sm px-3 py-1.5 rounded bg-green-600 text-white hover:bg-green-700 disabled:opacity-50"
                >
                  Release
                </button>
                <button
                  onClick={() => setConfirmCancelOpen(true)}
                  disabled={cancel.isPending}
                  className="text-sm px-3 py-1.5 rounded bg-red-600 text-white hover:bg-red-700 disabled:opacity-50"
                >
                  Cancel
                </button>
              </div>
            )}
          </div>
        )}

        {/* Inventory tab */}
        {activeTab === "inventory" && (
          <ReservationInventoryTab deviceIds={reservation.device_ids} />
        )}

        {/* Routes tab */}
        {activeTab === "routes" && (
          <ReservationRoutesTab deviceIds={reservation.device_ids} />
        )}

        {/* Schedule tab */}
        {activeTab === "status" && (
          <ReservationStatusTab reservation={reservation} onUpdated={onClose} />
        )}

        {/* AI Assistant tab */}
        {activeTab === "assistant" && (
          <AIAssistantTab
            reservationId={reservation.id}
            question={assistantQuestion}
            setQuestion={setAssistantQuestion}
            answer={assistantAnswer}
            setAnswer={setAssistantAnswer}
            setPendingApply={setPendingApply}
            toolCalls={assistantToolCalls}
            setToolCalls={setAssistantToolCalls}
            toolIterations={assistantToolIterations}
            setToolIterations={setAssistantToolIterations}
          />
        )}
      </Modal>

      {editDevicesOpen && (
        <EditDevicesModal
          reservation={reservation}
          open={editDevicesOpen}
          onClose={() => setEditDevicesOpen(false)}
          onUpdated={onClose}
        />
      )}

      {pendingApply && (
        <AIApplyConfirmModal
          pendingApply={pendingApply}
          planText={assistantAnswer}
          onClose={() => setPendingApply(null)}
        />
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
          cancel.mutate(reservation.id, { onSuccess: onClose });
        }}
        onCancel={() => setConfirmCancelOpen(false)}
      />
    </>
  );
}
