import { useState } from "react";
import toast from "react-hot-toast";
import { isAxiosError } from "axios";

import { Modal } from "@/components/ui/Modal";
import { useAICommit } from "@/api/ai";
import type {
  AICommitRequest,
  AICommitResponse,
  AIGenerateResponse,
} from "@/types/ai.types";

interface AICommitDialogProps {
  open: boolean;
  proposal: AIGenerateResponse | null;
  onClose: () => void;
  onCommitted: (result: AICommitResponse) => void;
}

function toLocalInputValue(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    d.getFullYear() +
    "-" +
    pad(d.getMonth() + 1) +
    "-" +
    pad(d.getDate()) +
    "T" +
    pad(d.getHours()) +
    ":" +
    pad(d.getMinutes())
  );
}

interface FormProps {
  proposal: AIGenerateResponse;
  onClose: () => void;
  onCommitted: (result: AICommitResponse) => void;
}

function CommitForm({ proposal, onClose, onCommitted }: FormProps) {
  const commit = useAICommit();
  const now = new Date();
  const defaultStart = new Date(now.getTime() + 60 * 60 * 1000);
  const defaultEnd = new Date(now.getTime() + 5 * 60 * 60 * 1000);
  const defaultName = `AI: ${proposal.purpose || "proposal"} (${toLocalInputValue(now).replace(
    "T",
    " "
  )})`.slice(0, 200);

  const [name, setName] = useState(defaultName);
  const [startTime, setStartTime] = useState(toLocalInputValue(defaultStart));
  const [endTime, setEndTime] = useState(toLocalInputValue(defaultEnd));
  const [purpose, setPurpose] = useState(proposal.purpose ?? "");
  const [applyConfigs, setApplyConfigs] = useState(false);

  const configCount = proposal.devices.filter(
    (d) => d.device && d.config && Object.keys(d.config).length > 0,
  ).length;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) {
      toast.error("Topology name is required");
      return;
    }
    if (new Date(endTime) <= new Date(startTime)) {
      toast.error("End time must be after start time");
      return;
    }

    const req: AICommitRequest = {
      topology_name: name.trim(),
      purpose: purpose.trim() || null,
      start_time: new Date(startTime).toISOString(),
      end_time: new Date(endTime).toISOString(),
      devices: proposal.devices
        .filter((d) => d.device)
        .map((d) => ({
          role: d.role,
          device_id: d.device!.id,
          config: d.config ?? null,
          connection_type: d.device!.connection_type ?? null,
        })),
      edges: proposal.edges.map((edge) => ({
        source_role: edge.source_role,
        target_role: edge.target_role,
        layer: edge.layer,
      })),
      apply_configs: applyConfigs,
    };

    try {
      const result = await commit.mutateAsync(req);
      const failed = result.config_results.filter((r) => r.status === "failed");
      if (failed.length > 0) {
        toast.error(
          `Topology created, but ${failed.length} device config${
            failed.length === 1 ? "" : "s"
          } failed to apply`,
        );
      } else {
        toast.success("Topology and reservation created");
      }
      onCommitted(result);
      onClose();
    } catch (err) {
      if (isAxiosError(err)) {
        const detail = err.response?.data?.detail;
        toast.error(detail ? `Commit failed: ${detail}` : "Commit failed");
      } else {
        toast.error("Commit failed");
      }
    }
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <p className="text-sm text-gray-600">
        Creates a new topology and reservation from the accepted proposal.{" "}
        {proposal.devices.length} devices, {proposal.edges.length} edges.
      </p>

      <div>
        <label htmlFor="ai-commit-name" className="block text-sm font-medium text-gray-700 mb-1">
          Topology name
        </label>
        <input
          id="ai-commit-name"
          type="text"
          required
          value={name}
          onChange={(e) => setName(e.target.value)}
          className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div>
          <label
            htmlFor="ai-commit-start"
            className="block text-sm font-medium text-gray-700 mb-1"
          >
            Start time
          </label>
          <input
            id="ai-commit-start"
            type="datetime-local"
            required
            value={startTime}
            onChange={(e) => setStartTime(e.target.value)}
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>
        <div>
          <label
            htmlFor="ai-commit-end"
            className="block text-sm font-medium text-gray-700 mb-1"
          >
            End time
          </label>
          <input
            id="ai-commit-end"
            type="datetime-local"
            required
            value={endTime}
            onChange={(e) => setEndTime(e.target.value)}
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>
      </div>

      <div>
        <label
          htmlFor="ai-commit-purpose"
          className="block text-sm font-medium text-gray-700 mb-1"
        >
          Purpose
        </label>
        <input
          id="ai-commit-purpose"
          type="text"
          value={purpose}
          onChange={(e) => setPurpose(e.target.value)}
          className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
      </div>

      {configCount > 0 && (
        <div className="flex items-start gap-2 rounded-md border border-purple-200 bg-purple-50 p-3">
          <input
            id="ai-commit-apply-configs"
            type="checkbox"
            checked={applyConfigs}
            onChange={(e) => setApplyConfigs(e.target.checked)}
            className="mt-0.5"
          />
          <label htmlFor="ai-commit-apply-configs" className="text-sm text-gray-700">
            Apply device configs after commit ({configCount} device
            {configCount === 1 ? "" : "s"} have suggested config). Requires admin on the
            execution service; per-device failures are reported, not rolled back.
          </label>
        </div>
      )}

      <div className="flex justify-end gap-2 pt-2">
        <button
          type="button"
          onClick={onClose}
          disabled={commit.isPending}
          className="px-4 py-2 text-sm text-gray-700 hover:bg-gray-100 rounded-md disabled:opacity-50"
        >
          Cancel
        </button>
        <button
          type="submit"
          disabled={commit.isPending}
          className="px-4 py-2 text-sm bg-purple-600 text-white hover:bg-purple-700 rounded-md disabled:opacity-50"
        >
          {commit.isPending ? "Committing..." : "Commit"}
        </button>
      </div>
    </form>
  );
}

export function AICommitDialog({ open, proposal, onClose, onCommitted }: AICommitDialogProps) {
  return (
    <Modal open={open} onClose={onClose} title="Commit AI proposal" className="max-w-xl">
      {proposal && <CommitForm proposal={proposal} onClose={onClose} onCommitted={onCommitted} />}
    </Modal>
  );
}
