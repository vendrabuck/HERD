import { useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  useTopologyTemplates,
  useDeleteTopologyTemplate,
  useTopologyTemplate,
  useInstantiateTemplate,
} from "@/api/topologyTemplates";
import type { TopologyTemplate } from "@/api/topologyTemplates";
import { useDevices } from "@/api/inventory";
import { useAuthStore } from "@/stores/authStore";
import { isAdminRole } from "@/lib/roles";
import { Modal } from "@/components/ui/Modal";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { Pagination } from "@/components/ui/Pagination";
import toast from "react-hot-toast";

export function TopologyTemplatesPage() {
  const user = useAuthStore((s) => s.user);
  const isAdmin = isAdminRole(user?.role);
  const navigate = useNavigate();

  const [skip, setSkip] = useState(0);
  const limit = 50;
  const { data, isLoading, isError } = useTopologyTemplates(skip, limit);
  const templates = data?.items ?? [];
  const total = data?.total ?? 0;

  const deleteTemplate = useDeleteTopologyTemplate();
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [useTemplateId, setUseTemplateId] = useState<string | null>(null);

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-6xl mx-auto px-6 py-6">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-gray-900">
            Topology Templates
            {data && <span className="ml-2 text-sm text-gray-400 font-normal">({total})</span>}
          </h2>
        </div>

        <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
          {isLoading && (
            <p role="status" className="text-sm text-gray-400 text-center py-8">
              Loading templates...
            </p>
          )}
          {isError && (
            <p className="text-sm text-red-500 text-center py-8">Failed to load templates</p>
          )}
          {!isLoading && templates.length === 0 && (
            <p className="text-sm text-gray-400 text-center py-8">
              No templates yet. Open a topology and click "Save as Template" to create one.
            </p>
          )}
          {templates.length > 0 && (
            <table className="w-full">
              <thead>
                <tr className="border-b border-gray-200 bg-gray-50">
                  <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Name</th>
                  <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Owner</th>
                  <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Description</th>
                  <th className="px-4 py-2 text-right"></th>
                </tr>
              </thead>
              <tbody>
                {templates.map((t) => (
                  <TemplateRow
                    key={t.id}
                    t={t}
                    canManage={isAdmin || t.created_by === user?.id}
                    onUse={() => setUseTemplateId(t.id)}
                    onDelete={() => setDeleteId(t.id)}
                  />
                ))}
              </tbody>
            </table>
          )}
          <Pagination total={total} skip={skip} limit={limit} onPageChange={setSkip} />
        </div>

        <ConfirmDialog
          open={!!deleteId}
          title="Delete template?"
          description="This permanently deletes the template definition. Existing topologies are unaffected."
          confirmLabel="Delete"
          destructive
          onConfirm={async () => {
            if (!deleteId) return;
            await deleteTemplate.mutateAsync(deleteId);
            setDeleteId(null);
          }}
          onCancel={() => setDeleteId(null)}
        />

        {useTemplateId && (
          <InstantiateTemplateDialog
            templateId={useTemplateId}
            onClose={() => setUseTemplateId(null)}
            onCreated={(topologyId) => {
              setUseTemplateId(null);
              navigate(`/topology/${topologyId}`);
            }}
          />
        )}
      </div>
    </div>
  );
}

function TemplateRow({
  t,
  canManage,
  onUse,
  onDelete,
}: {
  t: TopologyTemplate;
  canManage: boolean;
  onUse: () => void;
  onDelete: () => void;
}) {
  return (
    <tr className="border-b border-gray-100 hover:bg-gray-50">
      <td className="px-4 py-3 text-sm font-medium text-gray-900">{t.name}</td>
      <td className="px-4 py-3 text-sm text-gray-500">{t.owner_name || t.created_by.slice(0, 8)}</td>
      <td className="px-4 py-3 text-sm text-gray-500">{t.description ?? ""}</td>
      <td className="px-4 py-3 text-right">
        <div className="flex justify-end gap-1">
          <button
            type="button"
            onClick={onUse}
            className="text-xs text-blue-600 hover:text-blue-800 px-2 py-1 rounded hover:bg-blue-50"
          >
            Use
          </button>
          {canManage && (
            <button
              type="button"
              onClick={onDelete}
              className="text-xs text-red-600 hover:text-red-800 px-2 py-1 rounded hover:bg-red-50"
            >
              Delete
            </button>
          )}
        </div>
      </td>
    </tr>
  );
}

function InstantiateTemplateDialog({
  templateId,
  onClose,
  onCreated,
}: {
  templateId: string;
  onClose: () => void;
  onCreated: (topologyId: string) => void;
}) {
  const { data: template, isLoading } = useTopologyTemplate(templateId);
  const { data: devices } = useDevices();
  const instantiate = useInstantiateTemplate();

  const [name, setName] = useState("");
  const [assignments, setAssignments] = useState<Record<string, string>>({});

  const roles =
    template?.canvas_data?.nodes
      .map((n) => n.data?.device?.role)
      .filter((r): r is string => Boolean(r)) ?? [];
  const uniqueRoles = Array.from(new Set(roles));

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    for (const role of uniqueRoles) {
      if (!assignments[role]) {
        toast.error(`Pick a device for role ${role}`);
        return;
      }
    }
    try {
      const topology = await instantiate.mutateAsync({
        id: templateId,
        name: name.trim(),
        role_assignments: assignments,
      });
      onCreated(topology.id);
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      toast.error(detail ?? "Instantiate failed");
    }
  };

  return (
    <Modal open onClose={onClose} title={template ? `Instantiate "${template.name}"` : "Instantiate"}>
      {isLoading && <p className="text-xs text-gray-400">Loading template...</p>}
      {template && (
        <form onSubmit={handleSubmit}>
          <label htmlFor="instantiate-name" className="block text-sm font-medium text-gray-700 mb-1">
            New topology name
          </label>
          <input
            id="instantiate-name"
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            autoFocus
            className="w-full px-3 py-2 border border-gray-300 rounded text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          />

          <div className="mt-4 space-y-2">
            <p className="text-xs font-medium text-gray-700">Role to device map</p>
            {uniqueRoles.length === 0 && (
              <p className="text-xs text-gray-500">This template has no roles to assign.</p>
            )}
            {uniqueRoles.map((role) => (
              <div key={role} className="flex items-center gap-2">
                <span className="text-xs text-gray-700 w-32 truncate">{role}</span>
                <select
                  value={assignments[role] ?? ""}
                  onChange={(e) =>
                    setAssignments({ ...assignments, [role]: e.target.value })
                  }
                  className="flex-1 px-2 py-1 border border-gray-300 rounded text-xs"
                >
                  <option value="">Select a device...</option>
                  {(devices ?? []).map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.name} ({d.template_name ?? "?"})
                    </option>
                  ))}
                </select>
              </div>
            ))}
          </div>

          <div className="flex justify-end gap-2 mt-4">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={!name.trim() || instantiate.isPending}
              className="px-4 py-2 bg-blue-600 text-white text-sm rounded hover:bg-blue-700 disabled:opacity-50"
            >
              {instantiate.isPending ? "Creating..." : "Create"}
            </button>
          </div>
        </form>
      )}
    </Modal>
  );
}
