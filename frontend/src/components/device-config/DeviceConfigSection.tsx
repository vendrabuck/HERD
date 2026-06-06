import { useState } from "react";
import toast from "react-hot-toast";
import { Modal } from "@/components/ui/Modal";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import {
  useDeviceConfigVersions,
  useDeviceConfigVersion,
  useDeviceConfigDiff,
  useCreateDeviceConfigVersion,
  useRestoreDeviceConfigVersion,
  useApplyDeviceConfigVersion,
} from "@/api/deviceConfig";
import { useScheduleApplyJob } from "@/api/deviceConfigJobs";
import type { DeviceConfigVersion } from "@/api/deviceConfig";
import { ApplyJobsPanel } from "./ApplyJobsPanel";

interface Props {
  deviceId: string;
}

export function DeviceConfigSection({ deviceId }: Props) {
  const { data, isLoading, isError } = useDeviceConfigVersions(deviceId);
  const versions = data?.items ?? [];
  const total = data?.total ?? 0;

  const createVersion = useCreateDeviceConfigVersion(deviceId);
  const restoreVersion = useRestoreDeviceConfigVersion(deviceId);
  const applyVersion = useApplyDeviceConfigVersion(deviceId);
  const scheduleApply = useScheduleApplyJob(deviceId);

  const [showCreate, setShowCreate] = useState(false);
  const [configText, setConfigText] = useState("");
  const [createDescription, setCreateDescription] = useState("");
  const [createError, setCreateError] = useState<string | null>(null);

  const [viewVersionId, setViewVersionId] = useState<string | null>(null);
  const [restoreVersionId, setRestoreVersionId] = useState<string | null>(null);
  const [applyVersionId, setApplyVersionId] = useState<string | null>(null);
  const [scheduleAt, setScheduleAt] = useState("");

  const [compareSelection, setCompareSelection] = useState<string[]>([]);
  const [showDiff, setShowDiff] = useState(false);

  const toggleCompare = (id: string) => {
    setCompareSelection((prev) => {
      if (prev.includes(id)) return prev.filter((p) => p !== id);
      if (prev.length >= 2) return [prev[1], id];
      return [...prev, id];
    });
  };

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    setCreateError(null);
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(configText || "{}");
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : "Invalid JSON");
      return;
    }
    try {
      await createVersion.mutateAsync({
        config: parsed,
        description: createDescription.trim() || undefined,
      });
      setShowCreate(false);
      setConfigText("");
      setCreateDescription("");
      toast.success("Config version created");
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setCreateError(detail ?? (err instanceof Error ? err.message : "Failed"));
    }
  };

  return (
    <div className="bg-white rounded-lg border border-gray-200 p-4">
      <div className="flex items-center justify-between mb-3">
        <div>
          <h3 className="text-sm font-medium text-gray-900">Configuration history</h3>
          <p className="text-xs text-gray-500">{total} version{total === 1 ? "" : "s"}</p>
        </div>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={() => setShowDiff(true)}
            disabled={compareSelection.length !== 2}
            className="text-xs px-3 py-1.5 border border-gray-300 rounded hover:bg-gray-50 disabled:opacity-50"
          >
            Compare
          </button>
          <button
            type="button"
            onClick={() => setShowCreate(true)}
            className="text-xs px-3 py-1.5 bg-blue-600 text-white rounded hover:bg-blue-700"
          >
            New version
          </button>
        </div>
      </div>

      {isLoading && <p className="text-xs text-gray-400">Loading versions...</p>}
      {isError && <p className="text-xs text-red-600">Failed to load versions</p>}
      {!isLoading && versions.length === 0 && (
        <p className="text-xs text-gray-400">No config versions yet.</p>
      )}
      {versions.length > 0 && (
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-gray-200 text-left text-xs uppercase text-gray-500">
              <th className="py-2 w-10"></th>
              <th className="py-2 w-12">Ver</th>
              <th className="py-2">Author</th>
              <th className="py-2">When</th>
              <th className="py-2">Description</th>
              <th className="py-2 w-44 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {versions.map((v) => (
              <VersionRow
                key={v.id}
                v={v}
                checked={compareSelection.includes(v.id)}
                onToggle={() => toggleCompare(v.id)}
                onView={() => setViewVersionId(v.id)}
                onRestore={() => setRestoreVersionId(v.id)}
                onApply={() => setApplyVersionId(v.id)}
              />
            ))}
          </tbody>
        </table>
      )}

      <ApplyJobsPanel deviceId={deviceId} />

      <Modal open={showCreate} onClose={() => setShowCreate(false)} title="New config version">
        <form onSubmit={handleCreate}>
          <label htmlFor="config-json" className="block text-xs font-medium text-gray-700 mb-1">
            Config (JSON)
          </label>
          <textarea
            id="config-json"
            value={configText}
            onChange={(e) => setConfigText(e.target.value)}
            rows={8}
            spellCheck={false}
            className="w-full px-3 py-2 border border-gray-300 rounded font-mono text-xs focus:outline-none focus:ring-2 focus:ring-blue-500"
            placeholder='{ "vlan": 100, "ip": "10.0.0.1", "hostname": "fw-1" }'
          />
          <label
            htmlFor="config-description"
            className="block text-xs font-medium text-gray-700 mt-3 mb-1"
          >
            Description (optional)
          </label>
          <input
            id="config-description"
            value={createDescription}
            onChange={(e) => setCreateDescription(e.target.value)}
            className="w-full px-3 py-2 border border-gray-300 rounded text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
          {createError && (
            <p className="text-xs text-red-600 mt-2 whitespace-pre-wrap">{createError}</p>
          )}
          <div className="flex justify-end gap-2 mt-4">
            <button
              type="button"
              onClick={() => setShowCreate(false)}
              className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={createVersion.isPending}
              className="px-4 py-2 bg-blue-600 text-white text-sm rounded hover:bg-blue-700 disabled:opacity-50"
            >
              {createVersion.isPending ? "Saving..." : "Save"}
            </button>
          </div>
        </form>
      </Modal>

      {viewVersionId && (
        <ViewVersionModal
          deviceId={deviceId}
          versionId={viewVersionId}
          onClose={() => setViewVersionId(null)}
        />
      )}

      {showDiff && compareSelection.length === 2 && (
        <DiffModal
          deviceId={deviceId}
          from={compareSelection[0]}
          to={compareSelection[1]}
          onClose={() => setShowDiff(false)}
        />
      )}

      <ConfirmDialog
        open={!!restoreVersionId}
        title="Restore this version?"
        description="This writes a new snapshot with the older config. Use Apply afterwards to push it to the device."
        confirmLabel="Restore"
        onConfirm={async () => {
          if (!restoreVersionId) return;
          try {
            await restoreVersion.mutateAsync({ versionId: restoreVersionId, body: {} });
            toast.success("Restored as a new version");
          } catch {
            toast.error("Restore failed");
          } finally {
            setRestoreVersionId(null);
          }
        }}
        onCancel={() => setRestoreVersionId(null)}
      />

      <Modal
        open={!!applyVersionId}
        onClose={() => {
          setApplyVersionId(null);
          setScheduleAt("");
        }}
        title="Apply config to device"
      >
        <p className="text-sm text-gray-700">
          Apply this version now, or schedule it for later. Scheduled jobs fire from the server
          when their time arrives; jobs tied to a reservation only fire while the reservation is
          active.
        </p>
        <div className="mt-3">
          <label
            htmlFor="schedule-at"
            className="block text-xs font-medium text-gray-700 mb-1"
          >
            Schedule for (optional)
          </label>
          <input
            id="schedule-at"
            type="datetime-local"
            value={scheduleAt}
            onChange={(e) => setScheduleAt(e.target.value)}
            className="px-3 py-2 border border-gray-300 rounded text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
          <p className="text-xs text-gray-500 mt-1">
            Leave blank to apply now.
          </p>
        </div>

        <div className="flex justify-end gap-2 mt-4">
          <button
            type="button"
            onClick={() => {
              setApplyVersionId(null);
              setScheduleAt("");
            }}
            className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={applyVersion.isPending || scheduleApply.isPending}
            onClick={async () => {
              if (!applyVersionId) return;
              if (scheduleAt) {
                try {
                  const iso = new Date(scheduleAt).toISOString();
                  await scheduleApply.mutateAsync({
                    versionId: applyVersionId,
                    scheduled_for: iso,
                  });
                  toast.success("Scheduled");
                } catch (err: unknown) {
                  const detail = (err as { response?: { data?: { detail?: string } } })?.response
                    ?.data?.detail;
                  toast.error(detail ?? "Schedule failed");
                  return;
                }
              } else {
                try {
                  const result = await applyVersion.mutateAsync(applyVersionId);
                  if (result.status === "failed") {
                    toast.error(`Apply failed: ${result.error ?? "unknown"}`);
                  } else {
                    toast.success(`Applied (run ${result.run_id?.slice(0, 8) ?? "n/a"})`);
                  }
                } catch {
                  toast.error("Apply request failed");
                  return;
                }
              }
              setApplyVersionId(null);
              setScheduleAt("");
            }}
            className="px-4 py-2 bg-blue-600 text-white text-sm rounded hover:bg-blue-700 disabled:opacity-50"
          >
            {scheduleAt ? "Schedule" : "Apply now"}
          </button>
        </div>
      </Modal>
    </div>
  );
}

function VersionRow({
  v,
  checked,
  onToggle,
  onView,
  onRestore,
  onApply,
}: {
  v: DeviceConfigVersion;
  checked: boolean;
  onToggle: () => void;
  onView: () => void;
  onRestore: () => void;
  onApply: () => void;
}) {
  const created = new Date(v.created_at).toLocaleString();
  return (
    <tr className="border-b border-gray-100 hover:bg-gray-50">
      <td className="py-2">
        <input
          type="checkbox"
          checked={checked}
          onChange={onToggle}
          aria-label={`Compare v${v.version_number}`}
        />
      </td>
      <td className="py-2 text-xs font-medium">v{v.version_number}</td>
      <td className="py-2 text-xs text-gray-600">{v.author_name || v.created_by.slice(0, 8)}</td>
      <td className="py-2 text-xs text-gray-500">{created}</td>
      <td className="py-2 text-xs text-gray-700">{v.description ?? ""}</td>
      <td className="py-2 text-right">
        <div className="flex justify-end gap-1">
          <button
            type="button"
            onClick={onView}
            className="text-xs px-2 py-1 rounded hover:bg-gray-100"
          >
            View
          </button>
          <button
            type="button"
            onClick={onRestore}
            className="text-xs px-2 py-1 rounded hover:bg-gray-100"
          >
            Restore
          </button>
          <button
            type="button"
            onClick={onApply}
            className="text-xs px-2 py-1 rounded text-blue-700 hover:bg-blue-50"
          >
            Apply
          </button>
        </div>
      </td>
    </tr>
  );
}

function ViewVersionModal({
  deviceId,
  versionId,
  onClose,
}: {
  deviceId: string;
  versionId: string;
  onClose: () => void;
}) {
  const { data, isLoading } = useDeviceConfigVersion(deviceId, versionId);
  return (
    <Modal open onClose={onClose} title={data ? `Config v${data.version_number}` : "Config"}>
      {isLoading && <p className="text-xs text-gray-400">Loading...</p>}
      {data && (
        <pre className="bg-gray-50 border border-gray-200 rounded p-3 text-xs font-mono whitespace-pre-wrap break-all max-h-96 overflow-auto">
          {JSON.stringify(data.config, null, 2)}
        </pre>
      )}
      <div className="flex justify-end mt-4">
        <button
          type="button"
          onClick={onClose}
          className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800"
        >
          Close
        </button>
      </div>
    </Modal>
  );
}

function DiffModal({
  deviceId,
  from,
  to,
  onClose,
}: {
  deviceId: string;
  from: string;
  to: string;
  onClose: () => void;
}) {
  const { data, isLoading } = useDeviceConfigDiff(deviceId, from, to);
  return (
    <Modal open onClose={onClose} title="Config diff">
      {isLoading && <p className="text-xs text-gray-400">Computing...</p>}
      {data && (
        <pre className="bg-gray-50 border border-gray-200 rounded p-3 text-xs font-mono whitespace-pre-wrap break-all max-h-96 overflow-auto">
          {data.diff || "(no changes)"}
        </pre>
      )}
      <div className="flex justify-end mt-4">
        <button
          type="button"
          onClick={onClose}
          className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800"
        >
          Close
        </button>
      </div>
    </Modal>
  );
}
