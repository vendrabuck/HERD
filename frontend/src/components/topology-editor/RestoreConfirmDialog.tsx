import { useState } from "react";

import { Modal } from "@/components/ui/Modal";
import type { TopologyVersion } from "@/types/topology.types";

interface RestoreConfirmDialogProps {
  open: boolean;
  version: TopologyVersion | null;
  onClose: () => void;
  onConfirm: (opts: { description: string; restoreName: boolean }) => void;
  isPending?: boolean;
  blockingReservations?: Array<{ id: string; status: string; end_time?: string }>;
}

export function RestoreConfirmDialog({
  open,
  version,
  onClose,
  onConfirm,
  isPending,
  blockingReservations,
}: RestoreConfirmDialogProps) {
  const [description, setDescription] = useState("");
  const [restoreName, setRestoreName] = useState(false);

  if (!version) return null;

  const blocked = !!blockingReservations && blockingReservations.length > 0;

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={`Restore version v${version.version_number}`}
    >
      <div className="space-y-4 text-sm">
        <p className="text-gray-700">
          Restoring copies this snapshot back onto the topology and creates a new
          version so the restore itself is auditable. Any unsaved canvas changes
          will be discarded.
        </p>

        {blocked && (
          <div className="rounded border border-red-200 bg-red-50 p-3 text-red-800">
            <p className="font-medium">Restore blocked by active reservations:</p>
            <ul className="mt-1 text-xs list-disc pl-4">
              {blockingReservations!.map((r) => (
                <li key={r.id}>
                  {r.id} ({r.status})
                </li>
              ))}
            </ul>
          </div>
        )}

        <label className="block">
          <span className="text-xs uppercase text-gray-500">Description (optional)</span>
          <input
            type="text"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder={`Restored from v${version.version_number}`}
            className="mt-1 w-full border border-gray-300 rounded px-2 py-1 text-sm"
          />
        </label>

        <label className="flex items-center gap-2 text-sm text-gray-700">
          <input
            type="checkbox"
            checked={restoreName}
            onChange={(e) => setRestoreName(e.target.checked)}
          />
          Also restore the topology name to "{version.name}"
        </label>

        <div className="flex justify-end gap-3 pt-2">
          <button
            onClick={onClose}
            className="px-3 py-1.5 text-sm bg-gray-100 rounded hover:bg-gray-200"
          >
            Cancel
          </button>
          <button
            onClick={() => onConfirm({ description, restoreName })}
            disabled={isPending || blocked}
            className="px-3 py-1.5 text-sm bg-amber-600 text-white rounded hover:bg-amber-700 disabled:bg-gray-300 disabled:cursor-not-allowed"
          >
            {isPending ? "Restoring..." : "Restore"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
