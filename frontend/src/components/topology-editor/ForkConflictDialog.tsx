import { Modal } from "@/components/ui/Modal";
import type { ForkConflictDetail } from "@/types/reservation.types";

interface ForkConflictDialogProps {
  open: boolean;
  detail: ForkConflictDetail | null;
  onClose: () => void;
}

function shortId(id: string): string {
  return id.length > 8 ? id.slice(0, 8) : id;
}

// Shown when a fork save is refused with a cross-reservation port-claim 409
// (ADR 0006 Decision 4). Lists every blocking reservation, device, and port from
// the structured detail. The editor keeps the user's drawing so they can rework
// the wiring and save again.
export function ForkConflictDialog({ open, detail, onClose }: ForkConflictDialogProps) {
  const conflicts = detail?.conflicts ?? [];
  return (
    <Modal open={open} onClose={onClose} title="Ports already claimed" className="max-w-lg">
      <p className="text-sm text-gray-700">
        {detail?.message ??
          "One or more ports are already claimed by another active reservation."}
      </p>
      <p className="text-xs text-gray-500 mt-1">
        Your drawing is kept on the canvas. Re-wire the conflicting ports and save again.
      </p>
      <ul className="mt-3 divide-y divide-gray-100 border border-gray-200 rounded">
        {conflicts.map((c, i) => (
          <li key={`${c.reservation_id}-${c.device_id}-${c.port}-${i}`} className="px-3 py-2 text-sm">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-red-100 text-red-700">
                port {c.port}
              </span>
              <span className="text-gray-700">
                device <span className="font-mono text-xs">{shortId(c.device_id)}</span>
              </span>
              <span className="text-gray-500 text-xs">
                held by reservation <span className="font-mono">{shortId(c.reservation_id)}</span>
              </span>
            </div>
          </li>
        ))}
      </ul>
      <div className="flex justify-end mt-4">
        <button
          onClick={onClose}
          className="px-4 py-2 bg-blue-600 text-white text-sm rounded hover:bg-blue-700"
        >
          Back to editing
        </button>
      </div>
    </Modal>
  );
}
