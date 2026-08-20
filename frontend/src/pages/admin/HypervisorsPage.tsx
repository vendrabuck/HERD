import { useState } from "react";
import toast from "react-hot-toast";
import {
  usePaginatedHypervisors,
  useCreateHypervisor,
  useUpdateHypervisor,
  useDeleteHypervisor,
} from "@/api/hypervisors";
import { useSecrets } from "@/api/secrets";
import { Modal } from "@/components/ui/Modal";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { Pagination } from "@/components/ui/Pagination";
import type { Hypervisor } from "@/types/hypervisor.types";
import { errorDetail } from "@/lib/errors";

interface FormState {
  name: string;
  description: string;
  endpoint: string;
  hypervisorType: string;
  secretId: string;
  enabled: boolean;
}

const EMPTY_FORM: FormState = {
  name: "",
  description: "",
  endpoint: "",
  hypervisorType: "",
  secretId: "",
  enabled: true,
};

export function HypervisorsPage() {
  const [skip, setSkip] = useState(0);
  const limit = 50;
  const { data, isLoading } = usePaginatedHypervisors(skip, limit);
  const hypervisors = data?.items;
  const total = data?.total ?? 0;
  const { data: secrets } = useSecrets();

  const createHypervisor = useCreateHypervisor();
  const updateHypervisor = useUpdateHypervisor();
  const deleteHypervisor = useDeleteHypervisor();

  const [showCreate, setShowCreate] = useState(false);
  const [editTarget, setEditTarget] = useState<Hypervisor | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Hypervisor | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);

  const closeCreateModal = () => {
    setShowCreate(false);
    setForm(EMPTY_FORM);
  };

  const openEditModal = (h: Hypervisor) => {
    setEditTarget(h);
    setForm({
      name: h.name,
      description: h.description ?? "",
      endpoint: h.endpoint,
      hypervisorType: h.hypervisor_type,
      secretId: h.secret_id,
      enabled: h.enabled,
    });
  };

  const closeEditModal = () => {
    setEditTarget(null);
    setForm(EMPTY_FORM);
  };

  const validateForm = (): boolean => {
    if (!form.name.trim()) {
      toast.error("Name is required");
      return false;
    }
    if (!form.endpoint.trim()) {
      toast.error("Endpoint is required");
      return false;
    }
    if (!form.hypervisorType.trim()) {
      toast.error("Hypervisor type is required");
      return false;
    }
    if (!form.secretId) {
      toast.error("A secret is required");
      return false;
    }
    return true;
  };

  const handleCreate = async () => {
    if (!validateForm()) return;
    try {
      await createHypervisor.mutateAsync({
        name: form.name.trim(),
        description: form.description.trim() || undefined,
        endpoint: form.endpoint.trim(),
        hypervisor_type: form.hypervisorType.trim(),
        secret_id: form.secretId,
        enabled: form.enabled,
      });
      toast.success("Hypervisor registered");
      closeCreateModal();
    } catch (err: unknown) {
      toast.error(errorDetail(err, "Failed to register hypervisor"));
    }
  };

  const handleUpdate = async () => {
    if (!editTarget) return;
    if (!validateForm()) return;
    try {
      await updateHypervisor.mutateAsync({
        id: editTarget.id,
        data: {
          name: form.name.trim(),
          description: form.description.trim() || undefined,
          endpoint: form.endpoint.trim(),
          hypervisor_type: form.hypervisorType.trim(),
          secret_id: form.secretId,
          enabled: form.enabled,
        },
      });
      toast.success("Hypervisor updated");
      closeEditModal();
    } catch (err: unknown) {
      toast.error(errorDetail(err, "Failed to update hypervisor"));
    }
  };

  const handleDelete = async () => {
    if (!deleteTarget) return;
    try {
      await deleteHypervisor.mutateAsync(deleteTarget.id);
      toast.success("Hypervisor deleted");
    } catch (err: unknown) {
      toast.error(errorDetail(err, "Failed to delete hypervisor"));
    }
    setDeleteTarget(null);
  };

  // Issue #456: a deleted secret leaves an orphaned reference; render that
  // state explicitly instead of a bare truncated id. While secrets are still
  // loading, absence proves nothing, so keep the neutral truncated fallback.
  const secretIsOrphaned = (id: string) => Boolean(secrets && !secrets.some((s) => s.id === id));
  const secretName = (id: string) => {
    const match = secrets?.find((s) => s.id === id);
    if (match) return match.name;
    return secrets ? `Deleted secret ${id.slice(0, 8)}` : id.slice(0, 8) + "...";
  };

  const renderForm = (onSubmit: () => void, submitLabel: string, pending: boolean) => (
    <div className="space-y-4">
      <div>
        <label htmlFor="hv-name" className="block text-sm font-medium text-gray-700 mb-1">
          Name
        </label>
        <input
          id="hv-name"
          type="text"
          value={form.name}
          onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
          className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
      </div>
      <div>
        <label htmlFor="hv-description" className="block text-sm font-medium text-gray-700 mb-1">
          Description
        </label>
        <textarea
          id="hv-description"
          value={form.description}
          onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
          rows={2}
          className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
      </div>
      <div>
        <label htmlFor="hv-endpoint" className="block text-sm font-medium text-gray-700 mb-1">
          Endpoint
        </label>
        <input
          id="hv-endpoint"
          type="text"
          placeholder="https://proxmox.example.local:8006"
          value={form.endpoint}
          onChange={(e) => setForm((f) => ({ ...f, endpoint: e.target.value }))}
          className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
      </div>
      <div>
        <label htmlFor="hv-type" className="block text-sm font-medium text-gray-700 mb-1">
          Hypervisor Type
        </label>
        <input
          id="hv-type"
          type="text"
          placeholder="proxmox, vsphere, libvirt..."
          value={form.hypervisorType}
          onChange={(e) => setForm((f) => ({ ...f, hypervisorType: e.target.value }))}
          className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
      </div>
      <div>
        <label htmlFor="hv-secret" className="block text-sm font-medium text-gray-700 mb-1">
          Secret
        </label>
        <select
          id="hv-secret"
          value={form.secretId}
          onChange={(e) => setForm((f) => ({ ...f, secretId: e.target.value }))}
          className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        >
          <option value="">Select a secret</option>
          {form.secretId && secretIsOrphaned(form.secretId) && (
            <option value={form.secretId}>Deleted secret {form.secretId.slice(0, 8)}</option>
          )}
          {secrets?.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name} ({s.type})
            </option>
          ))}
        </select>
        {form.secretId && secretIsOrphaned(form.secretId) && (
          <p className="text-xs text-amber-600 mt-1">
            This hypervisor references a secret that no longer exists. Saving keeps the stored
            reference; select a live secret to repair it.
          </p>
        )}
        {secrets && secrets.length === 0 && (
          <p className="text-xs text-amber-600 mt-1">
            No secrets exist yet. Create one via the secrets API first, then register the
            hypervisor.
          </p>
        )}
      </div>
      <div className="flex items-center gap-2">
        <input
          id="hv-enabled"
          type="checkbox"
          checked={form.enabled}
          onChange={(e) => setForm((f) => ({ ...f, enabled: e.target.checked }))}
          className="h-4 w-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
        />
        <label htmlFor="hv-enabled" className="text-sm font-medium text-gray-700">
          Enabled
        </label>
      </div>
      <div className="flex justify-end gap-2 pt-2">
        <button
          type="button"
          onClick={editTarget ? closeEditModal : closeCreateModal}
          className="px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 rounded-lg hover:bg-gray-200 transition-colors"
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={onSubmit}
          disabled={pending}
          className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors"
        >
          {pending ? "Saving..." : submitLabel}
        </button>
      </div>
    </div>
  );

  return (
    <div className="h-full overflow-y-auto">
      <div className="px-6 xl:px-12 2xl:px-16 py-8">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-gray-900">Hypervisors</h2>
          <button
            onClick={() => setShowCreate(true)}
            className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition-colors"
          >
            Register Hypervisor
          </button>
        </div>

        <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
          {isLoading ? (
            <p className="text-sm text-gray-500 px-4 py-4">Loading hypervisors...</p>
          ) : !hypervisors || hypervisors.length === 0 ? (
            <p className="text-sm text-gray-500 px-4 py-4">No hypervisors found</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[700px] text-sm text-left">
                <thead className="bg-gray-50 text-gray-600 uppercase text-xs">
                  <tr>
                    <th className="px-4 py-3">Name</th>
                    <th className="px-4 py-3">Type</th>
                    <th className="px-4 py-3">Endpoint</th>
                    <th className="px-4 py-3">Secret</th>
                    <th className="px-4 py-3">Enabled</th>
                    <th className="px-4 py-3">Date</th>
                    <th className="px-4 py-3">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {hypervisors.map((h) => (
                    <tr key={h.id} className="hover:bg-gray-50">
                      <td className="px-4 py-3 font-medium text-gray-900">{h.name}</td>
                      <td className="px-4 py-3 text-gray-600">{h.hypervisor_type}</td>
                      <td className="px-4 py-3 text-gray-600 font-mono text-xs">{h.endpoint}</td>
                      <td
                        className={`px-4 py-3 ${
                          secretIsOrphaned(h.secret_id) ? "text-amber-600" : "text-gray-600"
                        }`}
                      >
                        {secretName(h.secret_id)}
                      </td>
                      <td className="px-4 py-3 text-gray-600">{h.enabled ? "Yes" : "No"}</td>
                      <td className="px-4 py-3 text-gray-500">
                        {new Date(h.created_at).toLocaleDateString()}
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex gap-2">
                          <button
                            onClick={() => openEditModal(h)}
                            className="text-xs text-blue-600 hover:text-blue-800"
                          >
                            Edit
                          </button>
                          <button
                            onClick={() => setDeleteTarget(h)}
                            className="text-xs text-red-600 hover:text-red-800"
                          >
                            Delete
                          </button>
                        </div>
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

      {/* One Modal shared by create and edit: rendering two Modal instances
          simultaneously would mount the form (with fixed input ids) twice in
          the DOM at once, since Modal always renders its children and only
          toggles visibility imperatively via showModal()/close(). */}
      <Modal
        open={showCreate || !!editTarget}
        onClose={editTarget ? closeEditModal : closeCreateModal}
        title={editTarget ? "Edit Hypervisor" : "Register Hypervisor"}
        className="max-w-md"
      >
        {editTarget
          ? renderForm(handleUpdate, "Save", updateHypervisor.isPending)
          : renderForm(handleCreate, "Register", createHypervisor.isPending)}
      </Modal>

      <ConfirmDialog
        open={!!deleteTarget}
        title="Delete Hypervisor"
        description={`Delete "${deleteTarget?.name}"? This cannot be undone. Deletion will fail if templates reference it.`}
        confirmLabel="Delete"
        destructive
        onConfirm={handleDelete}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}
