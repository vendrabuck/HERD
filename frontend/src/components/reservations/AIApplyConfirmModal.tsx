import { isAxiosError } from "axios";
import toast from "react-hot-toast";
import { Modal } from "@/components/ui/Modal";
import {
  useApplyJob,
  useCancelApplyJobById,
  useConfirmDryRunApply,
} from "@/api/deviceConfigJobs";
import { useCommandLog } from "@/api/executions";
import type { PendingApply } from "@/types/ai.types";

interface Props {
  pendingApply: PendingApply;
  planText: string;
  onClose: () => void;
}

export function AIApplyConfirmModal({ pendingApply, planText, onClose }: Props) {
  const { data: job, isLoading: jobLoading } = useApplyJob(pendingApply.job_id);
  const { data: transcript, isLoading: transcriptLoading } = useCommandLog(
    job?.run_id,
    !!job?.run_id,
  );
  const confirm = useConfirmDryRunApply();
  const cancel = useCancelApplyJobById();

  const status = job?.status ?? "pending";
  const terminalSuccess = status === "success";
  const terminalFailure = status === "failed" || status === "skipped" || status === "cancelled";
  const canConfirm = terminalSuccess && !confirm.isPending && !cancel.isPending;
  const canCancel = !terminalSuccess && !cancel.isPending && !confirm.isPending;

  const handleConfirm = async () => {
    try {
      await confirm.mutateAsync(pendingApply.job_id);
      toast.success("Real apply scheduled. Watch the device's apply jobs for the result.");
      onClose();
    } catch (err) {
      if (isAxiosError(err)) {
        const detail = err.response?.data?.detail;
        toast.error(detail || "Failed to confirm apply");
      } else {
        toast.error("Failed to confirm apply");
      }
    }
  };

  const handleCancel = async () => {
    try {
      await cancel.mutateAsync(pendingApply.job_id);
      toast.success("Dry-run cancelled.");
      onClose();
    } catch {
      toast.error("Failed to cancel dry-run");
    }
  };

  return (
    <Modal
      open={true}
      onClose={onClose}
      title="Review AI-proposed change"
      className="max-w-3xl"
    >
      <div className="space-y-4 text-sm">
        <section>
          <h3 className="text-xs uppercase tracking-wide text-gray-500 mb-1">Plan</h3>
          <div className="whitespace-pre-wrap text-gray-800 bg-gray-50 border border-gray-200 rounded p-2">
            {planText || "(no plan text)"}
          </div>
        </section>

        <section>
          <h3 className="text-xs uppercase tracking-wide text-gray-500 mb-1">
            Dry-run status
          </h3>
          <div className="text-gray-700">
            Job <span className="font-mono text-xs">{pendingApply.job_id.slice(0, 8)}</span>{" "}
            scheduled for {new Date(pendingApply.scheduled_for).toLocaleString()}.{" "}
            {jobLoading ? (
              <span className="text-gray-400 italic">Loading...</span>
            ) : terminalSuccess ? (
              <span className="text-green-700 font-medium">Dry-run succeeded.</span>
            ) : terminalFailure ? (
              <span className="text-red-700 font-medium">Dry-run {status}.</span>
            ) : (
              <span className="text-gray-500 italic">Waiting for dry-run to complete...</span>
            )}
          </div>
          {job?.error && (
            <div className="mt-1 text-red-700 text-xs whitespace-pre-wrap">{job.error}</div>
          )}
        </section>

        <section>
          <h3 className="text-xs uppercase tracking-wide text-gray-500 mb-1">
            Captured commands
          </h3>
          {!job?.run_id ? (
            <div className="text-gray-400 italic">
              No run yet. The transcript appears here once the dry-run completes.
            </div>
          ) : transcriptLoading ? (
            <div className="text-gray-400 italic">Loading transcript...</div>
          ) : !transcript || transcript.length === 0 ? (
            <div className="text-gray-400 italic">
              No commands recorded. The driver may not support per-command logging.
            </div>
          ) : (
            <div className="border border-gray-200 rounded overflow-hidden">
              <table className="w-full text-xs font-mono">
                <thead className="bg-gray-50 text-gray-500">
                  <tr>
                    <th className="text-left px-2 py-1 w-8">#</th>
                    <th className="text-left px-2 py-1">Command</th>
                    <th className="text-left px-2 py-1">Response</th>
                    <th className="text-left px-2 py-1 w-24">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {transcript.map((row) => (
                    <tr key={row.id} className="border-t border-gray-100">
                      <td className="px-2 py-1 text-gray-400">{row.seq}</td>
                      <td className="px-2 py-1 whitespace-pre-wrap">{row.command}</td>
                      <td className="px-2 py-1 text-gray-600 whitespace-pre-wrap">
                        {row.response ?? ""}
                      </td>
                      <td className="px-2 py-1">
                        {row.exit_status === "simulated" ? (
                          <span className="text-amber-700 font-sans">simulated</span>
                        ) : row.exit_status === "ok" ? (
                          <span className="text-green-700 font-sans">ok</span>
                        ) : (
                          <span className="text-red-700 font-sans">{row.exit_status}</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <div className="flex justify-end gap-2 pt-2 border-t border-gray-100">
          <button
            onClick={handleCancel}
            disabled={!canCancel}
            className="text-sm px-3 py-1.5 rounded border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50"
          >
            {cancel.isPending ? "Cancelling..." : "Cancel dry-run"}
          </button>
          <button
            onClick={onClose}
            className="text-sm px-3 py-1.5 rounded border border-gray-300 text-gray-700 hover:bg-gray-50"
          >
            Close
          </button>
          <button
            onClick={handleConfirm}
            disabled={!canConfirm}
            className="text-sm px-3 py-1.5 rounded bg-gray-900 text-white hover:bg-gray-800 disabled:opacity-50"
            title={
              terminalSuccess
                ? "Schedule a real apply with the same config"
                : "Available once the dry-run has succeeded"
            }
          >
            {confirm.isPending ? "Confirming..." : "Confirm and apply"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
