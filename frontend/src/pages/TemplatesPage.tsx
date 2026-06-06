import { useState } from "react";
import { useNavigate } from "react-router-dom";
import toast from "react-hot-toast";
import { usePaginatedTemplates, useDeleteTemplate, useCreateTemplate } from "@/api/templates";
import { useAuthStore } from "@/stores/authStore";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { Pagination } from "@/components/ui/Pagination";
import type { DeviceTemplate } from "@/types/template.types";

function fieldCount(template: DeviceTemplate): number {
  return template.sections.reduce((sum, s) => sum + s.fields.length, 0);
}

function CopyIcon() {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
      <path d="M7 3.5A1.5 1.5 0 0 1 8.5 2h3.879a1.5 1.5 0 0 1 1.06.44l3.122 3.12A1.5 1.5 0 0 1 17 6.622V12.5a1.5 1.5 0 0 1-1.5 1.5h-1v-3.379a3 3 0 0 0-.879-2.121L10.5 5.379A3 3 0 0 0 8.379 4.5H7v-1Z" />
      <path d="M4.5 6A1.5 1.5 0 0 0 3 7.5v9A1.5 1.5 0 0 0 4.5 18h7a1.5 1.5 0 0 0 1.5-1.5v-5.879a1.5 1.5 0 0 0-.44-1.06L9.44 6.439A1.5 1.5 0 0 0 8.378 6H4.5Z" />
    </svg>
  );
}

const TYPE_FILTER_OPTIONS = [
  { label: "All", value: "" },
  { label: "Device", value: "device" },
  { label: "Port", value: "port" },
];

export function TemplatesPage() {
  const navigate = useNavigate();
  const [typeFilter, setTypeFilter] = useState("");
  const [skip, setSkip] = useState(0);
  const limit = 50;
  const { data, isLoading, isError } = usePaginatedTemplates(
    typeFilter || undefined,
    skip,
    limit,
  );
  const templates = data?.items;
  const total = data?.total ?? 0;
  const deleteTemplate = useDeleteTemplate();
  const createTemplate = useCreateTemplate();
  const user = useAuthStore((s) => s.user);
  const isAdmin = user?.role === "admin" || user?.role === "superadmin";

  const [deleteTarget, setDeleteTarget] = useState<DeviceTemplate | null>(null);

  const handleTypeFilterChange = (value: string) => {
    setTypeFilter(value);
    setSkip(0);
  };

  const handleCopy = async (t: DeviceTemplate) => {
    try {
      await createTemplate.mutateAsync({
        name: `Copy of ${t.name}`,
        template_type: t.template_type as "device" | "port",
        icon: t.icon ?? undefined,
        description: t.description ?? undefined,
        sections: JSON.parse(JSON.stringify(t.sections)),
      });
      toast.success("Template duplicated");
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail;
      toast.error(detail ?? "Failed to duplicate template");
    }
  };

  const handleDelete = async () => {
    if (!deleteTarget) return;
    try {
      await deleteTemplate.mutateAsync(deleteTarget.id);
      toast.success("Template deleted");
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Failed to delete template";
      toast.error(msg);
    }
    setDeleteTarget(null);
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="px-6 xl:px-12 2xl:px-16 py-6 space-y-6">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-4">
            <h2 className="text-lg font-semibold text-gray-900">
              Templates
              {data && (
                <span className="ml-2 text-sm text-gray-400 font-normal">
                  ({total})
                </span>
              )}
            </h2>
            <select
              value={typeFilter}
              onChange={(e) => handleTypeFilterChange(e.target.value)}
              className="border border-gray-300 rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              {TYPE_FILTER_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </div>
          {isAdmin && (
            <button
              onClick={() => navigate("/templates/new")}
              className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition-colors"
            >
              Create Template
            </button>
          )}
        </div>

        <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
          {isLoading && (
            <p
              role="status"
              aria-live="polite"
              className="text-sm text-gray-400 text-center py-8"
            >
              Loading templates...
            </p>
          )}
          {isError && (
            <p className="text-sm text-red-500 text-center py-8">
              Failed to load templates
            </p>
          )}
          {templates && templates.length === 0 && (
            <p className="text-sm text-gray-400 text-center py-8">
              No templates found
            </p>
          )}
          {templates && templates.length > 0 && (
            <div className="overflow-x-auto">
            <table className="w-full min-w-[700px]">
              <thead>
                <tr className="border-b border-gray-200 bg-gray-50">
                  <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">
                    Icon
                  </th>
                  <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">
                    Name
                  </th>
                  <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">
                    Type
                  </th>
                  <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">
                    Description
                  </th>
                  <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">
                    Sections
                  </th>
                  <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">
                    Fields
                  </th>
                  {isAdmin && (
                    <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">
                      Actions
                    </th>
                  )}
                </tr>
              </thead>
              <tbody>
                {templates.map((t) => (
                  <tr
                    key={t.id}
                    className="border-b border-gray-100 hover:bg-gray-50 cursor-pointer"
                    onClick={() => navigate(`/templates/${t.id}`)}
                  >
                    <td className="px-4 py-2">
                      {t.icon ? (
                        <img
                          src={t.icon}
                          alt={t.name}
                          className="w-8 h-8 object-contain rounded"
                        />
                      ) : (
                        <span className="inline-block w-8 h-8 bg-gray-200 rounded" />
                      )}
                    </td>
                    <td className="px-4 py-2 text-sm font-medium text-gray-900">
                      {t.name}
                    </td>
                    <td className="px-4 py-2 text-sm text-gray-500 capitalize">
                      {t.template_type}
                    </td>
                    <td className="px-4 py-2 text-sm text-gray-500">
                      {t.description ?? "-"}
                    </td>
                    <td className="px-4 py-2 text-sm text-gray-600">
                      {t.sections.length}
                    </td>
                    <td className="px-4 py-2 text-sm text-gray-600">
                      {fieldCount(t)}
                    </td>
                    {isAdmin && (
                      <td className="px-4 py-2 flex items-center gap-2">
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            handleCopy(t);
                          }}
                          title="Duplicate template"
                          className="text-gray-400 hover:text-blue-600 transition-colors"
                        >
                          <CopyIcon />
                        </button>
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            setDeleteTarget(t);
                          }}
                          className="text-sm text-red-500 hover:text-red-700"
                        >
                          Delete
                        </button>
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
          )}
          <Pagination total={total} skip={skip} limit={limit} onPageChange={setSkip} />
        </div>
      </div>

      <ConfirmDialog
        open={!!deleteTarget}
        title="Delete Template"
        description={`Delete "${deleteTarget?.name}"? This cannot be undone. Templates with existing devices cannot be deleted.`}
        confirmLabel="Delete"
        destructive
        onConfirm={handleDelete}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}
